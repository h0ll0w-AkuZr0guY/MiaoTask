from backend.core.registry import ActivityMeta

META = ActivityMeta(
    'zfb', 'yueyuezhuan', '月月赚',
    'backend.app.zfb.activities.yueyuezhuan.activity:YueYueZhuanActivity',
    config='config.json', default=True,
)
