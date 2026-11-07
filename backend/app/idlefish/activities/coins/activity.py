"""闲鱼币大富翁（严格按人工操作流程实现）。

只保留与人工操作一致的步骤：
1. 打开闲鱼 → 我的 → 闲鱼币 → 大富翁页面（交给 EntryPolicy 的导航钩子）
2. 大富翁页面上「前进获得闲鱼币」右侧那颗圆形按钮同时显示「赚更多」和「领」，
   就是任务列表入口，点击进入（EntryPolicy.task_panel_entry）
3. 任务列表内：
   - 优先点「签到」「领取奖励」
   - 再识别浏览类任务（看视频等），按要求的秒数持续浏览，返回列表领奖
   - 当前视口没有浏览任务就下滑继续找（由框架 SessionLoop 负责滚动）
4. 任务列表顶部的「累计任务奖励」达标后顺手领取
   （+100完成3次 / +300完成6次 / +500完成10次）
5. 回到大富翁页面，点「前进获得闲鱼币」直到按钮不再显示可用步数
"""
import hashlib
import logging
import random
import re

from backend.core.config.loader import load_config
from backend.core.errors import TaskUnavailable
from backend.core.models import Task, TextNode, normalize
from backend.core.execution.activity import Activity

from backend.app.idlefish.manifest import PROFILE
from .entry import EntryPolicy

logger = logging.getLogger('backend.execution')

# 任务行右侧的动作按钮
ACTION_RE = r'^(签到|领取奖励|立即领取|去完成|去浏览)$'
# 累计任务奖励卡片：未完成时是 +100完成3次，达到进度后变成 +100领取奖励
CUMULATIVE_RE = r'^\+\d+(?:完成\d+次|领取奖励)$'
# 已达成的累计奖励卡片（只有这种才可点，未达成的点了也没用）
CUMULATIVE_READY_RE = r'^\+\d+领取奖励$'
# 「前进获得闲鱼币」按钮
STEP_RE = r'再走\d+步完成1圈'
# 可用步数显示在按钮右上方，形如「再走4步完成1圈 5步」末尾的「5步」
STEP_LEFT_RE = r'(\d+)步$'
# 页面自己给出的完成提示：出现即认定任务已达标
DONE_PROMPT_RE = r'^(?:任务已完成|浏览已完成|浏览完成|完成浏览|奖励已到账|恭喜完成|领取成功|已领取)[!！。\s]*$'
# 浏览页倒计时（「还需12秒 / 剩余8秒」），用来动态调整结束时间
COUNTDOWN_RE = r'(?:还需|剩余|再逛|再看|浏览还差)[^\d]{0,3}(\d{1,2})\s*秒'
# 进入浏览页时的总时长提示（「浏览20秒获得100闲鱼币」）
BROWSE_SECONDS_RE = r'浏览\s*(\d+)\s*[秒sS]'


