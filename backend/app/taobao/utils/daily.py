"""Taobao daily pages reuse the framework loop, gestures, search and storage.

Only page vocabulary is activity-specific. No device model, resolution, account,
watcher, input method or independent service is introduced here.
"""
import copy
import hashlib
import logging
import re
import time
from dataclasses import replace

from backend.app.common.models.task import TaskRules, parse_tasks
from backend.app.common.utils.destinations import DestinationPolicy
from backend.app.taobao.manifest import PROFILE
from backend.app.taobao.utils.actions import TaskActions
from backend.app.taobao.utils.rescan import RescanPolicy
from backend.app.taobao.utils.search import SearchPolicy
from backend.app.taobao.utils.task_rules import (
    CLAIM_ACTIONS, DONE_ACTIONS, EXTERNAL_APPS, PROGRESS_RE, classify,
)
from backend.app.taobao.utils.verification import VerificationPolicy
from backend.core.errors import NeedsAttention
from backend.core.execution.activity import Activity
from backend.core.models import Task, TextNode, normalize, bounds_of

logger = logging.getLogger('backend.execution')


DAILY_CLAIMS = CLAIM_ACTIONS | {'领取', '签到', '立即签到', '点击签到', '点击领取', '领肥料', '收下奖励'}
ACTION_RE = re.compile(r'^(?:去完成|去浏览|去逛逛|去看看|去看一看|看一看|点一点|去瞧瞧|瞧瞧|立即看看|'
                       r'去查看|立即查看|去体验|去搜索|去观看|去领取|去领币|去领钱|去领现金|'
                       r'去抢|立即抢|去参与|去下单|去逛逛领|领取奖励|立即领取|领奖励|立即领|马上领|去领|'
                       r'领取|签到|立即签到|点击签到|点击领取|领肥料|收下奖励|去签到|逛一逛|搜一下|'
                       r'领取任务|去答题|去邀请|去玩|已完成|已领取|明日再来|今日已签到|已签到|[0-9]{1,2}点可领)$')

# 标题只说“这个入口叫什么”，真正说明要不要动手的是它下方那行小字
# （点击去逛 / 浏览5秒 / 点击3个商品）。识别时两者必须一起看，否则
# “酒店超抵日至高5%”这类没有关键词的标题会被整行丢掉。
ACTIONABLE_DETAIL_RE = re.compile(
    r'点击|浏览\s*\d+\s*[秒sS个]|观看\s*\d+\s*[秒sS]|停留\s*\d+\s*[秒sS]|'
    r'逛一逛|逛逛|去逛|看一看|搜一搜|搜索|滑动|下滑|查看')
# 付款、拉人、装包、授权这类动作即使小字写着“点击去逛”也不能代做。
HARD_RISK_RE = re.compile(r'下单|购买|支付|充值|开通|订阅|邀|拉人|助力|授权|实名|'
                          r'下载|安装|施肥|浇水|兑换|提现|拍照|拍摄|答题|考试')
# 夺宝、抽奖、小游戏、关注收藏这类词只是入口的名字；小字明确写着
# “点击去逛 / 浏览N秒”时，实际动作仍然只是打开看一眼，可以做。
SOFT_RISK_RE = re.compile(r'抽赏|抽奖|夺宝|游戏|关注|收藏|加购|拍立淘|'
                          r'浏览\s*\d+\s*个|逛\s*\d+\s*个|\d+\s*件商品')
# 应用图标里印着应用名，OCR 会把图标读成一行短文本；它永远不是任务标题，
# 真正的标题在同一行的右边。
APP_ICON_LABELS = ({name for name, _ in EXTERNAL_APPS}
                   | {'头条', '头条极速版', '今日头条', '西瓜视频', '抖音极速版', '快手极速版'})
# 真正的验证页一定会出现这些动作词；只撞到“滑块”“验证”这类名词不算。
CHALLENGE_RE = re.compile(r'拖动|拖放|拖到|按住|请完成验证|安全验证|验证码|人机|行为验证|'
                          r'划一划|拼图|滑块验证|操作.{0,4}频繁|风控|存在风险|'
                          r'访问被拒绝|安全中心|身份核验')
# 核心 guard 的词表（见 backend/core/execution/guard.py）。它把“滑块”这种名词
# 也算作验证页，直播间横幅上的“导轨滑块/窗轨滑块”因此天天误伤。
CORE_STOP_RE = re.compile(r'滑块|拖动.{0,8}验证|安全验证|请完成验证|验证码|人机验证|'
                          r'操作过于频繁|账号存在风险|访问被拒绝|'
                          r'将满足描述的所有图片拖放到指定区域|短信登录|验证码登录|'
                          r'登录即同意|请先登录|立即登录|授权登录|欢迎登录|其他方式登录|'
                          r'扫一扫快速登录|确认付款|支付密码|确认支付')
# 真拦截：页面上必须出现“要动手做一件人才能做的事”的证据，才停止整轮。
# 只撞到“滑块/验证/登录”这类名词一律不算——这正是之前整轮被误停的原因。
HARD_STOP_RE = re.compile(r'将满足描述的所有图片拖放到指定区域|拖放.{0,10}图片|'
                          r'拖动.{0,10}(?:验证|拼图)|请拖动|请依次点击|按顺序点击|点击下图中|'
                          r'请完成下列验证|滑块验证|拖动滑块|按住滑块|拼图验证|'
                          r'人机验证|行为验证|安全验证|验证码|'
                          r'短信登录|验证码登录|登录即同意|请先登录|授权登录|欢迎登录|'
                          r'其他方式登录|扫一扫快速登录|确认付款|支付密码|确认支付|'
                          r'操作过于频繁|账号存在风险|访问被拒绝|安全中心|身份核验')
# 直播间页面自带的固定话术：用它判断“当前停在视频流里”，比看任务标题可靠
# （“好物沉浸看”这类标题里并没有“直播”两个字）。
LIVE_PAGE_RE = re.compile(r'正在直播|\d+\s*人观看|说点什么|直播间|点赞')
# 弹窗的关闭按钮：只认“关掉这层浮层”的写法。取消 / 残忍离开 / 确认 一律不点，
# 它们会把正在做的任务取消掉或替人做决定。
POPUP_CLOSE_RE = re.compile(r'^(?:×|✕|✖|╳|㐅|X|x|关闭|关闭弹窗|关闭窗口|关闭浮层|'
                            r'我知道了|知道了|知道啦|以后再说|稍后再说|稍后|'
                            r'暂不开启|暂不需要|暂不|不感兴趣|跳过|下次再说|不再提醒|'
                            r'点此关闭|点击关闭|收起)$')
# 弹窗的典型内容：抽到券、抢到红包、任务完成、入会提醒……出现这些字且页面上
# 有关闭按钮时，基本可以确定是浮层挡在页面上。
POPUP_HINT_RE = re.compile(r'恭喜|抢到|获得|奖励|红包|优惠券|折扣券|入会|权益|'
                           r'开通|绑定|升级|活动规则|邀请|已领取|翻倍|提醒|弹窗|'
                           r'完成任务|限本直播间|领取并使用')



