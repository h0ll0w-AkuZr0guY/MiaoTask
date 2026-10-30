"""Conservative interpretation of a task's explicit secondary browse instruction."""
import re

_BROWSE = re.compile(r'浏览|停留|观看|逛|滑动|下滑')
_TIME = re.compile(r'(\d{1,3})\s*(?:秒|[sS]\b)')
_REQUIRED = re.compile(r'支付|下单|购买|充值|开通|登录|登陆|授权|实名|安装|下载|邀请|助力|分享|加购|收藏|关注|提交|填写|完成答题|答对|回答|参与考试')


def apply_browse_contract(task):
    detail = task.detail or ''
    if task.kind == 'done' or not _BROWSE.search(detail) or _REQUIRED.search(detail):
        return task
    # A promotional quiz title can describe a browse offer; an actual quiz
    # requirement in the instruction or another protected action cannot.
    if task.kind == 'manual':
        title = re.sub(r'答题|考试', '', task.title)
        if _REQUIRED.search(title) or re.search(r'游戏|抽奖|拉人|组队', title):
            return task
        if not re.search(r'答题|考试', task.title):
            return task
        task.kind, task.skip_reason = 'browse', ''
    if task.kind == 'browse':
        duration = _TIME.search(detail)
        if duration and int(duration[1]) > 0:
            task.seconds = int(duration[1])
        else:
            task.browse_once = True
    return task
