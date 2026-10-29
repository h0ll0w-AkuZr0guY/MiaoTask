# 公用能力与接口手册

为新增应用、活动和设备适配提供可查阅的现成能力。先读 [ARCHITECTURE](ARCHITECTURE.md) 确认归属，再按本页定位实现；完整接入步骤见 [DEVELOPMENT](DEVELOPMENT.md)。下列签名对应当前源码，并非独立 SDK 的永久兼容承诺。扩展或改变这些契约的 PR 必须同步更新本页、调用者和必要的迁移说明。

## 按需求找入口

| 需求 | 首选能力 | 业务适配负责什么 |
| --- | --- | --- |
| 接入一个应用 / 活动 | `ApplicationProfile`、`Registry`、`Activity` | 包名范围、页面入口、解析、动作与核验 |
| 连接 USB / Wi-Fi / MuMu | `discovery`、`wifi`、`emulators`、`DeviceAdapter` | 使用明确设备目标，不自行猜端口或替换授权 |
| 获取控件与画面文字 | `ScreenReader`、`Screen`、`TextNode` | 页面判定与匹配词；需要时限定本帧识别区域 |
| 裁剪 / 缩放 OCR、适配比例 | `ImageProcessor`、`CoordinateTransform`、`DisplayGeometry` | 声明图像到输入坐标的变换，尊重安全区域 |
| 解析任务行 | `Task`、`TaskRules`、共享 `parse_tasks` | 注入本活动词表和分类；布局不符则自行解析 |
| 执行、暂停、停止、返回 | `Activity` / `Session`、`RunControl`、`Navigator` | 页面动作与有限恢复钩子 |
| 调整今日执行队列 | `Controller.dispatch`、`TaskPolicy` | 提供稳定任务语义；不把按钮理解成立即点击任务 |
| 保存统计与失败现场 | `DailyState`、`StorageNamespace`、会话回调 | 向现有记录管线报告，不另建全局日志 |
| 多设备与网页操作 | `DeviceHub`、各设备 `Controller`、`api.js` | 请求明确工作区，不共享可变会话 |
| 按需获取活动源码 | `GitHubSourceProvider`、`PluginManager`、`ActivityCatalogHub` | 提供审阅过的 manifest 与兼容声明，复用固定提交、下载与安装事务 |
| 顺序执行多个活动 | `ActivityPipeline`、`select_pipeline` | 显式提供应用/活动 ID 与正确实例，不并发控制同一设备 |

路径下文均相对仓库根目录；示例中的 `sample`、`org.example.sample` 和地址均为虚构占位。

## 1. 应用注册与分层配置

实现：[registry.py](../backend/core/registry.py)、[profile.py](../backend/core/device/profile.py)、[loader.py](../backend/core/config/loader.py)。

```text
ApplicationProfile(label, android_package, package_pattern,
                   native_package='', legacy_primary_instances=())
AppMeta(id, label, profile, config='configs/base.json', default=False)
ActivityMeta(app, id, label, factory, config='config.json',
             device_types=('uiautomator2', 'hmdriver2'), default=False,
             legacy_storage=False, rules_version='1')
Registry(app_dir=None, *, plugin_dir=None).discover()
registry.get(app=None, activity=None)              # Registration
registry.catalog()                               # 可序列化应用 / 活动目录
create_runner(device, config=None, *args, registry=None, **kwargs)
load_resolved(registration, overrides=None)        # ConfigSnapshot
load_config(path=None, *, app=None, activity=None,
            registry=None, overrides=None)        # dict
```

* `factory` 为 `完整模块路径:类名`，公开 ID 与 Python 包目录不要求同名。ID 只接受小写字母、数字、下划线和连字符，必须以字母或数字开头。
* `ApplicationProfile.matches(package)` 使用声明的包名模式核对身份；`package_for(driver)` 返回该驱动对应包名，没有声明则报错。
* `create_runner` / `registry.instantiate` 选择注册工厂、检查活动驱动范围及设备当前所选实例归属，返回活动对象；不负责连接设备或自动绑定每日记录。
* 配置逐层按顶层字段覆盖并校验。`snapshot.sources` 标明字段来自 `core`、`app`、`activity` 或 `user`；`snapshot.mutable()` 获取独立字典。读取嵌套值也不会修改快照。

```python
from backend.core.config.loader import load_resolved
from backend.core.registry import Registry

registry = Registry().discover()
registration = registry.get()  # 当前默认活动
snapshot = load_resolved(registration, {'max_tasks': 2})
assert snapshot.sources['max_tasks'] == 'user'
config = snapshot.mutable()
```

注册的 Python manifest 属于受审阅的仓库代码，不作为不可信插件执行。`default_registry()` 在进程内缓存，新增活动后重启服务。

## 2. 设备发现、无线接入与实例

实现：[discovery.py](../backend/core/device/discovery.py)、[wifi.py](../backend/core/device/wifi.py)、[inventory.py](../backend/core/device/inventory.py)、[emulators.py](../backend/core/device/emulators.py)。

| 调用 | 返回 / 行为 | 约束 |
| --- | --- | --- |
| `list_android_devices(adb_path=None, *, timeout=8, env=None)` | `serial/state/model` 字典列表 | 只查询，不重启 ADB，不等待授权 |
| `prepare_android_connection(serial=None, on_status=None, timeout=45)` | 已授权的连接标识 | 多设备时必须显式选择；等待用户在手机授权，不能代替确认 |
| `android_identity(serial)` | 序列属性，或带 `android-` 前缀的 Android ID | 用于连接别名去重，无法核定抛 `AndroidConnectionError` |
| `inventory()` | `{devices: [...], errors: [...]}` | 对已知授权连接去重，身份查询有短时缓存 |
| `discover_emulators()` | `{devices: [...], errors: [...]}` | 只读已安装 MuMu 配置 / CLI，使用实际运行端口 |
| `discover_wifi()` | `{services, devices, errors}` | 只读 mDNS 和已知 ADB 连接，不扫描网段 |
| `endpoint(value)` | 规范化 IP:端口 | 接受 IPv4 或方括号 IPv6，拒绝缺端口、无效地址和命令参数 |
| `pair_wifi(address, code, on_status=None)` | 成功返回 `None` | 当前六位码经标准输入传递，不写入命令行、配置或日志 |
| `connect_wifi(address, on_status=None)` | 规范化连接地址 | 有限连接尝试，配对端口与连接端口分开 |

