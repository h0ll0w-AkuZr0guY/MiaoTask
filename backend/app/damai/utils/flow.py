"""Real-time Damai ticket-grabbing sprint state machine.

Handles the complete live-sale path:
DETAIL (立即预订/立即购买)
  -> SKU (选场次 -> 选票档 -> 一键下单/确定/去选座)
  -> ORDER (DmOrderActivity: 核验并勾选未选观演人 -> 可选点击立即提交)
  -> PAY (H5PayActivity: 立即停止，交由人工完成支付)
  / SEAT (TSeatActivity: 停止并提示人工选座)
  / VERIFY (滑块安全验证: 系统提醒 + 暂停点击，人工滑完自动恢复)
"""
from __future__ import annotations

from dataclasses import dataclass
import logging
import re
import time
from typing import Callable

from backend.app.damai.utils import calibrate, pages, targets
from backend.app.damai.utils.adb_io import DamaiTransport
from backend.app.damai.utils.config import DamaiConfig
from backend.app.damai.utils.hierarchy import ViewerItem, split_rows, viewer_checkboxes
from backend.app.damai.utils.locators import CachedLocator, CoordCache, NativeLocator, Point, Snapshot
from backend.app.damai.utils.locators.native import pick_row
from backend.app.damai.utils.pages import PAGE_LABELS, Page
from backend.core.errors import NeedsAttention
from backend.core.service import notify as service_notify

log = logging.getLogger('backend.damai.flow')


@dataclass(frozen=True)
class SprintResult:
    status: str  # "submitted" | "order_ready" | "reached_pay" | "reached_seat" | "exhausted"
    page: Page
    attempts: int
    message: str


def viewer_name_matches(configured: str, displayed: str) -> bool:
    """Match configured real name ('张三' / '何云亮') against Damai masked display ('张*' / '何*亮')."""
    cfg_clean = re.sub(r'\s+', '', configured or '')
    disp_clean = re.sub(r'\s+', '', displayed or '')
    if not cfg_clean or not disp_clean:
        return False
    if cfg_clean == disp_clean or cfg_clean in disp_clean:
        return True
    if '*' in disp_clean:
        pattern = '^' + '.*'.join(re.escape(part) for part in disp_clean.split('*')) + '$'
        return bool(re.match(pattern, cfg_clean))
    return False


