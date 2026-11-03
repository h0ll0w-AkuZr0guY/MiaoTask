"""Task action classification; editable keyword policy owns text-based exclusion."""
import re
from backend.core.models import normalize

ACTION_RE = re.compile(r"^(?:去完成|去浏览|去逛逛|去看看|去看一看|看一看|点一点|去瞧瞧|瞧瞧|立即看看|去查看|立即查看|去体验|去搜索|去观看|去参与|去领取|领取奖励|立即领取|领能量|领体力|领奖励|已完成|已领取|明日再来|去签到)$")
CLAIM_ACTIONS = {"去领取", "领取奖励", "立即领取", "领能量", "领体力", "领奖励"}
DONE_ACTIONS = {"已完成", "已领取", "明日再来"}
PROGRESS_RE = re.compile(r"[([]\s*(\d+)\s*/\s*(\d+)\s*[)\]]")
# Map explicit app names and known Alipay mini-app brands; internal channels
# such as 淘宝闪购 remain usable without installing another application.
EXTERNAL_APPS = (
    ('快手极速版', ('com.kuaishou.nebula',)),
    ('快手', ('com.smile.gifmaker', 'com.kuaishou.nebula')),
    ('微博', ('com.sina.weibo', 'com.sina.weibolite')),
    ('抖音', ('com.ss.android.ugc.aweme', 'com.ss.android.ugc.aweme.lite')),
    ('支付宝', ('com.eg.android.AlipayGphone',)),
    # 天猫与淘宝直播会落在独立应用上；同一任务的备用包名保留淘宝本体，
    # 因为部分版本在淘宝内以 webview 容器打开，包名并不切换。
    ('天猫', ('com.tmall.wireless', 'com.taobao.taobao')),
    ('淘宝直播', ('com.taobao.live', 'com.taobao.taobao')),
    ('百度极速版', ('com.baidu.searchbox.lite',)),
    # 头条系任务要求真的停在资讯流里攒时长；不登记包名就只能按通用 8 秒
    # 停留，回来时奖励还没到账。
    ('头条', ('com.ss.android.article.news', 'com.ss.android.article.lite')),
    # UC 类红包任务在未安装时会落到系统浏览器，提前识别可避免整轮被带偏。
    ('UC', ('com.UCMobile', 'com.ucmobile')),
    ('京东', ('com.jingdong.app.mall',)),
    ('拼多多', ('com.xunmeng.pinduoduo',)),
    ('闲鱼', ('com.taobao.idlefish',)),
    ('菜鸟', ('com.cainiao.wireless',)),
    ('高德', ('com.autonavi.minimap',)),
    ('优酷', ('com.youku.phone',)),
)



def classify(title, detail, texts, action, current, total):
    combined = normalize(' '.join(texts))
    duration = re.search(r"(?:浏览|停留|观看|逛|滑动|下滑)[^\d]{0,5}(\d{1,3})[秒sS]", combined)
    seconds = int(duration.group(1)) if duration else 0
    kind, reason = "browse", ""
    target_app, packages = next(((name, packages) for name, packages in EXTERNAL_APPS
                                 if name in title), ('', ()))
    if not target_app and re.search(r'蚂蚁庄园|蚂蚁森林', title):
        target_app, packages = '支付宝', ('com.eg.android.AlipayGphone',)
    flash_visit = (title == '领闪购红包下单直接省' and '点击去逛' in combined
                   and not re.search(r'需下单|完成下单|支付|购买|消费|订单', detail))
    if action in DONE_ACTIONS or current >= total:
        kind, reason = "done", "今日已完成"
    elif action in CLAIM_ACTIONS:
        kind = "claim"
    elif flash_visit or target_app:
        kind = "visit"
    elif re.search(r"搜一搜|搜索", combined):
        kind = "search"
    elif re.search(r"支付宝|蚂蚁庄园|蚂蚁森林|农场|菜鸟|闲鱼|饿了么|优酷|高德|淘宝闪购|天猫|去逛", combined):
        kind = "visit"
    return seconds, kind, reason, target_app, packages