class DailyActivity(RescanPolicy, SearchPolicy, DestinationPolicy, TaskActions, VerificationPolicy, Activity):
    application = PROFILE
    claim_actions = DAILY_CLAIMS
    external_apps = EXTERNAL_APPS
    # 点「展开」的重试上限。展开有动画，一次读完常常还是折叠帧，所以要重试；
    # 但不能无限点——同一个位置上反复点会把列表收起，也白耗时间。
    EXPAND_TRIES = 3

    @property
    def page(self):
        return self.config['daily_page']

    def installed_app(self, packages):
        """Cache the device package list for this run; a task must not be opened
        only to discover on the landing page that its app is absent."""
        cached = getattr(self, '_installed_cache', None)
        if cached is None:
            try:
                cached = set(self.device.installed_packages() or ())
            except Exception:
                cached = set()
            self._installed_cache = cached
        return not cached or bool(cached.intersection(packages))

    def classify_daily(self, title, detail, texts, action, current, total):
        if re.fullmatch(r'[0-9]{1,2}点可领', action):
            return (0, 'done', '今日已完成', '', ()) if current >= total else (
                0, 'manual', '尚未到领取时段', '', ())
        # 淘金币里的轻互动小游戏和站内直播只要求“进去待一会儿”，既不下载安装
        # 也不涉及支付。风险词表里的 游戏/提现 是为拦截装包、拉人、资金操作而设，
        # 对这类任务整条丢弃会永久漏掉它们，因此单独放行成跨应用浏览。
        play = self.page.get('inline_play_markers')
        watch = self.page.get('inline_watch_markers')
        passive_play = ((play and re.search(play, title))
                        or (watch and re.search(watch, title)))
        if (passive_play and action not in DONE_ACTIONS
                and not re.search(r'下载|安装|支付|购买|充值|下单', normalize(detail))):
            target, packages = next(((name, pkgs) for name, pkgs in self.external_apps
                                     if name in title), ('', ()))
            if packages and not self.installed_app(packages):
                return 0, 'manual', f'未安装{target}，已跳过；安装后重新识别即可恢复', '', ()
            return self.config['visit_seconds'], 'visit', '', target, packages
        # This offer explicitly rewards browsing first; ordering is an optional
        # extra. Keep all real ordering requirements excluded, including any
        # additional requirement outside this exact, observed clause.
        optional_order = '先浏览赚肥料,下单再得肥料'
        safe_detail, safe_texts = detail, texts
        if title == '浏览全场1元淘工厂' and optional_order in normalize(detail):
            safe_detail = normalize(detail).replace(optional_order,'浏览赚肥料')
            safe_texts = [title, safe_detail]
        # Captions such as "浏览10秒 +20下单最高 +10000" advertise an optional
        # ordering bonus on top of a browse task. Strip only that trailing clause
        # so the base browse is not reclassified as a payment/manual task.
        if re.search(r'浏览[0-9]+[秒sS]', safe_detail):
            bonus = re.search(r'下单(?:最高|再得|可得)[^+]{0,8}', safe_detail)
            if bonus:
                safe_detail = safe_detail[:bonus.start()].strip()
                safe_texts = [title, safe_detail]
        classification_title = title
        if (title in ('你关注的商品都可抵','看看你关注的新品动态')
                and re.search(r'浏览[0-9]+[秒sS]',safe_detail)
                and not re.search(r'关注|订阅|收藏',safe_detail)):
            classification_title = title.replace('关注','浏览')
            safe_texts = [classification_title,safe_detail]
        scope = (classification_title+' '+safe_detail).replace('支付宝','蚂蚁平台')
        # 标题只说明入口叫什么，小字才说明要做什么。小字明确是“点一下/看几秒”
        # 时，标题里的夺宝、抽奖、小游戏不再构成拦截理由；付款、拉人、装包这类
        # 动作即使小字写着“点击去逛”也仍然不能代做。
        small_print = normalize(safe_detail)
        hard = HARD_RISK_RE.search(scope)
        soft = SOFT_RISK_RE.search(scope)
        clickable = (bool(ACTIONABLE_DETAIL_RE.search(small_print))
                     and not HARD_RISK_RE.search(small_print))
        # 已完成的行不能再进：下面的轻互动分支会绕过 classify 的完成判定。
        # 进度满不等于领过：签到/成就这类行显示 1/1 时按钮仍是「领取奖励」，
        # 见「领取奖励」就不能算完成，否则会白丢一次可领的金币。
        if action in DONE_ACTIONS or (total and current >= total
                                      and action not in CLAIM_ACTIONS):
            return 0, 'done', '今日已完成', '', ()
        # 摇钱树施肥 / 浇水是「点一下就完事」的轻互动：小字写的是“点击去逛”，
        # 进去点一次施肥按钮即可，不是需要人工反复照料的种植任务。硬风险表
        # 里的 施肥/浇水 是为拦多轮农活设的，对这类任务等于永久漏掉。
        if (clickable and re.search(r'摇钱树|施肥|浇水', scope)
                and not re.search(r'施肥\s*\d+\s*次|浇水\s*\d+\s*次|下单|购买|支付|充值',
                                  small_print)):
            return max(int(self.config.get('visit_seconds') or 0), 8), 'visit', '', '', ()
        # 趣味课堂这类答题：每题选 A 就能过，不需要人工判断，交给 answer_quiz。
        if (re.search(r'答题|趣味课堂|问答', scope)
                and not re.search(r'下单|购买|支付|充值|邀请|助力|下载|安装|授权|实名|拍照',
                                  small_print)):
            return max(int(self.config.get('visit_seconds') or 0), 10), 'visit', '', '', ()
        if action not in DONE_ACTIONS:
            if hard:
                return 0, 'manual', '需要手动操作：'+hard.group(), '', ()
            if soft and not clickable:
                return 0, 'manual', '需要手动操作：'+soft.group(), '', ()
        # 点击N个商品：进去逐个点开再返回即可，不需要人工判断。
        if re.search(r'(?:点击|浏览|逛)\s*\d{1,2}\s*个(?:商品|宝贝)', scope):
            return max(int(self.config.get('visit_seconds') or 0), 6), 'browse', '', '', ()
        # 标题里的「抽1000元红包」只是入口名，小字写的是点击或浏览几秒时，
        # 真实动作是打开看一眼就回来。共用的 classify 把这类行留给人工
        # （双11 的 v009 契约要求如此），金币面板这里改写入口名后按浏览处理，
        # 关键字仍留在 safe_detail 里参与付款、拉人的判断。
        if clickable:
            scrubbed = re.sub(r'抽\d+元红包|抽奖|夺宝', '领红包', classification_title)
            if scrubbed != classification_title:
                classification_title = scrubbed
                safe_texts = [classification_title, safe_detail]
        seconds, kind, reason, target, packages = classify(classification_title, safe_detail, safe_texts, action, current, total)
        # 跨应用任务停留多久由目标应用自己的页面说了算。通用的 8 秒对首次冷
        # 启动、带开屏广告的应用偏短，允许在配置里按应用名抬到更高的下限。
        floors = self.page.get('app_visit_seconds')
        if isinstance(floors, dict) and target:
            try:
                floor = int(floors.get(target) or 0)
            except (TypeError, ValueError):
                floor = 0
            if floor > seconds:
                seconds = floor
        # 目标应用不在本机时提前跳过：等点进去才发现，落地页常常是安装页或
        # 要求登录的 H5，会把整轮拖成人工介入。
        if packages and kind in ('visit', 'browse', 'search') and not self.installed_app(packages):
            return 0, 'manual', f'未安装{target}，已跳过；安装后重新识别即可恢复', '', ()
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
        # 表头滚出屏幕后仍要按行解析：更多金币等你赚 / 更多专享福利 是这张
        # 面板自己的分区标记，只要还在就说明当前页面仍是金币任务列表。
        if (self.page.get('reward_rows') and not getattr(self, '_row_depth', 0)
                and any(re.search(r'今日速赚|更多金币等你赚|更多专享福利', n.text) for n in nodes)):
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

    @staticmethod
    def merge_tasks(*groups):
        """One semantic row per caption; the earlier group wins on ties."""
        merged = []
        for group in groups:
            for task in group:
                prior = next((t for t in merged if normalize(t.title) == normalize(task.title)
                              and t.total == task.total), None)
                if prior is None:
                    if any(t.key == task.key for t in merged):
                        continue
                    merged.append(task)
                elif task.current > prior.current or task.kind == 'done':
                    merged[merged.index(prior)] = task
        return merged

    @staticmethod
    def clickable_rows(tasks, height):
        """底部手势条覆盖的位置点不动：留到下一轮滚上来再点，别白烧一次失败。"""
        return [t for t in tasks if (t.bounds[1]+t.bounds[3])/2 < height*.96]

    def button_rows(self, nodes, width, height):
        """Split the sheet by its right-hand buttons instead of by caption words.

        Every row carries exactly one right-aligned button (去完成 / 看一看 /
        去逛逛 / 已完成), while the caption, the small print under it and the
        reward badge arrive as three unrelated OCR nodes. Anchoring on the
        button is stable for rows the caption-driven path drops: captions that
        begin with 点击, and buttons such as 看一看 that no action word lists.
        """
        def amount(text):
            return bool(re.fullmatch(r'[+＋]?\d+(?:\.\d+)?(?:金币|元)?', text))

        buttons = [n for n in nodes
                   if n.center[0] > width*.58
                   and 0 < len(normalize(n.text)) <= 8
                   and 0 < n.bounds[3]-n.bounds[1] < height*.06
                   and height*.12 < n.center[1] < height*.97
                   and n.bounds[3] < height*.99
                   and re.search(r'[\u4e00-\u9fff]', n.text)
                   and not amount(normalize(n.text))]
        buttons.sort(key=lambda n: n.center[1])
        seats = []
        for button in buttons:
            if seats and abs(button.center[1]-seats[-1].center[1]) < height*.05:
                continue
            seats.append(button)
        tasks = []
        for index, button in enumerate(seats):
            above = seats[index-1].center[1] if index else button.center[1]-height*.09
            below = seats[index+1].center[1] if index+1 < len(seats) else button.center[1]+height*.09
            half = min((button.center[1]-above)/2, (below-button.center[1])/2, height*.05)
            left = [n for n in nodes
                    if n.center[0] < button.bounds[0]-width*.02
                    and n.center[0] > width*.04
                    and abs(n.center[1]-button.center[1]) < half
                    and 0 < n.bounds[3]-n.bounds[1] < height*.06
                    and re.search(r'[\u4e00-\u9fff]', n.text)]
            left.sort(key=lambda n: (n.center[1], n.bounds[0]))
            if not left:
                continue
            # 带进度的那行才是标题；OCR 偶尔把两行的 y 读反，按行首取会拿到
            # 小字（浏览5秒）当标题。
            head = next((n for n in left if PROGRESS_RE.search(normalize(n.text))), left[0])
            progress = PROGRESS_RE.search(normalize(head.text))
            current, total = map(int, progress.groups()) if progress else (0, 1)
            caption = PROGRESS_RE.sub('', normalize(head.text)).strip()
            small = ' '.join(normalize(n.text) for n in left if n is not head)
            badge = next((n for n in nodes if amount(normalize(n.text))
                          and re.search(r'[+＋]', normalize(n.text))
                          and head.center[0] < n.center[0] < button.bounds[0]-width*.02
                          and abs(n.center[1]-button.center[1]) < half), None)
            # A header strip (今日速赚 / 更多金币等你赚) also ends in a right-hand
            # label. It has no progress, no reward badge and no actionable small
            # print, so it never becomes a task here.
            if not (progress or badge or ACTIONABLE_DETAIL_RE.search(small)):
                continue
            if not 2 <= len(caption) < 40:
                continue
            detail = (small + ' ' + normalize(badge.text)).strip() if badge else small
            action = normalize(button.text)
            seconds, kind, reason, target, packages = self.classify_daily(
                caption, detail, [caption, small], action, current, total)
            tasks.append(Task(hashlib.sha256(caption.encode()).hexdigest()[:16], caption,
                              detail, action, button.bounds, current, total,
                              seconds, kind, reason, target, packages))
        return tasks

    def reward_rows(self, nodes, width, height):
        """Current coin panel uses clickable reward circles instead of action words."""
        more = next((n for n in nodes if normalize(n.text)=='更多专享福利'),None)
        extra_nodes = [n for n in nodes if not re.search(r'今日速赚',n.text)
                       and (more is None or n.center[1]>more.bounds[3])]
        # 下面的分区仍带着“更多专享福利”标记，重入本方法会无限递归。
        # 只允许展开一层，内层交给通用解析。
        if getattr(self, '_row_depth', 0):
            extra = []
        else:
            self._row_depth = 1
            try:
                extra = self.parse_tasks(extra_nodes,width,height)
            finally:
                self._row_depth = 0
        rows = self.button_rows(nodes, width, height)
        rewards = [n for n in nodes if re.fullmatch(r'\+\d+|已领取|已完成', normalize(n.text))
                   and 0 < n.bounds[3]-n.bounds[1] < height*.08]
        actions = [n for n in nodes if ACTION_RE.fullmatch(normalize(n.text))
                   and n.center[0] > width*.55 and 0 < n.bounds[3]-n.bounds[1] < height*.1]
        header = next((n for n in nodes if '今日速赚' in n.text), None)
        # Once the sheet is scrolled into the 更多金币等你赚 section the 今日速赚
        # header is off-screen. Pairing still works from the row geometry alone,
        # but only when the panel section itself is still painted.
        if header is None and not any(
                re.search(r'更多金币等你赚|更多专享福利', n.text) for n in nodes):
            return self.clickable_rows(self.merge_tasks(rows, extra), height)
        left_limit = (header.bounds[0] if header else 0) - width*.02

        def has_caption(node):
            """这一行上方还压着另一行标题时，它自己只是那行的小字。

            行距远大于标题到小字的间距，所以窗口取 height*.035：小字能看见上方
            的标题，标题看不见上一行的小字，不会把真正的标题误判成小字。
            """
            return any(0 < node.center[1]-n.center[1] < height*.035
                       and n.bounds[0] < node.bounds[2] and n.bounds[2] > node.bounds[0]
                       and 0 < n.bounds[3]-n.bounds[1] < height*.05
                       and re.search(r'[\u4e00-\u9fff]', n.text) for n in nodes)

        def titled(node):
            text = normalize(node.text)
            # Detail lines such as 点击去逛 sit on the same column as the caption
            # and carry the reward badge, so they look like a row of their own.
            # 以“点击”开头的行只有在上面还有一行标题时才是小字；否则它自己就是
            # 标题（例如“点击商品领优惠红包(0/1)”），不能被一起丢掉。
            if ACTION_RE.fullmatch(text):
                return False
            # 小字（浏览5秒 / 下单得 / 每日来任务面板）本身也带着奖励徽章，
            # 结构上像一个独立的行；它上方有标题就说明它只是描述，不是任务。
            if has_caption(node):
                return False
            if re.match(r'^点击', text) and any(
                    0 < node.center[1]-n.center[1] < height*.045
                    and n.bounds[0] < node.bounds[2]
                    and re.search(r'[\u4e00-\u9fff]', n.text) for n in nodes):
                return False
            # 应用图标里印着的应用名（头条 / 抖音）会被读成一行短文本，
            # 它不是标题；真正的标题在它右边或下一行。
            if text in APP_ICON_LABELS:
                return False
            # 图标被 OCR 读出来的符号（例如趣味课堂的 "?!"）不是任务标题。
            if not re.search(r'[\u4e00-\u9fff]', text) and not re.search(r'[A-Za-z0-9]{3,}', text):
                return False
            if PROGRESS_RE.search(text) or re.search(
                    r'任务到访得金币|逛清单|搜一搜|淘金币趣味课堂|^去.{2,20}|拍立淘', text):
                return True
            # Structural fallback: a row carrying a reward circle on its right is
            # a task even when its caption has no progress and no known keyword.
            return any(r.center[0] > node.center[0]
                       and abs(r.center[1]-node.center[1]) < height*.03 for r in rewards)

        titles = [n for n in nodes if titled(n)
                  and n.bounds[0] >= left_limit
                  and len(normalize(n.text)) < 50 and n.bounds[3]-n.bounds[1] < height*.05]
        tasks=[]
        for title in titles:
            text=normalize(title.text);progress=PROGRESS_RE.search(text)
            current,total=map(int,progress.groups()) if progress else (0,1)
            caption=PROGRESS_RE.sub('',text)
            nearby=[n for n in rewards if n.center[0]>title.center[0]
                    and abs(n.center[1]-title.center[1]) < height*.04]
            if not nearby:
                # Rows in the lower section carry a right-hand action button
                # instead of a reward circle, and their +N sits left of the long
                # caption centre. Anchor on the button before giving up.
                button=next((b for b in actions if b.center[0] > title.center[0]
                             and abs(b.center[1]-title.center[1]) < height*.035),None)
                if button is not None:
                    label=normalize(button.text)
                    detail=' '.join(n.text for n in nodes
                                    if title.bounds[3] <= n.center[1] <= title.bounds[3]+height*.035
                                    and n.bounds[0]>=title.bounds[0]-width*.02
                                    and n.bounds[2] < button.bounds[0] and len(n.text)<60)
                    seconds,kind,reason,target,packages=self.classify_daily(
                        caption,detail,[caption,detail],label,current,total)
                    key=hashlib.sha256(caption.encode()).hexdigest()[:16]
                    tasks.append(Task(key,caption,detail,label,button.bounds,current,total,
                                      seconds,kind,reason,target,packages))
                    continue
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
        # Button-anchored rows go in between: they are the only source for
        # captions that start with 点击 or end in a button no action word lists.
        # 按钮作为行锚点更准：标题通道拿到的点击位置落在奖励徽章上，
        # 那个位置在底部时会被手势条吃掉。
        return self.clickable_rows(self.merge_tasks(rows, tasks, extra), height)

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
        # guard 里可能已经退掉一层：继续用旧画面会把点击落到上一页的坐标上。
        retreat = getattr(self, '_retreat_screen', None)
        if retreat is not None:
            self._retreat_screen = None
            screen = retreat
        manual = self.page.get('manual_entry_markers')
        if manual and screen.find(manual):
            reason = self.page['manual_entry_message']
            saved = self.report_failure(reason, phase='attention')
            raise NeedsAttention(reason, evidence_saved=saved)
        return screen

    def sanitized(self, screen):
        """把商品名里的“滑块”从验证词表里摘出去。

        导购横幅、商品标题里的“导轨滑块 / 窗帘滑块”会被判定当成“拖动滑块验证”，
        结果整轮停在一个根本没有验证的页面上。真正的验证页一定还有拖动、拼图、
        验证码这类话；这些词都不在时，把 滑块 换成同义的商品词再交给判定。
        """
        try:
            text = normalize(screen.text)
        except Exception:
            return screen
        if '滑块' not in text or CHALLENGE_RE.search(text):
            return screen
        nodes, changed = [], False
        for node in screen.nodes:
            raw = normalize(node.text)
            if '滑块' not in raw:
                nodes.append(node)
                continue
            changed = True
            fresh = raw.replace('滑块', '滑轨')
            try:
                nodes.append(TextNode(fresh, node.bounds, node.source))
            except TypeError:
                nodes.append(TextNode(fresh, node.bounds))
        if not changed:
            return screen
        fresh_text = text.replace('滑块', '滑轨')
        try:
            return replace(screen, nodes=nodes, text=fresh_text)
        except Exception:
            pass
        try:
            clone = copy.copy(screen)
        except Exception:
            return screen
        try:
            clone.nodes = nodes
        except Exception:
            try:
                object.__setattr__(clone, 'nodes', nodes)
            except Exception:
                return screen
        try:
            clone.text = fresh_text
        except Exception:
            try:
                object.__setattr__(clone, 'text', fresh_text)
            except Exception:
                pass
        return clone

    def retreat(self, screen):
        """疑似被拦住时先退一层：退出去后判定不成立，就说明只是撞了词表。

        安全验证真在的时候退不走（下一页还是验证页），这里会返回 False，
        交给上层照常中断整轮，不会靠退回绕过真正的验证。
        """
        from backend.core.execution.guard import check_attention
        try:
            self.device.go_back()
        except Exception:
            return None
        self.pause(1.2)
        try:
            fresh = super().read(force_ocr=False)
        except Exception:
            return None
        if self.is_task_list(fresh.nodes):
            return fresh
        try:
            check_attention(self, self.sanitized(fresh))
        except NeedsAttention:
            return None
        except Exception:
            return None
        return fresh

    @staticmethod
    def looks_like_task_panel(screen):
        """判断这一屏是不是“任务清单”。

        任务面板和弹窗长得像：右上角都有 ✕，页面上都写着“奖励”。区别是清单里
        有多个可执行/已完成的按钮，弹窗只有一个。满屏 ✕ 一律不能当成弹窗关闭，
        否则会把整张任务面板关掉。
        """
        rows = [n for n in screen.nodes
                if re.match(r'^(?:去完成|去领取|去兑换|去逛逛|去浏览|去观看|去参与|'
                            r'去查看|已领取|已完成|立即领取|领取|去玩)$', normalize(n.text))]
        return len(rows) >= 2

    def popup_close_node(self, screen):
        """找“关掉这层浮层”的按钮；没有就返回 None。"""
        for node in screen.nodes:
            text = normalize(node.text)
            if not POPUP_CLOSE_RE.match(text):
                continue
            # 太高的位置是系统状态栏，太扁的是一行小字，都不像关闭按钮。
            if not 0 < node.bounds[3]-node.bounds[1] < screen.height*.12:
                continue
            if node.center[1] < screen.height*.05:
                continue
            return node
        return None

    def dismiss_popup(self, screen, force=False):
        """把挡在任务上的弹窗关掉再继续，而不是整轮停下来等人。

        直播间常弹“恭喜抢到 9.5 折直播折扣券 / 恭喜完成任务”，页面上读到的
        “确认付款”其实来自浮层下方的带货横幅，关掉浮层就没了。顺序是：
        点关闭类按钮 → 没有按钮就点卡片外侧空白 → 都没用就返回 None 交给调用方。
        任务面板不参与：面板右上角也有个 ✕，点它会把整张面板关掉。
        """
        if getattr(self, '_popup_busy', False):
            return None
        if screen.source == 'ui' and self.config['ocr'] and self.popup_close_node(screen):
            # The accessibility tree can expose a hidden tutorial's Skip over
            # an unrelated painted offer. Confirm the rendered dismiss control.
            screen = super().read(force_ocr=True)
            self.assert_instance(screen)
        # 滑块验证页右上角那个 × 等于「放弃这次验证」，关掉只会让任务失败，
        # 不能按普通浮层处理；这种页面交给 solve_slider。
        if self.on_slider_page(screen):
            return None
        text = normalize(screen.text)
        # 任务面板自己也有 ✕（关闭整张面板）和“奖励”这类字，不能当成弹窗。
        markers = self.page.get('list_markers')
        if (self.is_task_list(screen.nodes) or self.looks_like_task_panel(screen)
                or (markers and re.search(markers, text))):
            return None
        hinted = bool(POPUP_HINT_RE.search(text))
        close = self.popup_close_node(screen)
        if close is None and not (force and hinted):
            return None
        if close is None and not hinted:
            return None
        # 同一个位置只点一次：弹窗没关掉时不要在同一个 ✕ 上反复点。
        stamp = (close.bounds if close is not None else (0, 0, 0, 0), text[:40])
        seen = getattr(self, '_popup_seen', None)
        if seen is None:
            seen = self._popup_seen = {}
        if time.time() - seen.get(stamp, 0) < 15:
            return None
        seen[stamp] = time.time()
        self._popup_busy = True
        try:
            if close is not None:
                logger.info('关闭弹窗：%s', normalize(close.text)[:12])
                self.update(message='关闭弹窗')
                self.click(close)
            else:
                # 没有关闭按钮的浮层，点卡片外侧的空白处通常也能收起。
                logger.info('弹窗没有关闭按钮，点击外侧空白收起')
                self.update(message='收起弹窗')
                self.device.click(round(screen.width*.5), round(screen.height*.12))
            self.pause(1.0)
        except Exception:
            logger.warning('关闭弹窗失败', exc_info=True)
            return None
        finally:
            self._popup_busy = False
        fresh = super().read(force_ocr=bool(self.config['ocr']))
        if normalize(fresh.text) == text:
            return None
        self._popup_epoch = getattr(self, '_popup_epoch', 0) + 1
        return fresh

    def guard(self, screen):
        """判断“要不要停下来等人”。

        核心词表把“滑块”“验证”这类名词也当成验证页，而直播间的导购横幅常写着
        “HG系列导轨滑块”“窗轨滑块”，一撞上就整轮停，可屏幕上根本没有验证。
        所以核心判定只当提醒：真正的拦截由本地 HARD_STOP_RE 决定——只有出现
        拖动、拼图、验证码、登录框、支付密码这些“必须人动手”的东西才停。
        """
        self.assert_instance(screen)
        # 滑块验证单独处理：能自动拖就拖，拖不过去就停下来等人滑完再继续，
        # 不让整轮为一个滑块白白中断；真的等超时了才按拦截处理。
        if self.on_slider_page(screen):
            if self.handle_slider(screen):
                self._retreat_screen = None
                return
            logger.warning('滑块没有通过，按拦截处理')
        from backend.core.execution.guard import check_attention
        task = self._active_task
        text = normalize(screen.text)
        watch = self.page.get('inline_watch_markers')
        # 直播间是视频流页面：横幅上的“关注/会员/领券/到手价/立即购买/加入购物车”
        # 常被读成登录或支付提示，而这类任务只要求停留，并非真的要求授权。
        # 标题里未必写着“直播”（例如“好物沉浸看”），所以也认页面本身的特征。
        live_title = normalize(str(task.title if task is not None else '')
                               + ' ' + str(getattr(task, 'detail', '') or ''))
        live_page = bool(re.search(r'直播', live_title) or re.search(LIVE_PAGE_RE, text))
        if (watch and task is not None and task.kind in ('visit', 'browse')
                and live_page and not HARD_STOP_RE.search(text)):
            self._retreat_screen = None
            return
        try:
            check_attention(self, self.sanitized(screen))
        except NeedsAttention:
            if screen.source == 'ui' and self.config['ocr']:
                # A hidden tutorial/offer can survive in an accessibility tree.
                # Never dismiss an invisible control or infer a challenge from it.
                painted = super().read(force_ocr=True)
                self.assert_instance(painted)
                try:
                    check_attention(self, self.sanitized(painted))
                except NeedsAttention:
                    screen, text = painted, normalize(painted.text)
                else:
                    self._retreat_screen = painted
                    return
            if HARD_STOP_RE.search(text):
                raise
            if getattr(screen, 'obscured', False):
                raise
            # 先试着把挡路的弹窗关掉再说：直播间那张“恭喜抢到折扣券”的浮层
            # 一盖上，下面的带货横幅就会被读成“确认付款”，关掉浮层就干净了。
            fresh = self.dismiss_popup(screen, force=True)
            if fresh is not None:
                fresh_text = normalize(fresh.text)
                if not HARD_STOP_RE.search(fresh_text):
                    logger.info('关掉弹窗后页面不再有拦截提示，继续执行')
                    self._retreat_screen = fresh
                    return
                screen, text = fresh, fresh_text
            hit = HARD_STOP_RE.search(text)
            if hit is not None:
                logger.warning('页面确有拦截元素「%s」，停止自动执行', hit.group())
                raise
            soft = CORE_STOP_RE.search(text)
            logger.info('忽略疑似拦截：只撞到词表「%s」，页面上没有验证/登录元素；片段=%s',
                        soft.group() if soft else '未知', text[:60].replace('\n', ' '))
        self._retreat_screen = None

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
        if (self.page.get('expand_label') and not getattr(self, '_expanding', False)
                and self._phase not in ('opening','browsing','searching')
                and getattr(self, '_expand_fail', 0) < 3
                and screen.find(r'今日速赚|更多金币等你赚')):
            # The sheet opens collapsed and its 展开 control usually sits one
            # screen below the fold, so navigating once is not enough: the loop
            # scrolls past it and only ever sees the already-finished top rows.
            # 成功判据是“折叠段已经画出来了”，不是“按钮不见了”：展开后有动画，
            # 只等 0.9 秒读到的常常还是折叠帧，那时既不会再点、又会被当成已展开。
            # 只点折叠态，已展开时再点一下会把任务整段收回去。
            if self.collapsed_panel(screen):
                try:
                    self.update(message='展开“%s”里的任务' % self.page.get('expand_context'))
                    self._expanding = True
                    self.click(self.expand_button(screen))
                    self.pause(1.6)
                    fresh = super().read(force_ocr=force_ocr)
                    self._expand_fail = (0 if not self.collapsed_panel(fresh)
                                         else getattr(self, '_expand_fail', 0)+1)
                    screen = fresh
                except Exception:
                    self._expand_fail = getattr(self, '_expand_fail', 0)+1
                finally:
                    self._expanding = False
        # 顺手把挡在任务上的弹窗关掉：领券、入会、活动提醒这类浮层不关，
        # 后面的点击全落在浮层上，任务永远是“进度未增加”。
        popup = self.dismiss_popup(screen)
        if popup is not None:
            screen = popup
        return self.stall_guard(screen, force_ocr=force_ocr)

    def progress_signature(self, screen):
        """Business state for the watchdog, not pixels.

        Live streams and video pages repaint every second (danmaku, viewer
        counts, likes), so text diffing says "still responding" forever while a
        navigation loop makes no headway. What actually matters is whether the
        page moved closer to its goal: which app is in front, is this the task
        list yet, and did the running task's progress change.
        """
        task = self._active_task
        try:
            listed = bool(self.is_task_list(screen.nodes))
        except Exception:
            listed = False
        return '|'.join((
            str(screen.package),
            '1' if listed else '0',
            '1' if screen.package == self.origin else '0',
            str(getattr(task, 'key', '')),
            str(getattr(task, 'current', -1)),
        ))

    def stall_guard(self, screen, force_ocr=False):
        """Frozen frame with no feedback: back out instead of waiting it out.

        Some Taobao overlays never paint after a tap and never raise either, so
        the reader keeps returning an identical frame while navigation burns its
        whole hard deadline on one screen. A frame that has not changed for
        stall_timeout seconds is treated as a dead end and unwound one level.

        The guard only polices stages that are supposed to make headway (entry,
        return, launch). Browse and visit counters are timed by their own
        deadline, so a live stream that keeps repainting is never interrupted.
        """
        limit = float(self.page.get('stall_timeout', 20) or 0)
        if limit <= 0:
            return screen
        task = self._active_task
        phase = getattr(self, '_phase', '')
        # A motionless screen is expected while a browse/visit counter runs.
        if (phase in ('browsing', 'searching')
                and task is not None and task.kind in ('browse', 'search', 'visit')):
            self._stall = None
            return screen
        text = normalize(screen.text) or f'<blank:{screen.package}>'
        signature = hashlib.sha256(
            (text + '|' + str(screen.package) + '|' + self.progress_signature(screen))
            .encode('utf-8', 'ignore')).hexdigest()[:16]
        now = self.control.now()
        state = getattr(self, '_stall', None)
        if not state or state.get('sig') != signature:
            self._stall = {'sig': signature, 'since': now}
            return screen
        if now - state['since'] < limit:
            return screen
        self._stall = {'sig': signature, 'since': now}
        used = getattr(self, '_stall_backs', 0)
        if used >= 3:
            # Already unwound three times on this pass; let the caller's own
            # deadline decide rather than bouncing the page forever.
            return screen
        self._stall_backs = used + 1
        logger.warning('画面 %d 秒无变化，退回上一层（第 %d 次）', int(limit), used + 1)
        try:
            self.update(message='页面无响应，退回上一层')
            self.device.go_back()
            self.pause(1.2)
            fresh = super().read(force_ocr=force_ocr)
            if normalize(fresh.text):
                screen = fresh
        except Exception:
            logger.warning('无响应退回失败', exc_info=True)
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

    def voice_chip(self, screen):
        """语音搜索页上的分类/热词入口：短文本、非功能按钮，优先取靠上的。"""
        skip = re.compile(r'^搜索$|^取消$|^返回$|^语音$|^语音搜索$|^按住.{0,6}说$|^说点什么$|'
                          r'^历史搜索$|^搜索发现$|^猜你想搜$|^换一换$|^换一批$|^清空$|^关闭$|^[Xx×✕]$')
        nodes = [n for n in screen.nodes
                 if 2 <= len(normalize(n.text)) <= 8
                 and re.search(r'[\u4e00-\u9fff]', n.text)
                 and not skip.match(normalize(n.text))
                 and not ACTION_RE.fullmatch(normalize(n.text))
                 and 0 < n.bounds[3]-n.bounds[1] < screen.height*.06
                 and screen.height*.08 < n.center[1] < screen.height*.85]
        return min(nodes, key=lambda n: n.center[1]) if nodes else None

    def handle_search(self, screen, query=None, activity_search=False):
        voice = self.page.get('voice_search_markers')
        task = self._active_task
        if not (voice and task is not None and re.search(voice, task.title)):
            return super().handle_search(screen, query, activity_search)
        logger = logging.getLogger('backend.execution')
        if self.search_results_ready(screen):
            return True
        # 语音搜索不要求真的说话：页面上任意一个分类点一下就进结果页，
        # 直接找输入框只会一路报“没有可确认的输入框”。
        chip = self.voice_chip(screen)
        if chip is None:
            return super().handle_search(screen, query, activity_search)
        logger.info('语音搜索页点选分类：%s', normalize(chip.text))
        self.update('searching', message='点选搜索分类')
        self.click(chip)
        deadline = self.control.now() + self.config['page_timeout']
        while self.control.now() < deadline:
            self.pause(.6)
            result = self.read(force_ocr=bool(self.config['ocr']))
            self.guard(result)
            self.check_search_context(result)
            if self.search_results_ready(result):
                logger.info('语音搜索分类已跳转到结果页')
                return True
            if self.is_task_list(result.nodes):
                break
        self.report_failure('语音搜索分类点击后未进入结果页', phase='searching')
        logger.warning('语音搜索分类点击后未进入结果页')
        return False

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
        self.pause(.15)
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
                and not (self.promotion_visible(screen) or screen.find(r'提现机会|现金提现|恭喜|签到成功|奖励|送你.{0,6}(?:能量|金币)'))):
            return False
        if close and close.bounds[3]-close.bounds[1] < screen.height*.12:
            self.click(close)
            self.pause(.4)
            return True
        if self.promotion_visible(screen):
            # Decorative close icons may not be readable. Dismiss the known
            # promotion with Back rather than clicking its coupon/withdraw CTA.
            self.control.check()
            self.device.go_back()
            self.pause(.4)
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
            self.pause(.5)
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

    def install_swipe_guard(self):
        """The coin panel is a dismissable sheet, not a plain page.

        A single long fling (the framework scrolls .88h -> .43h, i.e. ~1080px on
        a 2400px screen) is read as a drag-down gesture and closes the sheet, so
        every row below the first screen is lost and the loop then "recovers" by
        reopening the same sheet at the top. Splitting one long vertical swipe
        into short hops keeps the sheet open while covering the same distance.
        """
        device = self.device
        if getattr(device, '_miao_split_swipe', False):
            return
        original = getattr(device, 'swipe', None)
        # 只包装适配器自己的绑定方法：外部替换过的可调用对象（测试替身、
        # 其他活动安装的包装）保持原样，避免覆盖调用方手动设置的行为。
        if not callable(original) or getattr(original, '__self__', None) is not device:
            return

        def split_swipe(x1, y1, x2, y2, duration=None, **kwargs):
            try:
                span = abs(int(y2)-int(y1))
                if span > 480 and abs(int(x2)-int(x1)) < span*.3:
                    steps = max(2, min(4, int(round(span/420))))
                    dy = (int(y2)-int(y1))/steps
                    step = max(.45, float(duration or .55)/steps)
                    for index in range(steps):
                        start = int(y1)+dy*index
                        original(x1, round(start), x2, round(start+dy), duration=step, **kwargs)
                    split_swipe.calls += 1
                    if split_swipe.calls <= 3:
                        logging.getLogger('backend.execution').info(
                            '滑动分段：%dpx 拆为 %d 段（每段约 %dpx）', span, steps, round(span/steps))
                    return None
            except Exception as exc:
                logging.getLogger('backend.execution').warning('滑动分段异常：%s', exc)
            if duration is None:
                return original(x1, y1, x2, y2, **kwargs)
            return original(x1, y1, x2, y2, duration=duration, **kwargs)

        split_swipe.calls = 0
        try:
            device.swipe = split_swipe
            device._miao_split_swipe = True
            logging.getLogger('backend.execution').info('已启用滑动分段保护')
        except Exception as exc:
            logging.getLogger('backend.execution').warning('滑动分段启用失败：%s', exc)

    def expand_button(self, screen):
        """The collapsed panel reads 展开⌄; OCR keeps the caret with the label.

        下拉箭头会被识别成各种符号（⌄ / v / ∨ / 一个小方块），只列白名单必然
        漏；改成「展开」后面不允许再跟汉字，这样既能带上任意图标，又不会把
        「展开更多」这类正文误认成按钮。
        """
        label = self.page.get('expand_label')
        if not label:
            return None
        pattern = r'^\s*'+re.escape(label)+r'[^\u4e00-\u9fff]{0,4}$'
        node = screen.find(pattern)
        if node is None:
            node = screen.find(label, exact=True)
        return node

    def collapsed_panel(self, screen):
        """「更多金币等你赚」那一段还折叠着。

        展开后这行标题会被任务行整行替换掉（标题本身消失），所以「展开」按钮
        还在，就是折叠的直接证据。不能只用“有没有任务行”判断：折叠态上面那
        排精选清单卡片同样能被解析成任务，一判就错。
        """
        if not self.page.get('expand_label') or self.expand_button(screen) is None:
            return False
        context = self.page.get('expand_context')
        return bool(screen.find(context)) if context else True

    def ensure_expanded(self, screen, force=False):
        """点「展开」把更多金币等你赚整段任务画出来，并确认真的画出来了。

        这一步必须校验：展开有动画，点完只等 0.9 秒常常还在动画里，读到的仍
        是折叠态；旧实现就此放弃，而且把这次点击记成“已展开”，后面把折叠列表
        当成完整列表翻一遍，判出“没有任务”直接收工——用户看到的就是“说要去
        更多金币检查，实际根本没展开”。

        ``force`` 只为兼容旧调用：现在一律按折叠态判断，绝不盲点，否则会把已
        经展开的列表重新折叠回去。
        """
        context = self.page.get('expand_context')
        if not context or not self.page.get('expand_label'):
            return screen
        if not self.collapsed_panel(screen):
            return screen
        for attempt in range(self.EXPAND_TRIES):
            if not self.collapsed_panel(screen):
                self._expand_fail = 0
                return screen
            button = self.expand_button(screen)
            if button is None:
                break
            self.update(message=('展开“%s”里的任务' % context) if not attempt
                        else ('再展开一次“%s”' % context))
            self.click(button)
            self.pause(1.6)
            # read() 自己也有一个「看到折叠就展开」的分支；这里的点击已经生效，
            # 让它再点一次会把刚画出来的任务收回去，所以先挂起那个分支。
            self._expanding = True
            try:
                fresh = self.read_page(visual=True)
            finally:
                self._expanding = False
            if not self.collapsed_panel(fresh):
                self._expand_fail = 0
                logger.info('展开：点开“%s”，任务行已画出来', context)
                return fresh
            self._expand_fail = attempt + 1
            logger.warning('展开：第 %d 次点「%s」后仍是折叠态，稍后重试',
                           attempt + 1, self.page['expand_label'])
            self.pause(1.0)
            screen = fresh
        logger.warning('展开：连续点「%s」都没能展开“%s”，列表可能只画了前几行',
                       self.page.get('expand_label'), context)
        return screen

    def navigate(self, *, auto_open, recovering=False):
        self.install_swipe_guard()
        # Each navigation pass gets a fresh stall budget: backing out is only
        # useful while it can still change the page.
        self._stall = None
        self._stall_backs = 0
        self.update('returning' if recovering else 'locating', message='进入'+self.config['name'])
        package = self.origin or self.application.package_for(self.device.device_type)
        self.origin = package
        deadline = self.control.now() + self.config['return_timeout'] + self.config['page_timeout']*3
        hard_deadline = deadline + self.config['return_timeout'] + self.config['page_timeout']*3
        taps, backs, channels, searched, restarts = {}, 0, False, False, 0
        viewport_returns = 0
        suggest_backs = 0
        loading_until = self.control.now()+self.config['page_timeout']
        seen_popup_epoch = getattr(self, '_popup_epoch', 0)
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
                self.pause(.5)
            if self.device.current_app().get('package') != package:
                self.device.go_home()
                self.pause(.4)
                desktop=self.read_page(visual=True)
                if 'launcher' not in desktop.package.lower() and not desktop.package.endswith('.home'):
                    raise NeedsAttention('逐层返回后未确认桌面，已停止恢复，请检查当前窗口',kind='navigation')
        if auto_open and self.device.current_app().get('package') != package:
            self.device.start_app(package)
            self.pause(.8)
        screen = self.read_page()
        while True:
            self.control.check()
            epoch = getattr(self, '_popup_epoch', 0)
            if epoch != seen_popup_epoch:
                taps.clear()
                seen_popup_epoch = epoch
                loading_until = self.control.now()+self.config['page_timeout']
                deadline = min(hard_deadline, max(deadline, loading_until))
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
                self.pause(.5)
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
                self.pause(.5)
                screen = self.read_page(visual=True)
                continue
            if (not taps.get('expanded') and self.page.get('expand_label')
                    and self.collapsed_panel(screen)):
                # The reward sheet paints only its first few rows until 展开 is
                # tapped, so the queue below 更多专享福利 never reaches the parser.
                # 只在折叠态点：列表已经展开时，页面上可能还留着一个游离的
                # 「展开」标签，再点一次会把已经露出的队列折叠回去。
                # 判定统一走 collapsed_panel（“展开后标题行会被任务行替换”），
                # 展开失败时 ensure_expanded 内部会重试，不靠这一个 taps 标记。
                taps['expanded'] = True
                screen = self.ensure_expanded(screen)
                continue
            if self.is_task_list(screen.nodes):
                # A WebView can leave hidden controls in the hierarchy. Confirm
                # the painted task header before accepting the list.
                painted = self.read_page(visual=True) if self.config['ocr'] and screen.source == 'ui' else screen
                if self.is_task_list(painted.nodes):
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
            if (suggest_backs < 2 and screen.package == package
                    and self._submit_buttons(screen)
                    and (screen.find(r'^清空$|^分词$|^符号$|^中/英$')
                         or screen.find(r'^历史搜索$|^搜索发现$|^猜你想搜$'))):
                # 搜索建议页既不是首页也不是任务页，卡在这里会把 back 配额耗尽
                # 后再也回不到入口。单独退回首页重走一次，不占用 backs 配额。
                suggest_backs += 1
                self.device.go_back()
                self.pause(.5)
                screen = self.read_page()
                continue
            if self.dismiss_overlay(screen):
                # Covered entry taps do not consume the visible entry's retry budget.
                taps.clear()
                loading_until = self.control.now()+self.config['page_timeout']
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
                    self.pause(1)
                    screen = self.read_page()
                    continue
            if self.control.now() >= deadline:
                break
            if len(screen.nodes) < 8 and self.control.now() < loading_until:
                self.pause(.5)
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
                    self.pause(.7)
                    screen = self.read_page()
                    continue
                if not searched and native.find(r'^搜索栏$|^搜索框$'):
                    searched = True
                    self.open_home_search(native)
                    screen = self.read_page()
                    continue
            if not auto_open:
                self.pause(.4)
            elif backs < self.config['max_back_steps']:
                self.device.go_back()
                backs += 1
                self.pause(.5)
            elif restarts < 1 and not self._task_restart_used:
                self.device.go_home()
                self.pause(.4)
                desktop = self.read_page()
                if 'launcher' not in desktop.package.lower() and not desktop.package.endswith('.home'):
                    break
                self._task_restart_used = True
                self.device.restart_app(package)
                restarts += 1
                backs, channels, searched = 0, False, False
                taps.clear()
                self.pause(.8)
            else:
                break
            screen = self.read_page()
        reason = '未识别到'+self.config['name']+'的任务页，已停止，请检查入口是否变化'
        saved = self.report_failure(reason, phase='returning' if recovering else 'locating')
        raise NeedsAttention(reason, kind='navigation', evidence_saved=saved)

    def play_game(self, task, before):
        """Enter one game from the mini-game hub and stay for the announced time.

        The hub page only counts progress while a game is actually running, so a
        plain visit of the list page always verifies as 0. Stay for the duration
        printed on the card ("玩游戏30秒") and cap it by max_task_seconds.
        """
        logger = logging.getLogger('backend.execution')
        self._active_task = task
        self.update('opening', current_task=task.title, message='打开小游戏')
        logger.info('执行 %s (%d/%d)，类型 小游戏', task.title, task.current, task.total)
        self.control.check()
        left, top, right, bottom = task.bounds
        self.device.click((left + right) // 2, (top + bottom) // 2)
        self.pause(4)
        screen = self.read(force_ocr=bool(self.config['ocr']))
        button = screen.find(self.page.get('game_play_label', r'^去玩$|^开始游戏$|^立即玩$'))
        # 进度只要求真的进过游戏：卡片写“玩游戏30秒”，实际十秒即可退出，
        # 因此固定按 game_seconds 停留，不再照抄横幅里的时长。
        seconds = int(self.page.get('game_seconds', 10))
        if button is not None:
            logger.info('进入游戏并停留 %s 秒', seconds)
            self.update('browsing', message='游戏中', remaining=seconds)
            self.click(button)
            self.pause(2)
        else:
            seconds = min(seconds, task.seconds or seconds)
        deadline = self.control.now() + min(seconds + self.config['settle_seconds'],
                                            self.config['max_task_seconds'])
        while self.control.now() < deadline:
            self.control.check()
            self.pause(2)
            try:
                if self.device.current_app().get('package') != self.origin:
                    break
            except Exception:
                pass
        try:
            self.device.go_back()
            self.pause(1.2)
        except Exception:
            logger.warning('退出小游戏时返回失败', exc_info=True)
        # 盲目 recover 会连续 back 把关掉的任务浮层一起退掉，先逐层确认：
        # 游戏 → 游戏列表 → 任务面板，任何一步已经是任务列表就停手。
        for _ in range(3):
            screen = self.read(force_ocr=bool(self.config['ocr']))
            if self.is_task_list(screen.nodes):
                return screen, True, False
            try:
                self.device.go_back()
            except Exception:
                break
            self.pause(1.2)
        return self.recover(), True, False

    def execute(self, task, before):
        play = self.page.get('inline_play_markers')
        # 小游戏合集页本身不计时：必须点进一款游戏再停留，否则进度永远停在 0。
        if play and task.kind == 'visit' and re.search(play, task.title):
            return self.play_game(task, before)
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
            self.pause(.5)
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
                    self.pause(.6)
                    entry_deadline = self.control.now()+self.config['page_timeout']
                    while self.control.now() < entry_deadline:
                        destination = self.read_page(visual=True)
                        if (len(destination.nodes) >= 2 and not self.promotion_visible(destination)
                                and not self.is_task_list(destination.nodes)):
                            after, _, cross = self.browse_destination(replace(task, kind='browse', seconds=seconds), destination)
                            return after, False, cross
                        self.pause(.4)
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
                self.pause(.4)
                return self.recover(), explicit, False
            if self.dismiss_overlay(screen):
                return self.recover(), explicit, False
        return self.recover(), explicit, False

    @staticmethod
    def coin_balance_increased(task, before, after, *, exact=False):
        def balance(screen):
            for node in screen.nodes:
                if node.center[1] < screen.height*.1:
                    match=re.search(r'抵(\d+)(?:\.(\d{1,2}))?元',normalize(node.text))
                    if match:
                        return int(match[1])*100+int((match[2] or '0').ljust(2,'0'))
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
