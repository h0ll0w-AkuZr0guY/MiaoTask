from backend.app.taobao.utils.daily import DailyActivity
from backend.core.config.loader import load_config


class CashSigninActivity(DailyActivity):
    def lonely_close(self, screen, node):
        if super().lonely_close(screen, node):
            return True
        # Product captions remain visible behind this centred promotion.
        # Only its lower central close is allowed to overlap background text.
        promotion = screen.find(self.page['promotion_markers'])
        return bool(promotion and screen.find(self.page['main_view_markers'])
                    and node.center[1] > max(promotion.bounds[3], screen.height * .5)
                    and node.center[1] < screen.height * .9
                    and abs(node.center[0] - screen.width * .5) < screen.width * .1)

    @staticmethod
    def default_config():
        return load_config(app='taobao', activity='cash-signin')
