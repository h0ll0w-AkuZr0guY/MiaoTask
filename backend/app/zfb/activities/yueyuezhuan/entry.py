"""月月赚入口识别与菜单打开。

真机验证到的导航路径（小米平板 / 支付宝 XRiverActivity）：
    支付宝首页  --点「更多」-->  全部应用页(AppManageActivityV3)
                --点「月月赚」-->  月月赚活动页(H5，但节点可读)

首页和「全部应用」页都是原生控件；活动页虽是 H5，内容也暴露在无障碍树里。
因此这里统一用**文字**定位，不依赖坐标。
"""
import logging
import re

from backend.core.models import TextNode, normalize

logger = logging.getLogger('backend.execution')


def _norm(text):
    """模块级归一化：这些判断既会被实例方法用，也会被静态方法用。"""
    return normalize(text or '')


def _is_alipay(package):
    """包名前缀判断（真实包名是大写 AlipayGphone，必须忽略大小写）。"""
    return (package or '').lower().startswith('com.eg.android.alipaygphone')


class EntryPolicy:
    _norm = staticmethod(_norm)

    # ---- 页面状态识别（供 Navigator 调度） ----

    @staticmethod
    def homepage_visible(screen):
        # 支付宝首页底部标签：理财 / 视频 / 消息 / 我的
        return any(normalize(n.text) == '我的' and n.center[1] > screen.height * 0.8
                   for n in screen.nodes)

    @staticmethod
    def launcher_screen(screen):
        package = (screen.package or '').lower()
        return not package.startswith('com.eg.android.alipaygphone') and (
            'launcher' in package or package.endswith('.home'))

    @staticmethod
    def launch_screen(screen):
        """支付宝启动/闪屏页：几乎没有任何正文文本。

        不能用「支付宝」文字判断——首页的「支付宝会员」等卡片也含该词，
        会把首页误判成启动页导致 Navigator 无限等待。
        """
        return _is_alipay(screen.package) and sum(
            1 for n in screen.nodes if _norm(n.text)) < 3

    @staticmethod
    def activity_landing(screen):
        titles = ('月月赚', '赚更多', '转账红包')
        return any(0 < n.bounds[3] - n.bounds[1] < screen.height * 0.15
                   and _norm(n.text) in titles
                   and n.center[1] < screen.height * 0.5
                   for n in screen.nodes)

    def task_panel_visible(self, screen):
        return any('去完成' in _norm(n.text) or '领取' in _norm(n.text)
                   for n in screen.nodes)

    # ---- 入口定位 ----

    def _home_more_node(self, screen):
        """首页应用宫格里的「更多」（不是右上角那个「更多操作」）。"""
        for n in screen.nodes:
            if normalize(n.text) != '更多':
                continue
            h = n.bounds[3] - n.bounds[1]
            if 0 < n.bounds[2] - n.bounds[0] < screen.width * 0.5 and 0 < h < screen.height * 0.12:
                return n
        return None

    def task_panel_entry(self, screen):
        """在「全部应用」页找到月月赚（活动页标题栏的同名文字不是入口）。"""
        if self.activity_landing(screen) or self.is_task_list(screen.nodes):
            return None
        for label in self.config.get('entry_labels', ['月月赚']):
            text = _norm(label)
            for n in screen.nodes:
                if _norm(n.text) == text and 0 < n.bounds[2] - n.bounds[0] < screen.width * 0.6:
                    return n
        return None

    @staticmethod
    def official_activity_entry(screen):
        for desc in screen.nodes:
            if '月月赚' in _norm(desc.text) and desc.center[1] < screen.height * 0.4:
                action = next((n for n in screen.nodes
                               if re.fullmatch(r'进入[>›》]?|去参与|立即参与|去完成', _norm(n.text))
                               and n.center[0] > screen.width * 0.6
                               and abs(n.center[1] - desc.center[1]) < screen.height * 0.06), None)
                if action:
                    return action
        return None

    def activity_entry(self, screen, homepage=False):
        official = self.official_activity_entry(screen)
        if official:
            return official
        if homepage:
            # 首页：先进「全部应用」，再点月月赚
            return self._home_more_node(screen)
        return self.task_panel_entry(screen)

    # ---- 打开任务列表 ----

    def enter_task_panel(self, screen, expected_activity=False):
        """从首页/全部应用页进入月月赚，并等待任务列表出现。"""
        deadline = self.control.now() + self.config['page_timeout']
        attempts = 0
        last_tap = self.control.now() - 10
        while True:
            self.guard(screen)
            if self._accept_list(screen):
                return screen
            if not _is_alipay(screen.package):
                return screen
            if self.control.now() >= deadline:
                return screen
            target = self.task_panel_entry(screen)
            if not target and self.homepage_visible(screen):
                target = self._home_more_node(screen)
            if target and attempts < 4 and self.control.now() - last_tap >= 1.0:
                self.update(message=f'打开月月赚（第{attempts + 1}/4次）')
                self.click(target)
                attempts += 1
                last_tap = self.control.now()
            self.control.wait(0.5)
            screen = self.read()

    def open_task_panel(self, screen):
        """Navigator 契约：从当前页打开任务面板（不重启应用）。"""
        if self.is_task_list(screen.nodes):
            return True
        node = self.task_panel_entry(screen)
        if node:
            self.click(node)
            return True
        return False

    def open_home_search(self, screen):
        """Navigator 契约：是否支持从首页搜索框进入活动。

        支付宝首页搜索的落地页不稳定（会跳到小程序/生活号聚合页），
        这里显式返回 False，让 Navigator 继续走返回重试而不是进错页面。
        """
        return False

    def _accept_list(self, screen):
        """只要在月月赚任务列表页就算成功（不要求必须解析出浏览任务）。

        浏览类任务做完当天就不会再出现，此时应正常识别页面并报告"无任务"，
        而不是一直重试进面板。
        """
        self.assert_instance(screen)
        if self.is_task_list(screen.nodes) and _is_alipay(screen.package):
            self.origin = screen.package
            return True
        return False

    # ---- 列表内的「更多任务」分页（滑到底才会出现） ----

    def browse_swipe(self, screen):
        """H5 列表吃不住框架默认的随机小幅手势，这里用稳定长距离滑动。

        实测：0.82H → 0.38H、0.8 秒，连滑 4 次到底并露出「更多任务」。
        """
        x = int(screen.width * 0.5)
        self.device.swipe(x, int(screen.height * 0.82),
                          x, int(screen.height * 0.38), duration=0.8)

    def alt_swipe(self, screen):
        """备用方向：换批后视口还停在底部，剩下的行要朝反方向滑才看得到。"""
        x = int(screen.width * 0.5)
        self.device.swipe(x, int(screen.height * 0.38),
                          x, int(screen.height * 0.82), duration=0.8)
        return True

    def _more_tasks_button(self, screen):
        label = _norm(self.config.get('more_tasks_label', '更多任务'))
        return next((n for n in screen.nodes if _norm(n.text) == label), None)

    @staticmethod
    def _view_signature(screen):
        """当前可视内容的指纹：只包含文字，滚动前后可比。"""
        return tuple(sorted({_norm(n.text) for n in screen.nodes if _norm(n.text)}))

    def view_signature(self, screen):
        """当前可视内容指纹：主循环靠它判断"还能不能滑出新内容"。"""
        return self._view_signature(screen)

    def next_list_page(self, screen):
        """主循环翻页契约：滑到底后用「更多任务」换下一批。

        主循环已经确认"再滑也没有新内容"，这里直接给出分页按钮即可。
        """
        return self._more_tasks_button(screen)

    # ---- 搜索直达入口：首页搜索栏 → 输入 → 点结果卡片 ----

    @staticmethod
    def _search_entry_node(screen):
        """首页 / 列表页上的「搜索」入口（取最靠上的那个）。"""
        hits = [n for n in screen.nodes
                if _norm(n.text) == '搜索' and n.bounds[3] < screen.height * 0.25]
        return min(hits, key=lambda n: (n.bounds[1], n.bounds[0])) if hits else None

    @staticmethod
    def _search_box(screen):
        """搜索页的输入框：顶部那条很宽的控件。"""
        hits = []
        for n in screen.nodes:
            w, h = n.bounds[2] - n.bounds[0], n.bounds[3] - n.bounds[1]
            top = n.bounds[1]
            if not (w > screen.width * 0.6 and 0 < h < screen.height * 0.12):
                continue
            # 状态栏之类的超宽横条要排除掉
            if not (screen.height * 0.02 < top < screen.height * 0.15):
                continue
            hits.append(n)
        return min(hits, key=lambda n: n.bounds[1]) if hits else None

    def _search_result(self, screen):
        """搜索结果页里的月月赚入口卡片。

        结果页几乎不暴露控件，read() 会自动降级到 OCR，所以按文字找。
        """
        keyword = (self.config.get('task_search_keyword')
                   or self.config.get('entry_labels', ['月月赚'])[0] or '月月赚')
        # 输入框的 text 也是关键词本身（top 约为 0.03H、宽超过 0.6W），
        # 必须排除，否则点了只是重新聚焦输入框。
        hits = [n for n in screen.nodes
                if _norm(n.text) == _norm(keyword)
                and screen.height * 0.07 < n.bounds[1] < screen.height * 0.45
                and n.bounds[2] - n.bounds[0] < screen.width * 0.6
                and 0 < n.bounds[3] - n.bounds[1] < screen.height * 0.2]
        if not hits:
            return None
        return min(hits, key=lambda n: (n.bounds[1], n.bounds[0]))

    def search_and_open(self, screen):
        """按用户验证过的方式进入月月赚：搜索栏 → 输入「月月赚」→ 点结果卡片。

        只在还没落到任务列表页时执行一次；失败留给框架的常规导航兜底。
        """
        if getattr(self, '_search_direct_done', False):
            return False
        if self._accept_list(screen) or not _is_alipay(screen.package):
            return False
        keyword = (self.config.get('task_search_keyword') or '').strip() or '月月赚'
        self._search_direct_done = True
        try:
            # 0) 起点必须能看到搜索入口；停在活动内页（如抽奖页）时先退回
            if self._search_entry_node(screen) is None and not self.homepage_visible(screen):
                for _ in range(3):
                    self.device.go_back()
                    self.control.wait(1.5)
                    screen = self.read()
                    if self.homepage_visible(screen) or self._search_entry_node(screen):
                        break
                    if not _is_alipay(screen.package):
                        break        # 已经退出支付宝了，别再一路退到桌面
            # 1) 打开搜索页
            entry = self._search_entry_node(screen)
            if not entry:
                return False
            self.update(message='打开支付宝搜索')
            self.click(entry)
            self.control.wait(1.5)
            screen = self.read()

            # 2) 输入关键词
            box = self._search_box(screen)
            if box:
                self.click(box)
                self.control.wait(1.2)
            self.update(message='搜索：' + keyword)
            try:
                self.device.input_text(keyword)
            except Exception:
                logger.info('常规输入失败，改用输入法写入')
                backend = getattr(self.device, '_device', None)
                if backend is None:
                    return False
                backend.set_fastinput_ime(True)
                backend.send_keys(keyword)
            self.control.wait(2.5)
            screen = self.read()

            # 3) 点开联想/结果里的活动入口。
            # 实测按回车会把联想结果清掉，所以直接点条目；
            # 结果页几乎不暴露控件，第二次退回实测过的卡片位置。
            for attempt in range(2):
                result = self._search_result(screen)
                if result is None and attempt:
                    # 入口卡片在顶部（实测中心约为 0.5W, 0.115H）
                    result = TextNode('', (int(screen.width * 0.2), int(screen.height * 0.08),
                                           int(screen.width * 0.8), int(screen.height * 0.15)))
                if result is None:
                    self.control.wait(1.5)
                    screen = self.read()
                    continue
                self.update(message='打开搜索结果里的活动')
                self.click(result)
                self.control.wait(3.5)
                screen = self.read()
                if self._accept_list(screen):
                    return True
                self.control.wait(1.5)
                screen = self.read()
                if self._accept_list(screen):
                    return True
            return False
        except Exception as exc:
            logger.info('搜索直达失败：%s', exc)
            return False
        return True

    def _bring_alipay_front(self):
        """保证支付宝在前台（不在就拉起并等到真正切回来）。"""
        try:
            screen = self.read()
        except Exception:
            screen = None
        if screen is not None and _is_alipay(screen.package):
            return screen
        try:
            self.device.start_app(self.application.package)
        except Exception:
            logger.info('拉起支付宝失败')
            return screen
        for _ in range(8):
            self.control.wait(2.0)
            try:
                screen = self.read()
            except Exception:
                return None
            if screen is not None and _is_alipay(screen.package):
                return screen
        return screen

    def ensure_list(self, auto_open=False):
        """优先用搜索直达，失败再走框架的常规导航。"""
        screen = self._bring_alipay_front()
        if screen is not None and self.search_and_open(screen):
            try:
                screen = self.read()
            except Exception:
                screen = None
            if screen is not None and self._accept_list(screen):
                return screen
        # 搜索过程中可能把支付宝退掉了，交给框架前再兜一次
        self._bring_alipay_front()
        return super().ensure_list(auto_open)

    def ensure_task_list(self, screen):
        """确保当前页就是月月赚任务列表；不在就退回，退回无用则重新进入。

        防呆：第三方活动任务列表（青春特权/网商银行…）曾被误判成"已回到列表"，
        于是脚本停在那儿继续点人家的任务。这里以「是否真的是月月赚列表」为准：
        先逐级返回；实在回不去就彻底退出到支付宝首页，重新搜索直达进入。
        """
        if self._accept_list(screen):
            return screen
        for _ in range(4):
            self.device.go_back()
            self.control.wait(1.2)
            try:
                screen = self.read()
            except Exception:
                break
            if self._accept_list(screen):
                return screen
        # 逐级返回不足以回来（第三方页面埋得深，或返回被H5拦住）：
        # 方案二——退出到支付宝首页，重新搜索进入月月赚。
        logger.info('未能返回月月赚列表，退出到首页后重新搜索进入')
        screen = self.return_to_home(screen)
        self._search_direct_done = False
        try:
            if self.search_and_open(screen):
                screen = self.read()
                if self._accept_list(screen):
                    return screen
        except Exception:
            logger.info('重新进入月月赚失败')
        return screen

    def scan(self, auto_open=False):
        """预览扫描：小额下滑、每次下滑后检索；滑到底后点「更多任务」换一批。"""
        screen = self.ensure_list(auto_open)
        collected = list(self.tasks(screen))
        limit = max(1, int(self.config.get('max_scroll_pages', 8)))
        known = {t.key for t in collected}
        before = self._view_signature(screen)
        for _ in range(limit):
            self.browse_swipe(screen)
            self.control.wait(1.2)
            screen = self.read()
            self.guard(screen)
            tasks = list(self.tasks(screen))
            collected.extend(t for t in tasks if t.key not in known)
            known.update(t.key for t in tasks)
            more = self._more_tasks_button(screen)
            now = self._view_signature(screen)
            bottom = (now == before)     # 再滑也没有新内容 = 到底
            before = now
            if not bottom:
                continue
            # 到底了：换批后视口往往还停在底部，先朝反方向再检索一遍
            if self.alt_swipe(screen):
                self.control.wait(1.2)
                flipped = self.read()
                self.guard(flipped)
                if self._view_signature(flipped) != now:
                    screen = flipped
                    for t in self.tasks(screen):
                        if t.key not in known:
                            collected.append(t)
                            known.add(t.key)
                    before = self._view_signature(screen)
                    continue
            # 两个方向都没有新内容了才换一批；没有分页入口就结束
            if more is None:
                break
            self.click(more)
            self.control.wait(1.5)
            screen = self.read()
            self.guard(screen)
            if not self.is_task_list(screen.nodes):
                screen = self.recover()
            tasks = list(self.tasks(screen))
            collected.extend(t for t in tasks if t.key not in known)
            known.update(t.key for t in tasks)
            before = self._view_signature(screen)
        self.remember(collected)
        self.update('ready', message='已识别当前屏幕任务，可以开始执行')
        return list(self.records.values())

    @staticmethod
    def _signature(screen, tasks):
        return tuple((t.key, t.current, t.action) for t in tasks)

    def return_to_home(self, screen):
        """任务都做完了（或没有可执行的浏览任务），回到支付宝首页。

        不要停在活动页/抽奖页：连续返回直到看到首页底部「我的」标签；
        一旦被退回支付宝之外（桌面），就重新拉起（默认进首页）。
        """
        for _ in range(6):
            try:
                if self.homepage_visible(screen) and _is_alipay(screen.package):
                    return screen
            except Exception:
                pass
            self.device.go_back()
            self.control.wait(1.3)
            try:
                screen = self.read()
            except Exception:
                break
            if not _is_alipay(screen.package):
                # 退出了支付宝，重新拉起（默认进首页）
                try:
                    self.device.start_app(self.application.package)
                except Exception:
                    return screen
                for _ in range(6):
                    self.control.wait(1.5)
                    try:
                        screen = self.read()
                    except Exception:
                        break
                    if self.homepage_visible(screen):
                        return screen
                return screen
        return screen
