"""月月赚任务列表解析。

真机验证（2026-10-25，小米平板 2032x3048 / 支付宝 XRiverActivity）：
* 任务列表是 H5，但内容完整暴露在无障碍树里：标题在左、"去完成"按钮在右。
* 同一个列表里混着大量**非浏览类**商业任务（办流量卡、充话费、买保险、
  ETC、订火车票、订酒店、填问卷…）。这些一律不碰。

因此这里**硬编码**浏览类过滤，而不是放进 config：
安全属性不能因为改配置而失效——只能按「标题」命中浏览类才生成任务，
否则今天的手滑会让脚本去办一张流量卡。
"""
import re

from backend.core.models import Task, normalize

CLAIM_ACTIONS = frozenset({'claim'})
EXTERNAL_APPS = ()

# 浏览类任务的标题特征。真机(2026-10-26)确认浏览任务形如：
#   「逛余额宝领10~18元体验金」「逛乐游记得现金好礼」——以「逛」开头
BROWSE_TITLE_RE = re.compile(r'浏览|逛|看一看|看看|逛逛|瞧一瞧|视频')

# 列表中永远不是"任务按钮"的文案，避免把它们当成任务标题
BUTTON_TEXT = {'去完成', '去看看', '领取', '立即领取', '做任务', '更多任务',
               '已完成', '去浏览', '去逛逛'}

# 浏览类任务的按钮文案（真机确认是「去看看」，商业任务是「去完成」）
BROWSE_BUTTONS = {'去看看', '去完成', '去逛逛'}


def _norm(text):
    return normalize(text or '')


def _has_branded_title(nodes):
    """顶部标题栏里必须有精确的「月月赚」——这是月月赚列表页的身份锁。

    第三方活动任务列表（青春特权/网商银行…）同样满是「去完成」按钮和
    「逛xx」标题，它们的标题栏写的是自己的名字，不会有「月月赚」。
    """
    bottom = max((n.bounds[3] for n in nodes), default=0) or 1
    # 只要求它出现在页面上半部分（标题栏位置）：不同屏幕（平板 3048 高、手机 2400 高）
    # 的标题栏控件高度占比不一样，卡死高度会让小屏设备被判成"不是列表页"。
    return any(_norm(n.text) == '月月赚' and n.bounds[1] < bottom * 0.5
               for n in nodes)


def is_task_list(nodes):
    """当前屏幕是否为月月赚任务列表页。"""
    texts = [_norm(n.text) for n in nodes]
    # 浏览类任务的按钮是「去看看 / 去逛逛 / 去浏览」，商业任务才是「去完成」；
    # 只看「去完成」会把整屏都是浏览任务的页面判成"不在任务列表"。
    # 任务行的按钮文本是固定的这几种，必须精确匹配：
    # 抽奖页之类的活动内页也会出现「转账红包/赚更多」等字样，子串匹配会误判。
    buttons = ('去完成', '去看看', '去逛逛', '去浏览', '去领取', '领取', '已完成')
    actions = [t for t in texts if t in buttons]
    if not actions:
        return False
    # 搜索结果页同样有「月月赚 + 去看看」，但只有一个入口按钮并带搜索提示，
    # 误判成任务列表会让整个流程卡在搜索页。
    if any('语音提问' in t or '取消搜索' in t for t in texts):
        return False
    if len(actions) < 2:
        return False
    # 任务标题里含「赚现金」「赚更多」就用子串命中，会把第三方活动页当成
    # 月月赚列表，于是在人家页面里继续点任务。这里改用标题栏精确身份判定。
    return _has_branded_title(nodes)


def _title_near(nodes, button, width, height):
    """在按钮同行或紧邻上方找任务标题。

    按「纵向中心距离」排序，其次看纵向空隙与横向间隔。
    只用"按钮顶与标题底的空隙"会在多个候选中并列为 0，
    从而误选上一行的商业任务标题（真机上遇到过）。
    """
    bx0, by0, bx1, by1 = button.bounds
    bcy = (by0 + by1) / 2
    best = None
    best_key = None
    for n in nodes:
        if n is button:
            continue
        t = _norm(n.text)
        if not t or t in BUTTON_TEXT:
            continue
        nx0, ny0, nx1, ny1 = n.bounds
        if ny0 > by1:                      # 完全在按钮下方，属于另一行
            continue
        gap = max(0, by0 - ny1)            # 与按钮顶部的纵向空隙
        if gap > height * 0.12:            # 离得太远，不是同一行
            continue
        hdist = max(0, bx0 - nx1)          # 标题在按钮左侧的横向间隔
        if hdist > width * 0.35:           # 横向太远
            continue
        key = (round(abs((ny0 + ny1) / 2 - bcy)), gap, hdist)
        if best_key is None or key < best_key:
            best_key, best = key, t
    return best


def parse_tasks(nodes, width, height):
    """只解析「浏览类」任务，其余一概跳过。"""
    tasks = []
    seen = set()
    for n in nodes:
        if _norm(n.text) not in BROWSE_BUTTONS:
            continue
        title = _title_near(nodes, n, width, height)
        if not title or not BROWSE_TITLE_RE.search(title):
            continue  # 商业/金融类任务，安全起见直接跳过
        if title in seen:
            continue
        seen.add(title)
        tasks.append(Task(
            key=title + '#browse',
            title=title,
            detail=_norm(n.text),
            action='browse',
            bounds=n.bounds,
            kind='browse',
            current=0,
            total=1,
        ))
    return tasks
