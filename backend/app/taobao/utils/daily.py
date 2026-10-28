"""Taobao daily pages reuse the framework loop, gestures, search and storage.

Only page vocabulary is activity-specific. No device model, resolution, account,
watcher, input method or independent service is introduced here.
"""
import hashlib
import re
from dataclasses import replace

from backend.app.common.models.task import TaskRules, parse_tasks
from backend.app.common.utils.destinations import DestinationPolicy
from backend.app.taobao.manifest import PROFILE
from backend.app.taobao.utils.actions import TaskActions
from backend.app.taobao.utils.search import SearchPolicy
from backend.app.taobao.utils.task_rules import (
    CLAIM_ACTIONS, DONE_ACTIONS, EXTERNAL_APPS, PROGRESS_RE, classify,
)
from backend.app.taobao.utils.verification import VerificationPolicy
from backend.core.errors import NeedsAttention
from backend.core.execution.activity import Activity
from backend.core.models import Task, TextNode, normalize, bounds_of


DAILY_CLAIMS = CLAIM_ACTIONS | {'领取', '签到', '立即签到', '点击签到', '点击领取', '领肥料', '收下奖励'}
ACTION_RE = re.compile(r'^(?:去完成|去浏览|去逛逛|去看看|去搜索|去观看|去领取|领取奖励|立即领取|领奖励|'
                       r'领取|签到|立即签到|点击签到|点击领取|领肥料|收下奖励|去签到|逛一逛|搜一下|'
                       r'领取任务|去答题|去邀请|去玩|已完成|已领取|明日再来|今日已签到|已签到|[0-9]{1,2}点可领)$')


