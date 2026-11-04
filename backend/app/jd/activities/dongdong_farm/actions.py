"""东东农场任务执行：浏览 / 领奖 / 浇水 / 京东验证 / 弹窗清理。

真机验证（2026-11-03）的现场事实：
* 点「去完成」后，京东会另起一个页面栈（商品详情、Babel 活动页、养猪猪…），
  按返回会先回活动页再回首页，**不会回到农场**，所以每一步都要重新进农场开面板。
* 做完浏览回到面板，这一行会从「去完成」变成「去领奖」，需要再点一次才真正到账。
* 「去浇水」不跳页，点完抽屉会收起、水壶里的水减少 10g、树的进度前进。
* 京东偶发「京东验证 / 快速验证」，点开是滑动验证；它是风控，不是登录或支付，
  这里按人工方式把滑块拖到底（可在配置里用 quick_verify 关掉），失败就交回人工。
* 页面上的推广弹窗（下1单奖励、添加桌面小组件、天降惊喜红包）没有可读的关闭按钮，
  只能按位置点右上角/底部的 ×；点错会掉进「去下单」，所以只对已知文案生效。
"""
import logging
import random
import re

from backend.core.errors import NeedsAttention
from backend.core.models import TextNode, normalize

logger = logging.getLogger('backend.execution')

REWARD_TEXTS = ('恭喜获得', '开心收下')
VERIFY_TEXTS = ('快速验证', '滑动验证中', '京东验证', '立即验证')


def _norm(text):
    return normalize(text or '')


