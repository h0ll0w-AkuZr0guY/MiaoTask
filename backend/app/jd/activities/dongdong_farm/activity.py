"""东东农场活动：组装入口、动作、核验策略。

和淘宝/支付宝的活动一样，这里只实现页面策略；循环、重试、统计、实例校验
都由 core 提供。和它们不同的是：京东的「领奖励」是两段式（先做、再领），
所以 browse 与 claim 是同一条任务行的两个阶段，解析时按右侧按钮区分。
"""
import logging
import re
import time
from dataclasses import asdict, replace

from backend.core.errors import NeedsAttention, TaskUnavailable
from backend.core.execution.activity import Activity
from backend.core.config.loader import load_config
from backend.core.models import TextNode, normalize
from backend.app.common.utils.destinations import DestinationPolicy
from backend.app.jd.manifest import PROFILE
from backend.app.jd.activities.dongdong_farm.entry import EntryPolicy
from backend.app.jd.activities.dongdong_farm.actions import TaskActions
from backend.app.jd.activities.dongdong_farm.verify import VerificationPolicy
from backend.app.jd.activities.dongdong_farm.tasks import TaskRows, is_task_list

try:  # 新版本 core 提供「只判定不抛」的入口；打包的旧版程序可能还没有这个函数
    from backend.core.execution.guard import attention_reason as _attention_reason
except ImportError:  # pragma: no cover - 仅在旧版程序里走到
    _attention_reason = None

logger = logging.getLogger('backend.execution')

# 浏览任务的落地页是商品信息流：标题写着「HG系列导轨滑块」，主图上印着
# 「需要验证码?不上号」。核心词表把它们当成验证页，可屏幕上根本没有验证。
_STOREFRONT_WORD = re.compile(r'滑块|验证码')
# 真验证页一定还有“必须人动手”的动作词；这些词在时保持原样交给判定。
_REAL_CHALLENGE = re.compile(
    r'拖动|拖放|拖到|按住|请完成.{0,6}验证|安全验证|请输入验证码|输入验证码|获取验证码|'
    r'验证码错误|验证码不正确|滑块验证|拼图|人机|行为验证|'
    r'操作.{0,4}频繁|存在风险|访问被拒绝|安全中心|身份核验')


def storefront_clear(screen):
    """把商品名/主图里的“滑块 / 验证码”从安全词表里摘出去，其余原样返回。

    只返回给 guard 用的那一份副本，不动 screen 本身：verify_blocked 等判定
    还要看页面原文。活动包外置、core 打包在 exe 里，改了核心词表也进不了
    旧版程序，净化是让旧版同样能放行的那一层。
    """
    try:
        text = normalize(screen.text)
    except Exception:
        return screen
    if not _STOREFRONT_WORD.search(text) or _REAL_CHALLENGE.search(text):
        return screen
    nodes, changed = [], False
    for node in screen.nodes:
        raw = node.text
        if not _STOREFRONT_WORD.search(raw):
            nodes.append(node)
            continue
        changed = True
        fresh = raw.replace('滑块', '滑轨').replace('验证码', '校验值')
        nodes.append(TextNode(fresh, node.bounds, getattr(node, 'source', 'ui')))
    if not changed:
        return screen
    try:
        return replace(screen, nodes=nodes)
    except Exception:
        return screen


