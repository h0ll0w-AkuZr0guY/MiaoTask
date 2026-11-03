"""东东农场入口识别与导航。

真机验证（2026-11-03，小米 PD2232 / 1080x2400 / 京东 15.x）的导航路径：
    京东首页 --点金刚区「东东农场」--> 东东农场（BabelActivity，Expo 页面）
    农场首页 --点「点击前往做任务领水滴页面」--> 「做任务 领水滴」抽屉（覆盖层）
    农场首页 --点「点击前往签到页面」--> 签到页（独立 H5，返回键回农场）

几处必须记住的现场事实：
* 首页的「东东农场」是金刚区里的图标（约 194x194），农场上又有一个铺满全屏的
  同名容器（0,0,1080,2400）——只按文字找会点到全屏容器，所以必须按尺寸过滤。
* 首页会随滚动收起顶部，图标可能被搜索栏压住（y 落到 0.1H 附近），
  这时要先往下滑回顶部再点，否则点中的是搜索框。
* 返回键在农场页会直接把农场退掉（回到首页），所以清理弹窗不能用返回键。
* 任务面板打开后，农场页的按钮被抽屉盖住，此时点签到/浇水会落到任务行上。
"""
import logging
import re

from backend.core.models import normalize

logger = logging.getLogger('backend.execution')

FARM_MARKERS = ('点击前往做任务领水滴页面', '这里是要浇水的大树', '选奖品按钮')
PANEL_TITLE = '做任务领水滴'
SIGN_MARKERS = ('连续签到', '已连续签到', '点击签到领水滴')


def _norm(text):
    return normalize(text or '')


def _texts(screen):
    return [_norm(node.text) for node in screen.nodes]


