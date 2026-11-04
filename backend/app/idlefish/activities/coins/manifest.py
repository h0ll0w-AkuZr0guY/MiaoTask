"""闲鱼币大富翁活动注册。

factory 指向 activity.py 中的活动类；device_types 限定为 uiautomator2。
"""
from backend.core.registry import ActivityMeta

META = ActivityMeta(
    'idlefish', 'coins', '闲鱼币大富翁',
    'backend.app.idlefish.activities.coins.activity:IdleFishCoinsActivity',
    config='config.json', device_types=('uiautomator2',), default=True,
)
