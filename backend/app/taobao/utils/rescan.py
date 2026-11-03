"""补扫：一轮跑完不代表任务做完，要回列表确认才算。

淘金币这类活动是「做完一个才刷出下一个」，而框架单轮的边界是「翻到一批空的
就结束」——那一批当时确实是空的，于是面板报「本轮结束」，列表里却还挂着
pending。所以每轮之后必须回到任务列表把整张翻一遍。

收工只有一条路：连续 `rescan_confirm` 次完整扫描都确认没有可执行行；扫描没看全
只算「不知道」，要重试，不能当成「做完了」。

补扫前默认先把客户端退出再重新进入活动：这两个活动的任务面板都在 WebView 里，
页面不刷新时补扫读到的是同一份缓存，明明还有任务却连续两次都判定「没有」，于是
提前收工。重启一次能强制重新拉取列表，代价是几秒的冷启动时间。

抽成 mixin 是因为淘金币日常（DailyActivity）和双11（直接继承 Activity）是两套
独立实现，但「什么时候才算真的做完」这个判定完全一致，只有「怎么回到列表」和
「列表长什么样」不同，那两点由子类或配置决定。
"""
import contextlib
import logging
import re
from backend.core.models import normalize

logger = logging.getLogger('backend.execution')


class RescanPolicy:
    # rescan 的三态结果。只有 SCAN_EMPTY 才是“真的做完了”，
    # SCAN_UNKNOWN 表示这次没看全（回列表失败、被浮层挡住、翻了上限还没到底），
    # 不能当成“没有任务”，否则会把还没做的任务直接丢掉。
    SCAN_FOUND = 'found'
    SCAN_EMPTY = 'empty'
    SCAN_UNKNOWN = 'unknown'

    @property
    def page(self):
        """页面级配置块；没有这块的活动（如双11）退化成空字典。"""
        return self.config.get('daily_page') or {}

    def rescan_option(self, name, default=None):
        """补扫参数：优先读 ``rescan`` 块，其次读活动自己的页面配置块。

        日常系列的历史配置放在 ``daily_page`` 里（不能动，改了会影响已跑通的
        行为），新活动用独立的 ``rescan`` 块，语义清楚、互不干扰。
        """
        block = self.config.get('rescan')
        if isinstance(block, dict) and block.get(name) is not None:
            return block[name]
        value = (self.page or {}).get(name)
        return default if value is None else value

    def list_name(self):
        return str(self.rescan_option('list_name', '更多金币'))

    def progress_mark(self):
        """(已定案任务数, 已完成数)：判断这一轮到底有没有真的做事。

        不能把尝试次数算进来：同一批任务反复失败时每轮都会 +1，看着像“在推进”，
        实际只是把同一个错误重试到轮数上限。只有出现新的定案记录（或同一任务从
        失败变成完成）才算进展，这样“怎么都推不动”的情况会很快停下来。
        """
        settled = done = 0
        for record in (getattr(self, 'records', None) or {}).values():
            if not isinstance(record, dict):
                continue
            status = record.get('status')
            if status in ('done', 'failed', 'disabled'):
                settled += 1
                if status == 'done':
                    done += 1
        return settled, done

    def runnable_rows(self, screen):
        """这一屏里还能动手的行（判据与框架挑任务时完全一致）。"""
        rows = []
        kinds = self.config.get('enabled_task_kinds') or ()
        for task in self.tasks(screen):
            if task.kind in ('done', 'manual') or task.kind not in kinds:
                continue
            if (self.records.get(task.key) or {}).get('status') == 'disabled':
                continue
            if task.kind == 'claim' and not self.config.get('claim_rewards', True):
                continue
            if not self.config.get('allow_cross_app', True) and getattr(task, 'target_app', ''):
                continue
            if self.task_policy.failures(task) >= self.config.get('max_attempts', 2):
                continue
            if self.task_policy.reason(task):
                continue
            rows.append(task)
        return rows

    def list_fingerprint(self, screen):
        """整张列表的内容指纹：每一行的标题、类型、进度。

        只问“现在有没有可执行任务”不够。一条任务做完之后，淘宝往往要过一会儿
        才把它重置成新一轮（比如「好物沉浸看 6/6」几分钟后又变成「0/6 去完成」）。
        收工那一刻列表上确实没有可执行行，但如果两次确认之间内容在变，说明列表
        还在刷新，这时候收工就会漏掉紧接着冒出来的任务。指纹就是用来区分
        “真的做完了、列表已经不动”和“还在刷新、马上有新任务”。
        """
        try:
            rows = []
            for task in self.tasks(screen):
                rows.append((
                    normalize(getattr(task, 'title', '') or ''),
                    str(getattr(task, 'kind', '') or ''),
                    int(getattr(task, 'current', 0) or 0),
                    int(getattr(task, 'total', 0) or 0),
                ))
            return tuple(sorted(rows))
        except Exception:
            logger.warning('补扫：计算列表指纹失败', exc_info=True)
            return ()

    # 任务行的动作词。列表很长时标题（“做任务赚能量”“更多金币等你赚”）会随
    # 滚动移出屏幕，只按标题判断会误判成“已经离开列表”，所以补一条兜底。
    ROW_ACTION_RE = re.compile(
        r'去完成|去浏览|去逛逛|去看看|已完成|已领取|领取奖励|立即领取|明日再来')

    def still_on_list(self, screen):
        """当前这屏是不是还在任务列表上。

        先按活动自己的 ``is_task_list`` 判；标题滚出屏幕时它可能为假，这时只要
        还能解析出任务行、并且屏上还有任务动作词，就认为仍在列表里。
        """
        try:
            if self.is_task_list(screen.nodes):
                return True
            if not self.tasks(screen):
                return False
        except Exception:
            return False
        return any(self.ROW_ACTION_RE.search(normalize(n.text)) for n in screen.nodes)

    def list_collapsed(self, screen):
        """任务段还折叠着（有「展开」但下面的队列没画出来）。

        折叠态对这张面板来说“看起来完全正常”：分区标题（今日速赚 / 更多专享
        福利 / 更多金币等你赚）都还在，``is_task_list`` 成立，于是翻一屏内容就
        不再变化——补扫会把它当成“翻到底了、没有任务”，其实是任务全被折起来了。
        没有折叠段的活动（如双11）恒为 False。
        """
        probe = getattr(self, 'collapsed_panel', None)
        if not callable(probe):
            return False
        try:
            return bool(probe(screen))
        except Exception:
            logger.warning('补扫：判断列表是否折叠失败', exc_info=True)
            return False

    def ensure_panel_open(self, screen):
        """把折叠的任务段展开，返回 (屏幕, 是否确认已展开)。

        展不开不能硬算“没有任务”：那等于把整段未读的队列当成空的。
        """
        if not self.list_collapsed(screen):
            return screen, True
        logger.info('补扫：“%s”还是折叠态，先展开再看', self.list_name())
        expand = getattr(self, 'ensure_expanded', None)
        if not callable(expand):
            return screen, False
        try:
            screen = expand(screen)
        except Exception:
            logger.warning('补扫：展开任务段失败', exc_info=True)
            return screen, False
        if self.list_collapsed(screen):
            logger.warning('补扫：“%s”点击后仍是折叠态，列表只画了一部分',
                           self.list_name())
            return screen, False
        return screen, True

    def reset_list_state(self):
        """把上一轮解析出来的任务清单整份丢掉，让重新进入的页面从头识别。

        补扫前客户端已经强停重开，任务面板会重新请求一次列表；但框架里还留着上一轮
        的解析结果：``_known_tasks``（按图标/位置继承的旧任务对象）、``records``
        （status/reason/current）、``_verified_progress``（“这次进度已核验，等页面
        刷新”），再加上 task_policy 里「今日已试一次」这类临时锁。扫描读的是这些
        缓存而不是当前屏幕，于是刚刷出来的任务被旧结论继续挡着，连续两次判「没有
        可执行任务」就收工——用户看到的就是「明明还能做，程序却说做完了」。

        临时锁每个任务在一次运行里只放行一次（``_rescan_retried``），所以「一直
        失败」的任务不会被清零成无限重试。
        """
        dropped = 0
        records = getattr(self, 'records', None)
        if isinstance(records, dict) and records:
            # 留一份快照：万一清空之后这一轮没跑出任何新记录（确认没有任务就收工），
            # 界面上还得能把这一轮的任务与结论显示出来，不能变成一张空表。
            self._records_snapshot = dict(records)
            dropped = len(records)
            records.clear()
        for name in ('_known_tasks', '_asset_keys', '_verified_progress'):
            state = getattr(self, name, None)
            if isinstance(state, (dict, set)):
                state.clear()
        # 重新识别时重读一次已安装应用列表：用户可能刚装上 UC 这类目标应用。
        if getattr(self, '_installed_packages', None) is not None:
            self._installed_packages = None
            self._installed_checked = False
        # 任务级的「不可用」是上一轮这一次尝试的结论，换成新列表后要重新判；
        # 应用级（未安装某某）保留，安装状态由上面那次重读负责刷新。
        for key in [k for k in (getattr(self, 'unavailable_tasks', None) or {})
                    if str(k).startswith('task:')]:
            self.unavailable_tasks.pop(key, None)
        policy = getattr(self, 'task_policy', None)
        # 每一轮补扫都是“客户端刚重启、列表刚重拉”的全新现场。上一轮放行过不代表
        # 这一轮不用再放行：任务只要失败一次就会重新上锁，名单不重置的话，第二轮
        # 之后这条任务就被永久跳过（实测好物沉浸看 3/6 正是卡在这里）。真正防止
        # 无限重试的是失败额度（max_attempts）和“连续三轮推不动”的兜底。
        self._rescan_retried = set()
        unlocked = self.unlock_once_only(policy, self._rescan_retried)
        if dropped or unlocked:
            logger.info('补扫：清空上一轮的任务清单（%d 条记录、%d 条临时锁），按当前页面重新识别',
                        dropped, unlocked)
        return dropped, unlocked

    ONCE_ONLY_KEYS = ('browse_once_attempted',)

    def unlock_once_only(self, policy, granted=None):
        """解开「无明确浏览时长，今日只试一次」的锁，每个任务每次运行只放行一次。

        新框架用 ``TaskPolicy.retry_transient``；分发包里跑的是 v0.1.5 的旧框架，
        没有这个方法，只能直接改它的 ``entries``（字段名从 0.1.3 起没变过，带兜底）。
        失败次数（failures/exhausted）不在放行范围：那是当日额度，清了等于把
        max_attempts 变成无限重试。用户手动跳过的 skip 永远不动。
        """
        granted = set() if granted is None else granted
        retry = getattr(policy, 'retry_transient', None)
        if callable(retry):
            return retry(granted)
        entries = getattr(policy, 'entries', None)
        if not isinstance(entries, dict):
            return 0
        lock = getattr(policy, 'lock', None)
        with (lock if lock is not None else contextlib.nullcontext()):
            cleared = 0
            for identity in list(entries):
                item = entries[identity]
                if not isinstance(item, dict) or item.get('skip') or identity in granted:
                    continue
                if not item.get('browse_once_attempted'):
                    continue
                item.pop('browse_once_attempted', None)
                granted.add(identity)
                cleared += 1
                if not item:
                    entries.pop(identity, None)
        if cleared:
            save = getattr(policy, '_save', None)
            if callable(save):
                save()
        return cleared

    def restore_list_state(self):
        """清空重认之后如果没跑出新的任务清单，把快照放回去。

        收工那一刻（确认整张列表没有可执行任务）界面上要还能看到这一轮的任务与
        结论；否则面板会是一张空表，看起来像“什么都没做”。
        """
        backup = getattr(self, '_records_snapshot', None)
        records = getattr(self, 'records', None)
        if not backup or not isinstance(records, dict) or records:
            return
        records.update(backup)
        self.update(tasks=list(records.values()))

    def app_package(self):
        """当前活动所属客户端的包名（导航时已经确定过就沿用）。"""
        package = getattr(self, 'origin', None)
        if package:
            return package
        driver = getattr(getattr(self, 'device', None), 'device_type', '') or ''
        try:
            return self.application.package_for(driver)
        except Exception:
            # 测试替身或非常规驱动下没有 device_type，按安卓包名兜底。
            return getattr(self.application, 'android_package', None)

    def restart_client(self):
        """补扫前把客户端彻底退出再冷启动，返回是否真的重启了。

        为什么必须这么做：任务面板是 WebView，页面本身不会因为“又点进来一次”而
        重新拉数据，补扫看到的还是上一次的缓存——两次扫描都读同一份旧列表，于是
        判定“没有可执行任务”提前收工，其实任务还在。force-stop 再启动会重建进程，
        列表必然重新请求。
        """
        if not self.rescan_option('rescan_restart_app', True):
            return False
        budget = max(0, int(self.rescan_option('rescan_restart_max', 12)))
        used = int(getattr(self, '_rescan_restarts', 0) or 0)
        if used >= budget:
            if not getattr(self, '_rescan_restart_capped', False):
                self._rescan_restart_capped = True
                logger.warning('补扫：本次已重启客户端 %d 次，达到上限，之后直接在当前页面扫描',
                               used)
            return False
        package = self.app_package()
        if not package:
            logger.warning('补扫：拿不到客户端包名，跳过重启，直接刷新当前页面')
            return False
        self.update('returning', message='先退出客户端，重新进入活动后再补扫',
                    current_task='', remaining=0)
        logger.info('补扫：退出客户端 %s 并重新进入，强制刷新任务列表', package)
        try:
            # 先回桌面再 force-stop：留在活动页上强停，部分机型下一次启动会直接
            # 恢复那个旧页面，等于没重启。
            self.device.go_home()
            self.pause(.6)
            self.device.stop_app(package)
            self.pause(1.2)
            self.device.start_app(package)
            self.pause(max(0.0, float(self.rescan_option('rescan_start_pause', 3.0))))
        except Exception:
            logger.warning('补扫：重启客户端失败，改为直接刷新当前页面', exc_info=True)
            return False
        self._rescan_restarts = used + 1
        # 导航内部还留着一次“从桌面重启”的兜底额度，重新打开之后要还给它。
        self._task_restart_used = False
        return True

    def reopen_list(self):
        """回到任务列表并保证整张列表都画出来，返回当前屏幕。

        默认走框架/活动自己的入口（``ensure_list``），有「展开」按钮的页面再展开
        一次。展开是否成功由 ``ensure_expanded`` 自己校验并重试，这里不盲点。
        """
        screen = self.ensure_list(auto_open=True)
        expand = getattr(self, 'ensure_expanded', None)
        if callable(expand):
            return expand(screen)
        return screen

    def scroll_list(self, screen):
        """把列表往下翻一屏。

        步长可配：淘金币那种可下拉关闭的弹层一次滑太长会被当成“下拉关闭”，
        双11 的面板更矮，所以拆成几段小步；日常系列沿用原来的单次 0.3 屏。
        """
        x = screen.width // 2
        top = round(screen.height * float(self.rescan_option('scroll_top', .72)))
        step = round(screen.height * float(self.rescan_option('scroll_step', .30)))
        times = max(1, int(self.rescan_option('scroll_times', 1)))
        wait = max(0.0, float(self.rescan_option('scroll_pause', .7)))
        for _ in range(times):
            self.device.swipe(x, top, x, top - step, duration=.45)
            self.pause(wait)

    def rescan(self, pages=None, restart=True):
        """先把客户端重开一遍，再回任务列表从头翻到尾，看还有没有能执行的任务。

        只做判断，不执行任何任务。

        返回值：
        - ``SCAN_FOUND``：找到可执行行，应该再来一轮；
        - ``SCAN_EMPTY``：整张列表完整翻到底，确认没有可执行行；
        - ``SCAN_UNKNOWN``：这次没看全，调用方应当重试，不能据此收工。
        """
        limit = max(2, int(pages or self.config.get('max_scroll_pages') or 10))
        name = self.list_name()
        if restart:
            # 重启失败不阻断：可能只是这台设备不允许 force-stop，用当前页面照样扫，
            # 结论仍然按 SCAN_UNKNOWN 严格判定，不会因此误判成“做完了”。
            self.restart_client()
        # 新进程拉起之后、真正开始读列表之前，先把上一轮的任务清单与临时锁丢掉：
        # 不重新识别，扫描看到的就是旧结论，能把还能做的任务判成“已做过”。
        self.reset_list_state()
        try:
            screen = self.reopen_list()
        except Exception:
            logger.warning('补扫：回到任务列表失败', exc_info=True)
            return self.SCAN_UNKNOWN
        if not self.still_on_list(screen):
            logger.warning('补扫：没能停在任务列表上，本次扫描不算数')
            return self.SCAN_UNKNOWN
        # 折叠态下这一屏同样“像”任务列表，但队列根本没画出来；不先展开就往下翻，
        # 翻不动的那一屏会被判成“到底了、没有任务”，然后提前收工。
        screen, opened = self.ensure_panel_open(screen)
        if not opened:
            logger.warning('补扫：“%s”处于折叠态且展不开，本次扫描不算数', name)
            return self.SCAN_UNKNOWN
        seen = set()
        for index in range(limit):
            try:
                rows = self.runnable_rows(screen)
            except Exception:
                logger.warning('补扫：解析当前屏失败', exc_info=True)
                return self.SCAN_UNKNOWN
            if rows:
                logger.info('补扫：列表里还有 %d 条可执行任务，例如 %s', len(rows),
                            '、'.join(task.title for task in rows[:3]))
                return self.SCAN_FOUND
            signature = tuple(sorted(task.key for task in self.tasks(screen)))
            if signature in seen:
                logger.info('补扫：列表内容不再变化，已翻到第 %d 屏底部', index + 1)
                # 把这一屏的内容指纹留给收工判定：两次确认之间指纹变了，就说明
                # 列表还在刷新（任务被重置成新一轮），不能据此收工。
                self._empty_fingerprint = self.list_fingerprint(screen)
                return self.SCAN_EMPTY
            seen.add(signature)
            self.update('scanning', message=f'回到“{name}”检查是否还有任务',
                        current_task='', remaining=0)
            self.control.check()
            before = normalize(screen.text)
            try:
                self.scroll_list(screen)
                fresh = self.read(force_ocr=bool(self.config['ocr']))
            except Exception:
                logger.warning('补扫：滚动失败', exc_info=True)
                return self.SCAN_UNKNOWN
            if not self.still_on_list(fresh):
                logger.warning('补扫：翻页后已不在任务列表（可能是浮层或跳转），本次扫描不算数')
                return self.SCAN_UNKNOWN
            # 翻页有时会把面板带回折叠态（下拉关闭后重进、页面重绘）。这时
            # “滑动没有产生新内容”并不能说明到底了，必须先展开再比。
            fresh, opened = self.ensure_panel_open(fresh)
            if not opened:
                logger.warning('补扫：翻页后任务段重新折叠且展不开，本次扫描不算数')
                return self.SCAN_UNKNOWN
            if normalize(fresh.text) == before:
                logger.info('补扫：滑动没有产生新内容，已到列表底部')
                return self.SCAN_EMPTY
            screen = fresh
        logger.warning('补扫：翻了 %d 屏仍未到底，无法确认是否还有任务', limit)
        return self.SCAN_UNKNOWN

    def confirm_scan(self, retries=3, restart=True):
        """反复扫描，直到拿到确定结论（还有任务 / 确认没有）。

        扫描本身会失败：回列表可能被浮层挡住、翻页可能被跳转打断、列表可能长过
        翻页上限。这些都返回 SCAN_UNKNOWN，只能重试，不能当成“做完了”。重试时
        逐次多翻几屏，给长列表留余量。

        ``restart`` 只对第一次尝试生效：重试是为了“这次没看清”，页面刚刚才重开
        过，再退一次客户端没有意义——而且会把“确认没有任务”变成永远退不完的
        循环。所以重试一律在已经打开的列表上做。
        """
        pages = int(self.config.get('max_scroll_pages') or 10)
        verdict = self.SCAN_UNKNOWN
        for attempt in range(max(1, int(retries))):
            verdict = self.rescan(pages, restart=restart and attempt == 0)
            if verdict != self.SCAN_UNKNOWN:
                return verdict
            wait = min(30.0, 8.0 * (attempt + 1))
            logger.warning('补扫：第 %d/%d 次扫描未能确认，%.0f 秒后重试',
                           attempt + 1, retries, wait)
            self.pause(wait)
            pages = min(24, pages + 5)
        return verdict

    def run(self, auto_open=False):
        """一直补扫到整张列表真的没有可执行任务，才收工。

        框架一轮的边界是：翻到某一批没有可执行行就判定本轮结束（任务做完一个才
        刷出下一个，这一批当时就是空的），于是面板报“本轮结束”，列表里却还挂着
        pending。所以每轮之后都要回列表把整张翻一遍——完工标准就是“再进去也不会
        自动执行任何任务”。

        每一轮的循环只有两步：
        1. 跑一轮任务（框架自己那套）；
        2. 退出客户端重进一次，把列表从头翻到尾看一眼——有可执行的就回到第 1
           步，一条都没有就直接收工。

        收工判定就是“重进后这一眼是空的”，不搞连续多次确认，也不为了再确认去
        反复退出重进（那会变成永远退不完的循环）。只有“没看全”（SCAN_UNKNOWN）
        才在原地重试几次，仍然看不清才带着明确日志停下。另外保留一个轮数保险，
        防止某个任务永远“可执行却推不动”导致空转。
        """
        if not self.rescan_option('rescan_enabled', True):
            # 总开关：出问题时可退回“一轮就收工”的旧行为，不用改代码。
            return super().run(auto_open)
        # 补扫的手段只有一个——翻页。调用方把翻页页数配成 0（或更小）就是明确
        # 要求“不要翻”，这时候再重进扫一遍既看不到新内容，还会把框架原本那句
        # “没有识别到任务”换成“补扫没能确认”。既然翻页被禁用，就按单轮收工。
        # 注意：schema 的取值范围是 1–20，真机不会走到这里，只有显式配 0 才会。
        if int(self.config.get('max_scroll_pages') or 0) < 1:
            logger.info('补扫：配置未允许翻页（max_scroll_pages=%s），按单轮收工',
                        self.config.get('max_scroll_pages'))
            return super().run(auto_open)
        # 重启预算是按“一次运行”算的：停一下再跑不该被上一次的用量卡住。
        self._rescan_restarts = 0
        self._rescan_restart_capped = False
        # 每个任务在一次运行里只放行一次“重新来过”，避免无限重试。
        self._rescan_retried = set()
        self._records_snapshot = None
        limit = max(2, int(self.rescan_option('rescan_rounds', None)
                           or self.rescan_option('round_limit', None) or 20))
        retries = max(1, int(self.rescan_option('rescan_retries', 3)))
        try:
            # 默认一批没东西就收工（empty_batch_stop=1），会把后面几批连同刚刷新
            # 出来的任务一起跳过；补扫期间多容忍几个空批，把整张列表翻完。
            self.config['empty_batch_stop'] = max(
                int(self.config.get('empty_batch_stop') or 1), 3)
        except Exception:
            logger.warning('无法调整 empty_batch_stop，按框架默认值继续', exc_info=True)
        stats, stalled, index = None, 0, 0
        # 每一轮结束后的固定动作只有一个：**重进客户端一次，进去看一眼**。
        # 有可执行任务就接着跑下一轮；一条都没有就直接收工。
        # 绝不为了“再确认一次”反复退出重进——那会变成一个永远退不完的循环，
        # 用户看到的就是“明明已经判定没有任务了，还在不停退客户端重进”。
        closing = '本轮任务已处理完毕'
        while index < limit:
            if index > 0:
                logger.info('补扫：第 %d 轮结束，重进客户端确认是否还有任务', index)
                verdict = self.confirm_scan(retries, restart=True)
                if verdict == self.SCAN_FOUND:
                    logger.info('补扫：重进后仍找到可执行任务，继续下一轮')
                elif verdict == self.SCAN_EMPTY:
                    logger.info('补扫完毕：重进后完整扫描没有可执行任务，收工')
                    closing = '本轮任务已处理完毕：重新进入列表确认没有可执行任务'
                    break
                else:
                    logger.warning('补扫：多次扫描都没能确认列表状态，停止；列表里可能还有任务')
                    closing = '补扫没能确认列表状态已停止；列表里可能还有任务，可稍后重新开始'
                    break
            before = self.progress_mark()
            # 补扫轮从“刚才发现任务的那一屏”接着做：ensure_list 默认会把列表拉回
            # 顶部，而可执行的行往往在下面几屏，回到顶部等于让它重新扫一遍并再次
            # 在中间那一批空批上收工。
            page, saved = self.page, None
            if index > 0 and 'reset_list_scroll' in page:
                saved = page.get('reset_list_scroll')
                page['reset_list_scroll'] = False
            try:
                stats = super().run(auto_open or index > 0)
            finally:
                if saved is not None:
                    page['reset_list_scroll'] = saved
            index += 1
            if self.progress_mark() > before:
                stalled = 0
            else:
                stalled += 1
                logger.info('补扫：第 %d 轮没有产生新的尝试', index)
                if stalled >= 3:
                    # 扫描说还有任务，但连续几轮一次都推不动：大概率是需要人工
                    # 的任务一直挂在列表里，继续空转没有意义。
                    logger.warning('补扫：连续 %d 轮有任务却无法推进，停止；请检查是否有需要人工处理的任务',
                                   stalled)
                    closing = '连续几轮有任务却推不动，已停止；请检查是否有需要人工处理的任务'
                    break
            if index >= limit:
                logger.warning('补扫达到 %d 轮保险上限，停止；列表里可能还有任务', limit)
                closing = '补扫达到轮数上限已停止；列表里可能还有任务'
        self.restore_list_state()
        self.update('finished', message=closing, current_task='', remaining=0)
        return stats
