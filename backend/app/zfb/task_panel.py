"""支付宝任务面板的入口、行解析与核验，活动差异由各自 config.json 声明。"""
import re

from backend.app.common.models.task import TaskRules, parse_tasks
from backend.app.common.utils.destinations import DestinationPolicy
from backend.app.zfb.manifest import PROFILE
from backend.core.config.loader import load_config
from backend.core.errors import NeedsAttention, TaskUnavailable
from backend.core.execution.activity import Activity
from backend.core.models import Task, TextNode, bounds_of, normalize


_PROGRESS = re.compile(r'\((\d+)/(\d+)\)')
_FARM_PROGRESS = re.compile(r'当前进度(\d+)[,，]总进度(\d+)')
_SECONDS = re.compile(r'(?:浏览(?:页面)?|逛|停留)(\d{1,3})(?:秒|s)', re.I)
_MANUAL = re.compile(
    r'下单|购买|支付(?!宝)|充值|兑换|提现|抽奖|夺宝|抽签|邀请|助力|分享|安装|下载|'
    r'开户|开通|授权|实名|登录|投保|意外险|百万保障|签约|填写|提交|添加组件|开启.*提醒|'
    r'游戏|玩一玩|玩一场|去玩|玩寻道|镇邪人|砍树|施肥|喂(?:小)?鸡|通过\d+关|居民订单|招募英雄|去守护|立即参与')
_GAME_PAGE = re.compile(r'游戏任务入口|继续玩游戏|重新开始游戏|通过第?\d+关')
_BROWSE = frozenset({'去完成', '逛一逛', '去浏览', '去看看', '去逛逛', '去访问'})
_CLAIM = frozenset({'领取', '立即领取', '去领取', '领奖励'})
_DONE = frozenset({'已完成', '已领取', '已领', '已达成'})
_ACTIONS = _BROWSE | _CLAIM | _DONE | {'去抽签', '去下单', '去参与'}
_ACTION_RE = re.compile('|'.join(sorted(_ACTIONS)))


