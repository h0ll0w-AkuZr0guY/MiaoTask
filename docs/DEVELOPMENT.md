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

源码运行仍直接发现仓库内活动。默认便携构建只包含框架和共用代码，活动中心从官方 GitHub main 按需获取活动源码；开发与构建验证方法见[构建文档](BUILDING.md)。无需为每次活动规则更新发布框架或额外的活动 Release。

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

### 先补词表，再扩策略

适配优先使用“场景证据＋词表＋相对区域”，允许文案的同义词、范围和正则匹配。首页可能把相邻图标说明合并为一行OCR，应在确认首页后复用原生图标区域；弹窗、任务、按钮的配对以当前页面为准，不依赖个人设备像素位置。

应用公共配置位于 `backend/app/<app>/configs/base.json`；活动入口、完成与推广词表位于各活动 `config.json`。跳转的公共默认词表在 `app/common/utils/destinations.py` 的 `JUMP_WORDS`，应用可在 `jump_words.request/allow/deny_context` 追加正则或别名，不需要修改匹配代码。例如新增一种确认说法可填入 `"jump_words": {"allow": ["好，继续跳转"]}`。词表逐组追加，不能通过新增词条取消登录、支付、安装或持续授权的边界。

修改词表仍要用最小合成页面验证正反例：目标词可命中，相邻商品、搜索联想、其他权限和普通列表不可误命中。能用词表表达的差异不新增机型判断；独特流程再新增有边界的活动策略。公共组件与错误语义变更同步接口手册。

| 工作 | 先读实现 | 预计改动归属 |
| --- | --- | --- |
| 已有活动的文案、入口或任务兼容 | `backend/app/taobao/activities/double11/` | 入口、解析、动作或核验策略；通用循环通常无需改动 |
| 新增同应用活动 | 现有活动 manifest 与 `Activity` | `backend/app/<app>/activities/<activity>/`，配置与活动工厂 |
| 新增应用 | `registry.py`、`profile.py`、接口手册第 1 / 6 节 | 新 app 目录，复用 core 与已有控制台 |
| 新设备 / 模拟器 / 屏幕策略 | `core/device/` 与接口手册第 2～4 / 9 节 | 设备发现、适配器、显示映射和图像处理；应用规则不下沉 |
| 队列、失败预算或记录 | `execution/task_policy.py`、`storage/`、服务绑定 | 保留语义身份、跨日、实例隔离和旧记录接续 |
| 页面功能 | `frontend/js/api.js`、相应组件和后端路由 | 复用请求层，明确工作区；后端仍判定任务状态 |

## 结构与扩展

代码分为 `backend/core` 框架、`backend/app` 平台与活动、`frontend` 网页。注册、配置、旧数据接续、屏幕和模拟器接口见 [架构与扩展](ARCHITECTURE.md)。淘宝的双11、淘金币日常、农场、红包签到和成就中心独立注册；新增平台仍需独立实现和验收，不复制固定坐标。

### 新应用 / 活动的接入顺序

1. 明确应用包名与用户空间、任务入口、成功证据和人工处理边界。确认能复用哪种解析布局与导航策略；页面不相似时替换策略，保留框架的执行和记录管线。
2. 创建 app manifest 和 `configs/base.json`，声明 `ApplicationProfile`；同应用新增活动直接复用原 app。包名模式只覆盖此应用的有效实例，不使用泛匹配。
3. 创建活动 manifest、`config.json` 和工厂类。提供入口、任务解析、动作与核验，可以按职责拆为独立文件。公开 ID 可以有连字符，Python 目录采用合法标识符，由 `factory` 显式对应。
4. 完成活动契约。通常继承 `Activity`、实现抽象方法，复用 `Session.read/click` 和控制句柄。默认 `Navigator` 还有页面策略钩子，见接口手册；没有同样页面结构时覆写 `ensure_list/recover`，仍须返回已确认的列表或有限失败。
5. 先用 `--list-activities` 核对目录与默认项，再用 `--app <id> --activity <id>` 选择目标。控制台从注册目录加载活动，不额外开发另一套页面或监听端口。

6. 在真正的空框架 exe 中检查活动源码获取、安装与工厂导入，再检查网页计划选择和设备执行；离线样本和打包方法见[构建文档](BUILDING.md)。只从源码成功 import 无法证明冻结运行库包含所需依赖。

6. 按影响更新使用说明、共享接口和待发布贡献；兼容验证与提交要求遵循 [CONTRIBUTING](CONTRIBUTING.md)，命令统一见 [BUILDING](BUILDING.md)。

### 让活动可按需安装

* app / activity manifest 的 `APP` / `META` 直接用 `AppMeta(...)` / `ActivityMeta(...)` 声明。公开身份、工厂和配置路径使用字面量，工厂留在本活动模块中；Python 目录使用合法小写模块名，公开 ID 可带连字符。
* 活动目录提供 `compat.json`，声明 `schema: 1`、所需 `framework_api` 与 Python 主次版本，例如 `3.13`。不要依赖针对旧 manifest 的摘要兼容白名单；新声明必须能在目录静态读取阶段核定。
* 同应用共享工具放在应用级目录，活动使用原有 `backend.app.<app_module>...` 导入。所选活动只下载应用共享内容和自身源码，不依赖另一个活动已安装。应用 `__init__` 和 manifest 也不要在导入阶段依赖某个可卸载的活动。
* 活动共用库不同版本并存时不能互相覆盖；同应用已装活动的共享文件清单与摘要需一致。改变共享工具时协调这些活动的兼容更新，不绕过安装器检查。
* 活动包不携带 pip 环境，也不会自动装依赖。优先复用框架；需要新增运行库时单独提交框架依赖变更、冻结验证与兼容声明。
* 活动更新以官方 main 的源码提交追踪，合并后可在活动中心刷新获取。普通 PR 继续追加 `UNRELEASED.md`，不创建活动 Release，不改框架版本；框架发布由维护者决定；Release Builder 默认自动递增末位版本，目标版本只写入构建副本。

### 计划、实例与并行

每个工作区用显式应用/活动列表保存流水线；UI 勾选与排序只改变计划。一个设备依次执行活动，每项再顺序处理自己的实例队列；不同设备的线程并行。跨活动仅复用公共任务设置，入口、搜索词与核验从当前活动配置和实现取得。

同一应用的实例选择需保持明确，跨应用切换重新绑定应用身份；未知或失效选择停止，不能自动换账号。工作区改连另一台设备时清空实例计划。单设备异常或手动停止不自动推进剩余活动，其他设备继续；包更新则需全部工作线程空闲，通过 `ActivityCatalogHub` 的全局安装门完成。

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
| `.venv/`、`runtime/`、`activities/`、`.local/`、`dist/` | 开发环境、运行数据、本机验证材料与产物，均不提交 |

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