连接初始化使用 [adapter.py](../backend/core/device/adapter.py)：

```text
DeviceAdapter(device_type, serial=None, on_status=None,
              application=None, on_resolved=None)
```

构造时会连接和查询实例。现有 `device_type` 为 `uiautomator2` / `hmdriver2`；`application` 传当前注册应用的 Profile。多设备服务通过 `on_resolved(kind, serial)` 先取得物理租约再初始化 Android 自动化驱动，应用活动应使用控制器已经提供的 `device`，不重复构造连接。

实例能力在 [instances.py](../backend/core/device/instances.py)：

* `AppInstance(package, user_id, label).id` 为 `包名@用户空间`，原生无 Android 用户空间时为 `包名@native`。
* `device.refresh_app_instances()` 返回实例字典列表；`device.select_app_instance(instance_id)` 返回选中 `AppInstance`。运行期间禁止刷新或切换。
* `with device.instance_session(): ...` 开启输入层实例核对并在退出时清理。服务已统一包裹活动执行，活动不要再嵌套开启。
* `InstanceMismatch` 表示选定应用实例与实际前台不符或无法核定。应停止输入，不能把它转换成普通任务失败后继续点击。

## 3. 设备操作与观察

适配器输出统一的设备像素坐标。以下操作已包含设备 I/O 锁、必要的截图缓存失效，以及执行期实例核对：

| 调用 | 输出 / 含义 |
| --- | --- |
| `device.display_size` / `device.display_geometry` | 当前 `(width, height)` / `DisplayGeometry` |
| `device.current_app()` | 至少提供前台 `package`；Android 路径还核定用户空间与活动 |
| `device.dump_hierarchy()` | 统一字典结构的控件树 |
| `device.screenshot(max_age=0)` | Pillow 图像；默认新帧，允许缓存时最多复用约 0.15 秒 |
| `device.click(x, y)` / `swipe(x1, y1, x2, y2, duration=.5)` | 点击 / 滑动；时长单位秒 |
| `device.go_back()` / `go_home()` / `press(key)` | 返回、桌面、按键；不直接推定已返回成功 |
| `device.input_text(text)` / `clear_text()` | Android 原生编辑控件填入 / 清空；不安装或切换输入法 |
| `device.start_app(package_name, activity=None)` / `resume_app(package_name)` | 启动 / 恢复所选应用实例 |
| `device.stop_app(package_name)` / `restart_app(package_name)` | 限定目标用户空间停止 / 冷启动所选实例 |

业务层优先 `Session.read/click/browse_swipe`，继承停止检查、身份核对与几何约束。底层 `device.device`、`shell` 和驱动选择器为兼容或驱动实现保留，不作为新增应用绕过这些约束的常规入口。

实现：[screen.py](../backend/core/device/screen.py)、[models.py](../backend/core/models.py)。

```text
TextNode(text, bounds, source='ui')
Screen(nodes, width, height, package, source='ui', tree=None,
       user_id=None, geometry=None, obscured=False)
ScreenReader(device, ocr=True, on_image=None, image_processor=None)
reader.read(force_ocr=False)                      # Screen
reader.enrich_regions(screen, regions)           # Screen
screen.find(pattern, exact=False)                # TextNode 或 None
```

`bounds` 为 `(left, top, right, bottom)`，`TextNode.center` 为中心。`find` 默认在规范化文案上使用正则，`exact=True` 则比较完整规范化文字。它只返回第一个匹配；需要“最右侧搜索按钮”等规则时，应筛选 `screen.nodes` 后显式排序。

读屏优先前台控件；控件不足或要求 `force_ocr` 时使用本地 OCR，黑屏无法识别时回查可读控件并标记遮挡。OCR 引擎按工作区上下文隔离。`enrich_regions` 只处理当前 `last_screen` 对应原帧中的像素区域，回映裁剪偏移，不另取一帧；返回的是新观察对象，调用者不要假定对象身份不变。切换画面后须重新读取，不能保留坐标等待稍后点击。

## 4. 屏幕、裁剪与缩放

实现：[geometry.py](../backend/core/device/geometry.py)、[displays.py](../backend/core/device/displays.py)。

```text
DisplayGeometry(width, height, rotation=0, safe_insets=(0, 0, 0, 0), cutouts=())
CoordinateTransform(scale_x=1, scale_y=1, offset_x=0, offset_y=0)
PreparedImage(image, to_device=CoordinateTransform())
```

* `safe_insets` 顺序为左、上、右、下；`content_bounds` 返回可用矩形，`contains(x, y)` 同时排除挖孔矩形。当前 `rotation` 是表达字段，窗口提取器不自动填充或据此旋转。
* `CoordinateTransform.point(x, y)` 按 `x * scale_x + offset_x` / `y * scale_y + offset_y` 回映；`bounds(box)` 回映矩形，比例必须为正。
* `ImageProcessor` 要求可哈希、能区分策略的 `cache_key` 和 `prepare(image) -> PreparedImage`。默认 `IdentityImageProcessor` 保持原图，默认 OCR 仍有自身宽度限制和宽屏分片；自定义策略处理器是额外接缝。
* `ScreenReader` 先处理图像，再将 OCR 自身缩放和处理器变换依次还原；宽屏分片偏移随后加回。处理器坐标针对它收到的图像，不得提前再加全屏分片偏移。

