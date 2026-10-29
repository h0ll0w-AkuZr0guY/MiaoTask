"""Return and restart budget with activity-provided entry policies."""
import logging
from backend.core.models import normalize
from backend.core.errors import Stopped, NeedsAttention
from backend.core.device.instances import InstanceMismatch
from backend.core.device.screen import Screen
logger = logging.getLogger('backend.execution')

class Navigator:
    def _restart_app(self, package):
        # Count the attempt before issuing RPCs, including failed cold starts.
        self._task_restart_used = True
        self.device.restart_app(package)


    def _navigate_list(self, screen, *, allow_restart, recovering=False, launched=False):
        """One bounded navigation budget, including at most one cold restart."""
        package = self.origin or self.application.package_for(self.device.device_type)
        budget = self.config['return_timeout'] + self.config['page_timeout'] * 2
        deadline = self.control.now() + budget
        back_count = 0
        home_pressed = False
        restarted = self._task_restart_used
        last_action = self.control.now() if launched else self.control.now() - 10
        clicked = set()
        searched = False
        home_reset = False
        official_clicks = 0
        panel_failures = set()
        awaiting_activity = False
        launch_pending = launched

        def step_back():
            nonlocal screen, back_count, home_pressed, last_action, awaiting_activity, launch_pending
            self.control.check()
            self.guard(screen)
            if back_count < self.config['max_back_steps']:
                self.update(message=f'逐层返回任务列表（{back_count + 1}）')
                self.device.go_back()
                back_count += 1
            elif not home_pressed:
                self.update(message='返回层级达到上限，先回到桌面确认后再恢复')
                self.device.go_home()
                home_pressed = True
            else:
                return False
            awaiting_activity = launch_pending = False
            last_action = self.control.now()
            self.control.wait(.4)
            screen = self.read(force_ocr=bool(self.config['ocr']))
            return True

        while True:
            self.control.check()
            self.guard(screen)
            if self._accept_list(screen):
                return screen
            if launched and screen.package != package and self.control.now() - last_action < self.config['page_timeout']:
                self.control.wait(.2)
                screen = self.read(force_ocr=bool(self.config['ocr']))
                continue
            launched = False
            if self.launcher_screen(screen):
                if not allow_restart or restarted:
                    break
                self.update(message='已回到桌面，重新启动所选应用并读取任务列表')
                self._restart_app(package)
                restarted = True
                back_count, home_pressed = 0, False
                clicked.clear()
                searched = home_reset = False
                official_clicks = 0
                panel_failures.clear()
                awaiting_activity, launch_pending, launched = False, True, True
                last_action = self.control.now()
                deadline = last_action + budget
                screen = self.read(force_ocr=bool(self.config['ocr']))
                continue
            if self.unavailable_landing(screen):
                self.remember_missing_destination(screen)
                if not step_back():
                    break
                continue
            # Process the last observation even if a slow OCR/RPC crossed the
            # navigation deadline. An arrived activity can still open its menu.
            panel_key = (screen.package, tuple((normalize(n.text), n.bounds) for n in screen.nodes))
            if (self.control.now() < deadline or not panel_failures) and panel_key not in panel_failures and screen.package == package and (awaiting_activity or self.task_panel_entry(screen)
                                             or self.activity_landing(screen) or self.task_panel_visible(screen)):
                screen = self.enter_task_panel(screen, expected_activity=awaiting_activity)
                if self._accept_list(screen):
                    return screen
                if self.unavailable_landing(screen) or screen.package != package:
                    awaiting_activity = False
                    continue
                panel_failures.add(panel_key)
                panel_failures.add((screen.package, tuple((normalize(n.text), n.bounds) for n in screen.nodes)))
                if not step_back():
                    break
                continue
            official = self.official_activity_entry(screen) if screen.package == package else None
            if official and official_clicks < 2 and ('activity', official.bounds) not in clicked:
                self.click(official)
                official_clicks += 1
                awaiting_activity = True
                clicked.add(('activity', official.bounds))
                last_action = self.control.now()
                deadline = max(deadline, last_action + self.config['page_timeout'])
                self.control.wait(.35)
                screen = self.read(force_ocr=bool(self.config['ocr']))
                continue
            if self.control.now() >= deadline:
                break
            # Only use native homepage controls after the painted page confirms
            # its bottom navigation. Icon labels such as 首页/搜索栏 are invisible
            # to OCR; a task overlay must never expose its background as an entry.
            home_visible = self.homepage_visible(screen)
            if (home_visible or self.activity_entry(screen, homepage=home_visible)
                    or self.control.now() - last_action >= self.config['page_timeout']):
                launch_pending = False
            if (screen.package == package and
                    (self.launch_screen(screen) or (launch_pending and not home_visible
                     and self.control.now() - last_action < self.config['page_timeout']))):
                self.update(message='等待应用启动完成，准备进入首页活动入口')
                self.control.wait(.35)
                screen = self.read(force_ocr=bool(self.config['ocr']))
                continue
            if home_visible:
                native = self.read()
                self.guard(native)
                if native.package == package:
                    screen = Screen([*native.nodes, *screen.nodes], screen.width, screen.height,
                                    package, 'ui+ocr', native.tree, native.user_id)
                    # A homepage overlay can expose the covered search/icon
                    # controls in its accessibility tree. Dismiss it before
                    # using those background controls.
                    if screen.find(r'^关闭按钮$|^关闭弹窗$'):
                        if not step_back():
                            break
                        continue
            # Empty/splash observations get a short rendering grace period.
            ready = bool(normalize(screen.text))
            if screen.package != package:
                if not step_back():
                    break
                continue
            elif ready:
                action = None
                target = self.activity_entry(screen, homepage=home_visible)
                key = ('activity', target.bounds if target else None)
                if target and key not in clicked:
                    action = target
                    clicked.add(key)
                if action:
                    logger.info('活动入口点击：文案 %s，坐标来自当前页面', action.text)
                    self.click(action)
                    awaiting_activity = True
                    last_action = self.control.now()
                elif self.control.now() - last_action < .7:
                    pass
                elif home_visible and not home_reset and screen.find('首页', exact=True):
                    self.click(screen.find('首页', exact=True))
                    home_reset = True
                    last_action = self.control.now()
                elif self.panel_loading(screen) and not self.tasks(screen):
                    # Preserve the independent rendering window for task rows.
                    if self.control.now() - last_action > self.config['page_timeout']:
                        if not step_back():
                            break
                        continue
                elif (not searched and screen.find(r'^搜索栏$|^搜索框$')
                      and (restarted or back_count > 0 or home_reset)):
                    searched = True
                    if not self.open_home_search(screen):
                        if not step_back():
                            break
                        continue
                    last_action = self.control.now()
                else:
                    if not step_back():
                        break
                    continue
            elif not ready and self.control.now() - last_action > 5:
                if not step_back():
                    break
                continue
            self.control.wait(.35)
            screen = self.read(force_ocr=bool(self.config['ocr']))
        if allow_restart and not self._task_restart_used and not home_pressed:
            # A non-responsive page can consume the observation budget. Confirm
            # the desktop before the single cold recovery; never restart blindly.
            self.guard(screen)
            self.control.check()
            self.device.go_home()
            self.control.wait(.4)
            screen = self.read(force_ocr=bool(self.config['ocr']))
            self.guard(screen)
            if self._accept_list(screen):
                return screen
            if self.launcher_screen(screen):
                self.control.check()
                self._restart_app(package)
                screen = self.read(force_ocr=bool(self.config['ocr']))
                return self._navigate_list(screen, allow_restart=False, recovering=recovering, launched=True)
        reason = ('未能自动恢复应用活动任务列表' if recovering else '未能自动进入应用活动任务列表')
        reason += '，已保存现场；请检查活动入口是否变化后重试'
        saved = self.report_failure(reason, phase='returning' if recovering else 'locating')
        raise NeedsAttention(reason, kind='navigation', evidence_saved=saved)


    def ensure_list(self, auto_open=False):
        self.update('locating', message='确认当前页面并进入活动任务列表')
        package = self.origin or self.application.package_for(self.device.device_type)
        if auto_open:
            info = self.device.current_app()
            foreground = info.get('package', '')
            # A launcher needs no screenshot/OCR. Stop the cached the application task
            # before opening it, so its old scrolled homepage cannot be restored.
            target = self.target_instance
            wrong_instance = bool(target and self.application.matches(foreground) and
                                  (foreground != target.package or target.user_id is not None and info.get('user_id') != target.user_id))
            if wrong_instance or isinstance(foreground, str) and not self.application.matches(foreground) and ('launcher' in foreground or foreground.endswith('.home')):
                self.update(message='从桌面启动：先关闭应用后台，再重新进入首页')
                self.device.restart_app(package)
                screen = self.read(force_ocr=bool(self.config['ocr']))
                return self._navigate_list(screen, allow_restart=True, launched=True)
        screen = self.read(force_ocr=bool(self.config['ocr']))
        self.guard(screen)
        if self._accept_list(screen):
            return screen
        if auto_open:
            if screen.package != package:
                self.update(message='先关闭应用后台，再重新进入首页')
                self.device.restart_app(package)
                screen = self.read(force_ocr=bool(self.config['ocr']))
                return self._navigate_list(screen, allow_restart=True, launched=True)
            return self._navigate_list(screen, allow_restart=True)
        if self.task_panel_entry(screen) or self.activity_landing(screen) or self.task_panel_visible(screen):
            screen = self.enter_task_panel(screen)
            if self._accept_list(screen):
                return screen
        # The advanced current-page mode never launches/restarts an application.
        deadline = self.control.now() + self.config['page_timeout']
        clicked = False
        while self.control.now() < deadline:
            if not clicked:
                clicked = self.open_task_panel(screen)
            self.control.wait(.4)
            screen = self.read(force_ocr=bool(self.config['ocr']))
            self.guard(screen)
            if self._accept_list(screen):
                return screen
        reason = '未识别到任务列表，请使用自动进入活动，或先在手机打开赚能量任务列表'
        saved = self.report_failure(reason, phase='locating')
        raise NeedsAttention(reason, evidence_saved=saved)


    def recover(self, cold_start=False):
        self.update('returning', message='自动恢复应用活动任务列表')
        try:
            screen = self.read(force_ocr=bool(self.config['ocr']))
        except (Stopped, NeedsAttention, InstanceMismatch):
            raise
        except Exception:
            if not cold_start or self._task_restart_used:
                raise
            screen = None
        if screen is not None:
            self.guard(screen)
        if screen is None:
            # Retry observation before sending input: the unreadable page could
            # be a verification screen and must not be dismissed blindly.
            self.control.wait(.2)
            screen = self.read(force_ocr=bool(self.config['ocr']))
        return self._navigate_list(screen, allow_restart=True, recovering=True)
