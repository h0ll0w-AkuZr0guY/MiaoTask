"""支付宝应用注册。"""
from backend.core.registry import AppMeta
from backend.core.device.profile import ApplicationProfile

PROFILE = ApplicationProfile(
    '支付宝', 'com.eg.android.AlipayGphone',
    r'com\.eg\.android\.AlipayGphone(?:[._][A-Za-z0-9_]+)*',
    'com.eg.android.AlipayGphone',
    ('com.eg.android.AlipayGphone@0',),
)
APP = AppMeta('zfb', '支付宝', PROFILE, default=False)
