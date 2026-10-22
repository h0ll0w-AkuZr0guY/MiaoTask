# 开发与设计

开发前依次阅读[架构](ARCHITECTURE.md)、[贡献规范](CONTRIBUTING.md)和本页，再按需求查阅[公用能力与接口手册](REUSABLE_INTERFACES.md)。设备接入与坐标属于 core，平台规则位于对应 app，前端只提交操作与显示状态。先复用已有能力，再判断是否需要扩展共用契约。

## 源码运行

使用 Python 3.13，准备可用的 Android ADB；手机保持解锁并允许调试。HarmonyOS 驱动接口需要另行提供 HDC，当前便携包不包含 HDC。

```powershell
git clone https://github.com/h0ll0w-AkuZr0guY/MiaoTask.git
cd MiaoTask
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe main.py
```

Windows 也可用 `start.bat`。网页无需 npm 或额外构建步骤。命令行参数示例：

```powershell
# 自动进入活动，限制本轮次数
.\.venv\Scripts\python.exe main.py --cli --auto-open --max-tasks 2
# 仅识别当前页面
.\.venv\Scripts\python.exe main.py --cli --scan
# 指定实际发现的 Android 用户空间（数字仅为示例）
.\.venv\Scripts\python.exe main.py --cli --instance com.taobao.taobao@999 --auto-open
# 修改网页端口
.\.venv\Scripts\python.exe main.py --port 8766 --no-browser
```

`--serial` 指定设备，`--config` 加载公共配置覆盖项，完整参数见 `main.py --help`。默认活动规则在 `backend/app/taobao/activities/double11/config.json` 与 `backend/core/config/defaults.json`，不保存个人设备信息。

默认网页模式和单设备 CLI 使用同一注册表与活动工厂。CLI 示例适合单进程单设备调试；多设备、日统计、实时队列选择与诊断绑定由控制台的 `DeviceHub/Controller` 统一管理，不应启动多份 CLI 去操作同一设备。

## 从哪里开始修改

| 工作 | 先读实现 | 预计改动归属 |
| --- | --- | --- |
| 已有活动的文案、入口或任务兼容 | `backend/app/taobao/activities/double11/` | 入口、解析、动作或核验策略；通用循环通常无需改动 |
| 新增同应用活动 | 现有活动 manifest 与 `Activity` | `backend/app/<app>/activities/<activity>/`，配置与活动工厂 |
| 新增应用 | `registry.py`、`profile.py`、接口手册第 1 / 6 节 | 新 app 目录，复用 core 与已有控制台 |
| 新设备 / 模拟器 / 屏幕策略 | `core/device/` 与接口手册第 2～4 / 9 节 | 设备发现、适配器、显示映射和图像处理；应用规则不下沉 |
| 队列、失败预算或记录 | `execution/task_policy.py`、`storage/`、服务绑定 | 保留语义身份、跨日、实例隔离和旧记录接续 |
| 页面功能 | `frontend/js/api.js`、相应组件和后端路由 | 复用请求层，明确工作区；后端仍判定任务状态 |

## 结构与扩展

代码分为 `backend/core` 框架、`backend/app` 平台与活动、`frontend` 网页。注册、配置、旧数据接续、屏幕和模拟器接口见 [架构与扩展](ARCHITECTURE.md)。当前真实应用适配范围为原有淘宝活动，新增平台需要独立实现和验收，不复制固定坐标。

### 新应用 / 活动的接入顺序

