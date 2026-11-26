from backend.core.registry import ActivityMeta

META = ActivityMeta(
    'zfb', 'ant_forest', '蚂蚁森林',
    'backend.app.zfb.activities.ant_forest.activity:AntForestActivity',
    device_types=('uiautomator2',),
)
