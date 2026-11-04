"""京东应用注册。"""
from backend.core.registry import AppMeta
from backend.core.device.profile import ApplicationProfile

PROFILE = ApplicationProfile(
    '京东', 'com.jingdong.app.mall',
    r'com\.jingdong\.app\.mall(?:[._][A-Za-z0-9_]+)*',
    'com.jingdong.app.mall',
    ('com.jingdong.app.mall@0',),
)
APP = AppMeta('jd', '京东', PROFILE, default=False)
