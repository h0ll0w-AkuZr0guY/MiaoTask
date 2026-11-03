"""东东农场任务行解析。

真机验证（2026-11-03，小米 PD2232 / 1080x2400 / 京东 15.x）：
* 农场首页与任务面板的所有文案都落在节点的 ``text`` 上（``content-desc`` 为空），
  形如「点击前往做任务领水滴页面」「每日累计浇水20次(1/20)」「去浇水」。
* 任务面板是覆盖在农场页之上的抽屉（标题「做任务 领水滴」底部约 0.32H），
  每行是「标题(进度) + 说明 + 右侧按钮」，按钮文案决定这一行能做什么：
  - 去完成 / 逛一逛 / 去关注 → 打开落地页浏览，回来变成「去领奖」；
  - 去领奖 → 已经做完，点一下才真正到账；
  - 去浇水 → 直接浇一次水（不跳页，面板会收起）；
  - 去下单 → 真实交易，永不点击。
* 面板可读控件在切页后要等一两秒才出现，调用方必须重试读取，不能读空一次就判「没有任务」。
"""
import re

from backend.core.models import Task, normalize

# 浏览类按钮：点了会离开面板去开页面
BROWSE_ACTIONS = frozenset({'去完成', '逛一逛', '去逛逛', '去看看', '去浏览', '去关注'})
# 已做完、点一下才到账
CLAIM_ACTIONS = frozenset({'去领奖', '领取', '立即领取'})
# 浇水：留在农场页，直接消耗一次浇水次数
WATER_ACTIONS = frozenset({'去浇水'})
# 其余按钮（下单/开通…）一律不可执行，交给跳过规则说明原因
OTHER_ACTIONS = frozenset({'去下单', '去购买', '立即购买', '去开通', '去下载', '去安装'})

ALL_ACTIONS = BROWSE_ACTIONS | CLAIM_ACTIONS | WATER_ACTIONS | OTHER_ACTIONS

# 「每日累计浇水20次(1/20)」「精选活动推荐(0/7)」
TITLE_PROGRESS = re.compile(r'^(.*?)\s*[（(]\s*(\d+)\s*/\s*(\d+)\s*[)）]\s*$')
# 「浏览6秒，奖励+20g」「读6s书得奖励+10g」
BROWSE_TIME = re.compile(r'(?:浏览|停留|观看|逛逛|逛|读|看)\s*(\d{1,3})\s*(?:秒|[sS])')

# 安全规则：这些任务一律不点。顺序即命中顺序。
SKIP_RULES = (
    (re.compile(r'下单|购买|抢购|品质保证|专供|加购'), '需要下单或加购，不自动执行'),
    (re.compile(r'通知|提醒|权限'), '需要开启系统通知或权限，不自动执行'),
    (re.compile(r'邀请|助力|组队|好友|拉人|分享'), '需要邀请好友助力，不自动执行'),
    (re.compile(r'通过\s*1\s*关|通关|第\s*[一二三1-9]\s*关|小游戏|消消乐|打个螺丝|箭了又箭|挑战我最牛|汪了个汪'),
     '需要通关小游戏，不自动执行'),
    (re.compile(r'快手|百度|抖音|拼多多|小红书|极速版'), '需要跳转到其他应用，当前不执行'),
    (re.compile(r'PLUS\s*会员中心', re.I), '点击后会跳转到站外应用，不自动执行'),
    (re.compile(r'小组件|桌面'), '需要添加桌面小组件，不自动执行'),
    (re.compile(r'话费|充值|办卡|保险|贷款|借款|白条|金条|理财|基金'), '涉及充值或金融业务，不自动执行'),
    (re.compile(r'开通|绑定|实名|授权'), '需要授权或实名，不自动执行'),
)


def _norm(text):
    return normalize(text or '')


def is_task_list(nodes):
    """当前屏幕是否停在「做任务 领水滴」抽屉上。

    只认抽屉标题 + 至少一个任务按钮：农场首页也有「去浇水」之类的按钮，
    单看按钮会把首页误判成任务面板，于是脚本在首页反复点空。
    """
    texts = [_norm(n.text) for n in nodes]
    if '做任务领水滴' not in texts:
        return False
    return sum(1 for text in texts if text in ALL_ACTIONS) >= 1