class IdleFishCoinsActivity(EntryPolicy, Activity):
    application = PROFILE

    def read(self, **kwargs):
        screen = super().read(**kwargs)
        self.guard(screen)
        return screen

    def _safe_read(self, screen=None, force_ocr=False):
        """读屏失败时沿用上一屏，不中断当前任务。

        闲鱼活动页是 H5 容器，浏览类任务的商品瀑布流滚得越久节点越多；
        uiautomator 导出控件树时可能抛出 StackOverflowError（Java 栈溢出，
        实测 20 秒连续滑动后必现）。这类崩溃只影响「看一眼」这一步，
        不应该让整个任务作废：返回上一屏，由调用方按「没有完成提示」
        继续走返回列表与核验流程。
        """
        try:
            return self.read(force_ocr=force_ocr)
        except Exception as exc:
            logger.warning('读取屏幕失败，沿用上一屏继续：%s', exc)
            return screen

    # ------------------------------------------------------------------
    # 契约：配置
    # ------------------------------------------------------------------
    @staticmethod
    def default_config():
        return load_config(app='idlefish', activity='coins')

    # ------------------------------------------------------------------
    # 契约：任务列表识别
    # ------------------------------------------------------------------
    def is_task_list(self, nodes):
        """任务列表特征：右侧至少两个动作按钮，或出现累计任务奖励卡片。

        不能只看单个「签到」字样——首页和「我的」页也有「现金签到」等文案，
        只靠词表会把普通页面误判成任务列表，进而在首页上执行假任务。
        """
        texts = [normalize(n.text) for n in nodes]
        actions = sum(1 for t in texts if re.fullmatch(ACTION_RE, t))
        if actions >= 2:
            return True
        return any(re.fullmatch(CUMULATIVE_RE, t) for t in texts)

    # ------------------------------------------------------------------
    # 契约：解析任务（签到 / 领取奖励 / 去完成）
    # ------------------------------------------------------------------
    def parse_tasks(self, nodes, width, height):
        tasks = []
        buttons = [n for n in nodes
                   if re.fullmatch(ACTION_RE, normalize(n.text))
                   and n.center[0] > width * 0.55]
        titles = [n for n in nodes
                  if width * 0.12 <= n.bounds[0] <= width * 0.35
                  and n.bounds[2] <= width * 0.75
                  and 0 < n.bounds[3] - n.bounds[1] < height * 0.05
                  and re.search(r'[\u4e00-\u9fff]', n.text)
                  and not re.fullmatch(ACTION_RE, normalize(n.text))
                  and not re.fullmatch(CUMULATIVE_RE, normalize(n.text))]
        for btn in buttons:
            action = normalize(btn.text)
            # 标题与按钮同一行；「签到」是独立按钮，没有同行标题
            row = [n for n in titles
                   if abs(n.center[1] - btn.center[1]) < height * 0.02]
            if row:
                title_node = min(row, key=lambda n: (n.bounds[0],
                                                    abs(n.center[1] - btn.center[1])))
                title = normalize(title_node.text)
            elif action == '签到':
                title = '每日签到'
            else:
                continue
            if any(k in title for k in self.config['game_keywords']):
                continue
            if any(k in title for k in self.config['skip_titles']):
                continue
            current, total = self._progress(nodes, btn, height)
            seconds = (self.config['browse']['video_seconds']
                       if self._is_video(title)
                       else self.config['browse']['regular_seconds'])
            # Stable identity: progress belongs to framework verification, not the key.
            key = hashlib.sha256(title.encode()).hexdigest()[:16]
            tasks.append(Task(key, title, title, action, btn.bounds,
                              current=current, total=total, seconds=seconds,
                              kind=self._classify(title, action)))
        return tasks

    @staticmethod
    def _is_video(title):
        return '视频' in title or '看15秒' in title or '看30秒' in title

    # 小游戏容器特征：XRiverActivity 常驻「游戏任务入口」，退出时弹
    # 「再玩1关游戏即可完成任务 / 残忍离开 / 继续玩」。
    GAME_SIGNS_RE = r'残忍离开|继续玩|再玩\d+关|游戏任务入口|即可完成任务'

    def _is_game_container(self, screen):
        return self._find(screen, self.GAME_SIGNS_RE) is not None

    def _exit_game(self):
        """退出小游戏容器，尽量回到任务列表。"""
        for _ in range(4):
            screen = self.read()
            if self._find(screen, r'残忍离开') is not None:
                self._click_text(screen, r'残忍离开')
                self.control.wait(2)
                continue
            if self._find(screen, r'游戏任务入口') is not None:
                # 点右上角关闭图标，触发「残忍离开」确认弹窗
                self.device.click(int(screen.width * .92), int(screen.height * .075))
                self.control.wait(2)
                continue
            if self.is_task_list(screen.nodes):
                return True
            self.device.go_back()
            self.control.wait(2)
        return self.is_task_list(self.read().nodes)

    def _classify(self, title, action):
        if action in ('签到', '领取奖励', '立即领取'):
            return 'claim'
        # 真实交易类（如「去浏览全新好物下单」）：涉及真实下单付款，默认跳过
        if '下单' in title or '购买' in title or '拍下' in title:
            return 'trade'
        # 搜索类任务（如「搜一搜推荐商品」）：进入搜索页后还需输入关键词并浏览
        if '搜一搜' in title or '搜索' in title or '搜' in title:
            return 'search'
        # 「看视频 / 浏览xx」都是闲鱼内的浏览任务（已实机验证：「去浏览福利好物」
        # 进入 CsjMall 页并提示「浏览15秒获得80闲鱼币」）。
        # 站内「逛逛」类（逛逛商城等）才滑动浏览；「去逛一逛芭芭农场」这类是
        # 跳转到外部小程序/活动的访问任务，跳过去后直接返回，不能在这里滑动
        if self._is_video(title) or '浏览' in title or '逛逛' in title:
            return 'browse'
        # 「点击指定频道好物」类任务已停用（用户要求不做）：归入 trade（跳过），
        # 由 enabled_task_kinds 过滤，框架直接不执行，也不会再点商品卡片。
        if self._is_click_task(title):
            return 'trade'
        # 其余「去完成」按跨应用处理，由 enabled_task_kinds 过滤掉。
        return 'visit'

    @staticmethod
    def _is_click_task(title):
        """点击商品类任务：仅「点击指定频道好物」这类明确要求逐个点商品的标题。

        不能按「逛」字泛匹配——「逛逛商城领超值优惠券」等只是逛逛商城，
        在那里逐个点「抵后价」商品无效，会白耗时间。
        """
        return '点击' in title and any(k in title for k in ('好物', '宝贝', '商品', '频道'))

    @staticmethod
    def _progress(nodes, btn, height):
        """读取同一行的「(0/4)」进度。"""
        row = sorted((n for n in nodes if abs(n.center[1] - btn.center[1]) < height * 0.02),
                     key=lambda n: n.bounds[0])
        match = re.search(r'(\d+)/(\d+)', ''.join(normalize(n.text) for n in row))
        return (int(match.group(1)), int(match.group(2))) if match else (0, 1)

    # ------------------------------------------------------------------
    # 契约：执行单个任务
    # ------------------------------------------------------------------
    def _stable_target(self, title, tries=2):
        """连续两次读取到同一坐标，才认为任务列表已经稳定。

        列表点击后会自行重排：完成的任务跳首行、领奖后整行消失、后面各行整体上移，
        在重排动画中间态点击极易落到相邻行，所以必须等坐标稳定再动手。
        按标题匹配；两次读数不同使用最新位置，进度由核验层处理。
        """
        screen, last = None, None
        for _ in range(tries):
            screen = self.read()
            current = next((t for t in self.parse_tasks(screen.nodes, screen.width, screen.height)
                            if t.title == title), None)
            if current is None:
                return screen, None
            if last is not None and last.bounds == current.bounds:
                return screen, current
            last = current
            self.control.wait(0.2)
        return screen, last

    def execute(self, task, before):
        if task.kind == 'trade':
            raise TaskUnavailable('该任务不自动执行')
        self._completed_evidence = set()
        # 每次点击前都按当前页面重新识别，绝不使用外层缓存的坐标
        screen, fresh = self._stable_target(task.title)
        if fresh is None:
            return screen, False, False
        if fresh.kind == 'trade':
            raise TaskUnavailable('该任务不自动执行')
        task = fresh
        self.update('opening', current_task=task.title, message='开始执行')
        logger.info('点击任务：%s | 按钮=%s | 区域=%s', task.title, fresh.action, fresh.bounds)
        self.click(TextNode(fresh.action, fresh.bounds))
        if task.kind == 'claim':
            # 「领取奖励」按钮点掉后整行约 3 秒才消失，那是页面自己的刷新，
            # 不需要脚本反复读屏核验，也不靠「领取成功/奖励已到账」等文案判定。
            # 点击本身不作为成功证据，交给回执或任务行变化核验。
            self.control.wait(float(self.config.get('claim_settle_seconds', 0.6)))
            screen = self.read()
            self.guard(screen)
            explicit = bool(screen.find(DONE_PROMPT_RE))
            if self._dismiss_popups(screen):
                screen = self.read()
            return screen, explicit, False
        self.control.wait(2)
        screen = self.read()
        if task.kind == 'trade':
            # 下单购买等真实交易类、以及已停用的「点击指定频道好物」类任务：
            # 不自动执行，直接跳过
            logger.info('跳过不执行的任务：%s', task.title)
            return self.recover(), False, False
        if task.kind == 'search':
            # 搜索类：进入搜索页后搜词并浏览
            screen, explicit = self._search_task(screen)
            return self._claim_ready(task, self._return_to_list(), explicit=explicit), explicit, False
        if task.kind == 'visit':
            # 跳转类：点「去完成」跳到其它 App，停留几秒后回闲鱼。
            # 回到任务列表后确认该任务是否已变成「领取奖励」/进度+1：
            # 未安装目标 App 时跳转无效，行仍是「去完成」，判为无法完成并跳过，
            # 不再重试（raise TaskUnavailable 让框架标记该任务不可用）。
            self.control.wait(float(self.config.get('visit_seconds', 5)))
            screen = self._return_to_list(cold_start=False)
            if not self._visit_done(task, screen):
                raise TaskUnavailable('跳转任务未完成（目标App可能未安装），跳过：%s' % task.title)
            return self._claim_ready(task, screen), False, False
        # 浏览类：跳进小游戏就退出跳过；否则立刻开始持续滑动。
        # 商品页靠滑动计时，任何停顿（弹窗处理、二次检测）都会中断计时。
        if self._is_game_container(screen):
            self._exit_game()
            raise TaskUnavailable('该任务实际是小游戏，已退出并跳过')
        if self._is_video(task.title):
            screen, explicit = self._video_browse(task.seconds)
        else:
            screen, explicit = self._browse(task.seconds)
        # 浏览完主动返回任务列表，顺手把变成「领取奖励」的行领掉。
        # 把浏览结束时那一屏传进去：读屏刚崩过时不用再读一次整屏。
        return self._claim_ready(task, self._return_to_list(screen), explicit=explicit), explicit, False

    def _claim_ready(self, task, screen, tries=2, explicit=False):
        """浏览/跳转完成后顺手领奖：默认只看一拍，页面仍在加载才再看一拍。

        服务端结算慢时不再原地死等三拍，把领奖交给下一轮扫描（框架给领取类
        排的优先级最高）；整行消失直接当完成，不再空耗读屏。
        """
        total = max(1, tries)
        if screen is None:
            return screen
        for index in range(total):
            if not self.is_task_list(screen.nodes):
                return screen
            row = next((t for t in self.parse_tasks(screen.nodes, screen.width, screen.height)
                        if t.title == task.title), None)
            if row is not None and row.action in ('领取奖励', '立即领取'):
                self._completed_evidence = getattr(self, '_completed_evidence', set()) | {task.key}
                logger.info('任务已完成，领取奖励：%s', task.title)
                self.click(TextNode(row.action, row.bounds))
                self.control.wait(float(self.config.get('claim_settle_seconds', 0.6)))
                self._dismiss_popups(self._safe_read(screen))
                return self._safe_read(screen)
            if row is None:
                # 行不在当前视口不能证明浏览完成；保留明确完成回执。
                if explicit:
                    self._completed_evidence = getattr(self, '_completed_evidence', set()) | {task.key}
                logger.info('任务行已消失，后续交给下一轮扫描：%s', task.title)
                return screen
            # 只有页面仍在「加载中」才多给一拍，否则直接返回交给下一轮扫描。
            # 浏览页给过完成提示时，领奖延后不代表本轮失败。
            if index + 1 >= total or not self.panel_loading(screen):
                if explicit:
                    self._completed_evidence = getattr(self, '_completed_evidence', set()) | {task.key}
                return screen
            self.control.wait(1)
            screen = self._safe_read(screen)
        return screen

    # ------------------------------------------------------------------
    # 契约：核验
    # ------------------------------------------------------------------
    def verify(self, task, before, after, explicit=False):
        """Count only same-task progress, reward eligibility or explicit receipts."""
        if after.package != self.origin:
            return False
        if explicit or task.key in getattr(self, '_completed_evidence', set()):
            return True
        if not self.is_task_list(after.nodes):
            return False
        tasks = self.parse_tasks(after.nodes, after.width, after.height)
        current = next((t for t in tasks if t.title == task.title), None)
        if current is not None:
            if current.current > task.current:
                return True
            # 按钮从「去完成」变「领取奖励」：任务已达标，只是还没领
            if task.kind != 'claim' and current.kind == 'claim':
                return True
            if task.kind == 'claim' and current.kind != 'claim':
                return True
            return bool(explicit) and current.kind == 'claim'
        # 任务行消失：领取类视为已领；浏览类要求页面给过完成提示
        return task.kind == 'claim'

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def setup(self):
        # 导航（闲鱼 → 我的 → 闲鱼币 → 赚更多）完全交给框架 ensure_list，
        # 进入后不做任何地图滑动或掷骰子，避免误触。
        pass

    def teardown(self):
        # 只有确实没有待执行任务时才收尾；否则把累计奖励和走步数留到下一轮，
        # 避免「任务还没做完就先走步数」（例如达到 max-tasks 提前结束）。
        try:
            screen = self.read()
            self.guard(screen)
            if self.is_task_list(screen.nodes) and self._has_pending(screen):
                logger.info('仍有待执行任务，本轮跳过累计奖励与走步数')
                return
        except Exception as exc:
            logger.info('收尾前检查未通过，停止收尾：%s', exc)
            return
        try:
            self._claim_top_rewards()
        except Exception as exc:
            logger.warning('领取累计任务奖励失败（已忽略）：%s', exc)
        try:
            self._back_to_coin_page()
            self._walk_steps()
        except Exception as exc:
            logger.warning('走步数阶段异常（已忽略）：%s', exc)

    def _has_pending(self, screen):
        """任务列表里是否还有本活动要做的「去完成」任务。"""
        enabled = self.config.get('enabled_task_kinds', [])
        return any(t.kind in enabled and t.action in ('去完成', '去浏览')
                   for t in self.parse_tasks(screen.nodes, screen.width, screen.height))

    # ------------------------------------------------------------------
    # 累计任务奖励：每轮回到任务列表都查一次，不必等收尾
    # ------------------------------------------------------------------
    def _claim_top_rewards(self, screen=None):
        """领取任务列表顶部的累计任务奖励卡片。

        卡片未达成时是「+100完成3次」，达标后文案变成「+100领取奖励」；
        只在已达成时点击，未达成的点了也没用。
        """
        if screen is None:
            screen = self.read()
        if not self.is_task_list(screen.nodes):
            try:
                screen = self.recover()
            except Exception as exc:
                logger.warning('未回到任务列表，跳过累计奖励：%s', exc)
                return screen
            if not self.is_task_list(screen.nodes):
                return screen
        cards = [n for n in screen.nodes
                 if re.fullmatch(CUMULATIVE_READY_RE, normalize(n.text))]
        if not cards:
            return screen
        self.update(message='领取累计任务奖励')
        for card in cards:
            logger.info('领取累计任务奖励：%s', card.text)
            self.click(card)
            self.control.wait(1)
            self._dismiss_popups(self.read())
        return self.read()

    # ------------------------------------------------------------------
    # 收尾 2：回到大富翁主页面
    # ------------------------------------------------------------------
    def _back_to_coin_page(self):
        """回到大富翁主页面：先关任务面板；若已跑出活动页则重新导航进去。"""
        for _ in range(4):
            screen = self.read()
            if self.activity_landing(screen) and not self.is_task_list(screen.nodes):
                return screen
            if not self.is_task_list(screen.nodes):
                break
            self.device.go_back()
            self.control.wait(1)
        try:
            self.ensure_list(auto_open=True)
        except Exception as exc:
            logger.warning('重新进入活动失败，跳过走步数：%s', exc)
            return self.read()
        for _ in range(4):
            screen = self.read()
            if not self.is_task_list(screen.nodes):
                return screen
            self.device.go_back()
            self.control.wait(1)
        return self.read()

    # ------------------------------------------------------------------
    # 收尾 3：点「前进获得闲鱼币」直到没有可用步数
    # ------------------------------------------------------------------
    def _walk_steps(self):
        last_left = None
        same = 0
        for _ in range(40):
            screen = self.read()
            if not self.activity_landing(screen):
                break
            step = self._step_button(screen) or self._find(screen, STEP_RE)
            if step is None:
                break
            match = re.search(STEP_LEFT_RE, normalize(step.text))
            left = int(match.group(1)) if match else None
            if left is not None and left <= 0:
                logger.info('「前进」不再显示可用步数，停止走步')
                break
            # 步数连续两次没变化，说明已经走不动（或被限次），提前收尾
            if left is not None and left == last_left:
                same += 1
                if same >= 2:
                    logger.info('步数连续未变化（%s步），停止走步', left)
                    break
            else:
                same = 0
            last_left = left
            # 读不到「N步」也继续点：能不能继续走，以按钮是否还在为准
            self.update(message='走步数，剩余 %s 步' % (left if left is not None else '?'))
            logger.info('剩余 %s 步，点击前进', left if left is not None else '?')
            self.click(step)
            self.control.wait(1.5)
            self._dismiss_popups(self.read())

    # ------------------------------------------------------------------
    # 浏览动作
    # ------------------------------------------------------------------
    def _at_task_list(self, screen):
        """是否已在闲鱼任务列表（跨 App 时 package 会变，必须一并判断）。"""
        return ((screen.package or '').startswith('com.taobao.idlefish')
                and self.is_task_list(screen.nodes))

    # 仍在视频/广告全屏页的特征（任务列表里虽也有「广告」标记，但不会有
    # 这些专属词，故用它们判定「尚未退出视频页」而不是「停在任务列表」）
    _AD_VIDEO_RE = r'重播|再来一条|立即下载|关闭广告|跳过广告|短视频|视频播放|已观看'

    def _is_ad_or_video_page(self, screen):
        return self._find(screen, self._AD_VIDEO_RE) is not None

    def _dismiss_exit_dialogs(self, screen, tries=None):
        """广告/视频页的退出确认会连弹两次：一直点到不再出现为止。

        「跳过」和「坚持退出/残忍离开/离开」合并成一组，不区分第几层弹窗；
        每次点完等一秒再读屏，回到任务列表或找不到按钮就停。
        """
        if tries is None:
            tries = int(self.config.get('exit_dialog_tries', 4))
        words = [str(w) for w in (list(self.config.get('video_skip_words') or [])
                                  + list(self.config.get('video_exit_words') or [])) if w]
        if not words:
            return screen
        pattern = '|'.join(re.escape(w) for w in words)
        for _ in range(max(1, tries)):
            if self._at_task_list(screen):
                return screen
            node = self._find(screen, pattern)
            if node is None:
                return screen
            logger.info('退出确认弹窗，点击「%s」', node.text)
            self.click(node)
            self.control.wait(float(self.config.get('exit_dialog_interval', 1.0)))
            screen = self.read(force_ocr=bool(self.config.get('ocr', False)))
        return screen

    def _return_to_list(self, screen=None, tries=5, cold_start=False):
        """任务页结束后主动返回任务列表。

        广告/视频页先反复点页面内的退出按钮（二次确认也点掉）；小游戏容器交给
        _exit_game 处理。普通浏览/搜索页只做一次返回动作，等 1～2 秒读一屏，
        仍不在任务列表就直接恢复导航，不再一轮轮按返回键。
        """
        if screen is None:
            screen = self._safe_read()
        if screen is None:
            # 读屏完全不可用时按返回键只是盲退，直接交给恢复导航重开活动
            raise TaskUnavailable('读屏不可用，无法确认是否回到任务列表')
        # 跳转到其它 App 时（package 不是闲鱼），返回键只会退那个 App 直到桌面，
        # 回不到闲鱼；直接恢复（启动闲鱼 + 导航回任务列表），不傻按返回键。
        if not (screen.package or '').startswith('com.taobao.idlefish'):
            if self._at_task_list(screen):
                return screen
            try:
                return self.recover(cold_start=True)
            except Exception as exc:
                logger.warning('跨 App 返回闲鱼失败：%s', exc)
                return screen
        # 视频/广告页：把连弹的退出确认一次点干净，再判断是否已经回到列表
        if self._is_ad_or_video_page(screen):
            screen = self._dismiss_exit_dialogs(screen)
            if self._at_task_list(screen):
                return screen
        back_tries = max(1, int(self.config.get('return_back_tries', 1)))
        settle = float(self.config.get('return_settle_seconds', 1.5))
        for _ in range(back_tries):
            if self._at_task_list(screen):
                return screen
            if self._is_game_container(screen):
                self._exit_game()
                screen = self.read()
                continue
            if self._is_ad_or_video_page(screen):
                screen = self._dismiss_exit_dialogs(screen)
                if self._at_task_list(screen):
                    return screen
            # 已退到金币落地页（不在任务面板）：再退就越退越远，直接重开面板
            if self.activity_landing(screen):
                break
            # 页面内「返回」按钮优先，找不到才按系统返回键；两者只做一次，
            # 避免一次点按钮 + 一次返回键把页面退过头。
            label = str(self.config.get('browse_reward_button', '返回'))
            back = self._find(screen, re.escape(label), exact=True)
            if back is not None:
                logger.info('点击页面内「%s」返回任务列表', label)
                self.click(back)
            else:
                self.device.go_back()
            self.control.wait(settle)
            screen = self.read()
        if not self._at_task_list(screen):
            try:
                return self.recover(cold_start=cold_start)
            except Exception as exc:
                logger.warning('返回任务列表失败：%s', exc)
        return screen

    def _visit_done(self, task, screen):
        """回到任务列表后，该跳转任务是否已完成（行变成「领取奖励」/进度+1）。

        未安装目标 App 时，点「去完成」跳不过去、行仍是「去完成」，据此判定
        任务无法完成，交由调用方跳过；其余情况给进度刷新留几拍再下结论。
        """
        deadline = self.control.now() + int(self.config.get('visit_settle_seconds', 4))
        while self.control.now() < deadline:
            if self.is_task_list(screen.nodes):
                cur = next((t for t in self.parse_tasks(screen.nodes, screen.width, screen.height)
                            if t.title == task.title), None)
                if cur is not None and cur.current > task.current:
                    return True
                if cur is not None and cur.action in ('领取奖励', '立即领取'):
                    return True
                # 仍是「去完成」：等进度刷新，再读一次
                self.control.wait(1)
                screen = self.read()
                continue
            screen = self.read()
            self.control.wait(1)
        return False

    def _swipe_once(self, screen):
        """一次连续上滑；手势本身拉长，间隔交给调用方压到极小。"""
        width, height = screen.width, screen.height
        self.device.swipe(int(width * .5), int(height * .72),
                          int(width * .5), int(height * .46), duration=0.5)

    def _video_browse(self, seconds):
        """视频页不用滑动，但要一直观察到时间到或出现完成提示。"""
        desired = int(seconds or self.config['browse']['video_seconds'])
        total = desired + int(self.config.get('browse_extra_seconds', 5))
        self.update(message='浏览视频 %d 秒' % total)
        deadline = self.control.now() + total
        screen = self.read()
        explicit = False
        while True:
            if screen.find(DONE_PROMPT_RE):
                explicit = True
                logger.info('检测到视频完成提示，提前结束浏览')
                break
            if self.is_task_list(screen.nodes):
                break
            remaining = deadline - self.control.now()
            if remaining <= 0:
                break
            self.control.wait(min(2, max(.2, remaining)))
            screen = self.read(force_ocr=bool(self.config['ocr']))
        # 退出确认可能连弹两次，「跳过」与「坚持退出/残忍离开/离开」合并成一组
        # 反复点，直到不再出现为止；原先 leave 只点一次，第二次弹窗无人处理。
        screen = self._dismiss_exit_dialogs(screen)
        return screen, explicit

    # 真正的商品卡片都标「抵后价」（闲鱼币抵扣后实付价），用它锁定商品；
    # 运营活动卡（现金夺宝 / 大夺宝等）没有「抵后价」，自然被排除，不必再
    # 维护关键词黑名单（黑名单永远补不完）。兼容个别「到手价 / 券后价」写法。
    _GOODS_MARKERS = ('抵后价', '到手价', '券后价', '券后到手')

    def _goods_nodes(self, screen):
        """商品列表里可点的商品卡片：必须带「抵后价」等商品价签，非广告、非按钮。"""
        out = []
        for n in screen.nodes:
            t = normalize(n.text)
            if not any(m in t for m in self._GOODS_MARKERS):
                continue
            if '广告' in t:
                continue
            if re.fullmatch(ACTION_RE, t):
                continue
            cy = n.center[1]
            if not (screen.height * 0.18 < cy < screen.height * 0.85):
                continue
            out.append(n)
        return out

    @staticmethod
    def _is_detail_page(screen):
        """是否已进入单品详情页：详情页有「立即购买/我想要/收藏」等特征，列表页没有。"""
        return any(h in normalize(screen.text)
                   for h in ('立即购买', '我想要', '加入购物车', '收藏', '马上抢'))

    def _click_goods(self, screen, count):
        """[已停用] 点击指定频道好物类任务（用户要求不做，保留代码备查）。

        已从 execute 与 _classify 中摘除：该任务现归 trade 类型，由框架直接跳过。

        人工流程：点「去完成」进入商品流 → 点一个商品 → 直接返回（计数+1）
        → 找下一个（必要时下滑）→ 点 → 返回 … 循环直到点满 count 个。
        注意：列表里混杂广告，点了不计数，需排除带「广告」标记的条目；
        同一商品不重复点（按文本去重）。
        """
        clicked = set()           # 已点商品的去重标识（文本）
        done = 0
        empty = 0                 # 连续没找到可点商品的屏数（防空转）
        hard = int(self.config.get('max_task_seconds', 180))
        deadline = self.control.now() + hard
        while done < count and self.control.now() < deadline:
            screen = self.read()
            # 已回到任务列表（任务完成 / 意外退出）则停止
            if self.is_task_list(screen.nodes):
                break
            # 退过头落到闲鱼首页/账户页（带原生底部导航）：首页商品不带
            # 「抵后价」，在这里点不到有效计数，立即停止交给外层重进
            if self._has_bottom_nav(screen):
                logger.warning('已退出活动商品流（检测到主页底部导航），停止点击')
                break
            # 仍卡在商品详情页（返回没生效）：补一次返回再读，别在详情页
            # 的「猜你喜欢」里找「抵后价」
            if self._is_detail_page(screen):
                logger.info('仍在商品详情页，补一次返回')
                self.device.go_back()
                self.control.wait(1.5)
                screen = self.read()
                if self.is_task_list(screen.nodes) or self._has_bottom_nav(screen):
                    break
            goods = self._goods_nodes(screen)
            if len(goods) < 2:
                # 页面可能正在刷新（返回后 H5 重渲染有短暂空白），先等一拍再读
                self.control.wait(1.5)
                screen = self.read()
                goods = self._goods_nodes(screen)
            if len(goods) < 2:
                # 连续两屏都没有「抵后价」商品：多半已不在有效计数页，放弃
                empty += 1
                if empty >= 2:
                    logger.warning('连续 %d 屏未找到「抵后价」商品，停止点击', empty)
                    break
                self._swipe_once(screen)
                self.control.wait(1.5)
                continue
            empty = 0
            # 选一个尚未点过的（按文本去重，避免重复点同一商品；自上而下）
            target = None
            for g in sorted(goods, key=lambda n: n.center[1]):
                if normalize(g.text) and normalize(g.text) not in clicked:
                    target = g
                    break
            if target is None:
                # 本屏商品都已点过：下滑刷新
                self._swipe_once(screen)
                self.control.wait(1.2)
                continue
            clicked.add(normalize(target.text))
            self.update(message='点击商品 %d/%d' % (done + 1, count))
            logger.info('点击商品 %d/%d：%s', done + 1, count, target.bounds)
            self.click(target)
            self.control.wait(1.2)
            # 验证是否真的进入详情：列表页会有多个商品（价格节点≥2），
            # 进详情后只剩单品，数量骤减；仍在列表说明点错（非商品卡片）。
            s2 = self.read()
            if not self._is_detail_page(s2):
                # 没进详情（点到夺宝 / 筛选等），不计数，换下一个目标
                logger.warning('点击后未进入商品详情，可能点到非商品，跳过该位置：%s',
                               target.bounds)
                self._swipe_once(s2)   # 小幅下移，避免重复选到同一位置
                self.control.wait(1.0)
                continue
            # 已进入详情：点一个商品只返回一次即可回到商品流（活动列表），
            # 不循环连按，否则会一路退到闲鱼首页。
            self.device.go_back()
            self.control.wait(1.5)
            s3 = self.read()
            if self._is_detail_page(s3):
                # 详情页 H5 有中转层，返回一次还没退出来，再退一次
                self.device.go_back()
                self.control.wait(1.5)
                s3 = self.read()
            # 必须同时满足「不在详情页」且「有多个商品」才算回到商品流，
            # 详情页底部的「猜你喜欢」也带「抵后价」，只看数量会误判
            if not self._is_detail_page(s3) and len(self._goods_nodes(s3)) >= 2:
                done += 1
                continue
            # 返回后已不在商品流（退到了落地页 / 闲鱼主页）：在这里滑动找
            # 「抵后价」是纯空转，立即停止，交给外层重新进入任务
            logger.warning('返回后已不在商品流，停止点击（已点 %d/%d）', done + 1, count)
            break
        return self.read()

    def _search_task(self, screen):
        """搜索类任务（如「搜一搜推荐商品」）。

        点「去完成」进入搜索页后，光点推荐词是过不去的：闲鱼按「真的搜了商品
        并浏览」计数，实测连续点「淘宝精选」十几次，进度始终 0/1。这里改成和
        人工一致的做法：点开搜索框 → 输入关键词 → 提交搜索 → 滑动浏览结果。

        关键词取活动配置 task_search_keyword，留空时从公共词表
        task_search_keywords 随机抽一个。输入不可用时退回点推荐词，
        两者都确认不了就跳过，不在未搜索的页面上滑动。
        """
        s = self.read()
        submitted = False
        box = self._find(s, r'搜索|搜一搜|找闲置|搜你想不到')
        if box is not None:
            self.click(box)
            self.control.wait(0.8)
            s = self.read()
            submitted = self._input_search(s) or self._tap_suggestion(s)
        if not submitted:
            raise TaskUnavailable('未确认搜索入口，跳过当前搜索任务')
        # 浏览搜索结果：连续滑动若干次
        swipes = int(self.config.get('search_browse_swipes', 6))
        for _ in range(swipes):
            self._swipe_once(s)
            self.control.wait(0.4)
            s = self._safe_read(s)
        return s, bool(s.find(DONE_PROMPT_RE))

    def _search_keyword(self):
        """搜索任务用的关键词：活动配置优先，其次公共词表随机抽取。"""
        configured = normalize(str(self.config.get('task_search_keyword') or ''))
        if configured:
            return configured
        pool = [normalize(str(w)) for w in (self.config.get('task_search_keywords') or [])]
        pool = [w for w in pool if w]
        return random.choice(pool) if pool else ''

    def _input_search(self, screen):
        """在搜索页输入关键词并提交搜索；输入不可用返回 False。"""
        keyword = self._search_keyword()
        if not keyword:
            return False
        try:
            self.device.input_text(keyword)
        except Exception as exc:
            logger.warning('搜索任务：输入关键词失败（%s），改用推荐词', exc)
            return False
        self.update(message='搜索：' + keyword)
        self.control.wait(1.2)
        later = self._safe_read(screen)
        submit = None
        if later is not None:
            submit = next((n for n in later.nodes
                           if normalize(n.text) == '搜索'
                           and n.center[1] < later.height * 0.2
                           and n.center[0] > later.width * 0.6), None)
        if submit is not None:
            self.click(submit)
        else:
            self.device.press('enter')
        self.control.wait(1.5)
        logger.info('搜索任务：输入关键词「%s」并提交搜索', keyword)
        return True

    def _tap_suggestion(self, screen):
        """输入不可用时，点搜索发现词云里的第一个推荐词。"""
        if not self._find(screen, r'搜索发现|热门搜索|猜你想搜|历史搜索'):
            return False
        # 排除词云标题本身（搜索发现 / 猜你想搜 / 历史搜索）和按钮文本。
        recs = [n for n in screen.nodes
                if 2 <= len(normalize(n.text)) <= 10
                and normalize(n.text) not in ('搜索发现', '热门搜索', '猜你想搜',
                                              '历史搜索', '清空')
                and screen.height * 0.25 < n.center[1] < screen.height * 0.72
                and not re.fullmatch(ACTION_RE, normalize(n.text))]
        if not recs:
            return False
        self.click(recs[0])
        self.control.wait(1.2)
        logger.info('搜索任务：点推荐词「%s」', normalize(recs[0].text))
        return True

    def _browse(self, seconds):
        """浏览页：必须不间断滑动满 N 秒才计时。

        闲鱼浏览任务按「页面持续滑动」计时，任何停顿（读屏、二次检测）都会让
        计时不连续、从而被判定为未完成。因此滑动期间**完全不读屏**，只靠时长
        覆盖；滑够时长后再读一次确认是否出现完成提示，必要时补滑一段。

        每次手势之间留 browse_swipe_interval（默认 0.2 秒）：手势过密会让商品
        瀑布流在十几秒内加载几十屏，控件树膨胀后读屏直接抛 StackOverflowError。
        收尾读屏失败时沿用最后一次成功的屏幕，按「没有完成提示」继续返回列表。
        """
        screen = self.read()
        desired = self._browse_seconds(screen, seconds)
        total = desired + int(self.config.get('browse_extra_seconds', 5))
        logger.info('浏览任务：页面要求 %d 秒，连续滑动 %d 秒', desired, total)
        interval = max(0.02, float(self.config.get('browse_swipe_interval', 0.2)))
        deadline = self.control.now() + total
        while self.control.now() < deadline:
            self._swipe_once(screen)
            # 手势之间只留短衔接间隔；不读屏，保证计时连续
            self.control.wait(interval)
        # 滑动结束后再读屏确认完成状态
        screen = self._safe_read(screen)
        explicit = bool(screen.find(DONE_PROMPT_RE))
        if not explicit:
            countdown = self._countdown(screen)
            if countdown is not None:
                # 页面仍有倒计时（极少触发）：补滑到倒计时结束
                logger.info('页面仍有倒计时 %d 秒，补滑', countdown)
                end = self.control.now() + countdown + 1
                while self.control.now() < end:
                    self._swipe_once(screen)
                    self.control.wait(interval)
                screen = self._safe_read(screen)
                explicit = bool(screen.find(DONE_PROMPT_RE))
        return screen, explicit

    @staticmethod
    def _browse_seconds(screen, fallback):
        """从页面文案里读「浏览20秒 / 滑动浏览20s」，读不到就用配置值。"""
        match = re.search(BROWSE_SECONDS_RE, normalize(screen.text))
        return int(match.group(1)) if match else int(fallback or 20)

    @staticmethod
    def _countdown(screen):
        """页面上的剩余秒数提示，没有就是 None。"""
        match = re.search(COUNTDOWN_RE, normalize(screen.text))
        return int(match.group(1)) if match else None

    # ------------------------------------------------------------------
    # 领奖后的通用弹窗
    # ------------------------------------------------------------------
    def _click_text(self, screen, pattern, exact=False):
        node = self._find(screen, pattern, exact=exact)
        if node is not None:
            self.click(node)
            return True
        return False

    def _dismiss_popups(self, screen):
        if screen is None:
            return False
        if self._click_text(screen, '立即领取|开心收下|全部收下|收下礼物'):
            self.control.wait(1.5)
            self._click_text(self.read(), '关闭')
            return True
        for btn in self.config['popup_buttons']:
            if btn in ('关闭', '返回'):
                continue
            if self._click_text(screen, re.escape(btn)):
                self.control.wait(1.2)
                return True
        return False
