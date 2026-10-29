"""Bounded task selection, retries and outcome accounting."""
from dataclasses import asdict
import logging
import time
from backend.core.errors import Stopped, NeedsAttention, TaskUnavailable
from backend.core.device.instances import InstanceMismatch
logger = logging.getLogger('backend.execution')

class SessionLoop:
    def _swipe_for_more(self, screen):
        """One extra swipe to check whether the list still reveals new rows.

        Returns the screen after the swipe (for the loop to re-parse), or None
        when nothing new is reachable. Crucially, when the "更多任务" pager is
        already visible we first scroll the opposite way: after paging the
        viewport often stays pinned at the bottom, so the rows above it are only
        reachable by scrolling back up.
        """
        try:
            before = self.view_signature(screen)
        except (Stopped, NeedsAttention, InstanceMismatch):
            raise
        except Exception:
            return None
        if before is None:
            return None
        self.update('scanning', message='检查列表下方的任务', current_task='', remaining=0)
        self.control.check()

        # 1) 本批已到底（「更多任务」入口已在屏）：先朝反方向把本批上方行检索一遍。
        #    换批后视口常停在底部，不回滑就漏掉上方那一段（用户纠正过这一点）。
        if self.next_list_page(screen) is not None:
            try:
                if not self.alt_swipe(screen):
                    return None
                self.control.wait(.7)
                flipped = self.read()
                if not self.is_task_list(flipped.nodes):
                    flipped = self.recover()
            except (Stopped, NeedsAttention, InstanceMismatch):
                raise
            except Exception:
                logger.exception('任务列表反向滚动异常')
                return None
            return flipped

        # 2) 否则先主方向下滑，看是否露出「更多任务」或新行
        try:
            # 用活动自己的浏览手势：H5 列表往往吃不住默认手势
            self.browse_swipe(screen)
            self.control.wait(.7)
            after = self.read()
            if not self.is_task_list(after.nodes):
                after = self.recover()
        except (Stopped, NeedsAttention, InstanceMismatch):
            raise
        except Exception:
            logger.exception('任务列表读取异常')
            after = self.recover()
        if self.next_list_page(after) is not None:
            return after          # 滑到底露出了分页入口，交给主循环翻批
        try:
            now = self.view_signature(after)
        except (Stopped, NeedsAttention, InstanceMismatch):
            raise
        except Exception:
            return None
        if now != before:
            return after          # 还有新行，继续检索
        # 3) 主方向滑不动：朝反方向再试一次（换批后停在另一段）
        try:
            if not self.alt_swipe(screen):
                return None
            self.control.wait(.7)
            logger.info('主方向滑不动，已在反方向（换批后停在底部，检底部以下的内容）再做一次检索')
            flipped = self.read()
            if not self.is_task_list(flipped.nodes):
                flipped = self.recover()
            if self.next_list_page(flipped) is not None:
                return flipped     # 反方向也露出了分页入口
            if self.view_signature(flipped) != before:
                return flipped     # 反向也有新行
        except (Stopped, NeedsAttention, InstanceMismatch):
            raise
        except Exception:
            logger.exception('任务列表反向滚动异常')
        return None

    def record_task_failure(self, task):
        if not getattr(self, '_task_budget_counted', False):
            self._task_budget_counted = True
            return self.task_policy.fail(task, self.config['max_attempts'])
        return self.task_policy.failures(task)

    def recover_task(self, task, **kwargs):
        """A terminal navigation failure still consumes this task's daily budget."""
        try:
            return self.recover(**kwargs)
        except (Stopped, InstanceMismatch):
            raise
        except NeedsAttention as exc:
            if exc.kind == 'navigation':
                self.record_task_failure(task)
            raise
        except Exception:
            self.record_task_failure(task)
            raise

    def run(self, auto_open=False):
        self.started = time.monotonic()
        try:
            screen = self.ensure_list(auto_open)
        except (Stopped, NeedsAttention, InstanceMismatch):
            raise
        except Exception:
            if not auto_open or self._task_restart_used:
                raise
            self.report_failure('初始页面读取异常', phase='locating')
            screen = self.recover(cold_start=True)
        seen_pages = set()
        scrolls, total_attempts, page_turns = 0, 0, 0
        # 换批后仍没有可执行任务时，连续空批次达到上限就收工，不再翻完剩余批次
        empty_batches, paged = 0, False
        empty_batch_stop = max(1, int(self.config.get('empty_batch_stop', 1)))
        while total_attempts < self.config['max_tasks']:
            self.control.check()
            self.guard(screen)
            tasks = self.tasks(screen)
            self.remember(tasks)
            candidates = []
            for task in tasks:
                if task.kind in ('done', 'manual') or self.records[task.key]['status'] == 'disabled':
                    continue
                if task.kind not in self.config['enabled_task_kinds']:
                    self.records[task.key].update(status='disabled', reason='本轮未选择该任务类型；调整设置后可再次执行')
                    continue
                if task.kind == 'claim' and not self.config['claim_rewards']:
                    continue
                if not self.config['allow_cross_app'] and task.target_app:
                    self.records[task.key].update(status='disabled', reason='跨应用任务已关闭')
                    continue
                if self.task_policy.failures(task) < self.config['max_attempts']:
                    candidates.append(task)
            if not candidates and paged:
                paged = False
                empty_batches += 1
                if empty_batches >= empty_batch_stop:
                    logger.info('连续 %d 批没有可执行任务，提前结束本轮扫描', empty_batches)
                    break
            if candidates:
                empty_batches, paged = 0, False
                task = min(candidates, key=lambda t: (self.task_policy.failures(t),
                                                      {'claim': 0, 'browse': 1, 'visit': 2, 'search': 3}.get(t.kind, 4), t.bounds[1]))
                if self.task_policy.reason(task):
                    continue
                total_attempts += 1
                self.stats.attempts += 1
                self.records[task.key]['status'] = 'running'
                self.update(tasks=list(self.records.values()))
                before = screen
                disabled_reason = ''
                self._task_restart_used = False
                self._task_budget_counted = False
                try:
                    after, explicit, cross = self.execute(task, before)
                    success = self.verify(task, before, after, explicit)
                    if not success:
                        # A single short refresh accommodates server-side settlement.
                        self.control.wait(1)
                        # Themis can retain a completed/removed row in its UI
                        # tree. Recheck the rendered page before retrying it.
                        after = self.read(force_ocr=bool(self.config['ocr'] and after.source == 'ui'))
                        if not self.is_task_list(after.nodes):
                            after = self.recover_task(task)
                        success = self.verify(task, before, after, explicit)
                    screen = after
                    if task.kind == 'claim' and success:
                        self.control.wait(.8)
                        screen = self.read()
                        if not self.is_task_list(screen.nodes):
                            screen = self.recover_task(task)
                except TaskUnavailable as exc:
                    self.report_failure(str(exc), task, phase='unavailable')
                    disabled_reason = self.availability_reason(task, check_installed=bool(task.target_packages)) or str(exc)
                    self.unavailable_tasks[self.unavailable_key(task)] = disabled_reason
                    screen = self.recover_task(task)
                    success = False
                except (Stopped, InstanceMismatch):
                    raise
                except NeedsAttention as exc:
                    if exc.kind != 'navigation' or self._task_restart_used:
                        if exc.kind == 'navigation':
                            self.record_task_failure(task)
                        raise
                    screen = self.recover_task(task, cold_start=True)
                    success = self.verify(task, before, screen)
                except Exception:
                    logger.exception('任务执行异常：%s', task.title)
                    self.report_failure('任务执行异常', task)
                    screen = self.recover_task(task, cold_start=True)
                    # Reopening may reveal already-settled server progress.
                    success = self.verify(task, before, screen)
                if not success and not disabled_reason and task.target_packages:
                    # Some deeplinks silently stay on a landing page. Only after
                    # that real attempt fails verification do we confirm absence.
                    disabled_reason = self.availability_reason(task, check_installed=True)
                self.remember(self.tasks(screen))
                if disabled_reason:
                    self.records[task.key].update(status='disabled', reason=disabled_reason, enabled=False)
                    logger.info('已跳过 %s：%s', task.title, disabled_reason)
                elif success:
                    if task.kind == 'claim':
                        self.stats.claims += 1
                    else:
                        self.stats.completed += 1
                    updated = next((t for t in self.tasks(screen) if t.key == task.key), None)
                    can_continue = updated and updated.current < updated.total and updated.kind not in ('done', 'claim')
                    self.records[task.key].update(status='pending' if can_continue else 'done',
                                                  enabled=bool(can_continue and not self.task_policy.reason(updated)),
                                                  reason='已核验本次进度，可继续执行' if can_continue else '已核验任务进度或奖励状态')
                    if updated is None and task.kind == 'claim':
                        self.records[task.key].update(current=task.total, reason='累计任务奖励已领取，已核验奖励状态')
                    elif updated is None and task.current == task.total - 1:
                        self.records[task.key].update(current=task.total, reason='任务完成后已从列表移除，已核验相邻任务位置')
                    logger.info('核验成功：%s', task.title)
                else:
                    self.report_failure('任务进度未增加', task, phase='verifying')
                    self.failures[(task.key, task.current)] += 1
                    daily_failures = self.record_task_failure(task)
                    exhausted = daily_failures >= self.config['max_attempts']
                    if exhausted:
                        self.stats.failed += 1
                    self.records[task.key].update(status='failed' if exhausted else 'pending',
                                                  enabled=not exhausted and not bool(self.task_policy.reason(task)),
                                                  reason='任务进度未增加' + ('，今日已达尝试上限' if exhausted else '，先执行其他任务'))
                    logger.warning('未核验成功：%s，当前进度 %d，尝试 %d/%d', task.title, task.current,
                                   daily_failures, self.config['max_attempts'])
                self.update(tasks=list(self.records.values()), remaining=0)
                # Progress changed: the same viewport may have newly available tasks.
                seen_pages.clear()
                continue
            signature = tuple((t.key, t.current, t.action) for t in tasks)
            if signature in seen_pages or scrolls >= self.config['max_scroll_pages']:
                moved = self._swipe_for_more(screen)
                if moved is not None:
                    # 滑动后露出了「更多任务」分页入口 → 本批已到底，直接翻批
                    if self.next_list_page(moved) is not None and page_turns < self.config['max_scroll_pages']:
                        page_turns += 1
                        self.update('scanning', message='打开下一批任务', current_task='', remaining=0)
                        self.control.check()
                        try:
                            self.click(self.next_list_page(moved))
                            self.control.wait(1.2)
                            screen = self.read()
                            if not self.is_task_list(screen.nodes):
                                screen = self.recover()
                        except (Stopped, NeedsAttention, InstanceMismatch):
                            raise
                        except Exception:
                            logger.exception('任务列表翻页异常')
                            screen = self.recover()
                        paged = True
                        continue
                    if scrolls < self.config['max_scroll_pages']:
                        screen = moved
                        scrolls += 1
                        continue
                # 两个方向都滑不动：仍无分页入口就结束
                if page_turns >= self.config['max_scroll_pages']:
                    break
                try:
                    next_page = self.next_list_page(screen)
                except (Stopped, NeedsAttention, InstanceMismatch):
                    raise
                except Exception:
                    logger.exception('任务列表翻页识别异常')
                    next_page = None
                if next_page is None:
                    break
                page_turns += 1
                self.update('scanning', message='打开下一批任务', current_task='', remaining=0)
                self.control.check()
                try:
                    self.click(next_page)
                    self.control.wait(1.2)
                    screen = self.read()
                    if not self.is_task_list(screen.nodes):
                        screen = self.recover()
                except (Stopped, NeedsAttention, InstanceMismatch):
                    raise
                except Exception:
                    logger.exception('任务列表翻页异常')
                    screen = self.recover()
                paged = True
                continue
            seen_pages.add(signature)
            self.update('scanning', message='检查列表下方的任务', current_task='', remaining=0)
            self.control.check()
            self._task_restart_used = False
            try:
                self.device.swipe(int(screen.width * .5), int(screen.height * .88),
                                  int(screen.width * .5), int(screen.height * .43), duration=.55)
                scrolls += 1
                self.control.wait(.7)
                screen = self.read()
                if not self.is_task_list(screen.nodes):
                    screen = self.recover()
            except (Stopped, NeedsAttention, InstanceMismatch):
                raise
            except Exception:
                logger.exception('任务列表读取异常')
                self.report_failure('任务列表读取异常', phase='scanning')
                screen = self.recover(cold_start=True)
        # 会话结束（无可执行任务或已达上限）：回到应用首页，避免停在活动页/抽奖页
        try:
            screen = self.return_to_home(screen)
        except (Stopped, NeedsAttention, InstanceMismatch):
            raise
        except Exception:
            logger.exception('返回首页失败')
        message = ('达到本轮任务次数上限，可再次开始继续' if total_attempts >= self.config['max_tasks']
                   else '本轮结束，可执行任务已处理；请查看待处理和未通过核验的任务')
        self.update('finished', message=message, current_task='', remaining=0,
                    tasks=list(self.records.values()))
        return asdict(self.stats)
