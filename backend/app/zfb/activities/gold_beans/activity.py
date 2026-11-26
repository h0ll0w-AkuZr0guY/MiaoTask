from backend.app.zfb.task_panel import AlipayTaskPanel
from backend.core.errors import TaskUnavailable
from backend.core.models import normalize


class GoldBeansActivity(AlipayTaskPanel):
    activity_id = 'gold_beans'

    def _browse_confirmation(self, screen, task):
        visit = self.config['homepage_visit']
        if normalize(task.title) != visit['title']:
            return super()._browse_confirmation(screen, task)
        if self.config['ocr'] and screen.source == 'ui':
            screen = self._read(visual=True)
        if screen.package != self.origin or not self._groups(screen.nodes, visit['prompt']):
            return screen
        forward = self._button(screen, visit['confirm_labels'], top=.78, left=.52)
        if forward is None:
            raise TaskUnavailable('未找到金豆首页访问确认按钮')
        self.click(forward)
        deadline = self.control.now() + self.config['page_timeout']
        while self.control.now() < deadline:
            self.control.wait(.7)
            screen = self._read()
            if screen.package != self.origin or self._groups(screen.nodes, self.config['landing_groups']):
                return screen
            home = screen.find(r'^搜索框$|^搜索栏$') and screen.find(r'^首页$|^我的$')
            if home:
                shortcut = self._button(screen, visit['shortcut_labels'], top=.1, bottom=.6)
                if shortcut is None:
                    raise TaskUnavailable('请先在支付宝首页「常用」添加金豆夺宝入口')
                self.click(shortcut)
                self.control.wait(1.2)
                return self._read()
        raise TaskUnavailable('金豆首页访问未返回可识别入口')
