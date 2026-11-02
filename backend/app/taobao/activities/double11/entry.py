"""Double 11 entry recognition and menu opening."""
import logging
import re
from backend.core.models import TextNode, normalize
logger = logging.getLogger('backend.execution')

from .tasks import normalize

class EntryPolicy:
    @staticmethod
    def passive_login_banner(screen):
        # Public homepage may advertise login in a bottom banner. A real login
        # page/modal still stops; no buttons on that authentication surface click.
        return bool(screen.find('领淘金币') and sum(bool(screen.find(label)) for label in
                    ('淘宝秒杀','飞猪旅行','淘宝闪购','阿里拍卖','淘票票')) >= 2)

    def task_panel_entry(self, screen):
        # Controls can say 去赚能量 / 立即赚能量 / 赚能量，按钮. Match short
        # captions containing the configured label, while excluding sheet titles
        # and task descriptions. OCR may split a short caption across two boxes.
        candidates = list(screen.nodes)
        anchor = screen.find(r'^升级赛车拿钱')
        fragments = [n for n in screen.nodes if len(normalize(n.text)) <= 8
                     and any(c in normalize(n.text) for c in '赚能量体力喵币做任务')]
        for first in fragments:
            for second in fragments:
                if first is second:
                    continue
                a, b = first.bounds, second.bounds
                height = max(a[3] - a[1], b[3] - b[1])
                horizontal = (0 <= b[0] - a[2] <= height * 1.5
                              and abs(first.center[1] - second.center[1]) <= height * .6)
                vertical = (0 <= b[1] - a[3] <= height
                            and abs(first.center[0] - second.center[0]) <= max(a[2]-a[0], b[2]-b[0]) * .6)
                if horizontal or vertical:
                    candidates.append(TextNode(first.text + second.text,
                                      (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])),
                                      'ocr-joined'))
        matches = []
        for priority, label in enumerate(self.config['task_entry_labels']):
            # This is the opened sheet's heading, never a button on this campaign.
            if normalize(label) == '做任务赚能量':
                continue
            caption = normalize(label)
            for target in candidates:
                text = re.sub(r'[，,。.:：;；!！>＞()（）\[\]【】\u200b]', '', normalize(target.text))
                if (caption in text and len(text) <= len(caption) + 8
                        and not re.search(r'做任务赚|累计|奖励|浏览|邀请|已完成|去完成|升级|商品|攻略|教程', text)
                        and (text == caption or caption != '做任务')
                        and 0 <= target.center[0] < screen.width and target.center[1] > 0
                        and (target.center[1] < screen.height * .7 or
                             anchor and abs(target.center[1] - anchor.center[1]) < screen.height * .12)
                        and 0 < target.bounds[2] - target.bounds[0] < screen.width * .7
                        and 0 < target.bounds[3] - target.bounds[1] < screen.height * .2):
                    outside_band = bool(anchor and abs(target.center[1] - anchor.center[1]) >= screen.height * .12)
                    matches.append((priority, outside_band, text != caption, len(text), target))
        return min(matches, key=lambda item: item[:4])[4] if matches else None


    def menu_visual_fallback(self, screen):
        """Re-read the visible control band at native resolution, without swipes."""
        anchor = screen.find(r'^升级赛车拿钱')
        if anchor:
            top = max(0, anchor.bounds[1] - round(screen.height * .08))
            bottom = min(screen.height, anchor.bounds[3] + round(screen.height * .08))
        elif self.activity_landing(screen):
            top, bottom = round(screen.height * .25), round(screen.height * .65)
        else:
            return screen
        regions = [(0, top, round(screen.width * .55), bottom),
                   (round(screen.width * .45), top, screen.width, bottom)]
        return self.reader.enrich_regions(screen, regions)


    @staticmethod
    def task_panel_visible(screen):
        return any(normalize(n.text) == '做任务赚能量'
                   and 0 < n.bounds[3] - n.bounds[1] < screen.height * .15
                   and n.center[1] < screen.height * .7 for n in screen.nodes)


    @staticmethod
    def homepage_visible(screen):
        return all(any(normalize(n.text) == label and n.center[1] > screen.height * .8
                       for n in screen.nodes) for label in ('我的淘宝', '购物车'))


    @staticmethod
    def launcher_screen(screen):
        package = (screen.package or '').lower()
        return not package.startswith('com.taobao.taobao') and ('launcher' in package or package.endswith('.home'))


    @staticmethod
    def launch_screen(screen):
        return bool(screen.find(r'阿里云提供计算服务') or
                    (screen.find(r'^天猫双') and screen.find(r'^正式开卖$')))


    @staticmethod
    def activity_landing(screen):
        return any(0 < n.bounds[3] - n.bounds[1] < screen.height * .15
                   and ((n.center[1] < screen.height * .18
                         and re.search(r'^淘金币双11|^淘金币双.{0,4}狂享20亿|^淘金币首页$', normalize(n.text)))
                        or (n.center[1] < screen.height * .5 and normalize(n.text) == '升级赛车拿钱'))
                   for n in screen.nodes)


    def enter_task_panel(self, screen, expected_activity=False):
        """Confirm the painted panel; retry a missed menu tap at most twice.

        A late-arriving activity page gets its own bounded rendering window.
        Native controls supplement a missing OCR label only on that visible page.
        Never trust background task rows from an accessibility tree as success.
        """
        deadline = self.control.now() + self.config['page_timeout']
        attempts = 0
        last_tap = self.control.now() - 10
        native_reads = 0
        last_native = self.control.now() - 10
        heading_seen = False
        local_pass = 0
        region_reads = 0
        while True:
            self.guard(screen)
            if self._accept_list(screen):
                return screen
            if not screen.package.startswith('com.taobao.taobao'):
                return screen
            panel_loading = self.task_panel_visible(screen) or bool(screen.find('累计任务奖励', exact=True))
            if not panel_loading and not self.activity_landing(screen) and self.unavailable_landing(screen):
                return screen
            if panel_loading and not heading_seen:
                heading_seen = True
                # The sheet opens first, then requests the task rows. This is a
                # successful forward transition, deserving its own load window.
                deadline = max(deadline, self.control.now() + self.config['page_timeout'])
                self.update(message='已打开做任务赚能量，等待任务列表加载')
            if self.control.now() >= deadline:
                if not heading_seen and local_pass == 0 and (self.activity_landing(screen) or expected_activity):
                    local_pass = 1
                    deadline = self.control.now() + self.config['page_timeout']
                    attempts = native_reads = region_reads = 0
                    self.update(message='仍在活动页，重新识别按钮并尝试备用点击位置')
                    screen = self.read(force_ocr=bool(self.config['ocr']))
                    continue
                return screen
            target = None if panel_loading else self.task_panel_entry(screen)
            if (not panel_loading and region_reads < 2 and (native_reads or local_pass)
                    and (not target or attempts)):
                region_reads += 1
                screen = self.menu_visual_fallback(screen)
                self.guard(screen)
                target = self.task_panel_entry(screen)
            if (screen.source == 'ocr' and native_reads < 3
                    and (target or self.activity_landing(screen) or expected_activity)
                    and not panel_loading and self.control.now() - last_native >= 1.2):
                native_reads += 1
                last_native = self.control.now()
                native = self.read()
                self.guard(native)
                if native.package == screen.package:
                    if self.task_panel_visible(native) or self.is_task_list(native.nodes):
                        # A sheet may appear during the hierarchy RPC. Confirm
                        # the actual foreground again instead of tapping old OCR.
                        screen = self.read(force_ocr=bool(self.config['ocr']))
                        continue
                    close = native.find(r'^关闭$|^关闭弹窗$|^关闭按钮$')
                    if close and 0 < close.bounds[3]-close.bounds[1] < native.height*.12:
                        self.click(close)
                        self.control.wait(.5)
                        screen = self.read(force_ocr=bool(self.config['ocr']))
                        continue
                    # Prefer painted button geometry over a broad accessibility caption.
                    target = target or self.task_panel_entry(native)
            if target and attempts < 3 and self.control.now() - last_tap >= 1.2:
                self.update(message=f'打开赚能量菜单（页面内第{local_pass + 1}轮，第{attempts + 1}/3次）')
                logger.info('任务菜单点击：页面内第%d轮，第%d次，来源%s，文案%s',
                            local_pass + 1, attempts + 1, target.source, target.text)
                if local_pass and target.bounds[3] - target.bounds[1] > screen.height * .035:
                    # An alternate point stays inside a freshly located native
                    # control, rather than guessing a phone-specific coordinate.
                    x = target.center[0]
                    y = target.bounds[1] + round((target.bounds[3] - target.bounds[1]) * .3)
                    self.control.check()
                    self.device.click(x, y)
                else:
                    self.click(target)
                attempts += 1
                last_tap = self.control.now()
            elif not (target or self.activity_landing(screen) or panel_loading or expected_activity or attempts):
                return screen
            self.control.wait(.35)
            screen = self.read(force_ocr=bool(self.config['ocr']))


    def open_task_panel(self, screen):
        if self.is_task_list(screen.nodes):
            return True
        node = self.task_panel_entry(screen)
        if node:
            self.click(node)
            return True
        return False


    def activity_entry(self, screen, homepage=False):
        """Locate entry words in controls/OCR, independent of icon coordinates."""
        official = self.official_activity_entry(screen)
        if official:
            return official
        for label in self.config['entry_labels']:
            text = normalize(label)
            candidates = [n for n in screen.nodes if 0 < n.bounds[2] - n.bounds[0]
                          and 0 < n.bounds[3] - n.bounds[1] < screen.height * .2
                          and (not homepage or n.center[1] < screen.height * .7)]
            exact = next((n for n in candidates if normalize(n.text) == text), None)
            if exact:
                return exact
            # Accessibility descriptions often append "抵现金" or "按钮".
            # Keep this fallback within homepage functions, outside product cards.
            if homepage:
                matches = [n for n in candidates if text in normalize(n.text)
                           and len(normalize(n.text)) <= 40
                           and not re.search(r'[¥￥]|已售|人付款|商品详情', n.text)]
                if matches:
                    return min(matches, key=lambda n: (len(normalize(n.text)), n.bounds[1]))
        return None


    @staticmethod
    def official_activity_entry(screen):
        # A search result contains TWO exact 淘金币 labels: the search field and
        # the official card. Enter the card's aligned right-hand action instead
        # of reopening the search field or a product advertising an entry.
        descriptions = [n for n in screen.nodes if re.search(r'双11狂享20亿', normalize(n.text))
                        and n.center[1] < screen.height * .35]
        for description in descriptions:
            title = any(normalize(n.text) == '淘金币' and n.center[0] < screen.width * .6
                        and 0 < description.center[1] - n.center[1] < screen.height * .08
                        for n in screen.nodes)
            if title:
                action = next((n for n in screen.nodes if re.fullmatch(r'进入[>›》]?', normalize(n.text))
                               and n.center[0] > screen.width * .65
                               and abs(n.center[1] - description.center[1]) < screen.height * .06), None)
                if action:
                    return action
        return None


    def _accept_list(self, screen):
        self.assert_instance(screen)
        if (self.task_panel_visible(screen) and self.is_task_list(screen.nodes)
                and self.parse_tasks(screen.nodes, screen.width, screen.height)
                and screen.package.startswith('com.taobao.taobao')):
            self.origin = screen.package
            return True
        return False