最小坐标示例，无设备操作：

```python
from backend.core.device.geometry import CoordinateTransform, PreparedImage

class CropForOCR:
    cache_key = ('crop', 10, 20, 60, 100)

    def prepare(self, image):
        return PreparedImage(
            image.crop((10, 20, 60, 100)),
            CoordinateTransform(offset_x=10, offset_y=20),
        )

# 裁剪图上的 (1, 2, 11, 12) 对应输入图的 (11, 22, 21, 32)。
```

`focused_display(window_dump)` 取得逻辑屏幕，`capture_display(display_dump, logical_id)` 解析采集映射。适配器负责将输入与采集对齐；应用无需调用这些底层方法，更不能用列表序号替代显示屏 ID。映射不明时停止。折叠姿态、自由窗口和全新厂商仍需专门适配。

## 5. 任务模型与行解析

实现：[models.py](../backend/core/models.py)、[app/common/models/task.py](../backend/app/common/models/task.py)。

```text
Task(key, title, detail, action, bounds, current=0, total=1,
     seconds=0, kind='browse', skip_reason='', target_app='', target_packages=())
TaskRules(action_re, progress_re, description_re, ignored_titles,
          milestone_header, milestone_action_re, milestone_title, classify)
parse_tasks(nodes, width=1440, height=3200, *, rules)  # list[Task]
```

共享解析器适合“右侧动作按钮、同行标题与进度、上方累计奖励”的页面，以相对几何配对，不能直接用于任意布局。`classify(title, detail, texts, action, current, total)` 返回五项：`(seconds, kind, skip_reason, target_app, target_packages)`；正则和分类由应用注入。

当前可执行类型为 `browse/search/visit/claim`，`manual/done` 用于展示和排除。`key` 支持本轮观察跟踪，不能当作持久化队列身份。`target_packages` 是当前任务的目标应用证据；不能把某个开发设备未安装应用的结论写成通用默认值。

## 6. 活动生命周期、控制与恢复

实现：[activity.py](../backend/core/execution/activity.py)、[session.py](../backend/core/execution/session.py)、[navigator.py](../backend/core/execution/navigator.py)、[run_control.py](../backend/core/execution/run_control.py)。

```text
Session(device, config=None, control=None, emit=None, on_image=None,
        unavailable_tasks=None, on_failure=None, task_policy=None)
```

`Activity` 组合 `SessionLoop`、`Navigator`、`TaskTracking` 与 `Session`，实现方声明 `application`，并提供以下方法：

| 方法 | 契约 |
| --- | --- |
| `default_config()` | 返回完整有效配置字典 |
| `parse_tasks(nodes, width, height)` | 返回当前观察中的 `Task` 列表 |
| `is_task_list(nodes)` | 判断任务列表；不能仅因为看到了一个任务按钮就认定进入 |
| `execute(task, before)` | 返回 `(after_screen, explicit, cross)`；通用循环据此核验；相关动作统计仍由活动按实际行为更新 |
| `verify(task, before, after, explicit=False)` | 返回布尔值，以进度、完成文案或奖励状态等页面证据为依据 |
| `ensure_list(auto_open=False)` / `recover(cold_start=False)` | 返回已确认的任务列表 `Screen`；无法恢复抛明确异常，必须有限 |
| `setup()` / `teardown()` | 运行前准备 / `finally` 收尾；默认空实现 |
| `view_signature(screen)` | 可选的可见内容指纹，返回可比较值；默认 `None`，保留已有列表扫描逻辑 |
| `alt_swipe(screen)` | 可选的反向滚动，执行后返回 `True`；默认 `False` |
| `next_list_page(screen)` | 可选的换批按钮，返回可点击节点；默认 `None`。通用循环在滚动无新任务时尝试换批，最多 `max_scroll_pages` 次 |
| `return_to_home(screen)` | 正常结束时返回应用首页并返回观察；默认原样返回。停止、认证及实例错误继续向上传递 |

`run(auto_open=False)` 保留通用调度、失败预算、重新读表和统计；`scan(auto_open=False)` 只进入并解析，返回任务记录列表。复杂活动优先替换策略方法，避免复制整个循环。

换批后无候选任务时，通用循环按 `empty_batch_stop`（默认 1，最小 1）结束扫描。支付宝月月赚使用这些可选钩子完成列表换批及返回首页；旧框架虽可导入活动源码，但不具备新增的调度行为，完整换批与结束返回需要包含本次框架实现的版本。原有活动不覆写钩子时保持默认行为，框架接口版本仍为 1。

默认 `Navigator` 除上表外还调用入口策略钩子：`_accept_list`、`launcher_screen`、`launch_screen`、`homepage_visible`、`activity_landing`、`activity_entry`、`official_activity_entry`、`task_panel_entry`、`task_panel_visible`、`enter_task_panel`、`open_task_panel`、`panel_loading`、`open_home_search`、`unavailable_landing`、`remember_missing_destination`。当前实现分别位于淘宝入口、搜索和公共跳转策略；新页面若没有同样结构，应覆盖 `ensure_list/recover` 并维持相同停止与恢复预算，不能仅实现抽象方法就假定默认导航可用。

会话内复用：`self.read(force_ocr=False)` 观察；`self.click(node)` 先检查停止与安全区域；`self.browse_swipe(screen)` 在当前区域取滚动参数；`self.control.wait(seconds)` 可响应暂停 / 停止；`self.update(phase=None, **data)` 发状态；`self.report_failure(reason, task=None, phase=None)` 在恢复前保存现场。

