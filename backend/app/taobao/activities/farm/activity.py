from backend.app.taobao.utils.daily import DailyActivity
from backend.core.config.loader import load_config
from backend.core.models import TextNode, normalize


class FarmActivity(DailyActivity):
    @staticmethod
    def default_config():
        return load_config(app='taobao', activity='farm')

    def official_activity_entry(self, screen):
        entry = super().official_activity_entry(screen)
        if entry is not None:
            return entry
        pattern = self.page.get('official_result_subtitle')
        subtitle = screen.find(pattern) if pattern else None
        if subtitle is None or subtitle.center[1] >= screen.height * .23:
            return None
        return next((node for node in screen.nodes
                     if normalize(node.text) in self.config['entry_labels']
                     and abs(node.bounds[0] - subtitle.bounds[0]) < screen.width * .03
                     and 0 < subtitle.center[1] - node.center[1] < screen.height * .055), None)

    def verify(self, task, before, after, explicit=False):
        if (task.title == '农场每日签到' and task.kind == 'claim' and before.package == after.package
                and self.application.matches(after.package) and before.width == after.width
                and before.height == after.height and self.is_task_list(after.nodes)):
            self.assert_instance(before)
            self.assert_instance(after)
            x, y = TextNode('', task.bounds).center

            def at_sign_button(node, label):
                return (normalize(node.text) == label and abs(node.center[0] - x) < after.width * .025
                        and abs(node.center[1] - y) < after.height * .02)

            if (any(at_sign_button(node, '去签到') for node in before.nodes)
                    and any(at_sign_button(node, '已完成') for node in after.nodes)):
                return True
        return super().verify(task, before, after, explicit)
