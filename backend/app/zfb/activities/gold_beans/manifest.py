from backend.core.registry import ActivityMeta

META = ActivityMeta(
    'zfb', 'gold_beans', '金豆夺宝',
    'backend.app.zfb.activities.gold_beans.activity:GoldBeansActivity',
    device_types=('uiautomator2',),
)
