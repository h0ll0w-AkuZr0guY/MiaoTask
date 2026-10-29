"""Android app instances are a package AND a user, never a package alone."""
from dataclasses import asdict, dataclass
import re
from backend.core.device.read_shell import parse_package_list


@dataclass(frozen=True)
class AppInstance:
    package: str
    user_id: int | None
    label: str

    @property
    def id(self):
        return f'{self.package}@{self.user_id if self.user_id is not None else "native"}'

    def to_dict(self):
        return dict(asdict(self), id=self.id)


class InstanceMismatch(RuntimeError):
    """Do not send any more input to a different/unknown application instance."""
    kind = 'instance'
    evidence_saved = False


def parse_foreground(output):
    # Window focus is the visible surface, not any background activity record.
    patterns = (
        r'mCurrentFocus=Window\{[^\n]*?\bu(\d+)\s+([\w.]+)/([\w.$]+)',
        # 部分机型（OPPO/ColorOS）下拉通知栏或有系统浮层时，mCurrentFocus 落在
        # NotificationShade 等非应用窗口上，只有 mFocusedApp 仍指向真实前台应用。
        r'mFocusedApp=ActivityRecord\{[^\n]*?\bu(\d+)\s+([\w.]+)/([\w.$]+)',
        r'topResumedActivity=ActivityRecord\{[^\n]*?\bu(\d+)\s+([\w.]+)/([\w.$]+)',
        r'mResumedActivity[:=]\s*ActivityRecord\{[^\n]*?\bu(\d+)\s+([\w.]+)/([\w.$]+)',
    )
    for pattern in patterns:
        match = re.search(pattern, output)
        if match:
            user_id, package, activity = match.groups()
            return dict(package=package, activity=activity, user_id=int(user_id))
    return None


def discover_android_instances(shell, profile=None):
    if profile is None:
        from backend.core.registry import default_registry
        profile = default_registry().default_app().profile
    def query(args):
        try:
            result = shell(args)
            output = result.output
            if (result.exit_code != 0 or not isinstance(output, str)
                    or re.search(r'SecurityException|(?:^|\n)\s*(?:Error:|Error |Exception|Failure|Permission denied|Unknown command)', output, re.I)):
                return None
            return output
        except Exception:
            # SDK sentinel errors can carry partial/wrong output. Never parse it
            # as evidence of an installed app, and never abort unrelated users.
            return None

    users = []
    for command in (['pm', 'list', 'users'], ['cmd', 'user', 'list']):
        output = query(command)
        if output is not None:
            users = sorted({int(value) for value in re.findall(r'UserInfo\{(\d+):', output)})
        if users:
            break
    if not users:
        raise RuntimeError('未取得应用用户空间列表，请检查 USB 调试权限')
    instances, warnings = [], []
    for user in users:
        packages, confirmed = [], False
        package = profile.package_for('uiautomator2')
        for filtered in (True, False):
            for prefix in (['pm', 'list', 'packages'], ['cmd', 'package', 'list', 'packages']):
                output = query(prefix + ['--user', str(user)] + ([package] if filtered else []))
                if output is None:
                    continue
                found = parse_package_list(output)
                if found is None:
                    continue
                packages = sorted(value for value in found if profile.matches(value))
                if packages or not filtered and found:
                    confirmed = True
                    break
            if confirmed:
                break
        if not confirmed:
            warnings.append(f'用户空间 {user} 的应用列表读取失败或返回空结果，无法确认是否安装 {profile.label}；未列为可执行实例，请重连后刷新')
        elif not packages:
            warnings.append(f'用户空间 {user} 已读取应用列表，未发现 {profile.label}；未列为可执行实例')
        for package in packages:
            label = profile.label + ' · 主应用（用户 0）' if user == 0 else f'{profile.label} · 分身 / 用户空间 {user}'
            if package != profile.package_for('uiautomator2'):
                label += f' · {package}'
            instances.append(AppInstance(package, user, label))
    return instances, warnings