class Sprint:
    def __init__(
        self,
        transport: DamaiTransport,
        cfg: DamaiConfig,
        cache: CoordCache,
        *,
        ocr_engine: Callable | None = None,
        emit: Callable[..., None] | None = None,
        log_fn: Callable[[str, str], None] | None = None,
    ) -> None:
        self.transport = transport
        self.cfg = cfg
        self.cache = cache
        self.targets = targets.build(cfg)
        self.cached = CachedLocator(cache)
        self.native = NativeLocator()
        self._ocr_engine = ocr_engine
        self.emit = emit or (lambda **event: None)
        self.log = log_fn or (lambda level, msg: log.info('%s', msg))

    def validate_cached_on_page(self, snap: Snapshot | None = None) -> None:
        """Cross-check cached points against live XML before/during sprint.

        Crucially handles presale -> live-sale transition where `btn_buy_view`
        (`x=839`) disappears and `id_quick_seat_selection_bottom_left_action_layout`
        (`一键下单`, `x=299`) takes over.
        """
        snap = snap or self.transport.snapshot(need_image=False)
        page = pages.detect(snap.activity, snap.root)
        changed = False
        for target in self.targets.values():
            if target.page != page:
                continue
            live = self.native.locate(target, snap)
            if live is None:
                continue
            cached_pt = self.cache.get(target.name, target=target, width=snap.width, height=snap.height)
            if cached_pt is None:
                self.cache.set(
                    target.name, live, source='native_live', detail='实时 XML 自动补齐',
                    target=target, screen_size=(snap.width, snap.height),
                )
                changed = True
            elif not calibrate.agree(cached_pt, live, self.cfg.agree_tolerance_px):
                self.log('INFO', f'检测到 [{target.name}] 实时位置由 {cached_pt} 变为 {live}，已自动更新缓存坐标')
                self.cache.set(
                    target.name, live, source='native_relocated', detail='开售按钮实时重定位',
                    target=target, screen_size=(snap.width, snap.height),
                )
                changed = True
        if changed:
            self.cache.save()

    def _point_for(self, name: str, snap: Snapshot) -> Point | None:
        target = self.targets[name]
        # Prefer 0ms live XML when present so layout shifts (e.g. 一键下单 vs 确定) never miss.
        live = self.native.locate(target, snap)
        if live is not None:
            cached_pt = self.cache.get(name, target=target, width=snap.width, height=snap.height)
            if cached_pt is None or not calibrate.agree(cached_pt, live, self.cfg.agree_tolerance_px):
                self.cache.set(
                    name, live, source='native_live', detail='实时 XML 核定',
                    target=target, screen_size=(snap.width, snap.height),
                )
            return live
        pt = self.cached.locate(target, snap)
        if pt is not None:
            return pt
        return self._relocate_with_vision(name)

    def _relocate_with_vision(self, name: str) -> Point | None:
        snap = self.transport.snapshot(need_image=True)
        page = pages.detect(snap.activity, snap.root)
        if page == Page.VERIFY:
            self._handle_verify()
            return None
        target = self.targets[name]
        verdicts = calibrate.calibrate_targets(snap, self.cfg, [target], ocr_engine=self._ocr_engine)
        verdict = verdicts[0]
        if verdict.point is not None:
            self.cache.set(
                name, verdict.point, source=verdict.status, detail=verdict.detail,
                target=target, screen_size=(snap.width, snap.height),
            )
            self.cache.save()
            self.log('INFO', f'实时视觉重定位 [{name}] -> {verdict.point}（{verdict.status}）')
        return verdict.point

    def _handle_verify(self) -> None:
        service_notify.notify_security_attention()
        if not self.cfg.auto_resume_verify:
            raise NeedsAttention('检测到滑块安全验证，已暂停自动点击；请在手机手动完成验证后重新开始', kind='security')
        self.log('WARNING', f'检测到滑块安全验证！已停止自动点击，请在 {int(self.cfg.verify_wait_s)} 秒内手动滑块，完成后将自动继续抢票')
        self.emit(phase='verify_paused', current_task='等待人工完成滑块验证', message='检测到滑块验证，请在手机手动滑块，完成后自动恢复')
        deadline = time.monotonic() + self.cfg.verify_wait_s
        while time.monotonic() < deadline:
            self.transport.wait(1.0)
            snap = self.transport.snapshot(need_image=False)
            now_page = pages.detect(snap.activity, snap.root)
            if now_page != Page.VERIFY:
                self.log('INFO', f'滑块验证已通过，当前页面：{PAGE_LABELS.get(now_page, now_page.value)}，自动恢复抢票')
                self.emit(phase='running', current_task='大麦抢票冲刺', message='滑块验证已完成，继续自动抢票')
                return
        raise NeedsAttention('滑块安全验证等待超时，请手动完成验证后重新开始', kind='security')

    def _wait_leave(self, origin: Page, timeout_s: float) -> tuple[Page, Snapshot]:
        deadline = time.monotonic() + max(0.2, timeout_s)
        last_snap = self.transport.snapshot(need_image=False)
        while True:
            now_page = pages.detect(last_snap.activity, last_snap.root)
            if now_page == Page.VERIFY:
                self._handle_verify()
                last_snap = self.transport.snapshot(need_image=False)
                return pages.detect(last_snap.activity, last_snap.root), last_snap
            if now_page != origin and now_page != Page.UNKNOWN:
                return now_page, last_snap
            if time.monotonic() >= deadline:
                return now_page, last_snap
            self.transport.wait(0.1)
            last_snap = self.transport.snapshot(need_image=False)

    def _wait_prices_expanded(self, timeout_s: float = 0.65) -> Snapshot:
        deadline = time.monotonic() + timeout_s
        snap = self.transport.snapshot(need_image=False)
        while time.monotonic() < deadline:
            page = pages.detect(snap.activity, snap.root)
            if page == Page.VERIFY:
                self._handle_verify()
                return self.transport.snapshot(need_image=False)
            _, prices = split_rows(snap.root)
            if prices:
                return snap
            self.transport.wait(0.1)
            snap = self.transport.snapshot(need_image=False)
        return snap

    def _step_sku(self, snap: Snapshot, attempt: int) -> Page:
        sessions, prices = split_rows(snap.root)
        sess_target = self.targets['session']
        chosen_sess = pick_row(sessions, sess_target, prefer_available=False)

        # Tap session if price rows have not expanded yet or XML shows session unselected on first attempt.
        need_session_tap = (not prices) or (chosen_sess is not None and not chosen_sess.selected and attempt == 1)
        if need_session_tap:
            sess_pt = self._point_for('session', snap)
            if sess_pt is not None:
                self.transport.tap(sess_pt, jitter_ms=self.cfg.jitter_ms)
                if not prices:
                    snap = self._wait_prices_expanded(timeout_s=0.65)
                    sessions, prices = split_rows(snap.root)

        price_pt = self._point_for('price', snap)
        if price_pt is not None:
            chosen_price = pick_row(prices, self.targets['price'], prefer_available=True)
            if chosen_price is not None and chosen_price.sold_out:
                self.log('INFO', f'第 {attempt} 轮：目标票档「{chosen_price.label}」暂显示缺货/无票，尝试点击刷回流票')
            self.transport.tap(price_pt, jitter_ms=self.cfg.jitter_ms)
        else:
            self.log('WARNING', f'第 {attempt} 轮：尚未定位到目标票档坐标')

        confirm_pt = self._point_for('confirm', snap)
        if confirm_pt is None:
            snap_after = self.transport.snapshot(need_image=False)
            confirm_pt = self._point_for('confirm', snap_after)
        if confirm_pt is not None:
            self.transport.tap(confirm_pt, jitter_ms=self.cfg.jitter_ms)

        next_page, _ = self._wait_leave(Page.SKU, timeout_s=min(1.2, self.cfg.page_timeout_s))
        if next_page == Page.SKU and attempt % 3 == 0:
            # Periodically inspect live-sale pixel border to verify row selection and refresh drifted cache.
            visual_snap = self.transport.snapshot(need_image=True)
            v_sessions, v_prices = split_rows(visual_snap.root, visual_snap.image)
            v_sess = pick_row(v_sessions, self.targets['session'], prefer_available=False)
            v_price = pick_row(v_prices, self.targets['price'], prefer_available=True)
            if v_sess is not None and not v_sess.selected:
                self.transport.tap(v_sess.box.center, jitter_ms=self.cfg.jitter_ms)
            if v_price is not None and not v_price.selected:
                self.transport.tap(v_price.box.center, jitter_ms=self.cfg.jitter_ms)
        return next_page

    def _select_target_viewers(self, items: list[ViewerItem]) -> list[ViewerItem]:
        if not items:
            return []
        if self.cfg.viewer_names:
            to_tap: list[ViewerItem] = []
            for name in self.cfg.viewer_names:
                match = next((it for it in items if viewer_name_matches(name, it.name)), None)
                if match is not None and not match.checked and match not in to_tap:
                    to_tap.append(match)
            return to_tap
        if self.cfg.viewer_count <= 0:
            return []
        checked_count = sum(1 for it in items if it.checked)
        needed = max(0, self.cfg.viewer_count - checked_count)
        if needed == 0:
            return []
        unchecked = [it for it in items if not it.checked]
        return unchecked[:needed]

    def _handle_order(self, snap: Snapshot, attempt: int) -> SprintResult | None:
        items = viewer_checkboxes(snap.root)
        to_tap = self._select_target_viewers(items)
        for viewer in to_tap:
            self.log('INFO', f'勾选实名观演人：{viewer.name or f"第 {viewer.index + 1} 位"} {viewer.box.center}')
            self.transport.tap(viewer.box.center, jitter_ms=self.cfg.jitter_ms)
        if to_tap:
            self.transport.wait(0.25)
            snap = self.transport.snapshot(need_image=False)
            now_page = pages.detect(snap.activity, snap.root)
            if now_page == Page.VERIFY:
                self._handle_verify()
                return None

        if not self.cfg.submit_order:
            self.log('INFO', '已到达订单确认页并完成观演人核验（当前未开启「自动提交订单」，请手动点击立即提交）')
            return SprintResult(
                status='order_ready',
                page=Page.ORDER,
                attempts=attempt,
                message='已到达订单确认页并核验观演人；未开启自动提交订单，请在手机确认提交',
            )

        submit_pt = self._point_for('submit', snap)
        if submit_pt is None:
            raise NeedsAttention('已到达订单确认页，但未定位到「立即提交」按钮，请在手机手动点击提交', kind='manual')
        self.log('INFO', f'点击订单页「立即提交」 {submit_pt}')
        self.transport.tap(submit_pt, jitter_ms=self.cfg.jitter_ms)
        next_page, _ = self._wait_leave(Page.ORDER, timeout_s=self.cfg.page_timeout_s)
        if next_page == Page.PAY:
            self.log('INFO', '订单已提交并进入支付收银台，停止自动操作，请在手机完成支付')
            return SprintResult(
                status='submitted',
                page=Page.PAY,
                attempts=attempt,
                message='已成功提交订单并进入收银台，请在手机上完成支付',
            )
        if next_page == Page.ORDER:
            # May need one more attempt if stock lock returned toast or viewer state refreshed.
            return None
        return SprintResult(
            status='submitted',
            page=next_page,
            attempts=attempt,
            message=f'已点击立即提交，当前页面：{PAGE_LABELS.get(next_page, next_page.value)}，请在手机确认订单与支付',
        )

    def run(self) -> SprintResult:
        initial_snap = self.transport.snapshot(need_image=False)
        self.validate_cached_on_page(initial_snap)

        for attempt in range(1, self.cfg.max_attempts + 1):
            self.transport.check()
            snap = initial_snap if attempt == 1 else self.transport.snapshot(need_image=False)
            page = pages.detect(snap.activity, snap.root)
            label = PAGE_LABELS.get(page, page.value)
            self.emit(
                phase='sprint',
                current_task=f'大麦抢票第 {attempt}/{self.cfg.max_attempts} 轮（{label}）',
                remaining=max(0, self.cfg.max_attempts - attempt),
            )

            if page == Page.VERIFY:
                self._handle_verify()
                continue

            if page == Page.PAY:
                self.log('INFO', '已到达支付收银台，停止一切自动操作，请在手机完成付款')
                return SprintResult(
                    status='reached_pay',
                    page=Page.PAY,
                    attempts=attempt,
                    message='已到达支付收银台，请在手机上完成付款',
                )

            if page == Page.SEAT:
                self.log('INFO', '已进入在线选座页，停止自动点击，请在手机手动选座提交')
                return SprintResult(
                    status='reached_seat',
                    page=Page.SEAT,
                    attempts=attempt,
                    message='已进入在线选座页，请在手机上手动选座并提交订单',
                )

            if page == Page.ORDER:
                done = self._handle_order(snap, attempt)
                if done is not None:
                    return done
                continue

            if page == Page.DETAIL:
                pt = self._point_for('buy_bar', snap)
                if pt is None:
                    self.log('WARNING', f'第 {attempt} 轮：详情页未定位到底部购票按钮')
                    self.transport.wait(0.2)
                    continue
                self.transport.tap(pt, jitter_ms=self.cfg.jitter_ms)
                self._wait_leave(Page.DETAIL, timeout_s=min(1.5, self.cfg.page_timeout_s))
                continue

            if page == Page.SKU:
                self._step_sku(snap, attempt)
                continue

            self.log('WARNING', f'第 {attempt} 轮：当前页面未知 ({snap.activity or "无焦点"})，等待页面稳定')
            self.transport.wait(0.25)

        final_snap = self.transport.snapshot(need_image=False)
        final_page = pages.detect(final_snap.activity, final_snap.root)
        return SprintResult(
            status='exhausted',
            page=final_page,
            attempts=self.cfg.max_attempts,
            message=f'已完成 {self.cfg.max_attempts} 轮抢票尝试，当前停留在「{PAGE_LABELS.get(final_page, final_page.value)}」',
        )
