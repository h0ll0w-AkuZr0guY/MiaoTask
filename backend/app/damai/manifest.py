from backend.core.device.profile import ApplicationProfile
from backend.core.registry import AppMeta

APP = AppMeta(
    'damai',
    '大麦',
    ApplicationProfile('大麦', 'cn.damai', r'^cn\.damai$', 'cn.damai', ('cn.damai@0',)),
)