1. 明确应用包名与用户空间、任务入口、成功证据和人工处理边界。确认能复用哪种解析布局与导航策略；页面不相似时替换策略，保留框架的执行和记录管线。
2. 创建 app manifest 和 `configs/base.json`，声明 `ApplicationProfile`；同应用新增活动直接复用原 app。包名模式只覆盖此应用的有效实例，不使用泛匹配。
3. 创建活动 manifest、`config.json` 和工厂类。提供入口、任务解析、动作与核验，可以按职责拆为独立文件。公开 ID 可以有连字符，Python 目录采用合法标识符，由 `factory` 显式对应。
4. 完成活动契约。通常继承 `Activity`、实现抽象方法，复用 `Session.read/click` 和控制句柄。默认 `Navigator` 还有页面策略钩子，见接口手册；没有同样页面结构时覆写 `ensure_list/recover`，仍须返回已确认的列表或有限失败。
5. 先用 `--list-activities` 核对目录与默认项，再用 `--app <id> --activity <id>` 选择目标。控制台从注册目录加载活动，不额外开发另一套页面或监听端口。
6. 按影响更新使用说明、共享接口和待发布贡献；兼容验证与提交要求遵循 [CONTRIBUTING](CONTRIBUTING.md)，命令统一见 [BUILDING](BUILDING.md)。

最小声明示例，下面的 `sample` 为尚未实现的占位应用：

```python
# backend/app/sample/manifest.py
from backend.core.device.profile import ApplicationProfile
from backend.core.registry import AppMeta

PROFILE = ApplicationProfile(
    label='示例应用',
    android_package='org.example.sample',
    package_pattern=r'org\.example\.sample',
)
APP = AppMeta('sample', '示例应用', PROFILE)
```

```python
# backend/app/sample/activities/daily_tasks/manifest.py
from backend.core.registry import ActivityMeta

META = ActivityMeta(
    app='sample', id='daily-tasks', label='每日任务',
    factory='backend.app.sample.activities.daily_tasks.activity:DailyTasksActivity',
    device_types=('uiautomator2',),
)
```

`DailyTasksActivity` 需要实现 `default_config`、`parse_tasks`、`is_task_list`、`execute`、`verify` 并绑定 `application = PROFILE`。`execute` 返回 `(after_screen, explicit, cross)`，`verify` 给出是否成功；动作产生的统计依实际行为更新，不能仅把停留时间当完成证明。`Activity.run()` 负责 setup、循环与 finally 中的 teardown；优先修改活动方法而非重写循环。

配置按 `core → app → activity → 用户覆盖` 读取并校验。当前 schema 保留入口文案、搜索词和固定任务类型等原有契约，新活动必须提供有效值；如需新任务类型，需同步考虑 schema、候选选择、统计和前端。顶层同名字段整体替换，嵌套字段不会自动深合并。

### 复用代码的选择

