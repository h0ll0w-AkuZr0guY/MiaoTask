from backend.core.registry import ActivityMeta

META = ActivityMeta(
    'jd', 'dongdong_farm', '东东农场',
    'backend.app.jd.activities.dongdong_farm.activity:DongDongFarmActivity',
    config='config.json', default=True,
)