`emit(**event)` 接受状态 / 任务 / 统计事件，`on_image(image)` 接受 Pillow 图像，`on_failure(reason=..., task=..., phase=...)` 接受现场元数据。服务已提供这些回调，不要由活动另写一个日统计文件。

| 异常 | 处理边界 |
| --- | --- |
| `Stopped` | 用户停止，向上传递；等待中不要吞掉 |
| `InstanceMismatch` | 目标实例不符，停止，不跨账号恢复 |
| `NeedsAttention(message, *, kind='attention', evidence_saved=False)` | 用户处理或有限导航失败；`kind='security'` 不能自动返回或重启绕过 |
| `TaskUnavailable` | 当前任务不可用，保存事实后有限返回，继续其他候选 |

默认 `guard(screen)` 核对身份并检查认证、登录、支付与不可读画面。页面有被遮挡的登录广告等例外时，必须在本活动中有可验证的判定，不能关闭全局守卫。恢复优先逐层 Back 并每次重读；到桌面后才用同一恢复预算内的一次冷启动。

### 淘宝应用内策略

实现：`backend/app/taobao/utils/`，不属于跨平台 core API。

| 组件 | 可复用契约与边界 |
| --- | --- |
| `SearchPolicy` | 原生输入框填字、重新定位右上搜索键与回车兜底；填写和每次提交前复查所选应用前台，页面切到其他应用时停止而不继续回车；`official_activity_entry` 由活动确认官方入口，不把查询文字当结果 |
| `TaskActions.execute(task, before)` | 点击已解析任务，再观察跳转；复用可停止浏览、随机滚动和当前活动的 `recover` |
| `TaskActions.browse_destination(task, screen)` | 已打开并确认目标页后开始观察与浏览，返回 `(after_screen, explicit, cross)`；不再次点击任务，不直接累加完成数 |
| `VerificationPolicy` | 原有任务进度与行收缩证据；每日领奖采用 `DailyActivity.verify` 的更严格规则，不将按钮标签差异作为奖励凭据 |
| `DailyActivity` | 继承 `Activity`，共用有界日常导航、弹窗关闭、任务解析、领取与余额核验；具体活动只提供独立 manifest、配置与 `default_config` |

`daily_page` 为淘宝活动的本地配置：必需 `list_markers/landing_markers`；可选 `inline_claims`（`title/action/done`）、`inline_only`、`reward_rows`、`list_support_markers`、`official_entry_labels`。`browse_reward` 只允许显式的浏览提示及对应按钮，`manual_entry_markers/manual_entry_message` 表示须本人初始化的页面。现金页通过 `main_view_markers/list_support_markers` 区分奖励面板与下方商品区；商品区最多四次向顶部滚动，重新观察后才领取。`inline_claims.exclude_left_labels` 排除与游戏/视频入口同排相邻的歧义领取按钮。现金页可用 `remaining_reward_balance` 提取待攒元宝整数，OCR 拆分的前缀与数字只按顶部同排组合；只在金额变化达到本任务标注奖励时核验，不能把其他余额或倒计时当到账。

`reset_list_scroll` 为日常列表的可选开关。开启后，`ensure_list(auto_open=True)` 按实际任务行区域最多向列表顶部滚动四次（仍受 `max_scroll_pages` 限制），每次重读并检查停止和安全状态；重复视口即结束。用于从中断后的列表底部开始完整队列。`recover()` 不执行这一步，避免打断刚执行任务的返回核验。定时领取行的“12点可领”等状态只在显式进度已满时视为完成，未满则等待，不把时间标签当奖励证据。

`earned_reward_balance` 可提取页面顶部明确累计到账的整数（例如成就点），必须增长至少本任务 `+N` 标注的金额才作为领取证据；余额在下方商品或说明中出现时不采用。与现金页的 `remaining_reward_balance` 递减证据分别配置，不把进度满但仍标“领取奖励”的行当作已经领取。余额核验仍要求回到已确认任务列表，不据单独奖励弹窗计数。

淘金币余额同时支持顶部抵现金额和与“淘金币”标题同排的整数。没有显式计数的快捷任务完成后可能换标题、只留下灰色勾选；只有返回已确认列表、原任务不再可执行且余额精确增加本任务标注的金币数，才补充核验，不把更大的延迟到账当作这次成功。“今日速赚”和“更多专享福利”分别解析，合并语义相同的镜像任务；分隔标题滚出屏幕后仍解析普通任务。

搜索导航优先配对活动名称与其右侧同排“去看看 / 去查看 / 进入”动作；查询栏与联想词中的高亮片段不作为入口。首页与全部频道的图标须先确认当前页面，只保留当前尺寸、安全区域内的可见候选；原生控件与画面尺寸或安全范围不一致时保留画面匹配，商品抵扣标识不能充当活动入口。“你关注的商品都可抵”等已确认的浏览条目仅在说明明确为浏览且无新增关注要求时执行，实际关注操作仍排除。

`DestinationPolicy.launch_confirmation(screen)` 使用公共 `JUMP_WORDS` 与应用配置 `jump_words` 的追加词表，先确认应用打开/跳转场景，再匹配“允许、本次允许、确认、打开”等短按钮，优先本次授权。持续授权、安装、付款等标签不采用，登录与其他权限场景不匹配。同一打开步骤最多确认两次；已确认的跳转可在三倍页面等待硬上限内获得加载余量；无法确认时返回并持久化当前任务的跳过原因，不把同应用其他任务一起封禁。日常导航先返回关闭中断遗留的跳转弹窗，再接受其下方的任务列表。`DailyActivity.recover()` 先逐层退出外部页面，再在确认桌面后恢复所选实例，保留停止和身份核验。

