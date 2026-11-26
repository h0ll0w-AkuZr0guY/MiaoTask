from backend.app.zfb.task_panel import AlipayTaskPanel
from backend.core.models import normalize


class AntForestActivity(AlipayTaskPanel):
    activity_id = 'ant_forest'

    def is_task_list(self, nodes):
        labels = {normalize(node.text) for node in nodes}
        if self.config['name'] in labels and self.config['panel_close_label'] in labels:
            # The close control stays on screen while the sign-in card scrolls away.
            return True
        if super().is_task_list(nodes):
            return True
        visual_labels = {normalize(node.text) for node in nodes if node.source.startswith('ocr')}
        if not set(self.config['visual_panel_labels']).issubset(visual_labels):
            return False
        return self._groups(nodes, self.config['visual_panel_groups'])
