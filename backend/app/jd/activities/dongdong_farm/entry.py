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
from backend.app.jd.activities.dongdong_farm.tasks import ALL_ACTIONS

logger = logging.getLogger('backend.execution')

FARM_MARKERS = ('点击前往做任务领水滴页面', '这里是要浇水的大树', '选奖品按钮')
PANEL_TITLE = '做任务领水滴'
SIGN_MARKERS = ('连续签到', '已连续签到', '点击签到领水滴')
# 容器页上的关闭/返回控件：小程序右上角「关闭」，H5 与商品详情左上角「返回」/「×」。
CLOSE_TEXTS = ('关闭', '关闭页面', '关闭弹窗', '返回', '×', '✕', 'X', 'close')


def _norm(text):
    return normalize(text or '')


def _texts(screen):
    return [_norm(node.text) for node in screen.nodes]


class EntryPolicy:
    # ---- 页面状态 ----

    @staticmethod
    def farm_visible(screen):
        """农场页。

        浇水动画、切换页面时控件树会有一两帧只剩零星节点，文案读不全；
        但农场页始终挂着一个铺满全屏的「东东农场」容器，认它比认按钮稳。
        首页金刚区那个同名图标只有 194x194，不会被这条判据误命中。
        """
        if any(marker in _texts(screen) for marker in FARM_MARKERS):
            return True
        for node in screen.nodes:
            if _norm(node.text) != '东东农场':
                continue
            width = node.bounds[2] - node.bounds[0]
            height = node.bounds[3] - node.bounds[1]
            if width >= screen.width * .8 and height >= screen.height * .8:
                return True
        return False

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
        return not any(_norm(node.text) in ALL_ACTIONS for node in screen.nodes)

    # ---- 京东自家的「其它页面」----

    def foreign_page(self, screen):
        """京东里既不是农场、也不是首页、也不是任务面板/签到的页面。

        真机上这类页面包括：商品详情、活动 H5（WebActivity）、PLUS 会员（FloatViewActivity）、
        小程序（MantoActivity，例如小游戏、任务中心）。它们的共同点是「不像我们的三个目标页」。
        恢复时必须先退出去，而不是把入口反复点在这张页面上。
        """
        if not (screen.package or '').startswith('com.jingdong.app.mall'):
            return False
        if (self.farm_visible(screen) or self.homepage_visible(screen)
                or self.panel_visible(screen) or self.sign_visible(screen)):
            return False
        # 农场本身是 Expo 容器页：点进去后的头一两帧只剩一个「返回」，控件树还没长出来。
        # 这时如果判成「外站」，恢复流程会立刻去点返回，把刚进去的农场又退掉，
        # 接着再点入口、再退回，循环到导航预算耗尽（真机 2026-11-05 08:35 现场）。
        return len([text for text in _texts(screen) if text]) > 2

    @staticmethod
    def page_close_node(screen):
        """容器页上的关闭/返回控件（只认顶部区域，避免点到正文里的「关闭」）。"""
        best = None
        for node in screen.nodes:
            text = _norm(node.text)
            if not text or text not in CLOSE_TEXTS:
                continue
            if node.center[1] > screen.height * .12:
                continue
            width = node.bounds[2] - node.bounds[0]
            height = node.bounds[3] - node.bounds[1]
            if width > screen.width * .5 or height > screen.height * .1:
                continue
            if not (0 <= node.bounds[0] < node.bounds[2] <= screen.width
                    and 0 <= node.bounds[1] < node.bounds[3] <= screen.height):
                # 农场容器会把「返回」报到屏幕外（真机 x 约 1098~1218，屏宽 1080）。
                # 点它会被框架的越界保护拦下，整轮直接以「目标位于系统栏」告吹。
                continue
            if best is None or (node.center[0], node.bounds[1]) < (best.center[0], best.bounds[1]):
                best = node
        return best

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
            x, y = node.center
            if not (0 <= x < screen.width and 0 <= y < screen.height):
                # 滚动收起顶部时图标会被搜索栏压到屏幕上沿外，点了也是白点
                # （框架还会直接判越界中断整轮）。
                continue
            return node
        return None

    def task_panel_entry(self, screen):
        if self.panel_visible(screen):
            return None
        if self.foreign_page(screen):
            # 恢复途中停在商品详情/H5/小程序上时，这里返回顶部关闭控件：
            # 导航循环点一下就能退出这张页面，下一轮再找真正的农场入口。
            return self.page_close_node(screen)
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
                    self._tap(node)
                    clicks += 1
                    last_click = self.control.now()
            self.control.wait(1.5)
            screen = self.read()
        return screen

    def enter_task_panel(self, screen, expected_activity=False):
        """点「点击前往做任务领水滴页面」并等抽屉出现。

        抽屉是 toggle：连点两下会「开→关」。Expo 页面打开抽屉要一两秒，
        所以每点一次都必须等到位，确认没开才允许再点（真机 2026-11-05 现场：
        0.7 秒的固定间隔让同一下被点了十几次，抽屉始终没留在屏幕上）。
        """
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
            if node is not None and clicks < 3 and self.control.now() - last_click >= 2.2:
                if not self._tap(node):
                    # 点不动（控件在屏幕外、或刚被重新渲染掉）不能算一次尝试，
                    # 否则三次额度瞬间花完，抽屉还没开就被判失败。
                    self.control.wait(.6)
                    screen = self.read()
                    continue
                clicks += 1
                last_click = self.control.now()
                screen = self.wait_screen(self.panel_visible, timeout=3.2, interval=.5)
                if self.panel_visible(screen):
                    self.origin = screen.package
                    return screen
                continue
            self.control.wait(.7)
            screen = self.read()

    def open_task_panel(self, screen):
        if self.panel_visible(screen):
            return True
        node = self.task_panel_entry(screen)
        if node is None:
            return False
        return self._tap(node)

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
        """反向「回顶部」。

        京东的任务抽屉是底部弹层：在里面往上拖（内容回到顶部）会被识别成
        「下拉关闭」，抽屉直接收起来——真机上连滑三次就退回农场首页了。
        所以反向检索一律用「收起 → 重新打开」实现，语义等价（回到列表顶部），
        副作用是重新渲染，不会把抽屉关掉。
        """
        return self.reopen_panel(screen)

    def reopen_panel(self, screen, timeout=8):
        """收起抽屉再打开，等价于把列表滚回顶部。

        收起和重开都要等，京东这两步各有 1-2 秒的渲染空档：点完「关闭任务面板」
        立刻读屏往往还是旧树，点完入口立刻读屏又还没画出行。真机上不等待就会
        判成「重开失败」，导航层接着去冷启动，白跑半分钟。
        """
        if not self.panel_visible(screen):
            return False
        closer = next((node for node in screen.nodes
                       if _norm(node.text) in ('关闭任务面板', '关闭')), None)
        tap = getattr(self, '_tap', self.click)
        if closer is not None:
            tap(closer)
        else:
            self.device.go_back()
        deadline = self.control.now() + timeout
        farm = None
        while self.control.now() < deadline:
            self.control.wait(.6)
            current = self.read()
            if self.farm_visible(current) and not self.panel_visible(current):
                farm = current
                break
        if farm is None:
            return False
        entry = self.task_panel_entry(farm)
        if entry is None:
            return False
        tap(entry)
        while self.control.now() < deadline:
            self.control.wait(.6)
            if self.panel_visible(self.read()):
                return True
        return False

    @staticmethod
    def view_signature(screen):
        """只看「有哪些行」，不看坐标。

        抽屉滚到底会回弹，行的 y 一直在抖；带上坐标会让主循环以为还有新内容，
        于是一路往下滑、再也回不到上面那些真正能做的任务。
        """
        return tuple(sorted(re.sub(r'\s+', '', _norm(node.text))
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
                        self._tap(node)
                        self.control.wait(1.2)
                        return self.read()
        except Exception:
            logger.info('收起任务面板失败，保留当前页面')
        return screen