`TaskActions` 可选 `destination_ready(screen)` 钩子决定是否已出现目标内容；日常活动不把仅有状态栏文字的白屏当作搜索页。未实现钩子的原活动保持原有等待判定。已确认推广但关闭图标不可读时，可用一次 Back 关闭，再重读；不点击折扣/提现主按钮作为关闭兜底。

`TaskActions` 的可选钩子 `browse_countdown(screen)` 返回当前页剩余秒数或 `None`，只可在原有任务硬上限内延长观察；`browse_observation_interval(scrolling)` 决定下次读屏前的连续滑动窗口。未提供钩子的原活动保持一秒观察间隔。日常活动在滚动时连续操作约三秒再读屏，每个手势仍检查暂停、停止与实例，避免高耗时 OCR 反复打断浏览计数。

`search_page_markers/search_reward_markers` 分别匹配顶部搜索活动标题与奖励提示，并结合右上搜索按钮和至少两处商品价格确认结果；历史搜索、联想页和仅有标题的页面均不接受。农场“搜索有福利 / 滑动浏览得肥料”等差异只需扩展这些词表。

`browse_counter` 配置当前页面的倒计时正则；`browse_counter_context` 为数字与文案分离的奖励徽标提供场景词表，只配对顶部右侧同排、紧邻左侧的短整数，不采用商品价格、购物车数或下方推荐内容。淘金币在完整画面未读到彩色任务横幅时复用 `ScreenReader.enrich_regions` 读取本帧顶部区域，保留原几何与安全标记，不混用旧截图。任务名写“搜索”但已直接到奖励商品网格时，确认倒计时和商品证据后沿用该页。实际搜索表单通过 `SearchPolicy.search_edit(screen, edits)` 和 `fill_search_field(query, edit)` 定位与填字；新日常活动先核定可见搜索页并聚焦，普通任务使用固定数字，避免字母停在原输入法的组合候选区、点击提交时被清空。活动入口仍使用实际 Unicode 名称；旧驱动保留原生聚焦填写路径。有多个不明确的原生输入框时不能用 OCR 绕过歧义。

日常导航使用当前控件区域与 OCR 文字框，不按参考设备的像素坐标点击；已确认前进的步骤可获得加载时间，总预算、点击次数、返回和重启仍有上限。HyperOS 控件树明确抛 `RPCStackOverflowError` 时，在本活动内改读本地 OCR，保留停止与实例守卫；其他错误继续按框架边界处理。不安装输入法、不替换全局守卫、不接受脱离跳转场景的确认按钮。现有四个日常活动仅声明 Android，成就中心未初始化时不会选择地址或参加抽奖。

## 7. 今日队列与持久化

实现：[task_policy.py](../backend/core/execution/task_policy.py)、[retry.py](../backend/core/execution/retry.py)、[daily_state.py](../backend/core/storage/daily_state.py)、[namespace.py](../backend/core/storage/namespace.py)。

`task_identity(task)` 接受 `Task` 或字典，返回 24 位摘要。参与字段为规范化标题、类型、总次数、秒数、目标应用和排序后的目标包名；不含当前进度、坐标或按钮文案。换标题或语义契约就得到新的今日选择，不能通过同一行位置继承。

`TaskPolicy.bind(store)` 加载当日选择；`reason(task)` / `failures(task)` 查询；`fail(task, limit)` 持久化失败预算；`choose(identity, skip)` 写入跳过或清除手动选择和失败预算。这些是框架内部方法。网页或外部调用应使用 `Controller.dispatch('task_skip'|'task_execute', {'task_id': ..., 'task_scope': ...})`，继承作用域、唯一任务、已完成、类型开关和不可用事实的校验。

选择变化只影响下一次安全选取，不立即执行该行。完成和人工任务不可强制执行，同签名多条无法唯一定位时禁止单独修改。用户执行不覆盖安全守卫或已确认应用缺失；安装情况改变后重新识别。

```text
StorageNamespace(app, activity, legacy=False, primary_instances=())
DailyState(root, serial, device_type, day=None, instance_id='', namespace=None)
store.progress(session_id, stats, tasks=None, unavailable_tasks=None,
               count_stats=True, baseline=None, state=None)
store.failure(event, image=None)
store.diagnostics(report)
```

命名空间隔离应用、活动、日期、设备摘要与实例摘要。`progress` 对同一 session 以累计水位取增量，避免重复事件再次计数；扫描可更新任务事实，不累计执行量。`failure` 在本地保存元数据与可取得的原始 PNG；`diagnostics` 返回 ZIP 字节并隐藏已知设备标识，截图仍需人工脱敏，不自动上传。

由 `Controller` 负责跨日、连接、活动和实例变化时重新绑定日记录与任务作用域。只有声明旧布局继承的原活动才导入旧 daily，保留源文件；新应用不设置 `legacy_storage=True`。同一应用实例内人工换号不会产生新的账号命名空间，当前框架不读取账号身份。

## 8. 多设备服务与前端

实现：[devices.py](../backend/core/service/devices.py)、[panel.py](../backend/core/service/panel.py)、[context.py](../backend/core/context.py)、[api.js](../frontend/js/api.js)。

```text
DeviceHub(config=None, registry=None, app=None, activity=None)
hub.add(label='', key=None)                       # workspace_id
hub.rename(key, label)                            # 只改名称，原工作区也可；不改任务/设备身份
hub.resolve(key=None)                            # Controller；未知 ID 报错
hub.batch(payload)                               # 各设备 {id, ok, error?}
hub.remove(key)                                  # 仅空闲，至少保留一个工作区
hub.close(timeout=25)                            # 全部停止、共用等待预算、保存
controller.dispatch(action, payload)              # 校验后提交，不等待任务完成
controller.snapshot()                            # 独立状态副本
```

