"""Pre-run Damai coordinate calibration with cross-locator arbitration and step-by-step SKU expansion."""
from __future__ import annotations

from dataclasses import dataclass
import logging
import math
from typing import Callable

from backend.app.damai.utils import pages, targets
from backend.app.damai.utils.adb_io import DamaiTransport
from backend.app.damai.utils.config import DamaiConfig
from backend.app.damai.utils.hierarchy import split_rows
from backend.app.damai.utils.locators import (
    CoordCache,
    LlmLocator,
    Locator,
    NativeLocator,
    OcrLocator,
    Point,
    Snapshot,
)
from backend.app.damai.utils.pages import PAGE_LABELS, Page
from backend.app.damai.utils.targets import Target
from backend.core.errors import NeedsAttention

log = logging.getLogger('backend.damai.calibrate')


@dataclass(frozen=True)
class Verdict:
    target: str
    point: Point | None
    status: str  # "agree" | "conflict" | "single" | "missing"
    detail: str
    candidates: dict[str, Point]

    def to_dict(self) -> dict[str, object]:
        return {
            'target': self.target,
            'point': list(self.point) if self.point else None,
            'status': self.status,
            'detail': self.detail,
            'candidates': {k: list(v) for k, v in self.candidates.items()},
        }


def agree(a: Point, b: Point, tol: int) -> bool:
    return math.hypot(a[0] - b[0], a[1] - b[1]) <= tol


def priority(target: Target) -> tuple[str, ...]:
    if target.name in ('session', 'price'):
        return ('ocr', 'native', 'llm')
    return ('native', 'ocr', 'llm')


