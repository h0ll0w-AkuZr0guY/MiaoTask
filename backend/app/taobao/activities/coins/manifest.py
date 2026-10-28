from backend.core.registry import ActivityMeta

META = ActivityMeta('taobao', 'coins', '淘金币日常',
    'backend.app.taobao.activities.coins.activity:CoinsActivity', device_types=('uiautomator2',))
