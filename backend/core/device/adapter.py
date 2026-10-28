"""
设备适配器层 - 统一hmdriver2和uiautomator2的API

通过此适配器，业务代码无需关心底层驱动差异，
使用统一的API操作鸿蒙(HarmonyOS)和安卓(Android)设备。
"""
import logging
import io
import re
from contextlib import contextmanager, nullcontext
from functools import wraps
import shlex
import tempfile
import threading
import time
from pathlib import Path
import xml.etree.ElementTree as ET
from backend.core.device.instances import AppInstance, InstanceMismatch, discover_android_instances, parse_foreground

logger = logging.getLogger(__name__)


def serialized(method):
    """Keep preview captures and task RPCs on one device from interleaving."""
    @wraps(method)
    def call(self, *args, **kwargs):
        with getattr(self, 'io_lock', None) or nullcontext():
            mutating = method.__name__ not in {'screenshot', 'current_app', 'dump_hierarchy', 'installed_packages', 'refresh_app_instances'}
            if getattr(self, '_instance_guard_active', False) and method.__name__ in {
                    'click', 'swipe', 'swipe_ext', 'go_back', 'input_text', 'input_ascii', 'clear_text', 'press'}:
                self.check_instance(self.current_app())
            if mutating:
                self._screenshot_cache = None
            try:
                return method(self, *args, **kwargs)
            finally:
                if mutating:
                    self._screenshot_cache = None
    return call


