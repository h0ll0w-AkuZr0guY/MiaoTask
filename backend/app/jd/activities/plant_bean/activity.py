"""种豆得豆的入口、任务行与页面核验，执行调度复用公共 Activity。"""
import re
from dataclasses import replace

from backend.app.common.models.task import TaskRules, parse_tasks
from backend.app.jd.manifest import PROFILE
from backend.core.config.loader import load_config
from backend.core.errors import NeedsAttention, TaskUnavailable
from backend.core.execution.activity import Activity
from backend.core.models import Task, TextNode, bounds_of, normalize


class PlantBeanActivity(Activity):
    application = PROFILE
    claim_actions = frozenset({'点击领奖', '领取', '去领取', '领奖励', '去签到'})
    external_apps = ()

    @staticmethod
    def default_config():
        return load_config(app='jd', activity='plant_bean')

    def panel(self, screen):
        return bool(screen.find(self.config['panel_markers']))

    def is_task_list(self, nodes):
        return any(re.search(self.config['panel_markers'], normalize(n.text)) for n in nodes)

    def classify(self, title, detail, texts, action, current, total):
        reason = ''
        seconds = re.search(r'浏览(?:页面)?(\d+)(?:秒|[sS])', detail)
        if re.search(self.config['game_task_words'], title):
            kind, reason = 'manual', '游戏类任务，默认跳过'
        elif re.search(self.config['bean_manual_words'], title + detail):
            kind, reason = 'manual', '需要本人处理的任务'
        elif action in ('已领取', '已完成') or current >= total and action not in self.claim_actions:
            kind = 'done'
        elif action in self.claim_actions:
            kind = 'claim'
        elif action in ('逛一逛', '去完成', '去浏览', '去看看'):
            kind = 'browse'
        else:
            kind, reason = 'manual', '需要本人处理的入口'
        return int(seconds[1]) if seconds else 0, kind, reason, '', ()

    def parse_tasks(self, nodes, width, height):
        if not self.is_task_list(nodes):
            return []
        rules = TaskRules(re.compile('|'.join(re.escape(s) for s in self.config['bean_actions'])),
                          re.compile(r'[（(](\d+)/(\d+)[)）]'), re.compile(r'(?<=\S)浏览(?:页面)?\d+'),
                          frozenset(), '\0', re.compile(r'(?!)'), '', self.classify)
        rows = parse_tasks(nodes, width, height, rules=rules)
        sign = next((n for n in nodes if normalize(n.text) == '去签到'), None)
        if sign:
            rows.insert(0, Task('bean-daily-sign', '种豆每日签到', '', '去签到', sign.bounds, kind='claim'))
        return rows

    def tasks(self, screen):
        if id(self) in getattr(screen, '_campaign_tasks', {}):
            return super().tasks(screen)
        panel_nodes = self.native_panel_nodes(screen) if screen.source.startswith('ui') else []
        scoped = replace(screen, nodes=panel_nodes) if panel_nodes else screen
        tasks = super().tasks(scoped)
        screen._campaign_tasks = scoped._campaign_tasks
        for task in tasks:
            if task.kind == 'browse':
                task.browse_once = not task.seconds
        return tasks

    def native_panel_nodes(self, screen):
        """Use the sheet subtree so the obscured homepage cannot alter task rules."""
        selected = []

        def visit(item):
            if isinstance(item, list):
                return [node for child in item for node in visit(child)]
            if not isinstance(item, dict):
                return []
            attrs = item.get('attributes', item)
            if any(attrs.get(key) in (False, 'false') for key in ('visible', 'visible-to-user', 'displayed')):
                return []
            text = attrs.get('text') or attrs.get('description') or attrs.get('content-desc') or ''
            nodes = [TextNode(text, bounds_of(attrs.get('bounds', attrs.get('rect'))))] if text else []
            for key in ('children', 'hierarchy'):
                nodes.extend(visit(item.get(key)))
            if (not selected and self.is_task_list(nodes)
                    and any(re.search(r'[（(]\d+/\d+[)）]', node.text) for node in nodes)):
                selected.extend(nodes)
            return nodes

        visit(screen.tree)
        header = next((n for n in selected if re.search(self.config['panel_markers'], normalize(n.text))), None)
        if header is None:
            return []
        return [header] + [n for n in selected if n.center[1] > header.bounds[3]
                           and n.bounds[3] - n.bounds[1] < screen.height * .1]

    def _read(self, visual=False):
        screen = self.read(force_ocr=visual and self.config['ocr'])
        self.guard(screen)
        return screen

    def setup(self):
        self._scan_from_top = True

    def view_signature(self, screen):
        return tuple((t.key, t.current, t.action) for t in self.tasks(screen))

    def alt_swipe(self, screen):
        header = screen.find(self.config['panel_markers'])
        if header is None:
            return False
        start = max(header.bounds[3] + screen.height * .04, screen.height * .4)
        end = screen.height * .88
        if start >= end:
            return False
        self.device.swipe(int(screen.width * .5), int(start), int(screen.width * .5), int(end), duration=.45)
        return True

    def panel_top(self, screen):
        for _ in range(self.config['max_scroll_pages']):
            signature = self.view_signature(screen)
            if not self.alt_swipe(screen):
                break
            self.control.wait(.5)
            fresh = self._read()
            if not self.panel(fresh):
                return self.ensure_list(True)
            screen = fresh
            if self.view_signature(screen) == signature:
                break
        return screen

    def ensure_list(self, auto_open=False):
        package = self.origin or self.application.package_for(self.device.device_type)
        self.origin = package
        if auto_open and self.device.current_app().get('package') != package:
            self.device.start_app(package)
            self.control.wait(1)
        self.update('locating', message='进入种豆得豆任务面板')
        deadline = self.control.now() + self.config['return_timeout'] + self.config['page_timeout'] * 3
        backs, searches, taps = 0, 0, {}
        last_action = self.control.now() - self.config['page_timeout']
        screen = self._read()
        if auto_open and self.game_exit_node(screen):
            screen = self.return_from_browse(screen)
        while self.control.now() < deadline:
            if self.panel(screen):
                # Wait for rows rather than accepting the sheet's first blank paint.
                if self.tasks(screen):
                    if getattr(self, '_scan_from_top', False):
                        self._scan_from_top = False
                        return self.panel_top(screen)
                    return screen
            elif screen.package == package:
                if screen.find(self.config['home_markers']) or screen.find(self.config['promotion_markers']):
                    painted = self._read(visual=True)
                    popup = painted.find(self.config['promotion_markers'])
                    close = next((n for n in painted.nodes if popup and normalize(n.text) in ('X', '×', '✕')
                                  and n.center[0] > painted.width * .5
                                  and painted.height * .15 < n.center[1] < painted.height * .8), None)
                    if close:
                        self.click(close)
                        last_action = self.control.now()
                    else:
                        entry = next((n for n in painted.nodes
                                      if normalize(n.text) in self.config['task_entry_labels']), None)
                        if entry:
                            self.click(entry)
                            last_action = self.control.now()
                elif auto_open:
                    search = screen.find(r'^搜索栏$|^搜索框$')
                    entry = next((n for n in screen.nodes
                                  if normalize(n.text) in self.config['entry_labels']
                                  and n.center[1] < screen.height * .7
                                  and n.bounds[3] - n.bounds[1] < screen.height * .15
                                  and (search is None or n.center[1] > search.bounds[3])
                                  and taps.get((normalize(n.text), n.bounds), 0) < 2), None)
                    enter = screen.find(r'^进入[>›]?$')
                    if (entry and enter and enter.center[0] > screen.width * .7
                            and abs(enter.center[1] - entry.center[1]) < screen.height * .14):
                        entry = enter
                    if entry:
                        key = (normalize(entry.text), entry.bounds)
                        if taps.get(key, 0) < 2:
                            taps[key] = taps.get(key, 0) + 1
                            self.click(entry)
                            last_action = self.control.now()
                    elif search and searches < 1:
                        self.click(search)
                        self.control.wait(.6)
                        fresh = self._read()
                        # A dedicated search page exposes a real native editor.
                        if self._has_edit(fresh.tree):
                            self.device.input_text(self.config['name'])
                            fresh = self._read()
                            submit = next((n for n in fresh.nodes if normalize(n.text) == '搜索'
                                           and n.center[0] > fresh.width * .7
                                           and n.center[1] < fresh.height * .25), None)
                            if submit:
                                self.click(submit)
                                searches += 1
                                last_action = self.control.now()
                    elif (backs < self.config['max_back_steps']
                          and self.control.now() - last_action >= self.config['page_timeout']):
                        self.device.go_back()
                        backs += 1
                        last_action = self.control.now()
            self.control.wait(1)
            screen = self._read()
        raise NeedsAttention('未能进入种豆得豆任务面板，请检查页面或登录状态', kind='navigation')

    @classmethod
    def _has_edit(cls, tree):
        if isinstance(tree, list):
            return any(cls._has_edit(item) for item in tree)
        if isinstance(tree, dict):
            attrs = tree.get('attributes', tree)
            if 'EditText' in str(attrs.get('class', attrs.get('type', ''))):
                return True
            return any(cls._has_edit(tree.get(key)) for key in ('children', 'hierarchy'))
        return False

    def recover(self, cold_start=False):
        if cold_start:
            package = self.origin or self.application.package_for(self.device.device_type)
            self._restart_app(package)
            self.control.wait(1)
        return self.ensure_list(auto_open=True)

    def settled_panel(self, task, before):
        screen = self.recover()
        for _ in range(3):
            if self.verify(task, before, screen):
                return screen
            self.control.wait(1)
            screen = self._read()
        return screen

    def game_exit_node(self, screen):
        """The mini-game capsule exposes its graphic close icon to accessibility."""
        if (screen.package != (self.origin or self.application.package_for(self.device.device_type))
                or self.panel(screen) or screen.find(self.config['home_markers'])
                or screen.find(self.config['promotion_markers'])):
            return None
        nodes = list(screen.nodes)

        def visit(item):
            if isinstance(item, list):
                for child in item:
                    visit(child)
            elif isinstance(item, dict):
                attrs = item.get('attributes', item)
                if any(attrs.get(key) in (False, 'false') for key in ('visible', 'visible-to-user', 'displayed')):
                    return
                text = normalize(attrs.get('description') or attrs.get('content-desc') or attrs.get('text') or '')
                if (attrs.get('package', screen.package) == screen.package
                        and text in self.config['game_menu_labels'] + self.config['game_exit_labels']):
                    nodes.append(TextNode(text, bounds_of(attrs.get('bounds', attrs.get('rect')))))
                for key in ('children', 'hierarchy'):
                    visit(item.get(key))

        # OCR cannot read these graphic icons. Retain their native descriptions
        # even when the reader selected OCR for the game canvas.
        visit(screen.tree)
        menus = [n for n in nodes
                 if normalize(n.text) in self.config['game_menu_labels']
                 and n.center[0] > screen.width * .7 and n.center[1] < screen.height * .2]
        for node in nodes:
            width = node.bounds[2] - node.bounds[0]
            height = node.bounds[3] - node.bounds[1]
            if (normalize(node.text) in self.config['game_exit_labels']
                    and node.center[0] > screen.width * .8
                    and 0 < node.center[1] < screen.height * .2
                    and 0 < width < screen.width * .15 and .6 <= width / max(1, height) <= 1.6
                    and any(menu.center[0] < node.center[0]
                            and 0 <= node.bounds[0] - menu.bounds[2] <= width
                            and abs(menu.center[1] - node.center[1]) < height * .5 for menu in menus)):
                return node
        return None

    def return_from_browse(self, screen):
        """Try the capsule once, then 1..5 uninterrupted Back presses per round."""
        package = self.origin or self.application.package_for(self.device.device_type)
        deadline = self.control.now() + self.config['return_timeout']
        self.guard(screen)
        exit_node = self.game_exit_node(screen)
        if exit_node:
            self.update('returning', message='点击小游戏右上角退出按钮')
            self.click(exit_node)
            self.control.wait(.5)
            screen = self._read()
        for count in range(1, 7):
            self.guard(screen)
            if (self.panel(screen) or screen.package != package
                    or screen.find(self.config['home_markers'])
                    or screen.find(r'^搜索栏$|^搜索框$')
                    or screen.find(self.config['promotion_markers'])):
                return screen
            if count > 5 or self.control.now() >= deadline:
                break
            self.update('returning', message=f'返回种豆得豆（连续返回 {count} 次）')
            # Read only between rounds, so OCR cannot break the double-Back window.
            for _ in range(count):
                self.control.check()
                if self.control.now() >= deadline:
                    break
                self.device.go_back()
            self.control.wait(.4)
            screen = self._read()
        raise NeedsAttention('已达到连续返回或返回时间上限，仍未退出任务页面，请本人检查')

    def browse_ready(self, screen):
        if self.panel(screen) or screen.find(self.config['loading_markers']):
            return False
        return sum(screen.height * .12 < n.center[1] < screen.height * .9
                   and n.bounds[3] - n.bounds[1] < screen.height * .3 for n in screen.nodes) >= 2

    def execute(self, task, before):
        current = next((t for t in self.tasks(before) if t.key == task.key), None)
        if current is None or current.kind in ('manual', 'done'):
            raise TaskUnavailable('当前任务已变化，请重新读取列表')
        self.click(TextNode(current.action, current.bounds))
        self.control.wait(1)
        screen = self._read()
        if task.kind == 'claim':
            deadline = self.control.now() + self.config['page_timeout']
            claimed = False
            while self.control.now() < deadline:
                claim = screen.find(r'^点击领奖$')
                if claim and not claimed and screen.find(r'连续签到奖励'):
                    self.click(claim)
                    claimed = True
                if self.panel(screen) and not any(t.key == task.key and t.kind == 'claim'
                                                 for t in self.tasks(screen)):
                    return screen, False, True
                if screen.find(r'明天继续领奖'):
                    break
                self.control.wait(1)
                screen = self._read()
            return self.settled_panel(task, before), False, True
        deadline = self.control.now() + self.config['page_timeout']
        while (not self.browse_ready(screen) and not self.game_exit_node(screen)
               and self.control.now() < deadline):
            self.control.wait(.5)
            screen = self._read()
        if self.game_exit_node(screen):
            self.return_from_browse(screen)
            raise TaskUnavailable('进入了小游戏页面，已按活动规则跳过')
        if not self.browse_ready(screen):
            self.return_from_browse(screen)
            return self.settled_panel(task, before), False, True
        self.guard(screen)
        if screen.package != self.origin:
            raise TaskUnavailable('当前活动只执行京东站内浏览任务')
        seconds = max(task.seconds, self.config['default_browse_seconds']) if task.seconds else 15
        deadline = self.control.now() + min(self.config['max_task_seconds'], seconds)
        self.update('browsing', message=task.title)
        while self.control.now() < deadline:
            if self.config['scroll_browse']:
                self.browse_swipe(screen)
            else:
                self.control.wait(min(.5, deadline - self.control.now()))
        self.return_from_browse(self._read())
        return self.settled_panel(task, before), False, True

    def verify(self, task, before, after, explicit=False):
        if not self.panel(after):
            return False
        updated = next((t for t in self.tasks(after) if t.key == task.key), None)
        if task.key == 'bean-daily-sign':
            return updated is None
        if updated is None and task.current == task.total - 1:
            # This activity removes completed one-off rows. Require its next
            # two rows to retain their order and the preceding rows on return.
            old = [t.key for t in self.tasks(before) if t.key != 'bean-daily-sign']
            new = [t.key for t in self.tasks(after) if t.key != 'bean-daily-sign']
            index = old.index(task.key) if task.key in old else len(old)
            neighbors = old[index + 1:index + 3]
            expected = old[:index] + neighbors
            return bool(neighbors and len(expected) >= 2 and new[:len(expected)] == expected)
        return bool(updated and (updated.current > task.current
                                 or task.kind == 'claim' and updated.kind == 'done'))