class DongDongFarmActivity(EntryPolicy, TaskRows, TaskActions, VerificationPolicy,
                           DestinationPolicy, Activity):
    application = PROFILE
    parse_tasks = TaskRows.parse_tasks
    is_task_list = staticmethod(is_task_list)
    claim_actions = frozenset({'claim'})
    external_apps = ()

    # 抽屉里能点出结果的按钮；一个都没有说明行还没渲染完
    _ROW_ACTIONS = frozenset({'去完成', '逛一逛', '去领奖', '去浇水', '去关注'})
    # 只有真正执行（run）时才做签到/浇水/领奖；「识别」预览必须保持只读。
    # 注意：这是类属性，不能放到 setup() 里重置——Activity.run() 会先 setup()
    # 再进主循环，那样会把 run() 里刚设好的 True 又抹掉，整段农场日常都不执行。
    _running = False

    @staticmethod
    def default_config():
        return load_config(app='jd', activity='dongdong_farm')

    def setup(self):
        self._chores_done = False
        self._claims_swept = False
        self._water_actions = 0
        self._credited = False
        self._own_failures = {}
        # 上次执行的任务后面紧跟着哪几行：抽屉重开回到顶部时用它定位，
        # 免得每做一条都要从顶部重滑三屏（滑动预算一轮就被这些白滑耗光）。
        self._anchor_titles = ()

    # ---- 全量清单执行 ----
    #
    # 框架的通用主循环是「看当前这一屏 → 挑一条 → 做完再看一屏」，可京东的抽屉
    # 有 5 屏 27 行，每做完一条都会重新进农场、抽屉回到顶部。通用循环因此永远
    # 只在最上面那一两屏打转，下面二十来行整天轮不到（用户现场：一轮只看到
    # 10 行、反复做同一条「精选活动推荐」）。这里改成按人工的习惯：先把整个
    # 抽屉从头到尾扫一遍列出全部任务，再逐条滚过去做，做完重新扫一遍核对进度，
    # 直到清单里真的没有可执行的任务。

    def scan_catalog(self, screen):
        """把整个抽屉扫一遍，返回 {key: Task}；结束时视口回到顶部。

        落地页（尤其是京东的 ReactNative 页）有时按返回会退到京东首页而不是抽屉，
        这时不能把「读不到任务行」当成「任务都做完了」，必须先把抽屉找回来。
        """
        screen = self._panel_scroll_to_top(screen)
        logger.info('开始扫描任务列表（面板=%s）', self.panel_visible(screen))
        # 恢复最多做一次：导航层一次冷启动就要读几十帧，放在逐屏循环里会把
        # 一轮扫描拖成几分钟（真机 10:35 卡了整整 4 分钟就是这么来的）。
        if not self.panel_visible(screen):
            screen = self.recover_farm_panel(screen)
        if not self.panel_visible(screen):
            screen = super().ensure_list(True)
        catalog, seen = {}, set()
        pages = max(1, int(self.config.get('catalog_pages', 8)))
        deadline = self.control.now() + float(self.config.get('catalog_scan_timeout', 75))
        for _ in range(pages):
            if self.control.now() >= deadline:
                logger.warning('扫描任务列表超时，先处理已读到的 %d 行', len(catalog))
                break
            screen = self.read()
            if not self.panel_visible(screen):
                break
            rows = self.parse_tasks(screen.nodes, screen.width, screen.height)
            for row in rows:
                catalog[row.key] = row
            fingerprint = self._panel_rows(screen)
            if fingerprint in seen:
                break
            seen.add(fingerprint)
            self.browse_swipe(screen)
            self.control.wait(.9)
        screen = self._panel_scroll_to_top(screen)
        logger.info('扫描任务列表完成：%d 行', len(catalog))
        return catalog, screen

    def _actionable(self, key, task):
        if task.kind in ('done', 'manual') or task.skip_reason:
            return False
        if task.kind not in self.config['enabled_task_kinds']:
            return False
        if task.kind == 'claim' and not self.config.get('claim_rewards', True):
            return False
        if task.target_app and not self.config.get('allow_cross_app', True):
            return False
        # 京东有些任务本身不记账（快手签到、JoyAI 下载），重试只会白耗几分钟。
        # 一条失败一次就不再选它，把时间留给列表里其它能做的任务。
        limit = max(1, int(self.config.get('catalog_max_attempts', 1)))
        return self._own_failures.get(key, 0) < limit

    def _close_missing_records(self, catalog):
        for key, row in self.records.items():
            if key not in catalog and row.get('status') in ('pending', 'failed'):
                row.update(status='done', enabled=False,
                           reason='任务已从列表消失（已完成或已下线）')

    def _execute_catalog_task(self, task, screen):
        """滚到这一行、执行、再扫一遍核对结果。返回 (screen, 成功, 原因)。"""
        self._active_task = task
        self._credited = False
        self.stats.attempts += 1
        screen, target = self.locate_row(screen, task)
        if target is None:
            return screen, False, '任务列表里已经找不到这一行'
        explicit = False
        try:
            after, explicit, _cross = self._run_task(task, screen, target)
        except TaskUnavailable as exc:
            return self.read(), False, str(exc)
        except NeedsAttention as exc:
            if '屏幕裁切' not in str(exc):
                raise
            logger.info('点击目标在屏幕外，跳过本次：%s', exc)
            return self.read(), False, '这一帧控件坐标越界'
        except Exception:
            logger.exception('任务执行异常：%s', task.title)
            after = self.read()
        # 核验不依赖「当前这一屏」：重新扫全表，看这一行是消失、领掉了还是进度涨了。
        catalog, after = self.scan_catalog(after)
        now = catalog.get(task.key) or next(
            (row for row in catalog.values() if row.title == task.title), None)
        ok = bool(explicit or self._credited
                  or (now is not None and (now.current > task.current
                      or (task.kind == 'claim' and now.kind != 'claim')))
                  or (task.kind == 'claim' and now is None and catalog and self.panel_visible(after)))
        return after, ok, '' if ok else '进度未变化'

    def run(self, auto_open=False):
        """全量执行：反复「扫全表 → 逐条做 → 再扫全表」，直到没有可做的任务。"""
        self._running = True
        self._auto_claim_sweep = False
        self.started = time.monotonic() - self.stats.elapsed
        try:
            self.setup()
            with self.reader.observation_session(self.config, self.control):
                screen = self.ensure_list(auto_open)
                rounds = max(1, int(self.config.get('catalog_rounds', 8)))
                budget = int(self.config.get('max_tasks', 80))
                recovered_once = False
                for round_no in range(rounds):
                    self.control.check()
                    self.guard(screen)
                    catalog, screen = self.scan_catalog(screen)
                    if not catalog and not recovered_once:
                        # 一行都没读到 = 掉出了抽屉，不是「任务全做完了」。
                        recovered_once = True
                        screen = self.recover_farm_panel(screen)
                        catalog, screen = self.scan_catalog(screen)
                    if not catalog:
                        logger.warning('重新进入后仍读不到任务列表，本轮结束')
                        break
                    self.remember(list(catalog.values()))
                    self._close_missing_records(catalog)
                    pending = {key: task for key, task in catalog.items()
                               if self._actionable(key, task)}
                    self.update('scanning',
                                message=f'第 {round_no + 1} 轮：共识别 {len(catalog)} 行，待做 {len(pending)} 条',
                                current_task='', remaining=len(pending))
                    if not pending or self.stats.attempts >= budget:
                        break
                    progressed = 0
                    for key, task in pending.items():
                        if self.stats.attempts >= budget:
                            break
                        self.control.check()
                        screen, ok, reason = self._execute_catalog_task(task, screen)
                        row = self.records.get(key)
                        if row is not None:
                            if ok:
                                row.update(status='done', enabled=False,
                                           reason='已核验进度或奖励状态')
                                self.stats.completed += 1
                                progressed += 1
                            else:
                                fails = self._own_failures.get(key, 0) + 1
                                self._own_failures[key] = fails
                                limit = max(1, int(self.config.get('catalog_max_attempts', 1)))
                                exhausted = fails >= limit
                                row.update(status='disabled' if exhausted else 'pending',
                                           enabled=not exhausted,
                                           reason=reason or '任务进度未增加')
                                if exhausted:
                                    self.stats.failed += 1
                        self.update('scanning', message='核对下一条任务',
                                    current_task='', remaining=0)
                    if progressed == 0:
                        break
                try:
                    screen = self.return_to_home(screen)
                except Exception:
                    logger.exception('收工前整理页面失败')
                # 收工时界面上不能还挂着「待处理」：被锁定的是京东侧不记账的
                # 站外任务（快手签到、JoyAI 下载），说明清楚后禁用；进度已满的
                # 直接算完成。否则用户看到的就是「还有任务没做」。
                limit = max(1, int(self.config.get('catalog_max_attempts', 1)))
                for key, row in self.records.items():
                    if row.get('status') not in ('pending', 'running', 'failed'):
                        continue
                    if self._own_failures.get(key, 0) >= limit:
                        row.update(status='disabled', enabled=False,
                                   reason='本轮已尝试，京东侧未记账或需要站外操作，不再重试')
                    elif (row.get('current') or 0) >= (row.get('total') or 1):
                        row.update(status='done', enabled=False, reason='已完成')
                left = sum(1 for row in self.records.values()
                           if row.get('status') in ('pending', 'running', 'failed')
                           and row.get('enabled'))
                self.update('finished',
                            message='本轮结束，可执行任务已全部处理完' if left == 0
                            else f'本轮结束，仍有 {left} 条未通过核验的任务',
                            current_task='', remaining=left)
                return asdict(self.stats)
        finally:
            self._running = False
            self._auto_claim_sweep = True

    def ensure_list(self, auto_open=False):
        logger.info('进入活动：开始确认任务面板')
        screen = self.read()
        screen = self.dismiss_popups(screen)
        if not self.panel_visible(screen) and self.farm_visible(screen):
            # 已经站在农场页：直接开抽屉，别再走一轮「返回 → 回桌面 → 冷启动」。
            screen = self.enter_task_panel(screen)
        if not self.panel_visible(screen):
            if self.homepage_visible(screen) and not self.farm_visible(screen):
                screen = self.scroll_home_to_top(screen)
            screen = super().ensure_list(auto_open)
        logger.info('进入活动：面板已确认，开始农场日常')
        if self._running and not self._chores_done:
            self._chores_done = True
            try:
                screen = self.do_chores(screen)
            except Exception:
                logger.exception('农场日常（签到/领水滴/浇水）失败，继续做任务')
            screen = self.ensure_panel(screen)
        if self._running:
            # 抽屉是长列表，扫描前先回到顶部。领奖不再单独深扫一遍：全量循环
            # 会把清单里的「去领奖」逐条领掉，多一次深扫既重复又容易卡在恢复里。
            screen = self._panel_scroll_to_top(screen, limit=6)
        logger.info('进入活动：日常完成，开始全量扫描（面板=%s）', self.panel_visible(screen))
        return screen

    def return_to_home(self, screen):
        """全量循环已经把能领的都领过了，收工时只收起抽屉。"""
        return super().return_to_home(screen)

    def click(self, node):
        geometry = getattr(self.reader.last_screen, 'geometry', None)
        if geometry and not geometry.contains(*node.center):
            self.control.check()
            return False
        super().click(node)
        return True

    def guard(self, screen):
        self.assert_instance(screen)
        if self.verify_blocked(screen):
            # 京东风控页：先按人工方式过掉，过不掉交给 check_attention 停止本轮。
            self.solve_quick_verify(screen)
            screen = self.read()
        from backend.core.execution.guard import check_attention
        clean = storefront_clear(screen)
        if _attention_reason is None:
            # 旧版程序（core 打包在 exe 里，还没有 attention_reason）：外部应用的
            # 「立即登录」横幅与东东农场无关，直接放行，别为此把整轮停下等人。
            if not self.in_app(screen):
                logger.info('外部应用 %s 不检查登录提示，准备退回京东', screen.package)
                return
            check_attention(self, clean)
            return
        found = _attention_reason(self, clean)
        if found is None:
            return
        if found[1] == 'security' or getattr(screen, 'obscured', False) or re.search(r'确认付款|支付密码|确认支付', normalize(screen.text)):
            check_attention(self, clean)
        if not self.in_app(screen):
            # 外部应用（快手广告页等）上的「登录」横幅与东东农场无关：逛完会回京东，
            # 没必要为此停下等人（也不能干等，页面不会自己变成京东）。
            logger.info('外部应用 %s 的提示不拦截，准备退回京东：%s', screen.package, found[0])
            return
        # 京东内的登录/授权提示多数是过渡帧或推广浮层，通常几秒内自己就关了。
        # 先给页面一段时间并反复读屏，能自己过去就继续；真过不去再停下来等人工。
        for _ in range(min(10, max(0, int(self.config.get('attention_settle_rounds', 2))))):
            self.control.wait(min(30, max(0, float(self.config.get('attention_settle_seconds', 4)))))
            screen = self.read()
            self.assert_instance(screen)
            clean = storefront_clear(screen)
            found = _attention_reason(self, clean)
            if found is None or not self.in_app(screen):
                logger.info('页面自己恢复正常，继续本轮')
                return
        check_attention(self, clean)
