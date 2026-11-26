from backend.core.registry import ActivityMeta

META = ActivityMeta(
    'zfb', 'baba_farm', '芭芭农场',
    'backend.app.zfb.activities.baba_farm.activity:BabaFarmActivity',
    device_types=('uiautomator2',),
)