* 页面任务也是右侧按钮与同行进度时，向 `app/common/models/task.py` 的 `TaskRules` 注入词表和分类；布局不同就返回同样的 `Task` 模型，不复制一份核心调度。
* 页面文字与图像通过 `ScreenReader` 统一观察。自定义裁剪 / 缩放提供 `PreparedImage.to_device`，点击使用回映后的当前设备坐标。系统栏、挖孔及逻辑 / 物理显示屏映射留在设备层。
* 活动等待使用 `self.control.wait()`，输入使用会话或适配器已有方法；长时间 `sleep`、直接驱动输入会削弱暂停与停止响应。
* 统计、失败现场与任务选择接入会话事件和 `TaskPolicy`，由控制器绑定日记录。跨应用缺失依据所选设备、所选用户空间的真实反馈，不能写死开发设备的安装情况。
* `DeviceBackend/DriverFactory` 等是扩展接缝，当前服务并不自动发现新驱动。新增后端需完整处理装配、物理身份、实例、显示屏及停止语义，具体限制见[接口手册](REUSABLE_INTERFACES.md#9-预留扩展与维护要求)。

## 本地资源的职责

| 位置 | 内容与使用方式 |
| --- | --- |
| `frontend/assets/` | 控制台与 Windows 统一品牌图标；现有资源由静态白名单提供 |
| `docs/assets/` | 可公开的脱敏功能图与架构图；界面变动时同步检查，不放原始设备画面 |
| `shared/` | 共享静态资源约定；便携包的 `shared/tools` 由构建装配，应用不把运行记录写入此处 |
| `build/` | 本地 ADB 工具、构建工作区等，除说明文件外忽略；不作为运行数据或源码包目录 |
| `.venv/`、`runtime/`、`.local/`、`dist/` | 开发环境、运行数据、本机验证材料与产物，均不提交 |

发布资源受静态路由与公共文件审计白名单约束。新增可公开图片或资源时更新相应白名单，逐一检查设备标识、账号、通知与元数据；不要为方便打包把整个本地文件夹加入源码或 exe。

首页概览为原创 AI 手绘风插图，用于说明组件关系；功能图来自 1920 × 1080 桌面视口的真实前端组件裁图。展示用工作区未连接设备，任务图为明确标注的合成数据，不作为实测完成证据。更新图片时保留比例与可读性，重新检查图片元数据，删除不再引用的旧图。

## 执行约束

流程为「观察 → 解析 → 执行 → 返回 → 核验 → 重读」。任务标题、进度和按钮配对后才操作；UI 树不可靠时使用本地 OCR 复核前台。搜索优先原生 `set_text`，重新定位右侧提交按钮，必要时回车；确认结果页后才浏览，不安装或切换输入法。

任务完成依据为进度、完成标签或对应奖励状态；无法确认时不计成功。新替换任务拥有独立身份。任务尝试、返回层数、等待、列表滚动和整轮执行均有上限。恢复优先逐层 Back，每次重读并检查认证和实例；到达返回上限后 Home，确认桌面才使用共享的一次冷重启额度。初次启动与异常恢复分别计数；认证等人工处理状态独立停止。

实例由包名与 Android 用户空间共同确定。启动、停止和恢复限定用户，操作前核对焦点窗口。启动转场未确认前台时，允许再次唤起同一实例一次，不重复强停；仍无法确认或操作途中身份不符时停止。队列串行执行，记录按实例隔离，不读取账号标识。

HTTP 仅绑定本机，写操作校验 Host、Origin 与会话令牌。每台设备一个独立工作线程，DeviceHub 在同一服务内并行调度；预览通过各自 I/O 锁读取；新截图按序替换，短时缓存遇到操作或前台变化失效，失败现场使用新帧。无网页访问时停止额外预览，Windows 托盘负责重新打开和退出。

## 数据与接口

源码数据默认位于 `runtime/`，便携版位于 `%LOCALAPPDATA%\MiaoTask`；`MIAO_DATA_DIR` 可覆盖位置。记录使用 `apps/<app>/activities/<activity>/daily/<date>/<device_hash>/instances/<instance_hash>/`。原活动首次接续旧 daily 记录，保留旧文件，其他活动不导入。

| 数据 | 含义 |
| --- | --- |
| `state.json` | 当日累计、计数水位、任务选择与失败预算、不可用任务 |
| `events.jsonl` | 阶段变化与日志 |
| `failures/` | 失败说明和可取得的原始截图 |
| API `stats` / `daily.stats` | 本轮 / 当前实例今日累计 |
| API `image_sequence` | 新截图序号 |
| GET `/api/diagnostics` | 当前设备、所选实例当天的本地诊断 ZIP |

任务队列的默认选择由后端识别给出，网页“执行 / 跳过”只改变选择，不立即启动；作用域和任务身份规则见架构文档。普通贡献在 `docs/UNRELEASED.md` 累积，版本汇总与发布由维护者统一操作。

接口和配置以当前代码为准。公共能力变更时同步[接口手册](REUSABLE_INTERFACES.md)的签名、输入输出、限制和示例；所有权或依赖方向变化时再更新架构。结构调整应独立提出迁移方案，避免让活动逻辑依赖网页布局或具体驱动细节。参与修改见[贡献规范](CONTRIBUTING.md)，验证、构建和发布见[测试与打包](BUILDING.md)。