`DeviceHub` 上限 8 个工作区，每台控制器各有线程、配置、`RunControl`、记录、预览和队列。批量操作 `payload` 包含显式 `devices` 和 `action`，可附 `config/auto_open/app/activity/activities`；支持 `run/scan/open_app/pause/resume/stop`。逐台提交给独立线程，某台忙碌或离线不阻止其他台；同一物理设备的连接别名不能重复租用。批量活动计划与设置可以复制，各设备的实例选择仍独立。

新增线程若产生设备日志或 OCR 状态，使用 `workspace_scope` 设置归属，或在拥有 `workspace_id` 的实例方法上使用 `@device_scoped`；不能另设全局“当前设备”。注意 `ContextVar` 不应依赖自动传入新线程，现有设备工作和预览入口均显式设置。

网页复用现有请求层，读写都带 `?device=<workspace_id>`，写操作带本服务令牌并接受 Host / Origin 校验。失效 ID 不回退其他设备；切换后迟到响应不能覆盖当前页。任务按钮还需 `task_id/task_scope`。完整路由分组见[架构文档](ARCHITECTURE.md#http-与前端)，用户流程见 [USAGE](USAGE.md)。

前端通用弹窗使用 [dialogs.js](../frontend/js/components/dialogs.js) 的 `MiaoDialogs.bind(dialog)`：对已有原生 `<dialog>` 注册一次外侧关闭交互；仅指针按下和点击均位于弹窗边界外时关闭，从内部拖出不关闭。内部按钮、原生 Esc 与焦点管理沿用 `<dialog>`，关闭弹窗不取消已提交的后端操作。

[preview-controls.js](../frontend/js/components/preview-controls.js) 管理预览宽度、显隐和放大显示，不修改任务配置或主动请求截图。宽度 160～280 px，默认 220 px，浏览器偏好键 `miaotask.preview-width.v1`；存储不可用时仍可操作。放大图复用当前截图地址，只有画面已加载且 URL 的 `device` 等于当前 `MiaoAPI.device` 才可打开；收到 `miaotask:device-selected` 时关闭并清除大图，迟到的旧设备加载不能启用。新增组件脚本需同步 `service/static.py` 的显式静态白名单。

侧栏显示偏好键为 `miaotask.preview-visible.v1`，默认显示。`preview-controls.js` 同步 `hidden`、工作区布局与按钮 `aria-pressed`；`app.js` 在侧栏隐藏时停止额外预览请求，不改变后台执行观察。设备新增与公共设置弹窗同样复用 `MiaoDialogs.bind`；公共设置字段保留在 `settings-form` 内，运行/连接/批量启动校验失败时自动打开对应分类。设备切换关闭设置，随后按原有初始化流程读取目标配置。

`POST /api/workspaces/rename` 接受 `{id, label}`，按原有令牌、Host 和 Origin 校验；名称去空白后为 1～60 字符。只修改当前服务中该工作区的展示名称，允许执行期间改名；不改连接、运行配置、租约或日记录，失效 ID 和退出中的服务拒绝修改。

`device-manager.js` 的 `MiaoDevices.prepare()` 用于连接提交前保存名称和解析目标。新增时先创建、选择并等待该工作区的 `miaotask:state`，最长 10 秒，再返回 ID；已有编辑仅重命名显式 ID，发生工作区切换时拒绝误投。USB/Wi-Fi 调用方在准备前捕获用户填写的地址，准备后再次核定当前工作区；无线配对码仍仅用于当前请求。`creating/preparing` 为只读界面状态，`refresh()` 在主页面渲染后同步弹窗控件。`miaotask:editor-mode` 通知无线编辑器清理新建设备的旧地址。设备名称与工作区一样保留在本次服务期间，不把原始地址保存到浏览器偏好。

工作区摘要新增 `connection_issue`，由预览连续三次失败产生；成功 `on_image` 清零，旧设备的迟到失败忽略。这是展示诊断标志，不改 `connected`、统计、执行目标或停止状态，也不触发重连。卡片同时显示颜色和文字，不能以颜色作为任务是否可执行的后端依据。

主题资源为 [themes.css](../frontend/css/themes.css) 与 [themes.js](../frontend/js/components/themes.js)。语义色变量集中定义背景、表面、文本、强调、成功、危险等角色，支持 `warm/blue/night`；根节点 `data-theme` 在页面解析前从偏好键 `miaotask.theme.v1` 恢复，未知值和存储异常回退 `warm`。外观按钮在 DOM 就绪后绑定原生弹窗，同浏览器 `storage` 事件同步其他标签。主题不保存或修改设备、活动及任务配置。新增界面优先使用已有语义色，保持连接异常和成功的语义与对比。

### 本机排行榜

实现：[rankings.py](../backend/core/storage/rankings.py)、[rankings.js](../frontend/js/components/rankings.js)。

```python
RankingStore(root).snapshot(group='app', period='all', metric='elapsed', catalog=())
# group: app | activity；period: all | today
# metric: elapsed | completed | skipped | failed（降序）
```

HTTP 为 `GET /api/rankings?group=activity&period=today&metric=completed`，设备参数仍经统一请求层附加与校验。返回 `rows/records/ignored/local_only/storage_dir/updated_at`；每行有完整匿名 `device_key`、应用/活动 ID 与标签、`rank/days/stats`，服务可为在线设备补 `device_label`。不返回原始设备序列号。未知分组、日期范围或指标返回 400。

统计按数据根的 `apps/*/activities/*/daily/*/*/instances/*/state.json` 读取，校验文件身份、日期、设备键与 schema，沿用日记录计数净化；按设备合并实例、按所选层级合并活动和日期。只读汇总，不写第二套计数；旧根目录迁移副本不重复计入。最多检查 50,000 个文件、单文件上限 8 MiB，跳过损坏、身份不符、链接或超限文件，`ignored` 非零提示未完整计入，原文件保留。读取原子替换后的日记录，不扫描事件日志或截图；没有记录返回空列表。前端只在打开、切换范围/排序或手动刷新时请求，并丢弃关闭后或旧筛选的迟到响应。

## 9. 预留扩展与维护要求

| 接缝 | 目前能做什么 | 接入产品前仍需完成什么 |
| --- | --- | --- |
| `DeviceBackend` / `DriverFactory` | 结构协议、显式注册并创建后端 | Controller / CLI 装配、驱动选择、身份 / 实例 / 显示屏及停止语义；当前不会自动出现在网页 |
| `EndpointProvider` / `ConfiguredEmulatorProvider` | 返回已配置的 `DeviceEndpoint` | 新厂商发现方式、明确授权 / 连接流程、重复物理身份核对；不猜端口 |
| `ImageProcessor` / `PreparedImage` | 自定义图像预处理及坐标回映 | 在读屏器创建处显式注入、布局选择、缓存策略及目标设备覆盖；当前无用户可选自动策略目录 |
| `DisplayGeometry.rotation` | 表达方向字段 | 厂商姿态采集、变换与输入对齐；不代表全部折叠 / 分屏模式已支持 |
| 应用 manifest / `Activity` | 无需修改 core 注册新活动 | 对真实 App 的入口、任务、动作、核验与异常适配；合成示例不算真实支持 |

更新能力时在 PR 说明调用者影响，并同步本页的签名、输入输出、异常、默认值、限制和最小示例。结构或所有权变化再更新架构文档；贡献条目追加 [UNRELEASED](UNRELEASED.md)，不把接口历史堆入 README 或每次新增说明文件。验证与构建统一见 [BUILDING](BUILDING.md)。

## 10. 官方活动源码、安装门与流水线

实现：[runtime.py](../backend/core/runtime.py)、[repository.py](../backend/core/plugins/repository.py)、[contracts.py](../backend/core/plugins/contracts.py)、[manager.py](../backend/core/plugins/manager.py)、[activity_hub.py](../backend/core/service/activity_hub.py)、[pipeline.py](../backend/core/service/pipeline.py)、[bundle_policy.py](../scripts/bundle_policy.py)。

```text
GitHubSourceProvider(fetch=None)
provider.catalog(commit=None)                      # 默认解析官方main；commit可固定40位SHA
provider.build_archive(entry, destination)          # 仅取所选活动与应用共享文件，返回摘要
activities_dir()                                  # 实际可写的活动根目录 Path
PluginManager(root=None, *, source_provider=None, catalog_url=None, allow_local=False)
manager.snapshot(registry=None, refresh=False)     # 目录、安装、兼容、提交、更新与实际路径
manager.install(app, activity, *, source_commit=None)
manager.install_local(archive_path, expected_sha256,
                      *, expected_app=None, expected_activity=None)
manager.uninstall(app, activity)                   # 移除活动，保留任务数据
validate_archive(path, expected_sha256, *, expected_app=None, expected_activity=None)
# → (manifest_dict, {relative_path: bytes})；校验阶段不导入 manifest
reload_plugin_registry()                          # 清理活动模块缓存并重建默认注册表
hub.plugins_snapshot(origin, refresh=False)
hub.change_plugin(action, {'app': ..., 'activity': ..., 'source_commit': ...}, origin)
hub.open_activity_folder(origin)                  # Windows 打开固定安装目录，返回路径字符串
```

产品默认使用官方 `main` 源码。目录先解析提交与树，只静态读取 manifest 和兼容声明，不执行远端 Python；源码按固定提交从官方地址获取，核对 Git blob 摘要、长度、路径及数量。每个目录项有 `source='github-source'`、`source_ref`、`source_commit`、短 `revision`、合成版本 `0.0.0+git.<revision>` 和兼容信息；客户端安装/更新传已展示的完整提交，目录变化时拒绝静默改装。

新活动或修改过的 manifest 在活动目录提供 `compat.json`，例如 `{"schema": 1, "framework_api": 1, "python": "3.13"}`。少量已审阅的旧 manifest 使用明确摘要白名单兼容，不能借此推断任意新活动兼容。目录中的 `warnings` 报告无效声明；`installed_source_commit`、`update_available` 和 `compatible` 用于界面展示。

普通产品请求只通过 `hub.change_plugin` 提交 `install/uninstall`，让服务检查全部设备线程空闲并串行替换模块；不能从单设备控制器绕过此门，也不能让网页传任意源码 URL。`build_archive` 是安装器内部复用归档校验的实现，客户端不依赖活动 Release。低层 `install_local` 和注入测试源仅供离线用例/打包验证，产品没有本机测试源路由或启动参数；调用方必须保证没有活动线程运行。

`validate_archive` 校验分发/Python/框架接口版本、ZIP 与文件摘要、路径、大小、文件数及应用/活动身份，只返回经验证的相对路径内容。安装的暂存阶段才实际导入工厂、读取配置，因此缺少冻结依赖会在替换前失败；可捕获的目录交换或记录写入错误尝试回滚。共享应用文件变化与其他已安装活动冲突时拒绝升级。更新公共接口必须同步 `FRAMEWORK_API` 的兼容决策、包构建和本页，不自动安装第三方依赖。

`PluginManager()` 默认根目录为 `activities_dir()`，源码在其 `apps/<app_module>/` 下，`installed.json` 与 `catalog.json` 保存在活动根。`snapshot` 的 `activities_dir` 是活动根的绝对路径，`source_dir` 是其中 `apps/` 的绝对路径。不要根据 `data_dir()` 自行拼接旧 `plugins/` 位置。显式 `PluginManager(root=...)` 用于受控构建或隔离用例，由调用方管理该目录。

`activities_dir()` 优先使用显式 `MIAO_ACTIVITIES_DIR`，不可写则报错；否则便携版使用 exe 旁的 `activities/`，源码使用仓库根的 `activities/`。设置 `MIAO_DATA_DIR` 会同时将活动隔离到该根下，避免测试读写正式活动。默认位置不可写时回退到私人数据根的 `activities/`。新目录不存在时复制旧 `plugins/`，或只读便携目录内的预装活动；保留源文件，不覆盖已有活动根。日记录和设备信息仍由 `data_dir()` 管理，不写入活动源码目录。

默认 `Registry()` 合并源码资源与 `activities_dir()/apps`，插件应用目录按模块名覆盖对应内置应用，避免同一 ID 重复注册。自定义 `Registry(app_dir)` 默认只发现该目录；需加载外部包时显式传 `plugin_dir`。框架空包的 `has_activities` 为 false，`load_config()` 返回通用界面配置，但不能据此执行默认应用；执行前必须安装、选择真实活动。

`POST /api/plugins/open-folder` 以空 JSON 对象请求，必须通过现有令牌、Host 与 Origin 校验。服务只打开已解析的 `manager.root`，忽略客户端路径；响应 `{ok: true, path: ...}`。非 Windows 或无法打开时返回可读错误，活动中心保留实际路径供手动访问。

目录失败时保留经验证的缓存并附错误，不能把缓存声称为最新 main。已安装代码留在本地可离线执行；更新需要重新访问官方源。缓存与安装记录不包含账号或设备授权，卸载不删除任务日记录。

```python
# 在已初始化的指定设备控制器上保存计划；不会立即执行。
controller.dispatch('select_pipeline', {'activities': [
    {'app': 'sample', 'activity': 'daily-tasks'},
    {'app': 'sample', 'activity': 'weekly-tasks'},
]})
# 等保存操作结束，再按现有入口启动；不要并发提交到同一控制器。
controller.dispatch('run', {'auto_open': True})
```

`activities` 必须是最多 16 项的列表，每项仅有有效 `app/activity`，拒绝空身份、重复活动及当前驱动不支持的活动。HTTP 使用 `/api/select_pipeline?device=<id>`；多设备批量启动可携带同结构列表。计划改变期间与运行期间不可更换活动，先停止再修改。空计划可以保存，启动空计划会拒绝。

同一设备的活动与应用实例串行，跨设备工作线程并行；公共设置可沿用，活动入口和任务规则重新从当前注册加载。实例计划属于当前物理设备，重连清空；缺失记忆实例不回退其他账号。认证、停止、身份错误和异常终止本设备后续活动，正常返回才进入下一项。`activity_pipeline` 状态用于运行进度，任务完成数继续由各活动核验统计。

## 11. 有界 Android 元数据回退

实现：[read_shell.py](../backend/core/device/read_shell.py)。`AdbReadShell(serial, *, timeout=8, budget=24)` 是绑定明确设备的可调用只读通道，返回 `ReadResult(output, exit_code)`。仅接受用户空间、包列表和显示/焦点查询白名单，通过 ADB `exec-out` 避开 SDK shell 哨兵/PTY 处理；不能用于点击、启动、输入或任意 shell 命令。

实例枚举使用总计最多 24 秒预算，安装列表与焦点回退分别限定 12 秒、8 秒；调用方复用预算对象，不能每个回退重新开始无限等待。空输出、诊断文本和失败按未知处理，不能据此记录应用未安装；确认可读的用户空间单独保留，坏分身空间通过 warning 报告，不推测额外用户或实例。已选实例失效仍停止，不能因读取失败转而执行主账号。本轮属于本地回归覆盖，未声称已在 Issue #7 的设备上复现验证。

## 12. Android UI 调用与可选系统输入

实现：[rpc_limits.py](../backend/core/device/rpc_limits.py)、[adapter.py](../backend/core/device/adapter.py)。

`install_rpc_limits(driver, timeout=12)` 在单个 SDK 驱动上约束 RPC 参数及其 ADB 隧道 socket。不会修改全局 SDK 常量，也不为超时留下仍会操作手机的后台线程。每次连接的 UI socket 上限默认 12 秒；SDK 重连和其他 ADB 操作有各自预算，不能把单次 socket 限制描述成整段恢复的总耗时。

`DeviceAdapter.input_ascii(text)` 是可选能力，仅 Android 接受 1～32 位 ASCII 字母或数字；调用者先确认和聚焦可见输入框，适配器再次检查所选实例，按当前逻辑显示屏执行系统输入，命令预算 8 秒。它不清空任意文本、不安装或切换输入法，不接受空格、Shell 字符或 Unicode；通用 Unicode 输入继续使用 `input_text`。旧框架及其他后端未必有此能力，活动应使用 `getattr` 检查，不把它列为所有 `DeviceBackend` 的强制方法。


### 自动分身与无线表单

`configure/run` 可携带布尔 `all_instances`，保存在当前服务工作区的状态中，默认关闭。流水线在每个活动装配后，开启时从当前 `app_instances` 重新生成全部 ID；显式关闭时选主应用，不继承上次的全部分身队列。批量启动沿用各工作区自身开关，不复制发起设备的选择；原有显式 `instance_ids` API 保留。身份核对、串行执行、异常中断及日记录隔离继续生效。

无线表单统一用 `wifi-full-address` 控制两项地址的四段 IPv4/完整地址模式，保留独立的配对与连接端口；切换模式同步已填值，设备切换恢复该工作区草稿。表单布局不再套用通用连接网格的跨列按钮规则。退出操作统一由系统托盘提供，未加入浏览器关闭自动退出，避免刷新或关闭一个标签时停止其他设备。