class DailyActivity(SearchPolicy, DestinationPolicy, TaskActions, VerificationPolicy, Activity):
    application = PROFILE
    claim_actions = DAILY_CLAIMS
    external_apps = EXTERNAL_APPS

    @property
    def page(self):
        return self.config['daily_page']

    def classify_daily(self, title, detail, texts, action, current, total):
        if re.fullmatch(r'[0-9]{1,2}点可领', action):
            return (0, 'done', '今日已完成', '', ()) if current >= total else (
                0, 'manual', '尚未到领取时段', '', ())
        # This offer explicitly rewards browsing first; ordering is an optional
        # extra. Keep all real ordering requirements excluded, including any
        # additional requirement outside this exact, observed clause.
        optional_order = '先浏览赚肥料,下单再得肥料'
        safe_detail, safe_texts = detail, texts
        if title == '浏览全场1元淘工厂' and optional_order in normalize(detail):
            safe_detail = normalize(detail).replace(optional_order,'浏览赚肥料')
            safe_texts = [title, safe_detail]
        classification_title = title
        if (title in ('你关注的商品都可抵','看看你关注的新品动态')
                and re.search(r'浏览[0-9]+[秒sS]',safe_detail)
                and not re.search(r'关注|订阅|收藏',safe_detail)):
            classification_title = title.replace('关注','浏览')
            safe_texts = [classification_title,safe_detail]
        risk = re.search(r'下单|购买|支付|充值|开通|订阅|邀|拉人|助力|抽赏|抽奖|夺宝|游戏|授权|实名|'
                         r'关注|收藏|加购|下载|安装|施肥|浇水|兑换|提现|拍立淘|拍照|拍摄|答题|考试',
                         (classification_title+' '+safe_detail).replace('支付宝','蚂蚁平台'))
        if risk and action not in DONE_ACTIONS:
            return 0, 'manual', '需要手动操作：'+risk.group(), '', ()
        seconds, kind, reason, target, packages = classify(classification_title, safe_detail, safe_texts, action, current, total)
        # Keep payment, invitations, games and permission changes excluded even
        # when a risky task labels its right-hand action as a reward button.
        if kind == 'manual' and reason != '任务类型暂不支持':
            return seconds, kind, reason, target, packages
        if action in DONE_ACTIONS | {'已签到', '今日已签到'}:
            kind, reason = 'done', '今日已完成'
        elif action in DAILY_CLAIMS or action == '去签到' and '签到' in title:
            kind, reason = 'claim', ''
        elif action == '搜一下':
            kind = 'search'
        return seconds, kind, reason, target, packages

    def parse_tasks(self, nodes, width, height):
        if self.page.get('reward_rows') and any(re.search(r'今日速赚', n.text) for n in nodes):
            return self.reward_rows(nodes, width, height)
        # Older frozen frameworks require explicit row counters. Daily pages
        # often have none: supply a one-off task contract to the existing parser,
        # without pretending the page reported progress or counting it as success.
        row_nodes = self.row_scope(nodes, width, height)
        expanded = list(row_nodes)
        actions = [n for n in row_nodes if ACTION_RE.fullmatch(normalize(n.text)) and n.center[0] > width*.55]
        for button in actions:
            row = [n for n in row_nodes if n.bounds[0] < button.bounds[0]-width*.025
                   and abs(n.center[1]-button.center[1]) < height*.04
                   and 0 < n.bounds[3]-n.bounds[1] < height*.1
                   and not ACTION_RE.fullmatch(normalize(n.text))
                   and not re.search(r'O1CN|\.(?:png|jpg)', n.text)]
            if row and not any(PROGRESS_RE.search(n.text) for n in row):
                labels = [n for n in row if re.search(r'[\u4e00-\u9fff]', n.text)
                          and not re.fullmatch(r'[x×+¥￥]?\d+(?:金币|肥料|元宝|元)?',normalize(n.text))]
                if labels:
                    title = min(labels, key=lambda n: (n.center[1], n.bounds[0]))
                    expanded.append(TextNode(title.text+' (0/1)', title.bounds, title.source))
        rules = TaskRules(ACTION_RE, PROGRESS_RE,
                          re.compile(r'(?:浏览|停留|观看)\d+[秒sS]'),
                          frozenset(self.config['task_entry_labels']), '\0', re.compile(r'(?!)'), '',
                          self.classify_daily)
        tasks = [] if self.page.get('inline_only') else parse_tasks(expanded, width, height, rules=rules)
        # Sign-in and timed reward controls may be centred or in a grid rather
        # than a right-hand list. Scope these to this activity's confirmed page.
        for item in self.page.get('inline_claims', []):
            def allowed(node):
                excluded = item.get('exclude_left_labels')
                return not excluded or not any(
                    re.search(excluded,normalize(n.text))
                    and 0 < node.center[0]-n.center[0] < width*.3
                    and abs(node.bounds[3]-n.bounds[3]) < height*.04 for n in nodes)
            amount_text = ''
            active = next((n for n in nodes if re.fullmatch(item['action'], normalize(n.text))
                           and 0 < n.bounds[3]-n.bounds[1] < height*.22 and allowed(n)), None)
            done = next((n for n in nodes if re.fullmatch(item['done'], normalize(n.text))
                         and 0 < n.bounds[3]-n.bounds[1] < height*.22), None)
            if active is None and item.get('amount_label'):
                # OCR splits a timed reward tile's amount and action. Require
                # a positive amount immediately above the same small tile.
                for button in nodes:
                    if normalize(button.text) != '点击领取' or not allowed(button):
                        continue
                    amount = next((n for n in nodes if re.fullmatch(item['amount_label'], normalize(n.text))
                                   and 0 < button.center[1]-n.center[1] < height*.08
                                   and abs(button.center[0]-n.center[0]) < width*.07), None)
                    if amount:
                        active, amount_text = button, amount.text
                        break
            node = active or done
            if node is None:
                continue
            tasks = [t for t in tasks if t.bounds != node.bounds]
            key = hashlib.sha256(item['title'].encode()).hexdigest()[:16]
            tasks.append(Task(key, item['title'], amount_text or normalize(node.text), normalize(node.text), node.bounds,
                              current=0 if active else 1, kind='claim' if active else 'done'))
        return tasks

    @staticmethod
    def row_scope(nodes, width, height):
        """Exclude exposed sidebar ads only when task titles prove one column."""
        actions = [n for n in nodes if ACTION_RE.fullmatch(normalize(n.text))]
        anchors = [n for n in nodes if PROGRESS_RE.search(n.text)
                   and n.bounds[3]-n.bounds[1] < height*.1
                   and any(abs(n.center[1]-a.center[1]) < height*.04
                           and n.bounds[0] < a.bounds[0]-width*.025 for a in actions)]
        lefts = [n.bounds[0] for n in anchors]
        if len(lefts) < 2 or max(lefts)-min(lefts) > width*.08:
            return nodes
        left = min(lefts)-width*.03
        return [n for n in nodes if n.bounds[0] >= left]

    def reward_rows(self, nodes, width, height):
        """Current coin panel uses clickable reward circles instead of action words."""
        more = next((n for n in nodes if normalize(n.text)=='更多专享福利'),None)
        extra_nodes = [n for n in nodes if not re.search(r'今日速赚',n.text)
                       and (more is None or n.center[1]>more.bounds[3])]
        extra = self.parse_tasks(extra_nodes,width,height)
        if more:
            nodes = [n for n in nodes if n.center[1]<more.bounds[1]]
        rewards = [n for n in nodes if re.fullmatch(r'\+\d+|已领取|已完成', normalize(n.text))
                   and 0 < n.bounds[3]-n.bounds[1] < height*.08]
        header = next(n for n in nodes if '今日速赚' in n.text)
        titles = [n for n in nodes if (PROGRESS_RE.search(n.text) or re.search(r'任务到访得金币|逛清单|搜一搜|淘金币趣味课堂|^去.{2,20}|拍立淘', normalize(n.text)))
                  and n.bounds[0] >= header.bounds[0]-width*.02
                  and len(normalize(n.text)) < 50 and n.bounds[3]-n.bounds[1] < height*.05]
        tasks=[]
        for title in titles:
            text=normalize(title.text);progress=PROGRESS_RE.search(text)
            current,total=map(int,progress.groups()) if progress else (0,1)
            caption=PROGRESS_RE.sub('',text)
            nearby=[n for n in rewards if n.center[0]>title.center[0]
                    and abs(n.center[1]-title.center[1]) < height*.04]
            if not nearby:
                # Completed rows move below pending tasks and lose the reward
                # circle, but their explicit (n/n) remains real progress proof.
                if progress and current >= total:
                    seconds,kind,reason,target,packages=self.classify_daily(
                        caption,'',[caption],'已完成',current,total)
                    old=next((t for t in self._known_tasks.values()
                              if t.title==caption and t.total==total),None)
                    seconds=seconds or (old.seconds if old else 0)
                    key=hashlib.sha256(caption.encode()).hexdigest()[:16]
                    tasks.append(Task(key,caption,'','已完成',title.bounds,current,total,
                                      seconds,kind,reason,target,packages))
                continue
            reward=min(nearby,key=lambda n: abs(n.center[1]-title.center[1]))
            margin = .1 if '逛清单' in caption else .035
            detail_nodes=[n for n in nodes if title.bounds[1] <= n.center[1] <= title.bounds[3]+height*margin
                          and n.bounds[0]>=title.bounds[0]-width*.02 and n.bounds[2]<reward.bounds[0]
                          and n is not title and not PROGRESS_RE.search(n.text)]
            detail=' '.join(n.text for n in detail_nodes if len(n.text)<60)+' '+reward.text
            action='领取奖励' if '到访' in caption else '去完成'
            if normalize(reward.text) in ('已领取','已完成'):action='已完成'
            seconds,kind,reason,target,packages=self.classify_daily(caption,detail,[caption,detail],action,current,total)
            bounds=reward.bounds
            if '逛清单' in caption:
                cards=[n for n in detail_nodes if n.bounds[3]-n.bounds[1] > height*.045]
                if cards:bounds=min(cards,key=lambda n:n.bounds[0]).bounds
            key=hashlib.sha256(caption.encode()).hexdigest()[:16]
            tasks.append(Task(key,caption,detail,action,bounds,current,total,seconds,kind,reason,target,packages))
        # The quick section can mirror a normal row below it. Keep one semantic
        # task, while retaining the lower section's other independent offers.
        for task in extra:
            prior = next((t for t in tasks if normalize(t.title)==normalize(task.title)
                          and t.total==task.total and t.seconds==task.seconds),None)
            if prior is None:
                tasks.append(task)
            elif task.current>prior.current or task.kind=='done':
                tasks[tasks.index(prior)] = task
        return tasks

    def is_task_list(self, nodes):
        if self.page.get('promotion_markers') and any(
                re.search(self.page['promotion_markers'], normalize(n.text))
                or self.page.get('inline_only') and normalize(n.text) in ('X','×','✕','✖') for n in nodes):
            return False
        support = self.page.get('list_support_markers')
        if support and not any(re.search(support, normalize(n.text)) for n in nodes):
            return False
        return any(re.search(self.page['list_markers'], normalize(n.text))
                   and len(normalize(n.text)) < 90 for n in nodes)

    def entry_node(self, screen, labels):
        matches = [n for n in screen.nodes for label in labels
                   if re.fullmatch(r'(?:去|进入|回)?'+re.escape(label)+r'[>〉按钮]*', normalize(n.text))
                   and 0 <= n.bounds[0] < n.bounds[2] <= screen.width
                   and 0 <= n.bounds[1] < n.bounds[3] <= screen.height
                   and (screen.geometry is None or screen.geometry.contains(*n.center))
                   and 0 < n.bounds[2]-n.bounds[0] < screen.width*.8
                   and 0 < n.bounds[3]-n.bounds[1] < screen.height*.2]
        return min(matches, key=lambda n: (n.source != 'ui', -(n.bounds[3]-n.bounds[1]))) if matches else None

    def official_activity_entry(self, screen):
        if screen.find(r'^历史搜索$|^搜索发现$|^猜你想搜$'):
            return None
        banner = self.entry_node(screen, self.page.get('official_entry_labels', []))
        if banner:
            button = next((n for n in screen.nodes if normalize(n.text) in ('点击进入>','点击进入〉','点击进入')
                           and 0 < n.center[1]-banner.center[1] < screen.height*.12), None)
            if button:
                return button
        search_buttons = self._submit_buttons(screen)
        if search_buttons:
            # The phone's search suggestions expose an official channel shortcut
            # directly below the edit. Coloured query fragments in lower
            # suggestions must never be mistaken for an activity card.
            below_edit = min(n.bounds[3] for n in search_buttons)+screen.height*.008
            for title in screen.nodes:
                if (normalize(title.text) not in self.config['entry_labels']
                        or not below_edit < title.center[1] < screen.height*.4):
                    continue
                button = next((n for n in screen.nodes
                               if re.fullmatch(r'(?:去看看|去查看|进入|点击进入)[>›〉》]?',normalize(n.text))
                               and n.center[0]>screen.width*.7
                               and abs(n.center[1]-title.center[1]) < screen.height*.035
                               and 0<n.bounds[2]-n.bounds[0]<screen.width*.3),None)
                if button:
                    return button
            return None
        # Homepage/channel icons are handled only after their page is confirmed
        # by navigate(). A product's discount badge can also say 淘金币.
        return None

    def search_results_ready(self, screen):
        if super().search_results_ready(screen):
            return True
        # Daily coin promotions can open the reward product grid directly even
        # when their rotating task caption says "search". There is no input bar.
        if screen.find(r'^历史搜索$|^搜索发现$|^猜你想搜$|^搜索推荐$'):
            return False
        counter = self.page.get('browse_counter')
        def banner(pattern):
            return any(n.center[1]<screen.height*.25 and re.search(pattern,normalize(n.text))
                       for n in screen.nodes)
        reward_grid = (banner(self.page.get('search_page_markers',r'^搜索有惊喜$'))
                       and banner(self.page.get('search_reward_markers',r'浏览得奖励'))
                       and self._submit_buttons(screen))
        if not (reward_grid or counter and screen.find(counter)):
            return False
        products = [n for n in screen.nodes if re.search(r'[¥￥]\d|金币已抵|金币热卖价', normalize(n.text))]
        return len(products) >= 2

    @staticmethod
    def destination_ready(screen):
        # A white loading frame still contains a clock, network and battery.
        # Those OCR nodes cannot prove that the destination or its form loaded.
        return any(screen.height*.1<n.center[1]<screen.height*.97 for n in screen.nodes)

    @staticmethod
    def homepage_visible(screen):
        return all(any(label in normalize(n.text) and n.center[1] > screen.height*.8
                       for n in screen.nodes) for label in ('我的淘宝', '购物车'))

    def read_page(self, visual=False):
        screen = self.read(force_ocr=bool(visual and self.config['ocr']))
        self.guard(screen)
        manual = self.page.get('manual_entry_markers')
        if manual and screen.find(manual):
            reason = self.page['manual_entry_message']
            saved = self.report_failure(reason, phase='attention')
            raise NeedsAttention(reason, evidence_saved=saved)
        return screen

    def read(self, force_ocr=False):
        task = self._active_task
        browsing = self._phase in ('browsing','searching') and task and task.kind in ('browse', 'search')
        force_ocr = bool(force_ocr or browsing and self.config['ocr'])
        try:
            screen = super().read(force_ocr=force_ocr)
        except Exception as exc:
            # Some HyperOS WebViews overflow the accessibility dumper. A fresh
            # screenshot still passes the same foreground/instance guards.
            if (force_ocr or not self.config['ocr'] or not (
                    type(exc).__name__ == 'RPCStackOverflowError' or isinstance(exc,TimeoutError))):
                raise
            self.update(message='控件树层级异常，改用本地画面识别')
            screen = super().read(force_ocr=True)
        if (screen.source == 'ui' and self.config['ocr'] and not self.page.get('inline_only')
                and self.is_task_list(screen.nodes)):
            screen = super().read(force_ocr=True)
        if (task and task.kind in ('browse','search') and self._phase in ('opening','browsing')
                and (self.page.get('browse_counter') or self.page.get('browse_counter_context'))
                and screen.source.startswith('ocr') and self.browse_countdown(screen) is None):
            # Full-height OCR may omit the coloured countdown banner on tablets.
            # Reuse a crop of this exact frame, with coordinates mapped by core.
            enriched = self.reader.enrich_regions(screen, [(0,0,screen.width,round(screen.height*.17))])
            enriched.geometry, enriched.obscured = screen.geometry, screen.obscured
            screen = enriched
        return screen

    def browse_countdown(self, screen):
        pattern = self.page.get('browse_counter')
        node = screen.find(pattern) if pattern else None
        match = re.search(pattern, normalize(node.text)) if node else None
        if match:
            return int(match[1])
        context = self.page.get('browse_counter_context')
        if not context:
            return None
        # Some reward badges paint a separate circular number before their
        # caption. Match that small top-row region, never arbitrary prices.
        captions = [n for n in screen.nodes if re.search(context,normalize(n.text))
                    and n.center[0]>screen.width*.5 and n.center[1]<screen.height*.2]
        for caption in captions:
            values = [n for n in screen.nodes if re.fullmatch(r'[0-9]{1,2}',normalize(n.text))
                      and 0<caption.bounds[0]-n.center[0]<screen.width*.13
                      and abs(n.center[1]-caption.center[1])<screen.height*.025
                      and n.bounds[2]-n.bounds[0]<screen.width*.1]
            if len(values)==1:
                return int(normalize(values[0].text))
        return None

    def fill_search_field(self, query, edit):
        if self.device.device_type != 'uiautomator2':
            return super().fill_search_field(query, edit)
        screen = self.read_page(visual=True)
        if screen.package != self.origin:
            raise NeedsAttention('搜索输入前页面已离开所选应用，已停止输入')
        if not (screen.find(r'^历史搜索$|^搜索发现$|^搜索有福利$|^猜你想搜$') and self._submit_buttons(screen)):
            raise NeedsAttention('无法重新确认搜索输入框，已停止输入')
        box = bounds_of(edit.get('bounds', edit.get('rect')))
        if not (0 <= box[0] < box[2] <= screen.width and 0 <= box[1] < box[3] <= screen.height*.3):
            raise NeedsAttention('搜索输入框位置无法确认，已停止输入')
        self.click(TextNode('',box))
        self.control.wait(.15)
        self.control.check()
        ascii_input = getattr(self.device,'input_ascii',None)
        if callable(ascii_input) and normalize(query) != normalize(self.config['search_keyword']):
            # Letter key events can remain in the user's IME composition buffer;
            # tapping Search then discards them and submits an empty field.
            # Digits commit directly without changing the user's input method.
            token = query if re.fullmatch(r'[0-9]{1,32}',query) else '123'
            ascii_input(token)
        else:
            # Activity navigation needs its actual Unicode name. Locate only
            # the confirmed focused field, avoiding a global EditText count.
            self.device(className='android.widget.EditText', focused=True).set_text(query, timeout=.25)

    def search_edit(self, screen, edits):
        edit = super().search_edit(screen, edits)
        if edit is not None:
            return edit
        if edits or not (callable(getattr(self.device,'input_ascii',None)) and self._submit_buttons(screen)
                and screen.find(r'^历史搜索$|^搜索发现$|^搜索有福利$')):
            return None
        hint = next((n for n in screen.nodes if normalize(n.text) in ('搜索宝贝','搜索商品')
                     and n.center[1]<screen.height*.3 and n.center[0]<screen.width*.7),None)
        return {'bounds':hint.bounds,'text':'','source':'ocr'} if hint else None

    def search_tree(self):
        try:
            return super().search_tree()
        except Exception as exc:
            if type(exc).__name__ != 'RPCStackOverflowError' and not isinstance(exc,TimeoutError):
                raise
            # Submitting can still use the freshly painted right-hand button.
            return None

    @staticmethod
    def browse_observation_interval(scrolling):
        # Keep scrolling between expensive observations. Every gesture still
        # checks pause/stop and the device's selected application instance.
        return 3 if scrolling else 1

    def dismiss_overlay(self, screen):
        close = screen.find(r'^(?:关闭|关闭按钮|取消|以后再说|暂不|下次再说|我知道了|×|✕|✖|X)$')
        # Never accept a generic confirm/open/withdrawal action. Authentication
        # was checked by guard before looking for a dismiss control.
        if (close and normalize(close.text) in ('X','×','✕','✖')
                and not (self.promotion_visible(screen) or screen.find(r'提现机会|现金提现|恭喜|签到成功|奖励'))):
            return False
        if close and close.bounds[3]-close.bounds[1] < screen.height*.12:
            self.click(close)
            self.control.wait(.4)
            return True
        if self.promotion_visible(screen):
            # Decorative close icons may not be readable. Dismiss the known
            # promotion with Back rather than clicking its coupon/withdraw CTA.
            self.control.check()
            self.device.go_back()
            self.control.wait(.4)
            return True
        return False

    def promotion_visible(self, screen):
        return bool(self.page.get('promotion_markers') and (
            screen.find(self.page['promotion_markers']) or self.page.get('inline_only')
            and screen.find(r'^(?:X|×|✕|✖)$')))

    def ensure_list(self, auto_open=False):
        screen = self.navigate(auto_open=auto_open)
        if not auto_open or not self.page.get('reset_list_scroll'):
            return screen
        # A resumed session may already be at the bottom of a scrollable modal.
        # Start the full queue at its top, using only this page's paired rows.
        # Recovery intentionally keeps its viewport so verification can compare
        # the task just executed without reopening or scrolling the whole list.
        seen = set()
        for _ in range(min(4, self.config['max_scroll_pages'])):
            self.control.check()
            tasks = self.parse_tasks(screen.nodes, screen.width, screen.height)
            signature = tuple((t.key,t.current,t.action) for t in tasks)
            if len(tasks) < 2 or signature in seen:
                break
            seen.add(signature)
            centers = sorted((t.bounds[1]+t.bounds[3])/2 for t in tasks)
            span = centers[-1]-centers[0]
            if span < screen.height*.12:
                break
            xs = sorted((t.bounds[0]+t.bounds[2])/2 for t in tasks)
            x = round(xs[len(xs)//2])
            self.device.swipe(x, round(centers[0]+span*.1),
                              x, round(centers[-1]-span*.1), duration=.45)
            self.control.wait(.5)
            fresh = self.read_page(visual=True)
            if not self.is_task_list(fresh.nodes):
                return self.navigate(auto_open=True)
            screen = fresh
        return screen

    def recover(self, cold_start=False):
        return self.navigate(auto_open=True, recovering=True)

    def availability_reason(self, task, check_installed=False):
        if task.key in getattr(self, '_settled_claims', set()):
            return '本轮已核验领取，下一轮再检查'
        return super().availability_reason(task, check_installed)

    def navigate(self, *, auto_open, recovering=False):
        self.update('returning' if recovering else 'locating', message='进入'+self.config['name'])
        package = self.origin or self.application.package_for(self.device.device_type)
        self.origin = package
        deadline = self.control.now() + self.config['return_timeout'] + self.config['page_timeout']*3
        hard_deadline = deadline + self.config['return_timeout'] + self.config['page_timeout']*3
        taps, backs, channels, searched, restarts = {}, 0, False, False, 0
        viewport_returns = 0
        loading_until = self.control.now()+self.config['page_timeout']
        if auto_open and recovering and self.device.current_app().get('package') != package:
            # A launch-confirmation window can block app_start's foreground
            # proof. Unwind the external stack first, retaining the same user.
            for _ in range(self.config['max_back_steps']):
                screen = self.read_page(visual=True)
                if (screen.package==package or 'launcher' in screen.package.lower()
                        or screen.package.endswith('.home')):
                    break
                self.device.go_back()
                backs += 1
                self.control.wait(.5)
            if self.device.current_app().get('package') != package:
                self.device.go_home()
                self.control.wait(.4)
                desktop=self.read_page(visual=True)
                if 'launcher' not in desktop.package.lower() and not desktop.package.endswith('.home'):
                    raise NeedsAttention('逐层返回后未确认桌面，已停止恢复，请检查当前窗口',kind='navigation')
        if auto_open and self.device.current_app().get('package') != package:
            self.device.start_app(package)
            self.control.wait(.8)
        screen = self.read_page()
        while True:
            self.control.check()
            native_frame = screen if screen.source == 'ui' else None
            if native_frame is not None and self.config['ocr']:
                # Embedded pages can retain the channel grid behind a canvas
                # or a delayed popup. Confirm the current painted page before
                # using any entry; only confirmed homepage icons reuse native
                # parent bounds below.
                screen = self.read_page(visual=True)
            if self.is_launch_prompt(screen):
                # A previous interrupted jump may leave its modal over a still
                # readable task list. Navigation unwinds it before scheduling;
                # only execute() confirms a jump for its own active task.
                if backs >= self.config['max_back_steps'] or self.control.now() >= hard_deadline:
                    break
                self.device.go_back()
                backs += 1
                self.control.wait(.5)
                screen = self.read_page()
                continue
            main_view = self.page.get('main_view_markers')
            if (main_view and screen.find(self.page['landing_markers']) and not screen.find(main_view)
                    and not self.promotion_visible(screen)
                    and sum(bool(re.search(r'[¥￥]\d',normalize(n.text))) for n in screen.nodes)>=2):
                if viewport_returns >= 4 or self.control.now() >= hard_deadline:
                    break
                viewport_returns += 1
                self.device.swipe(round(screen.width*.5),round(screen.height*.3),
                                  round(screen.width*.5),round(screen.height*.85),duration=.5)
                self.control.wait(.5)
                screen = self.read_page(visual=True)
                continue
            if self.is_task_list(screen.nodes):
                # A WebView can leave hidden controls in the hierarchy. Confirm
                # the painted task header before accepting the list.
                painted = self.read_page(visual=True) if self.config['ocr'] and screen.source == 'ui' else screen
                if self.is_task_list(painted.nodes):
                    expand = self.page.get('expand_label')
                    button = painted.find(expand, exact=True) if expand else None
                    if button and painted.find(self.page['expand_context']) and not taps.get('expanded'):
                        taps['expanded'] = True
                        self.click(button)
                        self.control.wait(.5)
                        screen = self.read_page(visual=True)
                        continue
                    if self.promotion_visible(painted):
                        if self.dismiss_overlay(painted):
                            screen = self.read_page()
                            continue
                        raise NeedsAttention('签到推广遮挡页面，未找到可确认的关闭按钮，请本人关闭后继续')
                    # A native reward tile includes amount + label in one
                    # accessible control; OCR often splits those into two boxes.
                    if (self.page.get('inline_only') and native_frame is not None
                            and self.parse_tasks(native_frame.nodes,native_frame.width,native_frame.height)):
                        return native_frame
                    return painted
                screen = painted
            if self.control.now() >= hard_deadline:
                break
            if self.dismiss_overlay(screen):
                deadline = min(hard_deadline, max(deadline, self.control.now()+self.config['page_timeout']))
                screen = self.read_page()
                continue
            node = self.official_activity_entry(screen) if screen.package == package else None
            landing = bool(screen.find(self.page['landing_markers']))
            if node is None and landing and screen.package == package:
                if screen.source == 'ui' and self.config['ocr']:
                    screen = self.read_page(visual=True)
                    continue
                node = self.entry_node(screen, self.config['task_entry_labels'])
            if (node is None and auto_open and screen.package == package
                    and (self.homepage_visible(screen) or screen.find('全部频道', exact=True))
                    and not self.search_results_ready(screen)
                    and not screen.find(r'^历史搜索$|^搜索发现$|^猜你想搜$')):
                node = self.entry_node(screen, self.config['entry_labels'])
                if native_frame is not None and self.homepage_visible(screen):
                    native = self.entry_node(native_frame, self.config['entry_labels'])
                    if (native and (native_frame.width,native_frame.height)==(screen.width,screen.height)
                            and (screen.geometry is None or screen.geometry.contains(*native.center))):
                        node = native
            if node is not None:
                key = (normalize(node.text), round(node.center[0]/screen.width, 2), round(node.center[1]/screen.height, 2))
                if taps.get(key, 0) < 2:
                    taps[key] = taps.get(key, 0)+1
                    self.update(message='打开'+normalize(node.text)+'，等待页面反馈')
                    self.click(node)
                    loading_until = self.control.now()+self.config['page_timeout']
                    deadline = min(hard_deadline, max(deadline, loading_until))
                    self.control.wait(1)
                    screen = self.read_page()
                    continue
            if self.control.now() >= deadline:
                break
            if len(screen.nodes) < 8 and self.control.now() < loading_until:
                self.control.wait(.5)
                screen = self.read_page()
                continue
            if not screen.source.startswith('ocr') and self.config['ocr']:
                screen = self.read_page(visual=True)
                continue
            if auto_open and self.homepage_visible(screen):
                native = self.read_page() if screen.source.startswith('ocr') else screen
                more = self.entry_node(native, ['全部频道'])
                if more and not channels:
                    channels = True
                    self.update(message='在首页全部频道中查找活动入口')
                    self.click(more)
                    self.control.wait(.7)
                    screen = self.read_page()
                    continue
                if not searched and native.find(r'^搜索栏$|^搜索框$'):
                    searched = True
                    self.open_home_search(native)
                    screen = self.read_page()
                    continue
            if not auto_open:
                self.control.wait(.4)
            elif backs < self.config['max_back_steps']:
                self.device.go_back()
                backs += 1
                self.control.wait(.5)
            elif restarts < 1 and not self._task_restart_used:
                self.device.go_home()
                self.control.wait(.4)
                desktop = self.read_page()
                if 'launcher' not in desktop.package.lower() and not desktop.package.endswith('.home'):
                    break
                self._task_restart_used = True
                self.device.restart_app(package)
                restarts += 1
                backs, channels, searched = 0, False, False
                taps.clear()
                self.control.wait(.8)
            else:
                break
            screen = self.read_page()
        reason = '未识别到'+self.config['name']+'的任务页，已停止，请检查入口是否变化'
        saved = self.report_failure(reason, phase='returning' if recovering else 'locating')
        raise NeedsAttention(reason, kind='navigation', evidence_saved=saved)

    def execute(self, task, before):
        if task.kind != 'claim':
            return super().execute(task, before)
        if self.page.get('inline_only') and self.config['ocr']:
            painted = self.read_page(visual=True)
            if self.promotion_visible(painted):
                if not self.dismiss_overlay(painted):
                    raise NeedsAttention('签到推广遮挡页面，请本人关闭后继续')
                before = self.recover()
                task = next((t for t in self.tasks(before) if t.key == task.key), task)
        self._active_task = task
        self.update('opening', current_task=task.title, message='领取并核对奖励状态')
        from backend.core.models import TextNode
        self.click(TextNode(task.action, task.bounds))
        deadline = self.control.now()+self.config['page_timeout']
        screen, explicit = before, False
        while self.control.now() < deadline:
            self.control.wait(.5)
            screen = self.read_page()
            if self.config['ocr'] and screen.source == 'ui' and (self.page.get('inline_only') or not self.is_task_list(screen.nodes)):
                screen = self.read_page(visual=True)
            browse_reward = self.page.get('browse_reward')
            if browse_reward:
                prompt = screen.find(browse_reward['prompt'])
                button = screen.find(browse_reward['button'], exact=True)
                if prompt and button:
                    seconds = int(re.search(browse_reward['prompt'], normalize(prompt.text))[1])
                    self.click(button)
                    self.control.wait(.6)
                    entry_deadline = self.control.now()+self.config['page_timeout']
                    while self.control.now() < entry_deadline:
                        destination = self.read_page(visual=True)
                        if (len(destination.nodes) >= 2 and not self.promotion_visible(destination)
                                and not self.is_task_list(destination.nodes)):
                            after, _, cross = self.browse_destination(replace(task, kind='browse', seconds=seconds), destination)
                            return after, False, cross
                        self.control.wait(.4)
                    continue
            explicit = bool(screen.find(self.page.get('success_markers', r'签到成功|领取成功|奖励已到账')))
            if self.is_task_list(screen.nodes):
                updated = next((t for t in self.tasks(screen) if t.key == task.key), None)
                if updated and (updated.kind == 'done' or updated.current > task.current):
                    return screen, True, False
                if self.reward_balance_increased(task, before, screen):
                    return screen, True, False
                if updated is None and self.page.get('reward_rows'):
                    painted = self.read_page(visual=True)
                    if self.coin_balance_increased(task, before, painted):
                        return painted, True, False
                if explicit:
                    return screen, True, False
                continue
            reward = None if self.is_task_list(screen.nodes) else screen.find(r'^(?:收下奖励|开心收下|领取奖励|知道了|我知道了)$')
            if reward:
                self.click(reward)
                self.control.wait(.4)
                return self.recover(), explicit, False
            if self.dismiss_overlay(screen):
                return self.recover(), explicit, False
        return self.recover(), explicit, False

    @staticmethod
    def coin_balance_increased(task, before, after, *, exact=False):
        def balance(screen):
            for node in screen.nodes:
                if node.center[1] < screen.height*.1:
                    match=re.search(r'抵(\d+)\.(\d{2})元',normalize(node.text))
                    if match:return int(match[1])*100+int(match[2])
            header = next((n for n in screen.nodes if normalize(n.text)=='淘金币'
                           and n.center[1]<screen.height*.1),None)
            if header:
                amount = next((n for n in screen.nodes if re.fullmatch(r'[0-9]{3,}',normalize(n.text))
                               and n.bounds[0]>header.bounds[2]
                               and abs(n.center[1]-header.center[1])<screen.height*.02
                               and n.bounds[2]-n.bounds[0]<screen.width*.3),None)
                if amount:return int(normalize(amount.text))
            return None
        old,new=balance(before),balance(after)
        amount=re.search(r'\+(\d+)',task.detail)
        if old is None or new is None or amount is None:
            return False
        return new-old==int(amount[1]) if exact else new-old>=int(amount[1])

    def reward_balance_increased(self, task, before, after):
        if self.page.get('reward_rows'):
            return self.coin_balance_increased(task, before, after)
        increasing = bool(self.page.get('earned_reward_balance'))
        pattern = self.page.get('earned_reward_balance') or self.page.get('remaining_reward_balance')
        if not pattern:
            return False
        def balance(screen):
            nodes = [n for n in screen.nodes if n.center[1] < screen.height*(.3 if increasing else .2)]
            for text in [normalize(n.text) for n in nodes]:
                match = re.search(pattern,text)
                if match:
                    return int(match[1])
            # OCR may split the explicit balance prefix and its value. Join
            # only a single top row, never different rows or product content.
            for node in nodes:
                row = sorted((n for n in nodes if abs(n.center[1]-node.center[1]) < screen.height*.012),
                             key=lambda n:n.bounds[0])
                match = re.search(pattern,normalize(''.join(n.text for n in row)))
                if match:
                    return int(match[1])
            return None
        old, new = balance(before), balance(after)
        amount = re.search(r'\+(\d+)', task.detail)
        if old is None or new is None or amount is None:
            return False
        return (new-old if increasing else old-new) >= int(amount[1])

    def verify(self, task, before, after, explicit=False):
        if not self.is_task_list(after.nodes):
            return False
        current = next((t for t in self.tasks(after) if t.key == task.key), None)
        progress = bool(current and (current.current > task.current or current.kind == 'done'
                                     or task.kind != 'claim' and current.kind == 'claim'))
        if task.kind == 'claim':
            success = bool(self.is_task_list(after.nodes) and (
                progress or self.reward_balance_increased(task, before, after) or explicit and current is None))
            if success and self.page.get('inline_only'):
                self._settled_claims = getattr(self, '_settled_claims', set()) | {task.key}
            return success
        if progress:
            return True
        if (current is None and self.page.get('reward_rows')
                and self.coin_balance_increased(task,before,after,exact=True)):
            return True
        return bool(self.is_task_list(after.nodes) and VerificationPolicy.verify(self, task, before, after, explicit))
