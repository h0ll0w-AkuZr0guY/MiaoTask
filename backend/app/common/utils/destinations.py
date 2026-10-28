"""External destination availability and installation evidence."""
import logging
import re
from backend.core.errors import TaskUnavailable
from backend.core.models import normalize
logger = logging.getLogger('backend.execution')

# Application configs can append aliases under jump_words without changing the
# matcher. Context and action words are separate; a generic 确认 is insufficient.
JUMP_WORDS = {
    'request': [r'(?:想要|即将|将要|尝试).{0,8}(?:打开|启动|跳转|唤起)',
                r'(?:正在|即将)?离开.{0,20}[,，].{0,4}(?:打开|启动|跳转)',
                r'是否.{0,16}(?:打开|启动|跳转)', r'^(?:打开|启动|跳转).{0,16}(?:应用|APP)[?？]?$',
                r'允许.{0,12}打开'],
    'allow': ['允许','允许本次','本次允许','仅此一次','仅允许一次','允许仅本次',
              '确认','确定','确认打开','确认并继续','打开','立即打开','继续打开','允许打开','继续','去看看','去支付宝'],
    'deny_context': [r'位置权限|位置信息|相机权限|录音权限|通讯录|读取短信|读取照片|访问照片',
                     r'确认付款|支付密码|确认支付|短信登录|授权登录|安装应用|下载并安装'],
}

class DestinationPolicy:
    def jump_word_patterns(self, group):
        extra = getattr(self,'config',{}).get('jump_words',{}).get(group,[])
        if not isinstance(extra,list) or any(not isinstance(word,str) or not word for word in extra):
            raise ValueError('jump_words 词表分组必须为非空字符串列表')
        return [normalize(word) for word in [*JUMP_WORDS[group],*extra]]

    def _launch_context(self, screen):
        text=normalize(screen.text)
        if any(re.search(pattern,text,re.I) for pattern in self.jump_word_patterns('deny_context')):
            return []
        prompts=[n for n in screen.nodes if any(re.search(pattern,normalize(n.text),re.I)
                 for pattern in self.jump_word_patterns('request'))]
        if not prompts and any(re.search(pattern,text,re.I) for pattern in self.jump_word_patterns('request')):
            prompts=[n for n in screen.nodes if re.search(r'打开|启动|跳转|唤起',normalize(n.text))]
        return prompts

    def is_launch_prompt(self, screen):
        return bool(self._launch_context(screen))

    def launch_confirmation(self, screen):
        """Loose wording inside a confirmed app-launch scene, never grants."""
        prompts=self._launch_context(screen)
        if not prompts:
            return None
        candidates=[]
        for node in screen.nodes:
            label=re.sub(r'[()（）\[\]【】>›〉》:：。.!！?？]','',normalize(node.text))
            if (re.search(r'始终|总是|不再询问|安装|下载|付款|支付|购买',label)
                    or not 0<node.bounds[3]-node.bounds[1]<screen.height*.15
                    or node.center[1]<min(n.center[1] for n in prompts)):
                continue
            if any(re.fullmatch(pattern,label,re.I) for pattern in self.jump_word_patterns('allow')):
                candidates.append(node)
        return min(candidates,key=lambda n:(not any(w in normalize(n.text) for w in ('本次','一次')),n.bounds[1])) if candidates else None

    def availability_reason(self, task, check_installed=False):
        # Failed confirmation is a task fact, not proof that every route to an
        # installed application is unavailable.
        reason=self.unavailable_tasks.get('task:'+normalize(task.title),'')
        return reason or super().availability_reason(task,check_installed)

    def check_destination(self, screen):
        if self.is_task_list(screen.nodes):
            return
        markets = {'com.xiaomi.market', 'com.android.vending', 'com.huawei.appmarket',
                   'com.oppo.market', 'com.heytap.market', 'com.bbk.appstore',
                   'com.tencent.android.qqdownloader'}
        if (screen.package in markets or screen.find(
                r'未安装|尚未安装|没有安装|未找到.{0,8}应用|找不到.{0,8}应用|应用不存在|'
                r'没有可处理|无法.{0,4}(?:打开|跳转)|^(?:立即下载|下载并安装|安装并打开)$')):
            raise TaskUnavailable('目标应用不可用或跳转到安装页，已跳过，不再重复尝试')


    def unavailable_landing(self, screen):
        try:
            self.check_destination(screen)
        except TaskUnavailable as exc:
            return str(exc)
        return ''


    def remember_missing_destination(self, screen):
        """Without an active task, only persist an identified, absent app fact."""
        if self.device.device_type != 'uiautomator2':
            return
        try:
            packages = self.device.installed_packages()
        except Exception:
            return
        if not isinstance(packages, (set, list, tuple)):
            return
        for name, targets in self.external_apps:
            if name in screen.text and not set(packages).intersection(targets):
                reason = f'未安装{name}，已跳过；安装后重新识别即可恢复'
                self.unavailable_tasks['app:' + name] = reason
                logger.info(reason)