def panel_top(nodes):
    """抽屉标题底边（像素）。找不到标题时返回 None。

    只用来确认「现在确实在抽屉里」：抽屉内容可以滚到标题上方（真机上见过
    任务行 y 落到 216 而标题仍停在 765），所以不能用它裁掉行。
    """
    for node in nodes:
        if _norm(node.text) == '做任务领水滴':
            return node.bounds[3]
    return None


class TaskRows:
    """把面板上的一屏行解析成 Task；只生成允许执行的任务。"""

    @staticmethod
    def _skip_reason(title):
        for pattern, reason in SKIP_RULES:
            if pattern.search(title):
                return reason
        return ''

    @staticmethod
    def _row_title(texts, action, top, width, height):
        """在按钮同一行、按钮左侧找「标题(进度)」，返回 (标题, 当前, 总数, 节点)。"""
        best = None
        for node, text in texts:
            if node is action:
                continue
            match = TITLE_PROGRESS.match(text)
            if not match:
                continue
            if node.bounds[2] > action.bounds[0] + width * .06:
                continue
            dy = abs(node.center[1] - action.center[1])
            if dy > height * .05:
                continue
            if best is None or (dy, node.bounds[1]) < best[0]:
                best = ((dy, node.bounds[1]), match, node)
        if best is None:
            return '', 0, 1, None
        _, match, node = best
        return match.group(1).strip(), int(match.group(2)), int(match.group(3)), node

    @staticmethod
    def _row_detail(texts, title_node, top, width, height):
        """标题正下方的说明行：给出「浏览6秒」这类真实停留时长。"""
        limit = title_node.bounds[3] + height * .05
        best = None
        for node, text in texts:
            if node is title_node or node.bounds[1] < title_node.bounds[3] - 4:
                continue
            if node.bounds[1] > limit:
                continue
            if text in ALL_ACTIONS or TITLE_PROGRESS.match(text):
                continue
            if abs(node.bounds[0] - title_node.bounds[0]) > width * .06:
                continue
            if best is None or node.bounds[1] < best[0]:
                best = (node.bounds[1], text)
        return best[1] if best else ''

    def parse_tasks(self, nodes, width, height):
        texts = [(node, _norm(node.text)) for node in nodes]
        top = panel_top(nodes)
        if top is None:
            return []
        default_seconds = int(self.config.get('browse_shortcut_seconds', 6))
        water_cap = int(self.config.get('water_times_per_run', 8))
        water_used = int(getattr(self, '_water_actions', 0))
        tasks, seen = [], set()
        for node, text in texts:
            if text not in ALL_ACTIONS:
                continue
            title, current, total, title_node = self._row_title(texts, node, top, width, height)
            if not title or title_node is None:
                continue
            detail = self._row_detail(texts, title_node, top, width, height)
            found = BROWSE_TIME.search(_norm(detail))
            seconds = int(found.group(1)) if found else 0
            current, total = min(current, total), max(1, total)
            if text in CLAIM_ACTIONS:
                kind, action, reason = 'claim', 'claim', ''
                key = title + '#claim'
            elif text in WATER_ACTIONS:
                key = title + '#water'
                if water_used >= water_cap:
                    kind, action, reason = 'manual', 'manual', '本轮浇水次数已达上限'
                else:
                    kind, action, reason = 'visit', 'browse', ''
            else:
                key = title + '#browse'
                reason = self._skip_reason(title)
                if not reason and text in OTHER_ACTIONS:
                    reason = '需要下单或开通，不自动执行'
                kind, action = ('manual', 'manual') if reason else ('browse', 'browse')
            if key in seen:
                continue
            seen.add(key)
            tasks.append(Task(
                key=key, title=title, detail=detail, action=action, bounds=node.bounds,
                kind=kind, skip_reason=reason, current=current, total=total,
                seconds=seconds or default_seconds,
            ))
        return tasks
