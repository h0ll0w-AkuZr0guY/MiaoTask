"""Taobao browse/search actions; activities provide list and return policies."""
import logging
import re
from backend.core.models import normalize
from backend.core.errors import TaskUnavailable
logger = logging.getLogger('backend.execution')

class TaskActions:
    def execute(self, task, before):
        self._active_task = task
        self.update('opening', current_task=task.title, message='打开任务页面', remaining=0)
        logger.info('执行 %s (%d/%d)，类型 %s', task.title, task.current, task.total, task.kind)
        self.control.check()
        left, top, right, bottom = task.bounds
        self.device.click((left + right) // 2, (top + bottom) // 2)
        self.control.wait(.6)
        deadline = self.control.now() + self.config['page_timeout']
        opening_hard_deadline = deadline + self.config['page_timeout'] * 2
        screen, left = before, False
        confirmations = 0
        def skip_launch():
            reason='应用跳转未确认，今日跳过此任务；其他任务继续'
            self.unavailable_tasks['task:'+normalize(task.title)]=reason
            self.report_failure(reason,task,phase='unavailable')
            logger.warning('%s：%s',reason,task.title)
            return self.recover(),False,False
        while self.control.now() < deadline:
            screen = self.read(force_ocr=bool(self.config['ocr']))
            # Always make the first real jump attempt. Installation evidence is
            # read from this device only, never from a bundled skip list.
            self.guard(screen)
            self.check_destination(screen)
            # Scoped external-app launch confirmation; never accept privacy or login grants.
            confirm = getattr(self,'launch_confirmation',None)
            one_time = confirm(screen) if callable(confirm) else None
            if one_time:
                if not self.config['allow_cross_app']:
                    break
                if confirmations>=2:
                    return skip_launch()
                confirmations += 1
                logger.info('确认本次应用跳转：%s',normalize(one_time.text))
                self.click(one_time)
                self.control.wait(.8)
                deadline = min(opening_hard_deadline, self.control.now() + self.config['page_timeout'])
                continue
            pending=getattr(self,'is_launch_prompt',None)
            if callable(pending) and pending(screen):
                return skip_launch()
            if screen.package == self.origin and self.is_task_list(screen.nodes):
                after = next((x for x in self.tasks(screen) if x.key == task.key), None)
                if task.kind == 'claim' and after and (after.action != task.action or after.current > task.current):
                    return screen, True, False
                if task.key.startswith('milestone-') and after is None and self.milestone_received(task, screen):
                    return screen, True, False
                self.control.wait(.6)
                continue
            if not callable(confirm) and screen.find(r'打开.{0,10}应用|是否.{0,12}打开|即将.{0,10}跳转|允许.{0,12}打开|尝试打开'):
                button = screen.find(r'^(打开|立即打开|继续打开|允许打开|去支付宝|去看看|允许)$')
                if button:
                    if not self.config['allow_cross_app']:
                        break
                    self.click(button)
                    self.control.wait(.8)
                    continue
            ready = getattr(self,'destination_ready',None)
            if (not normalize(screen.text) or len(screen.nodes) < 2
                    or callable(ready) and not ready(screen)):
                self.control.wait(.7)
                continue
            left = True
            break
        if not left:
            logger.warning('任务没有成功离开列表：%s', task.title)
            reason = self.availability_reason(task, check_installed=bool(task.target_packages))
            if reason:
                raise TaskUnavailable(reason)
            return self.recover(), False, False
        return self.browse_destination(task, screen)

    def browse_destination(self, task, screen):
        """Observe an already-open destination; callers confirm any entry first."""
        cross = screen.package != self.origin
        if cross:
            if not self.config['allow_cross_app']:
                return self.recover(), False, False
            self.stats.cross_app += 1
            logger.info('跨应用浏览：%s', screen.package)
        # Captions rotate between browse/search promotions. The destination is
        # authoritative when a browse caption opens the activity search form.
        search_landing = (task.kind == 'browse' and screen.package == self.origin
                          and screen.find(r'^历史搜索$|^搜索发现$|^搜索有福利$|^搜索后浏览立得奖励$'))
        if task.kind == 'search' or search_landing:
            searched = self.handle_search(screen)
            if not searched:
                return self.recover(), False, cross
            screen = self.read()
        seconds = task.seconds or (self.config['visit_seconds'] if task.kind == 'visit'
                                    else self.config['default_browse_seconds'])
        if task.kind in ('browse', 'search'):
            seconds += self.config['browse_extra_seconds']
        seconds += self.config['settle_seconds']
        browse_started = self.control.now()
        hard_deadline = browse_started + self.config['max_task_seconds']
        browse_deadline = browse_started + min(seconds, self.config['max_task_seconds'])
        remaining = browse_deadline - self.control.now()
        explicit_success = False
        next_observation = self.control.now() + 1
        while remaining > 0:
            self.control.check()
            self.check_destination(screen)
            self.guard(screen)
            if screen.package == self.origin and self.is_task_list(screen.nodes):
                break
            if screen.package != self.origin:
                if not cross:
                    self.stats.cross_app += 1
                    cross = True
                if not self.config['allow_cross_app']:
                    break
            if screen.find(r'任务已完成|浏览已完成|浏览完成|完成浏览|奖励已到账|恭喜完成|任务完成啦'):
                explicit_success = True
                logger.info('检测到任务完成提示')
                break
            # Visible remaining countdown may extend, but never exceed the task cap.
            countdown = re.search(r'(?:还需|再逛|再浏览|剩余|再看)[^\d]{0,3}(\d{1,2})\s*秒', screen.text)
            counter = getattr(self, 'browse_countdown', None)
            visible_remaining = counter(screen) if callable(counter) else None
            visible_remaining = visible_remaining if visible_remaining is not None else int(countdown.group(1)) if countdown else None
            if visible_remaining is not None:
                browse_deadline = max(browse_deadline, min(self.control.now() + visible_remaining + 1,
                                                           hard_deadline))
            remaining = browse_deadline - self.control.now()
            self.update('browsing', message='跨应用浏览中' if cross else '浏览中', remaining=max(0, int(remaining + .9)))
            # Sustained scroll gestures keep the activity's browse counter running.
            # Bounded varying bursts reverse direction before a finite page stalls.
            scrolling = self.config['scroll_browse'] and not cross and task.kind in ('browse', 'search')
            if scrolling and remaining > 0:
                self.browse_swipe(screen)
            interval = max(0, min(self.gestures.interval() if scrolling else 1, browse_deadline - self.control.now()))
            self.control.wait(interval)
            # Pause time must not count as browse time; wait() itself excludes pauses.
            remaining = browse_deadline - self.control.now()
            if remaining > 0 and self.control.now() >= next_observation:
                screen = self.read()
                cadence = getattr(self, 'browse_observation_interval', None)
                next_observation = self.control.now() + (cadence(scrolling) if callable(cadence) else 1)
                remaining = browse_deadline - self.control.now()
        return self.recover(), explicit_success, cross
