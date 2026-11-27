from backend.core.registry import ActivityMeta

META = ActivityMeta(
    'damai',
    'ticket_grab',
    '定时抢票与坐标校准',
    'backend.app.damai.activities.ticket_grab.activity:DamaiTicketActivity',
    config='config.json',
    device_types=('uiautomator2',),
    default=True,
)
