from backend.app.taobao.utils.daily import DailyActivity
from backend.core.config.loader import load_config
from backend.core.models import normalize


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
