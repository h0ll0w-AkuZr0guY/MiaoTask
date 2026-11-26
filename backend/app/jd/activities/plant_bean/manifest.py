from backend.core.registry import ActivityMeta

META = ActivityMeta('jd', 'plant_bean', '种豆得豆',
    'backend.app.jd.activities.plant_bean.activity:PlantBeanActivity',
    device_types=('uiautomator2',))