class DeviceAdapter:
    """设备适配器，统一HarmonyOS和Android的UI自动化接口"""

    def __init__(self, device_type, serial=None, on_status=None, application=None, on_resolved=None):
        """
        初始化设备适配器

        Args:
            device_type: 'hmdriver2' 或 'uiautomator2'（必填）
            serial: 设备序列号或无线连接地址（如 '192.168.1.100:5555'）
        """
        self._application = application
        self.on_resolved = on_resolved
        self.device_type = device_type
        self.serial = serial
        self.io_lock = threading.RLock()
        self.on_status = on_status or logger.info
        self._device = None
        self._last_xml = None  # 缓存uiautomator2的原始XML，避免json2xml重复调用
        self.app_instances = []
        self.selected_instance = None
        self._instance_guard_active = False
        self._init_device()
        self.refresh_app_instances()

    @property
    def application(self):
        from backend.core.registry import default_registry
        return getattr(self, '_application', None) or default_registry().default_app().profile

    def bind_application(self, profile):
        if getattr(self, '_instance_guard_active', False):
            raise ValueError('执行期间不能切换应用')
        self._application = profile
        return self.refresh_app_instances()

    def _init_device(self):
        """初始化底层设备驱动"""
        if self.device_type == 'hmdriver2':
            import hmdriver2
            from hmdriver2.driver import Driver
            hmdriver2.driver.logger.disabled = False
            if self.serial and ':' in self.serial:
                # 无线连接：先通过 hdc tconn 连接远程设备
                import subprocess
                logger.info(f'通过WiFi连接鸿蒙设备: {self.serial}')
                result = subprocess.run(
                    ['hdc', 'tconn', self.serial],
                    capture_output=True, text=True, timeout=10
                )
                if 'Connect OK' not in result.stdout and result.returncode != 0:
                    raise ConnectionError(
                        f'hdc tconn {self.serial} 失败: {result.stderr or result.stdout}\n'
                        f'请确保: 1. 先用USB执行 hdc tmode port 5555 开启TCP模式\n'
                        f'        2. 设备与电脑在同一局域网'
                    )
                logger.info(f'WiFi连接成功: {self.serial}')
            self._device = Driver(serial=self.serial)
        elif self.device_type == 'uiautomator2':
            import uiautomator2 as u2
            from backend.core.device.discovery import prepare_android_connection
            self.serial = prepare_android_connection(self.serial, on_status=self.on_status)
            claimed_identity = self.on_resolved(self.device_type, self.serial) if self.on_resolved else None
            if self.serial:
                # serial 可以是 IP:PORT（无线）或设备序列号（USB）
                logger.info(f'连接安卓设备: {self.serial}')
                self._device = u2.connect(self.serial)
            else:
                self._device = u2.connect()
            from backend.core.device.rpc_limits import install_rpc_limits
            install_rpc_limits(self._device)
            # 设置默认等待超时
            self._device.settings['wait_timeout'] = 3
            self._device.settings['operation_delay'] = (0.1, 0.1)
            self._device.settings['operation_delay_methods'] = [
                method for method in self._device.settings['operation_delay_methods'] if method != 'swipe']
            self.identity_serial = claimed_identity or self._android_identity() or self.serial
        else:
            raise ValueError(f"不支持的设备类型: {self.device_type}")

    def _android_identity(self):
        """Keep USB and Wi-Fi transports of the same handset in one daily record."""
        for prop in ('ro.serialno', 'ro.boot.serialno'):
            try:
                result = self._device.shell(['getprop', prop])
                value = result.output.strip()
                if (result.exit_code == 0 and re.fullmatch(r'[A-Za-z0-9._-]{3,128}', value)
                        and value.lower() not in ('unknown', 'null', 'none')):
                    return value
            except Exception:
                logger.debug('无法读取设备持久标识，使用连接标识', exc_info=True)
        return None

    @property
    def device(self):
        """获取底层设备对象"""
        return self._device

    def xpath(self, xpath_str):
        """XPath定位，返回兼容的选择器对象"""
        return self._device.xpath(xpath_str)

    def xpath_exists(self, xpath_str):
        """
        XPath判断元素是否存在（兼容hmdriver2和uiautomator2）

        hmdriver2: d.xpath('xxx').exists() 是方法调用
        uiautomator2: d.xpath('xxx').exists 是属性（bool值）
        """
        if self.device_type == 'hmdriver2':
            return self._device.xpath(xpath_str).exists()
        else:
            return self._device.xpath(xpath_str).exists

    def __call__(self, **kwargs):
        """选择器定位，支持 text, resourceId, className 等"""
        return self._device(**kwargs)

    def click_if_exists(self, **kwargs):
        """
        如果元素存在则点击（兼容hmdriver2和uiautomator2）

        hmdriver2的selector有click_if_exists()方法
        uiautomator2的selector没有，需先exists()再click()
        """
        if self.device_type == 'hmdriver2':
            self._device(**kwargs).click_if_exists()
        else:
            obj = self._device(**kwargs)
            if obj.exists():
                obj.click()

    @serialized
    def click(self, x, y):
        """点击坐标"""
        if getattr(self, '_display_id', 0):
            self._checked_shell(['input', '-d', str(self._display_id), 'tap', str(round(x)), str(round(y))])
        else:
            self._device.click(x, y)

    @serialized
    def swipe(self, x1, y1, x2, y2, duration=0.5):
        """滑动"""
        if self.device_type == 'hmdriver2':
            self._device.swipe(x1=x1, y1=y1, x2=x2, y2=y2)
        else:
            if getattr(self, '_display_id', 0):
                self._checked_shell(['input','-d',str(self._display_id),'swipe',
                    *[str(round(n)) for n in (x1,y1,x2,y2)], str(max(1,round(duration*1000)))])
            else:
                self._device.swipe(x1, y1, x2, y2, duration=duration)

    @serialized
    def swipe_ext(self, direction, box=None):
        """方向滑动"""
        if box:
            self._device.swipe_ext(direction, box=box)
        else:
            self._device.swipe_ext(direction)

    @serialized
    def go_back(self):
        """返回"""
        if self.device_type == 'hmdriver2':
            self._device.go_back()
        else:
            self.press('back')

    @serialized
    def go_home(self):
        """回到主页"""
        if self.device_type == 'hmdriver2':
            self._device.go_home()
        else:
            self.press('home')

    @serialized
    def input_text(self, text):
        """输入文本"""
        if self.device_type == 'hmdriver2':
            self._device.input_text(text)
        else:
            # The native editable node supports Unicode directly. send_keys /
            # clear_text may install and switch IMEs and then retry broadcasts.
            edit = self._device(className='android.widget.EditText')
            if edit.count == 1:
                edit.set_text(text, timeout=.25)
            else:
                self._device(className='android.widget.EditText', focused=True).set_text(text, timeout=.25)

    @serialized
    def input_ascii(self, text):
        """Type a short fixed ASCII token into an already confirmed field.

        Does not install/switch an IME or use the accessibility selector tree.
        The caller confirms/focuses the field; this adapter rechecks the instance
        and routes system input to the current logical display.
        """
        if self.device_type != 'uiautomator2' or not isinstance(text, str) or not re.fullmatch(r'[A-Za-z0-9]{1,32}',text):
            raise ValueError('系统输入仅支持 Android 上 1～32 位字母和数字')
        display = getattr(self,'_display_id',0)
        args = ['input'] + (['-d',str(display)] if display else []) + ['text',text]
        self._checked_shell(args, timeout=8)

    @serialized
    def clear_text(self):
        """清除文本"""
        if self.device_type == 'hmdriver2':
            self._device.input_text("")
        else:
            self._device(focused=True).set_text('')

    @serialized
    def start_app(self, package_name, activity=None):
        """启动应用"""
        if self.device_type == 'hmdriver2':
            self._device.start_app(package_name)
        else:
            if self.application.matches(package_name):
                self._launch_instance(package_name, activity)
            else:
                self._device.app_start(package_name, activity=activity)

    @serialized
    def stop_app(self, package_name):
        """停止应用"""
        if self.device_type == 'hmdriver2':
            self._device.stop_app(package_name)
        else:
            if self.application.matches(package_name):
                target = self._target(package_name)
                self._checked_shell(['am', 'force-stop', '--user', str(target.user_id), package_name])
            else:
                self._device.app_stop(package_name)

    @serialized
    def shell(self, cmd):
        """执行shell命令"""
        if self.device_type == 'hmdriver2':
            return self._device.shell(cmd)
        else:
            if isinstance(cmd, str):
                cmd = shlex.split(cmd)
            return self._device.shell(cmd)

    @serialized
    def current_app(self):
        """Return a common package/activity mapping on both drivers."""
        if self.device_type == 'hmdriver2':
            package, activity = self._device.current_app()
            info = {'package': package or '', 'activity': activity or ''}
        else:
            fallback = [None]
            def reliable_read(args):
                from backend.core.device.read_shell import AdbReadShell
                if fallback[0] is None:
                    fallback[0] = AdbReadShell(self.serial, budget=8)
                return fallback[0](args)
            # adbutils drops the user field. Read it from the SAME focused
            # window record instead; a background instance is never evidence.
            result = self._read_metadata(['dumpsys', 'window', 'windows'], fallback=reliable_read)
            if result.exit_code == 0:
                self._window_dump = result.output
                from backend.core.device.displays import focused_display
                display_id = focused_display(result.output)
                if display_id != getattr(self, '_display_id', 0):
                    self._screenshot_cache = None
                    self._capture_id = None
                    self._active_image_size = None
                self._display_id = display_id
            info = parse_foreground(result.output) if result.exit_code == 0 else None
            if info is None and result.exit_code == 0:
                from backend.core.device.displays import focused_window_dump
                displays=self._read_metadata(['dumpsys','window','displays'], fallback=reliable_read)
                if displays.exit_code == 0:
                    self._focused_window_dump=focused_window_dump(displays.output,getattr(self,'_display_id',0))
                    self._focused_window_stamp=time.monotonic()
                    info=parse_foreground(self._focused_window_dump)
            if info is None:
                result = self._read_metadata(['dumpsys', 'activity', 'activities'], fallback=reliable_read)
                info = parse_foreground(result.output) if result.exit_code == 0 else None
            if info is None:
                try:
                    info = dict(self._device.app_current(), user_id=None)
                except Exception as exc:
                    raise InstanceMismatch('无法读取并核定前台应用身份，请检查调试连接后重试；未执行输入操作') from exc
                if not info.get('package'):
                    raise InstanceMismatch('未取得前台应用身份，请检查调试连接后重试；未执行输入操作')
        identity = (info.get('package', ''), info.get('user_id'))
        if identity != getattr(self, '_foreground_package', None):
            self._screenshot_cache = None
            self._foreground_package = identity
        return info

    def _read_metadata(self, args, *, fallback=None):
        """Preserve successful SDK reads; isolate empty/sentinel failures."""
        from backend.core.device.read_shell import AdbReadShell, ReadResult
        def usable(result):
            return (result.exit_code == 0 and isinstance(result.output, str) and result.output.strip()
                    and not re.search(r'SecurityException|Permission Denial|(?:^|\n)\s*(?:Error:|Exception)', result.output))
        try:
            result = self._device.shell(args)
            if usable(result):
                return result
        except Exception:
            pass
        try:
            result = (fallback or AdbReadShell(self.serial, budget=8))(args)
            if usable(result):
                return result
        except Exception:
            pass
        return ReadResult('', 1)

    @serialized
    def installed_packages(self):
        """Return installed Android packages, or None when detection is unavailable."""
        if self.device_type != 'uiautomator2':
            return None
        target = getattr(self, 'selected_instance', None)
        if not isinstance(target, AppInstance) or not isinstance(target.user_id, int) or isinstance(target.user_id, bool) or target.user_id < 0:
            return None
        from backend.core.device.read_shell import AdbReadShell, parse_package_list
        try:
            read = AdbReadShell(self.serial, budget=12)
        except ValueError:
            return None
        for prefix in (['pm', 'list', 'packages'], ['cmd', 'package', 'list', 'packages']):
            try:
                result = read(prefix + ['--user', str(target.user_id)])
                if result.exit_code != 0 or re.search(r'SecurityException|Error:|Exception|Permission Denial', result.output):
                    continue
                packages = parse_package_list(result.output)
                if packages:
                    return packages
            except Exception:
                continue
        return None

    @serialized
    def resume_app(self, package_name):
        """Bring the existing application task forward without force stopping it."""
        if self.device_type == 'hmdriver2':
            self._device.start_app(package_name)
        else:
            # Supplying the launcher activity avoids u2's monkey fallback, which
            # can change rotation lock settings. Android restores the existing task.
            if self.application.matches(package_name):
                self._launch_instance(package_name)
            else:
                result = self._device.shell(['cmd', 'package', 'resolve-activity', '--brief',
                                             '-a', 'android.intent.action.MAIN', '-c',
                                             'android.intent.category.LAUNCHER', package_name])
                component = result.output.strip().splitlines()[-1] if result.output.strip() else ''
                if '/' not in component or ' ' in component:
                    raise RuntimeError(f'无法找到应用启动入口：{package_name}')
                self._device.app_start(package_name, activity=component.split('/', 1)[1], stop=False)

    def _checked_shell(self, args, *, timeout=None):
        result = self._device.shell(args) if timeout is None else self._device.shell(args, timeout=timeout)
        if result.exit_code != 0 or re.search(r'Error:|Exception|Permission Denial', result.output):
            raise RuntimeError('指定应用实例操作失败：' + result.output.strip()[:300])
        return result.output

    def _target(self, package):
        target = getattr(self, 'selected_instance', None)
        if not isinstance(target, AppInstance) or target.package != package or target.user_id is None:
            raise InstanceMismatch('没有明确选定对应的所选应用实例，已阻止启动或停止其他所选应用')
        return target

    def _launch_instance(self, package, activity=None):
        target = self._target(package)
        if activity:
            component = f'{package}/{activity}'
        else:
            output = self._checked_shell(['cmd', 'package', 'resolve-activity', '--brief', '--user',
                                         str(target.user_id), '-a', 'android.intent.action.MAIN',
                                         '-c', 'android.intent.category.LAUNCHER', package])
            component = output.strip().splitlines()[-1] if output.strip() else ''
        if not re.fullmatch(re.escape(package) + r'/[\w.$]+', component):
            raise RuntimeError('无法找到所选所选应用实例的启动入口，请刷新实例列表')
        start = ['am', 'start', '--user', str(target.user_id), '-a', 'android.intent.action.MAIN',
                 '-c', 'android.intent.category.LAUNCHER', '-n', component]
        # A cold-started task can finish its old activity and expose the previous
        # app again. Reactivate the SAME selected instance once, without another
        # force-stop or any input into an unconfirmed account.
        for attempt in range(2):
            self._checked_shell(start)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                try:
                    info = self.current_app()
                except Exception:
                    logger.debug('启动转场中暂未取得前台实例', exc_info=True)
                    info = {}
                if (info.get('package'), info.get('user_id')) == (target.package, target.user_id):
                    return
                time.sleep(.15)
            if attempt == 0:
                self._screenshot_cache = None
                logger.info('启动后未确认所选所选应用前台，重新唤起同一实例后核对')
        raise InstanceMismatch('未确认所选所选应用实例已进入前台，已停止，未改用其他所选应用')

    @serialized
    def refresh_app_instances(self):
        if getattr(self, '_instance_guard_active', False):
            raise ValueError('执行期间不能刷新或切换应用实例')
        if self.device_type == 'uiautomator2':
            from backend.core.device.read_shell import AdbReadShell
            instances, warnings = discover_android_instances(AdbReadShell(self.serial), self.application)
        else:
            instances = [AppInstance(self.application.package_for('hmdriver2'), None, self.application.label + ' · HarmonyOS 原生应用')]
            warnings = []
        self.app_instances = instances
        self.instance_warnings = warnings
        previous = getattr(self, 'selected_instance', None)
        self.selected_instance = (next((i for i in instances if i == previous), None) if previous is not None
                                  else instances[0] if instances else None)
        if previous is not None and self.selected_instance is None:
            self.instance_warnings.append('之前选定的应用实例未能重新确认，请明确选择可用实例；未自动切换到其他账号')
        return [item.to_dict() for item in instances]

    @serialized
    def select_app_instance(self, instance_id):
        if getattr(self, '_instance_guard_active', False):
            raise ValueError('执行期间不能切换应用实例，请先停止')
        target = next((i for i in self.app_instances if i.id == instance_id), None)
        if target is None:
            raise ValueError('实例已失效，请刷新列表并重新选择')
        self.selected_instance = target
        self._screenshot_cache = None
        return target

    def check_instance(self, info):
        target = getattr(self, 'selected_instance', None)
        if isinstance(target, AppInstance) and self.application.matches(info.get('package')):
            if info.get('package') != target.package or (target.user_id is not None and info.get('user_id') != target.user_id):
                raise InstanceMismatch('前台所选应用与选定实例不一致或无法确认用户空间，已停止操作；请检查实例选择后重新开始')

    @contextmanager
    def instance_session(self):
        with self.io_lock:
            if self.selected_instance is None or self._instance_guard_active:
                raise InstanceMismatch('请先选择有效的所选应用实例')
            self._instance_guard_active = True
        try:
            yield
        finally:
            with self.io_lock:
                self._instance_guard_active = False

    @serialized
    def restart_app(self, package_name):
        """Cold-start for fresh desktop entry or a bounded recovery attempt."""
        self.stop_app(package_name)
        self.resume_app(package_name)

    @serialized
    def screenshot(self, max_age=0):
        """Return a Pillow image; keep Harmony's temporary file off disk afterwards."""
        cached = getattr(self, '_screenshot_cache', None)
        if max_age > 0 and cached is not None and time.monotonic() - cached[0] <= min(max_age, .15):
            return cached[1].copy()
        started = time.monotonic()
        self._screenshot_cache = None
        if self.device_type == 'uiautomator2':
            try:
                if getattr(self, '_display_id', 0):
                    from PIL import Image
                    import adbutils
                    from backend.core.device.displays import capture_display
                    if not getattr(self, '_capture_id', None) or time.monotonic()-getattr(self,'_capture_stamp',0) > 2:
                        output = self._read_metadata(['dumpsys','display'])
                        self._capture_id = capture_display(output.output, self._display_id)
                        self._capture_stamp = time.monotonic()
                    raw = adbutils.adb.device(self.serial).shell(['screencap','-p','-d',self._capture_id], encoding=None)
                    image = Image.open(io.BytesIO(raw)).convert('RGB')
                else:
                    image = self._device.screenshot()
            except OSError:
                if getattr(self, '_display_id', 0):
                    self._capture_id = None
                    raise
                # Cold launch can momentarily close the automation RPC socket.
                # Explicit primary display uses u2's public direct-ADB capture,
                # returning a fresh frame without another app/server restart.
                logger.debug('截图服务暂时不可用，改用 ADB 截图', exc_info=True)
                image = self._device.screenshot(display_id=0)
        else:
            from PIL import Image
            with tempfile.TemporaryDirectory(prefix='miao-screen-') as folder:
                path = str(Path(folder) / 'screen.png')
                self._device.screenshot(path)
                with Image.open(path) as source:
                    image = source.convert('RGB').copy()
        if hasattr(image, 'copy'):
            self._active_image_size = image.size
            self._screenshot_cache = (started, image.copy())
        return image

    @serialized
    def dump_hierarchy(self):
        """
        获取UI层级结构，统一返回JSON dict

        hmdriver2 原生返回 JSON dict
        uiautomator2 返回 XML string，需转换为 JSON dict（属性名映射为兼容格式）
        """
        if self.device_type == 'hmdriver2':
            return self._device.dump_hierarchy()
        else:
            # uiautomator2 返回XML，缓存原始XML并转为兼容JSON
            self._last_xml = self._device.dump_hierarchy(compressed=True)
            return self._xml_to_json(self._last_xml)

    # XML属性名到hmdriver2 JSON key的映射
    _XML_ATTR_MAP = {
        'resource-id': 'id',
        'class': 'type',
        'content-desc': 'description',
        'long-clickable': 'longClickable',
        'scrollable': 'scrollable',
        'password': 'password',
    }

    def _xml_to_json(self, xml_str):
        """将uiautomator2的XML层级转为与hmdriver2兼容的JSON格式"""
        root = ET.fromstring(xml_str)
        return self._xml_element_to_dict(root)

    def _xml_element_to_dict(self, element):
        """递归将XML元素转为dict，属性名映射为hmdriver2兼容格式"""
        result = {}
        # 将XML属性映射为dict的key（兼容hmdriver2的JSON格式）
        for key, value in element.attrib.items():
            mapped_key = self._XML_ATTR_MAP.get(key, key)
            result[mapped_key] = value

        # 递归处理子元素
        children = list(element)
        if children:
            result['children'] = [self._xml_element_to_dict(child) for child in children]

        return result

    @property
    def display_size(self):
        """获取屏幕尺寸 (width, height)"""
        if self.device_type == 'hmdriver2':
            return self._device.display_size
        else:
            if getattr(self, '_display_id', 0):
                from backend.core.device.displays import focused_window_dump
                if time.monotonic()-getattr(self,'_focused_window_stamp',0) > .2:
                    result=self._read_metadata(['dumpsys','window','displays'])
                    if result.exit_code == 0:
                        self._focused_window_dump=focused_window_dump(result.output,self._display_id)
                        self._focused_window_stamp=time.monotonic()
                size=re.search(r'cur=(\d+)x(\d+)',getattr(self,'_focused_window_dump',''))
                if size:
                    return int(size[1]),int(size[2])
                return getattr(self, '_active_image_size', None) or self.screenshot().size
            w, h = self._device.window_size()
            return (w, h)

    @property
    def display_geometry(self):
        from backend.core.device.geometry import geometry_from_window
        return geometry_from_window(*self.display_size, getattr(self,'_focused_window_dump','') or getattr(self,'_window_dump',''))

    @property
    def gesture(self):
        """手势操作（仅hmdriver2支持）"""
        if self.device_type == 'hmdriver2':
            return self._device.gesture
        else:
            raise NotImplementedError("uiautomator2不支持gesture API，请使用swipe方法")

    @serialized
    def press(self, key):
        """按键"""
        if self.device_type == 'hmdriver2':
            if key == 'back':
                self._device.go_back()
            elif key == 'home':
                self._device.go_home()
            else:
                self._device.press_key(key)
        else:
            if getattr(self, '_display_id', 0):
                mapping = {'back':'4','home':'3','enter':'66','search':'84'}
                if key not in mapping:
                    raise ValueError('当前多屏设备不支持该按键')
                self._checked_shell(['input','-d',str(self._display_id),'keyevent',mapping[key]])
            else:
                self._device.press(key)

    def mute(self):
        """静音：多次按音量减"""
        if self.device_type == 'hmdriver2':
            for _ in range(10):
                self._device.shell("uitest uiInput keyEvent 17")
        else:
            for _ in range(10):
                self._device.press("volume_down")

    def json2xml(self, hierarchy):
        """
        将hierarchy转为XML字符串（用于xpath查找）

        hmdriver2: 使用内置_XPath._json2xml
        uiautomator2: 使用缓存的原始XML，避免重复RPC调用
        """
        if self.device_type == 'hmdriver2':
            from hmdriver2._xpath import _XPath
            return _XPath._json2xml(hierarchy)
        else:
            # 优先使用缓存的XML，避免再次调用dump_hierarchy
            if self._last_xml is not None:
                return self._last_xml
            return self._device.dump_hierarchy()

    def __repr__(self):
        conn = f", serial={self.serial}" if self.serial else ""
        return f"DeviceAdapter(type={self.device_type}{conn})"


def create_device(device_type, serial=None):
    """工厂函数，创建设备适配器"""
    return DeviceAdapter(device_type, serial)