class AlipayTaskPanel(DestinationPolicy, Activity):
    application = PROFILE
    claim_actions = _CLAIM
    activity_id = ''

    @classmethod
    def default_config(cls):
        return load_config(app='zfb', activity=cls.activity_id)

    @staticmethod
    def _groups(nodes, groups):
        texts = [normalize(n.text) for n in nodes]
        return bool(groups) and all(any(word in text for word in group for text in texts) for group in groups)

    def is_task_list(self, nodes):
        return self._groups(nodes, self.config['panel_groups'])

    def _button(self, screen, labels, *, top=0, bottom=1, left=0, right=1):
        for label in labels:
            found = {node.bounds: node for node in screen.nodes
                     if normalize(node.text) == label
                     and left * screen.width < node.center[0] < right * screen.width
                     and top * screen.height < node.center[1] < bottom * screen.height
                     and 0 < node.bounds[3] - node.bounds[1] < screen.height * .18}
            if len(found) == 1:
                return next(iter(found.values()))
        return None

    def _read(self, visual=False):
        screen = self.read(force_ocr=visual and self.config['ocr'])
        self.guard(screen)
        return screen

    def _search_field(self, screen):
        pending = [screen.tree]
        fields = []
        while pending:
            item = pending.pop()
            if isinstance(item, list):
                pending.extend(item)
            elif isinstance(item, dict):
                attrs = item.get('attributes', item)
                if attrs.get('visible-to-user', True) in (False, 'false'):
                    continue
                pending.extend(item.get(key) for key in ('children', 'hierarchy'))
                resource = attrs.get('id', attrs.get('resource-id', ''))
                kind = attrs.get('type', attrs.get('class', ''))
                bounds = bounds_of(attrs.get('bounds', attrs.get('rect')))
                if (resource.rsplit('/', 1)[-1] == 'search_input_box' and 'EditText' in kind
                        and attrs.get('package', screen.package) == screen.package
                        and 0 < bounds[1] < bounds[3] < screen.height * .18):
                    fields.append(TextNode('', bounds))
        return fields[0] if len(fields) == 1 else None

    def _search_result(self, screen, field):
        labels = self.config['search_results']
        candidates = [n for n in screen.nodes if normalize(n.text) in labels
                      and n.bounds[1] >= field.bounds[3]
                      and n.center[1] < screen.height * .37
                      and n.center[0] < screen.width * .65
                      and 0 < n.bounds[3] - n.bounds[1] < screen.height * .13]
        if not candidates:
            return None
        target = min(candidates, key=lambda n: n.center[1])
        evidence = self.config.get('result_card_evidence', [])
        if screen.find(r'^生活号$') and evidence and not any(
                normalize(n.text) in evidence and 0 < n.center[1] - target.center[1] < screen.height * .19
                for n in screen.nodes):
            return None
        return target

    def ensure_list(self, auto_open=False):
        self.origin = self.origin or self.application.package_for(self.device.device_type)
        screen = self._read()
        return self._locate(screen, auto_open)

    def _close_landing_overlay(self):
        markers = self.config.get('landing_overlay_markers', [])
        if not markers or not self.config['ocr']:
            return False
        painted = self._read(visual=True)
        if painted.package != self.origin or not self._groups(painted.nodes, [markers]):
            return False
        close = self._button(painted, ['X', '×', '✕'], top=.16, bottom=.35, left=.8, right=.98)
        if close is None:
            raise NeedsAttention('农场推广浮层遮住任务入口，请在手机上关闭后继续')
        self.click(close)
        return True

    def _locate(self, screen, auto_open):
        deadline = self.control.now() + self.config['return_timeout'] + 2 * self.config['page_timeout']
        backs = entry_attempts = panel_attempts = 0
        typed = launched = False
        checked_overlay = False
        wait_until = 0
        while self.control.now() < deadline:
            if screen.package != self.origin:
                if not auto_open or launched:
                    break
                self.device.start_app(self.origin)
                launched = True
                self.control.wait(1.2)
                screen = self._read()
                continue
            if self._groups(screen.nodes, self.config.get('popup_groups', [])):
                close = self._button(screen, self.config['popup_close_labels'], top=.55, bottom=.94, left=.35, right=.65)
                if close:
                    self.click(close)
                    self.control.wait(.6)
                    screen = self._read()
                    continue
                raise NeedsAttention('签到提示遮住任务面板，请关闭提示后继续')
            if self.is_task_list(screen.nodes):
                if self.config.get('popup_groups') and screen.find(r'^关闭$'):
                    close = self._button(screen, self.config['popup_close_labels'], top=.55, bottom=.94, left=.35, right=.65)
                    if close:
                        self.click(close)
                        self.control.wait(.6)
                        screen = self._read()
                        continue
                return screen
            if self._groups(screen.nodes, self.config['landing_groups']):
                if not checked_overlay:
                    checked_overlay = True
                    if self._close_landing_overlay():
                        self.control.wait(.75)
                        screen = self._read()
                        continue
                target = self._button(screen, self.config['task_entry_labels'], top=.25, bottom=.95)
                if target is None and self.config['ocr']:
                    painted = self._read(visual=True)
                    if painted.package == self.origin:
                        if self.is_task_list(painted.nodes):
                            return painted
                        target = self._button(painted, self.config['task_entry_labels'], top=.25, bottom=.95)
                if target and self.control.now() >= wait_until and panel_attempts < 3:
                    if normalize(target.text) == '签到领豆' and not self.config['claim_rewards']:
                        raise NeedsAttention('当前入口是签到领豆，请在手机上打开任务面板后继续')
                    self.click(target)
                    panel_attempts += 1
                    wait_until = self.control.now() + 2
            elif auto_open and self.control.now() >= wait_until:
                field = self._search_field(screen)
                if field and not typed:
                    self.device.input_text(self.config['search_keyword'])
                    typed = True
                    wait_until = self.control.now() + 1.1
                elif field and typed:
                    target = self._search_result(screen, field)
                    if self.config['ocr'] and (target is None or target.center[1] < screen.height * .17):
                        # Heads-up notifications can cover the first search result while
                        # the underlying accessibility tree still exposes it.
                        painted = self._read(visual=True)
                        target = (self._search_result(painted, field) if painted.package == self.origin
                                  and (painted.width, painted.height) == (screen.width, screen.height) else None)
                    if target and entry_attempts < 3:
                        self.click(target)
                        entry_attempts += 1
                        wait_until = self.control.now() + 8
                else:
                    target = self._button(screen, self.config.get('native_home_labels', []), top=.12, bottom=.48)
                    if target and screen.find(r'^搜索框$|^搜索栏$') and entry_attempts == 0:
                        self.click(target)
                        entry_attempts += 1
                        wait_until = self.control.now() + 8
                        self.control.wait(.55)
                        screen = self._read()
                        continue
                    target = self._button(screen, ['搜索框', '搜索栏'], bottom=.2)
                    if target:
                        self.click(target)
                        wait_until = self.control.now() + 1
                    elif backs < min(self.config['max_back_steps'], 5):
                        self.device.go_back()
                        backs += 1
                        wait_until = self.control.now() + 2
            self.control.wait(.55)
            screen = self._read()
        raise NeedsAttention(f'未能确认{self.config["name"]}任务面板，请检查当前页面', kind='navigation')

    def recover(self, cold_start=False):
        return self.ensure_list(auto_open=True)

    def _classify(self, title, detail, texts, action, current, total):
        duration = _SECONDS.search(detail)
        seconds = int(duration[1]) if duration else 0
        if action in _DONE or current >= total and action not in _CLAIM:
            kind, reason = 'done', ''
        elif (_MANUAL.search(title + detail)
              or any(re.search(pattern, title) for pattern in self.config.get('manual_task_titles', []))):
            kind, reason = 'manual', '需要本人处理的任务'
        elif action in _CLAIM:
            kind, reason = 'claim', ''
        elif action in _BROWSE and seconds <= self.config['max_task_seconds']:
            kind, reason = 'browse', ''
        else:
            kind, reason = 'manual', '需要本人处理的入口或超出浏览时长上限'
        return seconds, kind, reason, '', ()

    def parse_tasks(self, nodes, width, height):
        if not self.is_task_list(nodes):
            return []
        sign_task = self._sign_task(nodes, width, height)
        markers = set(self.config.get('panel_headers', []))
        header = next((n for n in nodes if normalize(n.text) in markers and n.center[1] > height * .13), None)
        if header:
            nodes = [n for n in nodes if n.center[1] > header.bounds[3]]
        rows = []
        for node in nodes:
            text = normalize(node.text)
            farm = _FARM_PROGRESS.search(text)
            if farm:
                text = _FARM_PROGRESS.sub(lambda match: f'({match[1]}/{match[2]})', text, count=1)
                rows.append(TextNode(text, node.bounds, 'ui'))
            elif re.fullmatch(r'\d+/\d+', text):
                titles = [n for n in nodes if -width * .02 <= node.bounds[0] - n.bounds[2] < width * .07
                          and abs(n.center[1] - node.center[1]) < height * .013
                          and re.search(r'[\u4e00-\u9fff]', n.text)]
                if titles:
                    title = min(titles, key=lambda n: node.bounds[0] - n.bounds[2])
                    rows.append(TextNode(normalize(title.text) + '(' + text + ')', title.bounds, title.source))
            elif text not in ('(', ')'):
                rows.append(node)
        rules = TaskRules(_ACTION_RE, _PROGRESS, re.compile(r'(?!)'), frozenset(), '\0',
                          re.compile(r'(?!)'), '', self._classify)
        if self.config.get('progressless_rows'):
            if sign_task:
                rows = [node for node in rows if node.center[1] > sign_task.bounds[3]]
            rows.extend(self._implicit_progress(tuple(rows), width, height))
        tasks = parse_tasks(rows, width, height, rules=rules)
        if sign_task:
            tasks.insert(0, sign_task)
        return tasks

    def _sign_task(self, nodes, width, height):
        if not self._groups(nodes, self.config.get('sign_groups', [])):
            return None
        rule = next((n for n in nodes if normalize(n.text) == self.config['sign_heading']), None)
        if rule is None:
            return None
        actions = {n.bounds: n for n in nodes if normalize(n.text) in self.config['sign_labels'] + self.config['sign_done_labels']
                   and n.center[0] > width * .65 and abs(n.center[1] - rule.center[1]) < height * .075}
        if len(actions) != 1:
            return None
        action = next(iter(actions.values()))
        done = normalize(action.text) in self.config['sign_done_labels']
        return Task('daily-sign', '每日签到', '', action.text, action.bounds, int(done), 1,
                    kind='done' if done else 'claim')

    def _implicit_progress(self, nodes, width, height):
        """A Forest row without a counter can still advertise a time or a claim action."""
        for action in nodes:
            verb = normalize(action.text)
            if action.center[0] <= width * .6 or verb not in _BROWSE | _CLAIM | _DONE:
                continue
            near = [n for n in nodes if n.bounds[2] < action.bounds[0] + width * .015
                    and n.bounds[0] > width * .05
                    and abs(n.center[1] - action.center[1]) <= height * .043
                    and 0 < n.bounds[3] - n.bounds[1] < height * .075]
            if any(_PROGRESS.search(normalize(n.text)) for n in near):
                continue
            titles = [n for n in near if re.match(r'[\u4e00-\u9fff]', normalize(n.text))
                      and n.bounds[0] > width * .1
                      and not _SECONDS.search(normalize(n.text))
                      and normalize(n.text) not in _ACTIONS
                      and n.center[1] <= action.center[1]]
            if titles:
                title = min(titles, key=lambda n: action.center[1] - n.center[1])
                progress = '1/1' if verb in _DONE else '0/1'
                yield TextNode(f'{normalize(title.text)}({progress})', title.bounds, title.source)

    def tasks(self, screen):
        if screen.package != (self.origin or self.application.package_for(self.device.device_type)):
            return []
        tasks = super().tasks(screen)
        for task in tasks:
            if task.kind == 'browse':
                task.browse_once = not task.seconds
        return tasks

    def view_signature(self, screen):
        return tuple((task.key, task.current, task.action) for task in self.tasks(screen))

    def _refresh_after_success(self, task=None):
        screen = self._read()
        return screen if self.is_task_list(screen.nodes) else self.recover()

    def _return_to_panel(self, screen):
        for _ in range(min(self.config['max_back_steps'], 4)):
            if self.is_task_list(screen.nodes) and screen.package == self.origin:
                return screen
            if screen.package != self.origin or self._groups(screen.nodes, self.config['landing_groups']):
                break
            self.device.go_back()
            self.control.wait(.65)
            screen = self._read()
        return self._locate(screen, True)

    def _browse_confirmation(self, screen, task):
        pattern = self.config.get('browse_confirmation')
        title = self.config.get('browse_confirmation_task')
        if (not pattern or normalize(task.title) != title
                or not self.config['ocr'] or not self.is_task_list(screen.nodes)):
            return screen
        painted = self._read(visual=True)
        if painted.package != self.origin:
            return painted
        instruction = painted.find(pattern)
        target = self._button(painted, ['去完成'], top=.56, bottom=.8, left=.32, right=.68)
        if instruction and target and _SECONDS.search(normalize(instruction.text)) and not _MANUAL.search(instruction.text):
            self.click(target)
            self.control.wait(.9)
            return self._read()
        return screen

    def _check_browse_destination(self, screen):
        if screen.find(r'支付宝.{0,8}(?:想要|即将|请求)打开') and self.is_launch_prompt(screen):
            decline = self._button(screen, ['拒绝', '取消'], top=.35)
            if decline is None:
                raise NeedsAttention('支付宝请求打开其他应用，请在手机上处理')
            self.click(decline)
            raise TaskUnavailable('需要本人确认外部应用跳转，已跳过本次浏览')
        if screen.package != self.origin:
            raise TaskUnavailable('浏览跳转至其他应用，返回支付宝后继续')
        self.check_destination(screen)
        if not self.is_task_list(screen.nodes) and screen.find(_GAME_PAGE):
            raise TaskUnavailable('打开的是游戏任务，返回活动后继续')

    def execute(self, task, before):
        fresh = self._read()
        current = next((t for t in self.tasks(fresh) if t.key == task.key), None)
        if (current is None or current.kind not in ('browse', 'claim')
                or current.kind != task.kind or current.current != task.current):
            return fresh, False, False
        self.click(TextNode(current.action, current.bounds))
        self.control.wait(.9)
        screen = self._read()
        if task.kind == 'browse':
            screen = self._browse_confirmation(screen, task)
            deadline = self.control.now() + self.config['page_timeout']
            while self.control.now() < deadline:
                self._check_browse_destination(screen)
                if (not self.is_task_list(screen.nodes) and len(screen.nodes) >= 4
                        and not screen.find(r'加载中|请稍候|正在加载|正在努力加载|重新加载')):
                    break
                if self.verify(task, before, screen):
                    return screen, False, False
                self.control.wait(.6)
                screen = self._read()
            else:
                return self._return_to_panel(screen), False, False
            self.update('browsing', message=task.title)
            seconds = (max(task.seconds, self.config['default_browse_seconds']) if task.seconds else 15)
            seconds = min(self.config['max_task_seconds'], seconds)
            if self.config['scroll_browse'] and screen.find(r'滑动浏览|下滑浏览|上滑浏览|滑动\d+秒'):
                deadline = self.control.now() + seconds
                while self.control.now() < deadline:
                    self.browse_swipe(screen)
            else:
                self.control.wait(seconds)
            screen = self._read()
            self._check_browse_destination(screen)
        screen = self._return_to_panel(screen)
        for _ in range(3):
            if self.verify(task, before, screen):
                break
            self.control.wait(1)
            screen = self._read()
        return screen, False, False

    def verify(self, task, before, after, explicit=False):
        after_tasks = self.tasks(after)
        updated = next((row for row in after_tasks if row.key == task.key), None)
        if updated:
            return (updated.current > task.current or updated.kind == 'done'
                    or task.kind == 'browse' and updated.kind == 'claim')
        if not self.config.get('claim_removes_row') or task.kind != 'claim' or after.source != 'ui':
            return False
        # The Forest sheet removes a claimed row. Match the surrounding rows
        # in both frames before treating disappearance as a success.
        old = [row.key for row in self.tasks(before) if row.key != 'daily-sign']
        new = [row.key for row in after_tasks if row.key != 'daily-sign']
        if task.key not in old:
            return False
        index = old.index(task.key)
        following = old[index + 1:index + 3]
        expected = old[:index] + following
        known = set(old)
        visible_again = [key for key in new if key in known]
        return bool(following and len(expected) >= 2 and visible_again[:len(expected)] == expected)