class TaskActions:
    # ---- 基础工具 ----

    def _texts(self, screen):
        return [_norm(node.text) for node in screen.nodes]

    def _find_exact(self, screen, labels):
        wanted = {_norm(label) for label in labels}
        for node in screen.nodes:
            if _norm(node.text) in wanted:
                return node
        return None

    def _find_contains(self, screen, labels):
        for node in screen.nodes:
            text = _norm(node.text)
            if any(_norm(label) in text for label in labels):
                return node
        return None

    def _tap(self, node, fallback=True):
        """点一下；弹出系统级窗口时控件坐标会落到应用窗口外，退化成原始点击。

        京东的「想要打开百度极速版」这类系统跳转框由 com.vivo.appfilter 绘制，
        控件树里的 bounds 常常超出当前应用窗口，框架的越界保护会直接抛异常。
        对这种整屏确认框，按坐标点等同于点按钮，所以兜底用 device.click。
        """
        try:
            self.click(node)
            return True
        except NeedsAttention:
            if not fallback:
                return False
        except Exception:
            logger.debug('点击控件失败', exc_info=True)
            if not fallback:
                return False
        try:
            self.device.click(*node.center)
            return True
        except Exception:
            logger.debug('兜底坐标点击失败', exc_info=True)
            return False

    def wait_screen(self, predicate, timeout=10.0, interval=.7):
        """反复读屏直到满足条件；京东的控件树切页后会空一两秒，不能只读一次。"""
        deadline = self.control.now() + max(timeout, 1)
        screen = self.read()
        while True:
            try:
                if predicate(screen):
                    return screen
            except Exception:
                logger.debug('读屏条件判断异常，继续等待', exc_info=True)
            if self.control.now() >= deadline:
                return screen
            self.control.wait(interval)
            screen = self.read()

    def reward_seen(self, screen):
        texts = self._texts(screen)
        return any(marker in text for text in texts for marker in REWARD_TEXTS)

    # ---- 弹窗 ----

    def in_app(self, screen):
        return self.application.matches(screen.package or '')

    def back_to_jd(self, screen, tries=2):
        """从外部应用回到京东。

        真机事实：快手极速版的首页会吃掉返回键，京东自己的小程序（MantoActivity）
        也不响应返回。这两种情况下继续按返回只是在原地打转，必须直接把京东拉回前台。
        """
        for _ in range(max(1, tries)):
            if self.in_app(screen) or not screen.package:
                return screen
            logger.info('在外部应用 %s 里，先按返回', screen.package)
            self.device.go_back()
            self.control.wait(1.4)
            screen = self.read()
        if not self.in_app(screen) and screen.package:
            package = self.origin or self.application.android_package
            logger.info('返回键退不出 %s，重新拉起京东', screen.package)
            self.device.start_app(package)
            self.control.wait(2.4)
            screen = self.read()
        return screen

    def leave_inner_page(self, screen):
        """退出京东自家的 H5 / 小程序 / 商品详情页，回到农场或首页。"""
        settled = self.wait_screen(lambda s: not self.foreign_page(s), timeout=2.5, interval=.6)
        if not self.foreign_page(settled):
            return settled
        screen = settled
        node = self.page_close_node(screen)
        if node is not None:
            logger.info('关闭京东内嵌页面：%s', _norm(node.text))
            self._tap(node)
        else:
            self.device.go_back()
        self.control.wait(1.4)
        return self.read()

    def escape_foreign(self, screen, tries=2):
        """跳出京东：系统「京东想要打开XX」跳转框、外部浏览器/应用。

        这类任务一律不做（见 tasks.SKIP_RULES），先点「取消」，点不到就返回键，
        回到京东自己的页面为止。
        """
        for _ in range(max(1, tries)):
            if self.in_app(screen) or not screen.package:
                return screen
            logger.info('跳出了京东（%s），退回应用内', screen.package)
            node = self._find_exact(screen, ['取消', '关闭', '不允许', '禁止', '拒绝', '仅打开一次'])
            if node is not None and _norm(node.text) in ('取消', '关闭', '不允许', '禁止', '拒绝'):
                self._tap(node)
            else:
                self.device.go_back()
            self.control.wait(1.3)
            screen = self.read()
        return screen

    def dismiss_popups(self, screen, tries=3):
        """关掉挡路的奖励/推广弹窗，返回清理后的屏幕。绝不因为点不中而中断。"""
        labels = self.config.get('popup_close_labels', [])
        promos = [_norm(x) for x in self.config.get('promo_popup_labels', [])]
        screen = self.escape_foreign(screen)
        for _ in range(tries):
            node = self._find_exact(screen, labels)
            if node is not None and node.bounds[2] - node.bounds[0] < screen.width * .9:
                if not self._tap(node):
                    break
                self.control.wait(1.1)
                screen = self.escape_foreign(self.read())
                continue
            texts = self._texts(screen)
            if any(promo in text for text in texts for promo in promos):
                if self._tap_close_candidate(screen, promos):
                    screen = self.escape_foreign(self.read())
                    continue
            break
        return screen

    def _tap_close_candidate(self, screen, promos):
        """已知推广弹窗没有可读关闭控件：按配置里的相对位置挨个试。"""
        points = self.config.get('popup_close_points') or []
        for fx, fy in points:
            point = TextNode('', (int(screen.width * fx) - 2, int(screen.height * fy) - 2,
                                  int(screen.width * fx) + 2, int(screen.height * fy) + 2))
            self._tap(point)
            self.control.wait(.9)
            screen = self.read()
            texts = self._texts(screen)
            if not any(promo in text for text in texts for promo in promos):
                logger.info('已关闭推广弹窗')
                return True
        return False

    # ---- 京东验证 ----

    def verify_blocked(self, screen):
        texts = self._texts(screen)
        return any(marker == text or marker in text for text in texts for marker in VERIFY_TEXTS)

    def solve_quick_verify(self, screen, tries=2):
        """点「快速验证」并把滑块拖到底。失败就把页面交回人工（guard 会停止本轮）。"""
        if not self.config.get('quick_verify', True):
            logger.warning('页面要求京东安全验证，已按配置停止自动操作')
            return screen
        for attempt in range(tries):
            track = self._find_exact(screen, ['滑动验证中', '拖动滑块', '向右滑动'])
            if track is not None:
                y = track.center[1]
                start = int(screen.width * .19)
                end = int(screen.width * .87)
                logger.info('京东验证：拖动滑块到右端（第 %d 次）', attempt + 1)
                self._human_drag(start, y, end, y)
                screen = self.wait_screen(lambda s: not self.verify_blocked(s), timeout=6)
                if not self.verify_blocked(screen):
                    return screen
                continue
            node = self._find_exact(screen, self.config.get('quick_verify_labels', ['快速验证']))
            if node is None:
                break
            self.click(node)
            screen = self.wait_screen(lambda s: self.verify_blocked(s) and (
                self._find_exact(s, ['滑动验证中']) is not None), timeout=6)
        return screen

    def _human_drag(self, x1, y1, x2, y2):
        """一段带轻微起伏的滑动：京东的滑块只认手势本身，不用精确拼图。"""
        self.device.swipe(x1, y1, x2, int(y2 + random.uniform(-3, 3)),
                          duration=random.uniform(.7, 1.0))

    # ---- 回到农场 / 打开面板 ----

    def ensure_farm_home(self, screen, timeout=20):
        """保证停在农场首页（抽屉收起）。"""
        deadline = self.control.now() + timeout
        while self.control.now() < deadline:
            if self.panel_visible(screen):
                closer = self._find_exact(screen, ['关闭任务面板'])
                if closer is not None:
                    self._tap(closer)
                    self.control.wait(1.2)
                    screen = self.read()
                    continue
                self.device.go_back()
                self.control.wait(1.2)
                screen = self.read()
                continue
            if self.farm_visible(screen):
                return screen
            if not self.in_app(screen):
                screen = self.back_to_jd(screen)
                continue
            if self.foreign_page(screen):
                screen = self.leave_inner_page(screen)
                continue
            if self.homepage_visible(screen) or self.launcher_screen(screen):
                screen = self.open_farm(screen)
                continue
            self.device.go_back()
            self.control.wait(1.4)
            screen = self.read()
        return screen

    def ensure_panel(self, screen, timeout=16):
        """保证「做任务 领水滴」抽屉打开且至少解析出一行。"""
        started = self.control.now()
        deadline = started + timeout
        while self.control.now() < deadline:
            if self.panel_visible(screen):
                if self.is_task_list(screen.nodes) or self.control.now() - started > 6:
                    return screen
                self.control.wait(.8)
                screen = self.read()
                continue
            screen = self.dismiss_popups(screen)
            if self.panel_visible(screen):
                continue
            if self.farm_visible(screen):
                entry = self.task_panel_entry(screen)
                if entry is None:
                    self.alt_swipe(screen)
                    self.control.wait(1.0)
                    screen = self.read()
                    continue
                self.click(entry)
                self.control.wait(1.6)
                screen = self.read()
                continue
            screen = self.recover_farm_panel(screen)
        return screen

    def recover_farm_panel(self, screen, tries=4):
        """从落地页回到农场并打开面板。返回键会一路退到京东首页，两种落点都要处理。"""
        for _ in range(tries):
            if self.panel_visible(screen):
                return screen
            if self.farm_visible(screen):
                entry = self.task_panel_entry(screen)
                if entry is None:
                    return screen
                self.click(entry)
                self.control.wait(1.6)
                screen = self.read()
                continue
            if not self.in_app(screen):
                screen = self.back_to_jd(screen)
                continue
            if self.foreign_page(screen):
                screen = self.leave_inner_page(screen)
                continue
            if self.homepage_visible(screen):
                screen = self.open_farm(screen)
                continue
            self.device.go_back()
            self.control.wait(1.5)
            screen = self.read()
        return screen

    # ---- 行定位 ----

    def _row_node(self, screen, task):
        rows = self.tasks(screen)
        for row in rows:
            if row.key == task.key:
                return TextNode('', row.bounds)
        for row in rows:
            if row.title == task.title:
                return TextNode('', row.bounds)
        return None

    def locate_row(self, screen, task):
        """在当前面板里找到目标行的按钮；找不到就滚动面板再找。"""
        node = self._row_node(screen, task)
        if node is not None:
            return screen, node
        for _ in range(3):
            self.browse_swipe(screen)
            self.control.wait(1.1)
            screen = self.read()
            if not self.panel_visible(screen):
                screen = self.ensure_panel(screen)
            node = self._row_node(screen, task)
            if node is not None:
                return screen, node
        return screen, None

    # ---- 主执行 ----

    def execute(self, task, before):
        self._active_task = task
        self._credited = False
        self.update('opening', message='打开任务：' + task.title, remaining=0)
        screen = self.ensure_panel(before)
        screen, target = self.locate_row(screen, task)
        if target is None:
            logger.info('任务面板里没有找到这一行，跳过本次：%s', task.title)
            return screen, False, False
        self.click(target)
        self.control.wait(1.5)
        screen = self.read()
        if self.verify_blocked(screen):
            self.update('verifying', message='京东安全验证', remaining=0)
            screen = self.solve_quick_verify(screen)
            if self.verify_blocked(screen):
                from backend.core.errors import NeedsAttention
                reason = '页面要求京东安全验证，自动操作已停止；请在手机上完成后重新开始'
                saved = self.report_failure(reason, phase='security')
                raise NeedsAttention(reason, kind='security', evidence_saved=saved)
            screen = self.recover_farm_panel(screen)
        if task.target_app:
            screen = self._finish_cross_app(task, screen)
            return screen, self._credited, True
        if task.kind == 'claim':
            screen = self._finish_claim(screen)
        elif task.kind == 'visit':
            screen = self._finish_water(screen)
        else:
            screen = self._finish_browse(task, screen)
        return screen, self._credited, False

    # ---- 站外应用任务 ----

    @staticmethod
    def target_running(screen, task):
        return bool(screen.package) and screen.package in tuple(task.target_packages or ())

    def launch_button(self, screen):
        """系统「想要打开 XX」跳转框里的确认按钮：只点一次性的，绝不点「始终」。"""
        for node in screen.nodes:
            text = _norm(node.text)
            if not text or len(text) > 8:
                continue
            if any(bad in text for bad in ('始终', '总是', '不再', '取消', '拒绝', '禁止')):
                continue
            if text in ('仅打开一次', '仅此一次', '打开', '立即打开', '继续打开', '允许'):
                return node
        return None

    def confirm_launch(self, screen, tries=2):
        """确认应用跳转框；京东有时直接拉起目标应用，没有这一框。"""
        for _ in range(max(1, tries)):
            context = re.search(r'想要打开|即将打开|即将跳转|是否打开|允许.{0,8}打开', _norm(screen.text))
            node = self.launch_button(screen) if context else None
            if node is None:
                return screen
            logger.info('确认应用跳转：%s', _norm(node.text))
            self._tap(node)
            self.control.wait(1.2)
            screen = self.read()
        return screen

    def _finish_cross_app(self, task, screen):
        """站外任务：确认跳转 → 等目标应用起来 → 停留 → 回京东 → 面板。"""
        screen = self.confirm_launch(screen)
        screen = self.wait_screen(lambda s: self.target_running(s, task), timeout=10)
        if not self.target_running(screen, task):
            logger.info('没有进入%s（当前 %s），这一条按未完成处理', task.target_app, screen.package)
            screen = self.back_to_jd(screen)
            return self.ensure_panel(screen)
        self.update('browsing', message='浏览%s：%s' % (task.target_app, task.title), remaining=0)
        self.control.wait(max(6, int(task.seconds or 15)))
        self.device.go_back()
        self.control.wait(1.2)
        screen = self.read()
        screen = self.back_to_jd(screen)
        screen = self.dismiss_popups(screen)
        screen = self.recover_farm_panel(screen)
        screen = self.ensure_panel(screen)
        self.control.wait(2.5)  # 站外任务的归因有延迟，回到面板后再给一点结算时间
        return self.read()

    def _finish_claim(self, screen):
        self.update('browsing', message='领取水滴奖励', remaining=0)
        screen = self.wait_screen(lambda s: self.reward_seen(s) or self.panel_visible(s), timeout=5)
        screen = self.dismiss_popups(screen)
        if self.panel_visible(screen):
            return screen
        return self.ensure_panel(screen)

    def _finish_water(self, screen):
        self.update('browsing', message='浇水', remaining=0)
        self.control.wait(2.2)
        screen = self.read()
        self._water_actions = int(getattr(self, '_water_actions', 0)) + 1
        screen = self.dismiss_popups(screen)
        if not self.panel_visible(screen):
            screen = self.ensure_panel(screen)
        return screen

    def _finish_browse(self, task, screen):
        seconds = max(3, int(task.seconds or self.config.get('browse_shortcut_seconds', 6)))
        self.update('browsing', message='浏览：' + task.title, remaining=0)
        screen = self._dwell(screen, seconds)
        # 跨应用任务要先把人从外部应用带回来，否则后面的恢复会在别人的首页里打转。
        screen = self.back_to_jd(screen)
        screen = self.dismiss_popups(screen)
        screen = self.recover_farm_panel(screen)
        screen = self.ensure_panel(screen)
        return self.dismiss_popups(screen)

    def _dwell(self, screen, seconds):
        """在落地页停留足够时长，中途做几次小幅滑动，尽量像真人浏览。"""
        deadline = self.control.now() + seconds
        self.control.wait(1.2)
        screen = self.read()
        if self.reward_seen(screen):
            self._credited = True
            return screen
        while self.control.now() < deadline:
            try:
                self.browse_swipe(screen)
            except Exception:
                logger.debug('落地页滑动失败，继续等待', exc_info=True)
            self.control.wait(random.uniform(.9, 1.6))
            screen = self.read()
            if self.reward_seen(screen):
                self._credited = True
                return screen
        return screen

    # ---- 农场日常：签到 / 领水滴 / 浇水 ----

    def do_chores(self, screen):
        screen = self.ensure_farm_home(screen)
        screen = self.do_sign(screen)
        if self.config.get('claim_water_bubbles', True):
            screen = self.claim_water_bubbles(screen)
        return self.do_watering(screen)

    def do_sign(self, screen):
        entry = self._find_exact(screen, self.config.get('sign_entry_labels', ['点击前往签到页面']))
        if entry is None:
            logger.info('农场首页没有签到入口，跳过签到')
            return screen
        self.update('scanning', message='农场日常：签到领水滴', current_task='', remaining=0)
        self.click(entry)
        screen = self.wait_screen(
            lambda s: any('连续签到' in text for text in self._texts(s)), timeout=10)
        if not any('连续签到' in text for text in self._texts(screen)):
            logger.info('签到页没有打开，退回农场继续')
            return self.ensure_farm_home(screen)
        if any('已连续签到' in text for text in self._texts(screen)):
            logger.info('今天已经签到过了')
            return self._leave_sign(screen)
        button = self._find_exact(screen, self.config.get('sign_button_labels', []))
        if button is not None:
            self.click(button)
        else:
            # 签到大按钮画在 canvas 上，读不到控件时按实测位置点（原图 0.5W / 0.424H）
            center_x, center_y = screen.width // 2, int(screen.height * .424)
            self.click(TextNode('', (center_x - 6, center_y - 6, center_x + 6, center_y + 6)))
        self.control.wait(1.8)
        screen = self.read()
        if self.reward_seen(screen):
            self.update('browsing', message='签到成功，收下水滴', remaining=0)
            screen = self.dismiss_popups(screen)
        logger.info('完成农场签到')
        return self._leave_sign(screen)

    def _leave_sign(self, screen):
        """签到页是独立页面，返回键回农场（不会退出京东）。"""
        for _ in range(3):
            if self.farm_visible(screen):
                return screen
            self.device.go_back()
            self.control.wait(1.6)
            screen = self.read()
            if self.farm_visible(screen):
                return screen
        return self.ensure_farm_home(screen)

    def claim_water_bubbles(self, screen, limit=3):
        """农场左侧「领 N水滴」气泡：点一下就进水壶。"""
        pattern = re.compile(r'^领\s*\d+\s*水滴$')
        for _ in range(limit):
            node = next((n for n in screen.nodes if pattern.match(_norm(n.text))), None)
            if node is None:
                break
            self.update('browsing', message='领取水滴', remaining=0)
            self.click(node)
            self.control.wait(1.6)
            screen = self.dismiss_popups(self.read())
            if not self.farm_visible(screen):
                screen = self.ensure_farm_home(screen)
        return screen

    def do_watering(self, screen):
        """水壶里每 10g 浇一次，浇到没水或达到本轮上限。"""
        cap = int(self.config.get('water_times_per_run', 8))
        done = 0
        while done < cap:
            can = next((n for n in screen.nodes if _norm(n.text).startswith('水滴剩余')), None)
            if can is None:
                break
            found = re.search(r'(\d+)', _norm(can.text))
            if not found or int(found.group(1)) < 10:
                logger.info('水壶里的水滴不足 10g，停止浇水')
                break
            self.update('browsing', message='浇水（本轮第 %d 次）' % (done + 1), remaining=0)
            self.click(can)
            done += 1
            self._water_actions = int(getattr(self, '_water_actions', 0)) + 1
            self.control.wait(2.3)
            screen = self.read()
            if self.reward_seen(screen):
                screen = self.dismiss_popups(screen)
            if not self.farm_visible(screen):
                screen = self.ensure_farm_home(screen)
        if done:
            logger.info('本轮浇了 %d 次水', done)
        return screen
