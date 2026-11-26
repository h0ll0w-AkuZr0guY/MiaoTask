import re

from backend.app.taobao.utils.daily import DailyActivity
from backend.core.config.loader import load_config
from backend.core.models import normalize


class AchievementsActivity(DailyActivity):
    @staticmethod
    def default_config():
        return load_config(app='taobao', activity='achievements')

    def is_task_list(self, nodes):
        points = self.page.get('points_panel_markers')
        shop = self.page.get('merchandise_panel_markers')
        captions = '\n'.join(normalize(node.text) for node in nodes)
        if shop and re.search(shop, captions) and not (points and re.search(points, captions)):
            return False
        return super().is_task_list(nodes)

    def entry_node(self, screen, labels):
        if self.config['ocr'] and screen.source == 'ui':
            # The native footer may still be exposed while merchandise covers it.
            labels = [label for label in labels if label != '我的成就']
        return super().entry_node(screen, labels)
