"""MiaoTask Activity adapter for Damai timed ticket grabbing, page inspection, and calibration."""
from __future__ import annotations

from dataclasses import asdict
import logging
import os

from backend.app.damai.manifest import APP
from backend.app.damai.utils import calibrate, clock, pages, targets
from backend.app.damai.utils.adb_io import DamaiTransport
from backend.app.damai.utils.config import DamaiConfig, damai_config_from_options, validate_damai_options
from backend.app.damai.utils.flow import Sprint
from backend.app.damai.utils.hierarchy import ID_SELL_TIME, first_text, split_rows, viewer_checkboxes
from backend.app.damai.utils.locators import CoordCache, Snapshot, cache_path_for_device, list_models
from backend.app.damai.utils.pages import PAGE_LABELS, Page
from backend.app.damai.utils.targets import TARGET_LABELS
from backend.core.config.loader import load_config
from backend.core.execution.activity import Activity
from backend.core.models import Task
from backend.core.storage.preferences import has_activity_secret, read_activity_secret

logger = logging.getLogger('backend.damai.activity')


class DamaiTicketActivity(Activity):
    application = APP.profile
    offline_actions = frozenset({'clear_cache', 'models'})

    @staticmethod
    def default_config():
        return load_config(app='damai', activity='ticket_grab')

    @classmethod
    def validate_activity_options(cls, options):
        return validate_damai_options(options)

    @classmethod
    def describe_state(cls, device, config):
        raw_opts = (config or {}).get('activity_options') or {}
        ocr_on = bool((config or {}).get('ocr', True))
        cfg = damai_config_from_options(raw_opts, ocr_enabled=ocr_on)
        cache = CoordCache(cache_path_for_device(device))
        target_map = targets.build(cfg)
        has_key = has_activity_secret('damai', 'llm_api_key') or bool(os.environ.get('ANTHROPIC_API_KEY'))
        return {
            'damai': True,
            'cache_file': cache.path.name,
            'has_llm_key': has_key,
            'targets': [
                dict(
                    cache.entry_info(target),
                    label=TARGET_LABELS.get(target.name, target.name),
                    description=target.description,
                    keywords=list(target.keywords),
                    index=target.index,
                )
                for target in target_map.values()
            ],
        }

    def _log(self, level: str, message: str) -> None:
        lvl = getattr(logging, str(level).upper(), logging.INFO)
        logger.log(lvl, '%s', message)

    def _damai_config(self) -> DamaiConfig:
        api_key = read_activity_secret('damai', 'llm_api_key')
        return damai_config_from_options(
            self.config.get('activity_options'),
            api_key=api_key,
            ocr_enabled=bool(self.config.get('ocr', True)),
        )

    def _cache(self) -> CoordCache:
        return CoordCache(cache_path_for_device(self.device))

    def _transport(self) -> DamaiTransport:
        return DamaiTransport(self.device, self.control, on_image=self.reader.on_image)

    def _ensure_foreground_if_needed(self, auto_open: bool) -> None:
        if not auto_open or self.device is None:
            return
        try:
            info = self.device.current_app() or {}
            pkg = str(info.get('package') or '')
        except Exception:
            pkg = ''
        if pkg != 'cn.damai':
            self.device.resume_app('cn.damai')
            self.control.wait(0.6)

    def _inspect_snapshot(self, snap: Snapshot) -> tuple[dict[str, object], list[dict[str, object]], str]:
        page = pages.detect(snap.activity, snap.root)
        page_label = PAGE_LABELS.get(page, page.value)
        sell_txt = first_text(snap.root, ID_SELL_TIME)
        parsed_sell = clock.parse_sell_time(sell_txt)
        sessions, prices = split_rows(snap.root, snap.image)
        viewers = viewer_checkboxes(snap.root)

        inspection = {
            'page': page.value,
            'page_label': page_label,
            'activity': snap.activity,
            'screen_size': [snap.width, snap.height],
            'sell_time_raw': sell_txt,
            'sell_time_parsed': parsed_sell.strftime('%Y-%m-%d %H:%M:%S') if parsed_sell else '',
            'sessions': [
                {
                    'index': r.index,
                    'label': r.label,
                    'center': list(r.box.center),
                    'selected': r.selected,
                    'sold_out': r.sold_out,
                }
                for r in sessions
            ],
            'prices': [
                {
                    'index': r.index,
                    'label': r.label,
                    'center': list(r.box.center),
                    'selected': r.selected,
                    'sold_out': r.sold_out,
                }
                for r in prices
            ],
            'viewers': [
                {
                    'index': v.index,
                    'name': v.name,
                    'center': list(v.box.center),
                    'checked': v.checked,
                }
                for v in viewers
            ],
        }

        task_rows: list[dict[str, object]] = []
        if page == Page.DETAIL:
            detail = f'开售时间：{sell_txt}' if sell_txt else '已识别底部购票栏'
            t = Task('damai:detail', f'详情页就绪（{page_label}）', detail, '购票栏', (0, 0, 100, 40), 0, 1, kind='browse')
            row = t.to_dict()
            row.update(controllable=False, status='pending', reason='可点击「校准当前页面坐标」或「开始自动执行」')
            task_rows.append(row)
        elif page == Page.SKU:
            for s in sessions:
                tag = ('已选中' if s.selected else '可选') + (' · 缺货' if s.sold_out else '')
                t = Task(f'damai:sess:{s.index}', f'场次 [{s.index}] {s.label}', tag, '场次',
                         (s.box.left, s.box.top, s.box.right, s.box.bottom), 1 if s.selected else 0, 1, kind='browse')
                row = t.to_dict()
                row.update(controllable=False, status='done' if s.selected else 'pending', reason=f'中心坐标 {s.box.center}')
                task_rows.append(row)
            for p in prices:
                tag = ('已选中' if p.selected else '可选') + (' · 缺货/无票' if p.sold_out else ' · 可售')
                t = Task(f'damai:price:{p.index}', f'票档 [{p.index}] {p.label}', tag, '票档',
                         (p.box.left, p.box.top, p.box.right, p.box.bottom), 1 if p.selected else 0, 1, kind='browse')
                row = t.to_dict()
                row.update(controllable=False, status='done' if p.selected else 'pending', reason=f'中心坐标 {p.box.center}')
                task_rows.append(row)
        elif page == Page.ORDER:
            for v in viewers:
                t = Task(f'damai:viewer:{v.index}', f'实名观演人：{v.name or f"第 {v.index + 1} 位"}',
                         '已勾选' if v.checked else '未勾选', '观演人',
                         (v.box.left, v.box.top, v.box.right, v.box.bottom), 1 if v.checked else 0, 1, kind='browse')
                row = t.to_dict()
                row.update(controllable=False, status='done' if v.checked else 'pending', reason=f'复选框 {v.box.center}')
                task_rows.append(row)

        parts = [f'当前页面：{page_label}']
        if sell_txt:
            parts.append(f'开售：{sell_txt}')
        if sessions or prices:
            parts.append(f'场次 {len(sessions)} 项 / 票档 {len(prices)} 项')
        if viewers:
            parts.append(f'观演人 {len(viewers)} 人')
        return inspection, task_rows, ' · '.join(parts)

    def scan(self, auto_open=False):
        with self.reader.observation_session(self.config, self.control):
            self.control.check()
            self._ensure_foreground_if_needed(auto_open)
            transport = self._transport()
            snap = transport.snapshot(need_image=True)
            inspection, task_rows, summary = self._inspect_snapshot(snap)
            act_state = self.describe_state(self.device, self.config)
            act_state['inspection'] = inspection
            self._log('INFO', summary)
            self.emit(
                phase='ready',
                package='cn.damai' if 'cn.damai' in (snap.activity or '') else snap.activity,
                source='damai-xml',
                tasks=task_rows,
                remaining=0,
                current_task='',
                activity_state=act_state,
                message=summary,
            )
            return task_rows

    def perform_action(self, action_name, params=None):
        if action_name == 'clear_cache':
            cache = self._cache()
            cache.clear()
            self._log('INFO', '已清空当前设备的大麦坐标缓存')
            return {'last_calibration': None, 'message': '已清空当前设备的大麦坐标缓存'}

        if action_name == 'models':
            opts = self.config.get('activity_options') or {}
            base_url = str(opts.get('llm_base_url') or os.environ.get('ANTHROPIC_BASE_URL', '')).strip()
            api_key = read_activity_secret('damai', 'llm_api_key') or os.environ.get('ANTHROPIC_API_KEY', '')
            models = list_models(base_url, api_key)
            self._log('INFO', f'已检测视觉模型接口，返回 {len(models)} 个模型')
            return {'models': models, 'message': f'已获取 {len(models)} 个可用视觉模型'}

        if action_name == 'status':
            transport = self._transport()
            snap = transport.snapshot(need_image=True)
            inspection, task_rows, summary = self._inspect_snapshot(snap)
            self.emit(tasks=task_rows, package='cn.damai' if 'cn.damai' in (snap.activity or '') else snap.activity)
            self._log('INFO', summary)
            return {'inspection': inspection, 'message': summary}

        if action_name == 'calibrate':
            cfg = self._damai_config()
            cache = self._cache()
            transport = self._transport()
            page, verdicts, snap = calibrate.calibrate_live(transport, cfg, cache, log_fn=self._log)
            inspection, task_rows, _ = self._inspect_snapshot(snap)
            self.emit(tasks=task_rows, package='cn.damai' if 'cn.damai' in (snap.activity or '') else snap.activity)
            ok_count = sum(1 for v in verdicts if v.point is not None)
            page_label = PAGE_LABELS.get(page, page.value)
            return {
                'inspection': inspection,
                'last_calibration': {
                    'page': page.value,
                    'page_label': page_label,
                    'verdicts': [v.to_dict() for v in verdicts],
                },
                'message': f'坐标校准完成（{page_label}）：已写入 {ok_count}/{len(verdicts)} 个目标坐标',
            }

        raise ValueError(f'大麦活动不支持操作：{action_name}')

    def run(self, auto_open=False):
        with self.reader.observation_session(self.config, self.control):
            try:
                self.setup()
                self.control.check()
                self._ensure_foreground_if_needed(auto_open)
                cfg = self._damai_config()
                cache = self._cache()
                transport = self._transport()
                sprint = Sprint(transport, cfg, cache, emit=self.emit, log_fn=self._log)

                snap = transport.snapshot(need_image=False)
                sprint.validate_cached_on_page(snap)

                page = pages.detect(snap.activity, snap.root)
                target_dt = None
                if cfg.sell_time:
                    target_dt = clock.parse_config_time(cfg.sell_time)
                elif page == Page.DETAIL:
                    raw_sell = first_text(snap.root, ID_SELL_TIME)
                    parsed = clock.parse_sell_time(raw_sell)
                    if parsed is not None and parsed.timestamp() > (clock.time.time() + 1.0):
                        target_dt = parsed

                if target_dt is not None:
                    try:
                        offset_ms = clock.server_offset_ms()
                        self._log('INFO', f'大麦 mtop 时钟同步成功：server - local = {offset_ms:+.1f}ms')
                    except Exception as exc:
                        offset_ms = 0.0
                        self._log('WARNING', f'大麦 mtop 时钟接口暂不可达（{exc}），回退使用本机系统时钟 (0ms)')
                    fire_ms = target_dt.timestamp() * 1000.0 - cfg.lead_ms
                    if (fire_ms - offset_ms) / 1000.0 > clock.time.time():
                        self._log(
                            'INFO',
                            f'进入定时等待：目标开售 {target_dt:%Y-%m-%d %H:%M:%S}（提前 {cfg.lead_ms}ms 触发）',
                        )
                        clock.wait_until(
                            fire_ms,
                            offset_ms,
                            control=self.control,
                            on_tick=lambda rem: self.emit(
                                phase='countdown',
                                current_task=f'等待开售 {target_dt:%H:%M:%S}',
                                message=f'倒计时 {rem:.1f}s · 提前 {cfg.lead_ms}ms 开抢（时钟偏差 {offset_ms:+.0f}ms）',
                            ),
                        )

                result = sprint.run()
                self.stats.attempts = result.attempts
                if result.status in ('submitted', 'order_ready', 'reached_pay', 'reached_seat'):
                    self.stats.completed = 1
                self.daily_exhausted = False

                act_state = self.describe_state(self.device, self.config)
                act_state['last_sprint'] = {
                    'status': result.status,
                    'page': result.page.value,
                    'page_label': PAGE_LABELS.get(result.page, result.page.value),
                    'attempts': result.attempts,
                    'message': result.message,
                }
                self.emit(
                    phase='finished',
                    stats=asdict(self.stats),
                    current_task='',
                    remaining=0,
                    activity_state=act_state,
                    message=result.message,
                )
                return asdict(self.stats)
            finally:
                self.teardown()

    # Unused generic task-list hooks kept minimal to satisfy Activity ABC.
    def ensure_list(self, auto_open=False):
        return self.read()

    def parse_tasks(self, nodes, width, height):
        return []

    def is_task_list(self, nodes):
        return True

    def execute(self, task, before):
        return before, True, False

    def verify(self, task, before, after, explicit=False):
        return explicit