def decide(target: Target, hits: dict[str, Point], tol: int) -> Verdict:
    if not hits:
        return Verdict(target.name, None, 'missing', '未在当前页面识别到该目标控件', {})
    order = [name for name in priority(target) if name in hits] + [name for name in hits if name not in priority(target)]
    if len(hits) == 1:
        only = order[0]
        return Verdict(target.name, hits[only], 'single', f'仅 {only} 定位命中', dict(hits))

    names = list(order)
    pairs = [(names[i], names[j]) for i in range(len(names)) for j in range(i + 1, len(names))]
    agreeing = [(a, b) for a, b in pairs if agree(hits[a], hits[b], tol)]

    if len(agreeing) == len(pairs):
        xs = [p[0] for p in hits.values()]
        ys = [p[1] for p in hits.values()]
        avg = (round(sum(xs) / len(xs)), round(sum(ys) / len(ys)))
        return Verdict(target.name, avg, 'agree', f'{len(hits)} 路定位一致 (<= {tol}px)', dict(hits))

    if agreeing:
        a, b = agreeing[0]
        avg = ((hits[a][0] + hits[b][0]) // 2, (hits[a][1] + hits[b][1]) // 2)
        outliers = [k for k in names if k not in (a, b)]
        return Verdict(
            target.name,
            avg,
            'agree',
            f'{a}+{b} 双路一致；忽略偏差源 {outliers}',
            dict(hits),
        )

    winner = order[0]
    return Verdict(
        target.name,
        hits[winner],
        'conflict',
        f'多源定位偏差 > {tol}px {hits}，按优先级采用 {winner}',
        dict(hits),
    )


def build_locators(cfg: DamaiConfig, *, ocr_engine: Callable | None = None) -> list[Locator]:
    chain: list[Locator] = [NativeLocator(), OcrLocator(cfg.ocr, engine=ocr_engine)]
    if cfg.llm.enabled:
        chain.append(LlmLocator(cfg.llm))
    return chain


def calibrate_targets(
    snap: Snapshot,
    cfg: DamaiConfig,
    wanted: list[Target],
    *,
    ocr_engine: Callable | None = None,
) -> list[Verdict]:
    locators = build_locators(cfg, ocr_engine=ocr_engine)
    verdicts: list[Verdict] = []
    for target in wanted:
        hits: dict[str, Point] = {}
        for locator in locators:
            try:
                pt = locator.locate(target, snap)
            except Exception as exc:
                log.warning('校准定位器 %s 在 %s 异常：%s', locator.name, target.name, exc)
                pt = None
            if pt is not None:
                hits[locator.name] = pt
        verdicts.append(decide(target, hits, cfg.agree_tolerance_px))
    return verdicts


def calibrate_live(
    transport: DamaiTransport,
    cfg: DamaiConfig,
    cache: CoordCache,
    *,
    ocr_engine: Callable | None = None,
    log_fn: Callable[[str, str], None] | None = None,
) -> tuple[Page, list[Verdict], Snapshot]:
    """Calibrate all targets on the currently visible Damai page and merge into `cache`.

    When calibrating on a live-sale SKU page whose price section is hidden until a
    session row is selected (`sessions` non-empty and `prices` empty), automatically
    taps the matched session row if `cfg.auto_select_session_on_calibrate` is True,
    re-snapshots, and calibrates `price` + `confirm`.
    """
    emit_log = log_fn or (lambda level, msg: log.info('%s', msg))
    snap = transport.snapshot(need_image=True)
    page = pages.detect(snap.activity, snap.root)
    if page == Page.VERIFY:
        raise NeedsAttention('当前处于滑块安全验证页面，请先在手机完成滑块验证后再校准坐标', kind='security')
    if page in (Page.UNKNOWN, Page.PAY, Page.SEAT):
        raise ValueError(
            f'当前页面为「{PAGE_LABELS.get(page, page.value)}」({snap.activity or "未知 Activity"})，'
            '请先在手机大麦打开【商品详情页】、【场次票档页】或【订单确认页】后再点击校准'
        )

    all_targets = targets.build(cfg)
    verdicts: list[Verdict] = []

    if page == Page.SKU:
        sessions, prices = split_rows(snap.root, snap.image)
        if sessions and not prices and cfg.auto_select_session_on_calibrate:
            sess_target = all_targets['session']
            sess_verdict = calibrate_targets(snap, cfg, [sess_target], ocr_engine=ocr_engine)[0]
            verdicts.append(sess_verdict)
            if sess_verdict.point is not None:
                cache.set(
                    sess_target.name,
                    sess_verdict.point,
                    source=sess_verdict.status,
                    detail=sess_verdict.detail,
                    target=sess_target,
                    screen_size=(snap.width, snap.height),
                )
                emit_log('INFO', f'当前场次尚未展开票档，自动点击场次 {sess_verdict.point} 以展开票档继续校准')
                transport.tap(sess_verdict.point, jitter_ms=cfg.jitter_ms)
                transport.wait(0.65)
                snap2 = transport.snapshot(need_image=True)
                page2 = pages.detect(snap2.activity, snap2.root)
                if page2 == Page.VERIFY:
                    cache.save()
                    raise NeedsAttention('点击场次后触发滑块验证，请先手动完成滑块验证后再继续校准', kind='security')
                if page2 == Page.SKU:
                    snap = snap2
                    rest = calibrate_targets(
                        snap2, cfg, [all_targets['price'], all_targets['confirm']], ocr_engine=ocr_engine
                    )
                    verdicts.extend(rest)
                else:
                    emit_log('WARNING', f'点击场次后页面变为 {PAGE_LABELS.get(page2, page2.value)}，仅保存场次坐标')
            else:
                emit_log('WARNING', '未能在当前场次列表定位目标场次，请核对场次关键词或序号')
        else:
            wanted = [all_targets['session'], all_targets['price'], all_targets['confirm']]
            verdicts = calibrate_targets(snap, cfg, wanted, ocr_engine=ocr_engine)
    else:
        wanted = [t for t in all_targets.values() if t.page == page]
        verdicts = calibrate_targets(snap, cfg, wanted, ocr_engine=ocr_engine)

    for v in verdicts:
        target = all_targets[v.target]
        if v.point is not None:
            cache.set(
                v.target,
                v.point,
                source=v.status,
                detail=v.detail,
                target=target,
                screen_size=(snap.width, snap.height),
            )
            emit_log('INFO', f'已校准 [{v.target}] -> {v.point}（{v.status}: {v.detail}）')
        else:
            emit_log('WARNING', f'未校准 [{v.target}]：{v.detail}')

    cache.save()
    return page, verdicts, snap
