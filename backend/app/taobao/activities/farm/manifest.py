from backend.core.registry import ActivityMeta

META = ActivityMeta('taobao', 'farm', '芭芭农场',
    'backend.app.taobao.activities.farm.activity:FarmActivity', device_types=('uiautomator2',))