class EntryPolicy:
    # ---- 页面状态 ----

    @staticmethod
    def farm_visible(screen):
        return any(marker in _texts(screen) for marker in FARM_MARKERS)

    @staticmethod
    def panel_visible(screen):
        texts = _texts(screen)
        return PANEL_TITLE in texts

    @staticmethod
    def sign_visible(screen):
        return any(marker in _texts(screen) for marker in SIGN_MARKERS)

    @staticmethod
    def homepage_visible(screen):
        """京东首页：底部导航「我的」在屏幕下沿。"""
        for node in screen.nodes:
            if _norm(node.text) == '我的' and node.center[1] > screen.height * .8:
                return True
        return False

    @staticmethod
    def launcher_screen(screen):
        package = (screen.package or '').lower()
        return not package.startswith('com.jingdong.app.mall') and (
            'launcher' in package or package.endswith('.home'))

    @staticmethod
    def launch_screen(screen):
        """京东启动/闪屏页：几乎没有正文文字（首页卡片很多，不会误判）。"""
        if not (screen.package or '').startswith('com.jingdong.app.mall'):
            return False
        return sum(1 for node in screen.nodes if _norm(node.text)) < 3

    @staticmethod
    def activity_landing(screen):
        return (EntryPolicy.farm_visible(screen) or EntryPolicy.panel_visible(screen)
                or EntryPolicy.sign_visible(screen))

    def task_panel_visible(self, screen):
        return self.panel_visible(screen)

    @classmethod
    def panel_loading(cls, screen):
        """抽屉已经出现、行还没渲染完：给任务行独立的等待窗口。"""
        if not cls.panel_visible(screen):
            return False
        return not any(_norm(node.text) in {'去完成', '逛一逛', '去领奖', '去浇水', '去关注'}
                       for node in screen.nodes)

    # ---- 入口定位 ----

    @staticmethod
    def official_activity_entry(screen):
        return None

    def home_entry_node(self, screen):
        """首页金刚区的「东东农场」图标（排除农场上那个全屏同名容器）。"""
        for node in screen.nodes:
            if _norm(node.text) != '东东农场':
                continue
            width = node.bounds[2] - node.bounds[0]
            height = node.bounds[3] - node.bounds[1]
            if width > screen.width * .45 or height > screen.height * .12:
                continue
            return node
        return None

    def task_panel_entry(self, screen):
        if self.panel_visible(screen):
            return None
        for label in self.config.get('task_entry_labels', ['点击前往做任务领水滴页面']):
            text = _norm(label)
            for node in screen.nodes:
                if _norm(node.text) == text:
                    return node
        return None

    def activity_entry(self, screen, homepage=False):
        if self.farm_visible(screen) or self.panel_visible(screen) or self.sign_visible(screen):
            return None
        return self.home_entry_node(screen)

    # ---- 打开面板 / 进农场 ----

    def scroll_home_to_top(self, screen):
        """把首页滑回顶部，让金刚区图标离开搜索栏的遮挡。"""
        for _ in range(3):
            node = self.home_entry_node(screen)
            if node is not None and node.center[1] > screen.height * .2:
                return screen
            self.device.swipe(int(screen.width * .5), int(screen.height * .42),
                              int(screen.width * .5), int(screen.height * .78), duration=.35)
            self.control.wait(.9)
            screen = self.read()
        return screen

    def open_farm(self, screen, timeout=None):
        """从首页点进东东农场，等到农场页可读。"""
        budget = timeout if timeout is not None else self.config['page_timeout']
        deadline = self.control.now() + max(budget, 8)
        last_click = 0
        clicks = 0
        while self.control.now() < deadline:
            if self.farm_visible(screen) or self.panel_visible(screen):
                return screen
            if not self.homepage_visible(screen):
                if self.control.now() - last_click > 1.5:
                    self.device.go_back()
                    last_click = self.control.now()
                    self.control.wait(1.2)
                    screen = self.read()
                    continue
            else:
                screen = self.scroll_home_to_top(screen)
                node = self.home_entry_node(screen)
                if node is not None and clicks < 3 and self.control.now() - last_click > 1.2:
                    self.update(message='打开东东农场（第%d/3次）' % (clicks + 1))
                    self.click(node)
                    clicks += 1
                    last_click = self.control.now()
            self.control.wait(1.5)
            screen = self.read()
        return screen

    def enter_task_panel(self, screen, expected_activity=False):
        """点「点击前往做任务领水滴页面」并等抽屉出现。"""
        deadline = self.control.now() + self.config['page_timeout']
        last_click = self.control.now() - 10
        clicks = 0
        while True:
            self.guard(screen)
            if self.panel_visible(screen):
                self.origin = screen.package
                return screen
            if self.control.now() >= deadline:
                return screen
            node = self.task_panel_entry(screen)
            if node is not None and clicks < 3 and self.control.now() - last_click >= 1.2:
                self.click(node)
                clicks += 1
                last_click = self.control.now()
            self.control.wait(.7)
            screen = self.read()

    def open_task_panel(self, screen):
        if self.panel_visible(screen):
            return True
        node = self.task_panel_entry(screen)
        if node is None:
            return False
        self.click(node)
        return True

    def open_home_search(self, screen):
        return False

    def _accept_list(self, screen):
        self.assert_instance(screen)
        if self.panel_visible(screen) and (screen.package or '').startswith('com.jingdong.app.mall'):
            self.origin = screen.package
            return True
        return False

    # ---- 列表滚动：抽屉自己滚，手势必须落在抽屉里 ----

    def browse_swipe(self, screen):
        x = int(screen.width * .5)
        self.device.swipe(x, int(screen.height * .78), x, int(screen.height * .42), duration=.4)

    def alt_swipe(self, screen):
        x = int(screen.width * .5)
        self.device.swipe(x, int(screen.height * .42), x, int(screen.height * .78), duration=.4)
        return True

    @staticmethod
    def view_signature(screen):
        return tuple(sorted((_norm(node.text), node.bounds[1])
                            for node in screen.nodes
                            if re.search(r'\(\d+/\d+\)', _norm(node.text))))

    def next_list_page(self, screen):
        return None

    def return_to_home(self, screen):
        """收工：收起抽屉，停在农场首页（不退出京东）。"""
        try:
            if self.panel_visible(screen):
                for node in screen.nodes:
                    if _norm(node.text) == '关闭任务面板':
                        self.click(node)
                        self.control.wait(1.2)
                        return self.read()
        except Exception:
            logger.info('收起任务面板失败，保留当前页面')
        return screen
