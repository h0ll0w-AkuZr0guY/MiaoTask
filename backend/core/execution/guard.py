"""Human verification is a stop condition, never an automatic return action."""
import re
from backend.core.models import normalize
from backend.core.errors import NeedsAttention

# 只认“必须人动手”的完整提示，不认裸名词。
# 搜索结果、导购页、商品主图上「HG系列导轨滑块」「需要验证码?不上号」这类
# 货架文案天天出现，按裸词命中就会整轮停在一个根本没有验证的页面上。
_SECURITY = re.compile(
    r'将满足描述的所有图片拖放到指定区域|拖放.{0,10}图片|'
    r'拖动.{0,10}(?:验证|拼图|滑块)|按住.{0,6}滑块|'
    r'滑块.{0,6}(?:验证|拼图)|拼图验证|向右.{0,6}滑(?:动|块).{0,10}(?:验证|拼图)|'
    r'请完成(?:下列|下方|安全)?验证|'
    r'(?:输入|填写|获取|发送|重新获取|校验|核对)[^。；;]{0,6}验证码|'
    r'验证码[^。；;]{0,6}(?:错误|不正确|已发送|失效)|'
    r'人机验证|行为验证|安全验证|操作过于频繁|账号存在风险|访问被拒绝')
_LOGIN = re.compile(
    r'短信登录|验证码登录|登录即同意|请先登录|立即登录|授权登录|欢迎登录|其他方式登录|扫一扫快速登录|确认付款|支付密码|确认支付')

SECURITY_REASON = '页面要求安全验证，自动操作已停止；请在手机上本人处理，完成后点击开始自动执行'
LOGIN_REASON = '页面要求登录、授权或支付，请在手机处理后从当前页面重新开始'
OBSCURED_REASON = '当前应用画面不可读取，可能处于受保护页面；已停止自动输入，请本人检查设备'


def attention_reason(session, screen):
    """判定这一帧是否卡在需要人处理的页面，返回 (reason, phase) 或 None。

    只做判断、不保存现场：调用方可能要先等一会儿再看一眼（登录/推广浮层常常
    是过渡帧或自己会关掉），这时不该每次都生成一份失败证据。
    """
    text = normalize(screen.text)
    if _SECURITY.search(text):
        return SECURITY_REASON, 'security'
    attention_text = text
    passive = getattr(session, 'passive_login_banner', None)
    if callable(passive) and passive(screen):
        attention_text = normalize('\n'.join(
            n.text for n in screen.nodes
            if normalize(n.text) != '立即登录' or n.center[1] < screen.height * .8))
    if _LOGIN.search(attention_text):
        return LOGIN_REASON, 'attention'
    if getattr(screen, 'obscured', False):
        return OBSCURED_REASON, 'attention'
    return None


def check_attention(session, screen):
    found = attention_reason(session, screen)
    if not found:
        return
    reason, phase = found
    saved = session.report_failure(reason, phase=phase)
    raise NeedsAttention(reason, kind='security' if phase == 'security' else 'attention',
                         evidence_saved=saved)
