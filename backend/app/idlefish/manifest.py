"""闲鱼（Idle Fish）应用注册。

参考 backend/app/taobao/manifest.py 的写法：只声明包名、用户空间匹配与
应用级配置路径，业务规则下沉到 activities/*。坐标与机型不写在这里。
"""
from backend.core.device.profile import ApplicationProfile
from backend.core.registry import AppMeta

PROFILE = ApplicationProfile(
    label='闲鱼',
    android_package='com.taobao.idlefish',
    package_pattern=r'com\.taobao\.idlefish(?:[._][A-Za-z0-9_]+)*',
)

APP = AppMeta('idlefish', '闲鱼', PROFILE, default=False)
