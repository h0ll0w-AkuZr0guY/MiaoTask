from backend.app.taobao.utils.daily import DailyActivity
from backend.core.config.loader import load_config


class FarmActivity(DailyActivity):
    @staticmethod
    def default_config():
        return load_config(app='taobao', activity='farm')
