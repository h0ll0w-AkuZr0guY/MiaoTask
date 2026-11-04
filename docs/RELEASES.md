# 发布记录

按新到旧维护用户可见变化。GitHub Release 正文从对应的 `## 版本号` 小节提取，发布状态以 [Releases](https://github.com/h0ll0w-AkuZr0guY/MiaoTask/releases) 为准。

## MiaoTask-v0.2.1

完成返回节奏与 MTK 屏幕兼容优化。

* 【新增】公共设置 → 任务设置提供「完成后随机返回延时」，在读到明确完成提示后随机等待再返回。默认范围为0～1000毫秒，可按实际情况调整上限；**CPU识别本身较慢的用户建议设为0**。等待支持暂停/停止，并受剩余任务预算限制。
* 【修复】兼容部分MTK设备及其他ROM的主屏设备名、分散字段和uniqueId格式，优先解析当前逻辑屏的物理ID；同名屏幕有歧义时停止，不猜测目标。
* 【文档】贡献规范与AI开发约定明确：活动入口、解析、点击和核验问题优先修改活动源码；确有公共框架问题时说明理由、兼容影响并更新接口文档。

本版主要针对完成后返回过快的反馈提供可调节奏，不保证避免平台安全验证；出现验证仍需本人处理。已有用户保存的配置继续保留，实际延时以公共设置当前值为准。

下载 Windows x64 ZIP，完整解压后运行 `MiaoTask.exe`，保留 `_internal/` 与 `activities/`；预装当前六项活动。升级前退出旧托盘服务，用户目录中的配置与日记录保留。

GPU加速继续使用独立的 [CUDA扩展 v1.0](https://github.com/h0ll0w-AkuZr0guY/MiaoTask/releases/tag/MiaoTask-CUDA-v1.0)，已安装的兼容扩展可迁移 `runtime_plugins/`，无需重新下载。[使用说明](https://github.com/h0ll0w-AkuZr0guY/MiaoTask/blob/main/docs/USAGE.md) · [项目介绍](https://github.com/h0ll0w-AkuZr0guY/MiaoTask#readme)

感谢 @jiang123574（#39、#40）的贡献，以及 [MonsterNone/tmall-miao](https://github.com/MonsterNone/tmall-miao) 提供的早期思路参考。

| 框架模块 | 主要修改 |
| --- | --- |
| `core/device/displays.py` | MTK等ROM的逻辑屏块与物理uniqueId解析，同名歧义保护 |
| `core/execution/session.py`、`app/taobao/utils/actions.py` | 活动显式调用完成后等待，暂停/停止与剩余预算约束 |
| `core/config`、`frontend` | 可持久化的毫秒上限、默认值与CPU调整提示 |
| `docs/CONTRIBUTING.md`、`AGENTS.md`、接口文档 | 活动与框架修改边界，新增设置及接口契约 |

## MiaoTask-v0.2.0

可选 CUDA 加速与活动入口修复。

**2026-11-04 修订包：** 已包含 [#36](https://github.com/h0ll0w-AkuZr0guY/MiaoTask/pull/36)，修复多屏设备未压缩 PNG 截图解码失败；已下载旧 v0.2.0 的用户请重新下载基础 ZIP，CUDA 扩展 v1.0 可继续沿用。

* 【新增】可选 CUDA 扩展 v1.0，独立下载与更新；基础包继续使用 CPU，扩展不兼容或 GPU 初始化、推理失败时回退 CPU。
* 【优化】CPU 保留串行稀疏识别；CUDA 在浏览末尾核验并有界补滑。页面加载后再开始浏览计时，减少动画期间过早返回。
* 【修复】搜索必须确认提交，重新定位可输入控件；输入法 X 键不再被误当弹窗关闭。跳转短暂失焦有界重读；PNG 截图显式映射主屏，避免多屏警告混入图片。保留实例与显示屏身份检查。
* 【修复】红包签到首页入口、淘金币任务入口优先级、农场叠层推广关闭，以及月月赚实例唤起与全局搜索入口；奖励仍以回执或对应任务进度核验。
* 【新增】公共设置支持 OCR 后端与活动下载代理；复用已有偏好存储，保留词表、任务配置和分支选择。代理连接异常有界重试。

下载 Windows x64 ZIP，完整解压后运行 `MiaoTask.exe`，保留 `_internal/` 和 `activities/`。包内预装当前六项活动。升级前退出旧助手，用户目录中的公共配置与日记录保留；安全认证需本人处理。

**可选 GPU 加速：** [CUDA 扩展 v1.0 与安装说明](https://github.com/h0ll0w-AkuZr0guY/MiaoTask/releases/tag/MiaoTask-CUDA-v1.0) · [直接下载扩展 ZIP](https://github.com/h0ll0w-AkuZr0guY/MiaoTask/releases/download/MiaoTask-CUDA-v1.0/MiaoTask-CUDA-v1.0-windows-x64.zip)。退出助手后，将扩展中的 `runtime_plugins/` 放在 `MiaoTask.exe` 旁再启动。需要兼容的 NVIDIA 显卡及驱动；扩展按 Windows x64、Python 3.13 与 ONNX Runtime 1.31.0 校验，不需要安装 PyTorch。以后兼容的框架版本继续引用此独立扩展，无需重复下载。

本版提供运行时扩展加载，源码自动更新与本地重打包启动器尚未实现。

感谢 @jiang123574（#33）的 CUDA 与下载代理贡献、@kamao945（#36）的主屏截图修复，以及 [MonsterNone/tmall-miao](https://github.com/MonsterNone/tmall-miao) 提供的早期思路参考。[使用说明](https://github.com/h0ll0w-AkuZr0guY/MiaoTask/blob/main/docs/USAGE.md) · [项目介绍](https://github.com/h0ll0w-AkuZr0guY/MiaoTask#readme)

| 框架模块 | 主要修改 |
| --- | --- |
| `core/device/ocr.py`、`runtime_plugin.py`、`screen.py` | 可选运行库加载、兼容校验、实际执行后端与 CPU 回退；保留物理设备模型复用 |
| `core/device/adapter.py`、`read_shell.py` | 短暂失焦共享超时预算、窗口信息回退与诊断；PNG 主屏映射与有界取消，保留显示屏和实例守卫 |
| `core/preferences.py`、`core/service` | OCR 后端与代理偏好持久化；后端切换限定设备空闲，保留既有配置 |
| `app/taobao/utils`、`app/zfb` | 搜索提交、页面就绪与浏览补时；日常入口和任务核验修复 |
| `scripts/package_windows.py`、`configs/environments/requirements-cuda.lock` | CPU 基础包与独立版本 CUDA ZIP 分开构建，共有依赖对齐 |
| `frontend`、`docs` | 后端与代理设置提示，扩展安装、兼容、回退及贡献接口说明 |

## MiaoTask-v0.1.6

串行低频识别、公共词表与设备兼容优化。

* 【优化】首轮连续浏览后返回核验，失败才有界补滑；移除持续采集、后台OCR与试验性CPU/GPU资源配置，保留多设备并行。
* 【新增】搜索词每次随机抽取，任务跳过统一使用可编辑公共词表；配置保存到用户目录，升级保留。
* 【修复】三态补扫、折叠任务列表与落地页关闭按钮保护；未确认的扫描不标记完成，补查额度跨重启保留。
* 【优化】物理设备模型复用、重复任务按实际进度计数；活动中心先显示本地目录，再后台刷新。
* 【修复】部分ROM忽略媒体音量命令时回退AudioService并读回核验，结束恢复原音量；默认基础浏览17秒、补滑复查3次、今日失败上限3次，本轮80次。

下载 Windows x64 ZIP，完整解压后运行 `MiaoTask.exe`，保留 `_internal/` 和 `activities/`。包内预装当前六项活动。升级前退出旧助手，安全认证需本人处理。

感谢 @ashen0116（#26）、@J202407（#28）、@Chilzen-smx（#31）、@jiang123574（#32）的贡献，以及 [MonsterNone/tmall-miao](https://github.com/MonsterNone/tmall-miao) 提供的早期思路参考。[使用说明](https://github.com/h0ll0w-AkuZr0guY/MiaoTask/blob/main/docs/USAGE.md) · [项目介绍](https://github.com/h0ll0w-AkuZr0guY/MiaoTask#readme)

| 框架模块 | 主要修改 |
| --- | --- |
| `core/execution` | 串行观察、失败后补滑、有界延期，整轮预算共享 |
| `core/device/screen.py`、`ocr.py` | 按需截图、物理设备模型复用与运行结束释放 |
| `core/device/comfort.py` | 媒体流命令兼容回退，降低与恢复均核验 |
| `core/device/read_shell.py`、`adapter.py` | 有界可取消I/O、连续滑动和显示屏核对 |
| `core/config`、`storage` | 公共词表与参数继承，持久化及旧配置迁移 |
| `core/plugins`、`service` | 活动目录缓存、后台单次刷新与工作区状态隔离 |
| `frontend` | 公共词表、串行浏览配置，移除失效资源选项 |

## MiaoTask-v0.1.5

任务复查、持续滑动与设备兼容修复。

* 【修复】手动开始重新读取任务，旧完成标记不再阻止启动（#23）；新增「清理任务标记」及默认关闭的结束后复查开关，保留累计统计。
* 【优化】滚动浏览采用后台 OCR 与有效手势计时，零间隔不插入等待，截图、识别和额外间隔不抵扣浏览时间；重复任务按自身进度核验，进度增加后重置连续失败预算。
* 【修复】平板居中任务面板、领奖后旧控件树、遮挡弹窗及搜索兜底；补充头条「浏览器打开」等跳转确认，返回后继续核验。
* 【新增】可选执行期间保持亮屏，结束后恢复；开始前清空设备剪贴板默认开启、可关闭；最低媒体音量兼容新旧系统命令，设置及恢复后读回核验。
* 【维护】活动说明缩为选择框内的小图标，识别日志明确为正常本地 OCR；合并同类测试。活动新增 `bundle_by_default` 配置，本包默认预装现有全部六项活动。

下载 Windows x64 ZIP，完整解压后运行 `MiaoTask.exe`，保留 `_internal/` 与 `activities/`。升级前从托盘退出旧助手；日记录与公共配置保留在系统用户目录。安全认证需本人处理，诊断截图分享前请脱敏。跨应用任务以返回列表的进度为准，无法通过核验的任务保留失败记录，不计为完成。

感谢 [MonsterNone/tmall-miao](https://github.com/MonsterNone/tmall-miao) 提供早期实现思路参考。[使用说明](https://github.com/h0ll0w-AkuZr0guY/MiaoTask/blob/main/docs/USAGE.md) · [项目介绍](https://github.com/h0ll0w-AkuZr0guY/MiaoTask#readme)

### 框架与公共组件修改

| 模块 | 主要修改文件 | 行为变化 |
| --- | --- | --- |
| 启动与任务记录 | `backend/core/service/panel.py`、`backend/core/execution/task_policy.py` | 手动启动重读列表；清理任务标记不清累计统计；完成后可选复查一次，仍保留设备、活动和实例隔离。 |
| 执行循环与任务身份 | `backend/core/execution/loop.py`、`retry.py`、`session.py` | 重复任务按当前进度继续；有效进度重置连续失败预算；语义优先于行位置，收尾有界重扫。 |
| 读屏与连续滑动 | `backend/core/device/screen.py`、`backend/core/execution/session.py` | 新增单帧在途的后台视觉识别接口；OCR 线程不访问设备；手势返回时长，供动作层累计有效滑动时间；保留停止、身份和画面新鲜度检查。 |
| 设备输入 | `backend/core/device/adapter.py` | 滑动 RPC 超时可有界回退系统滑动；新增清空设备剪贴板接口，不读取内容或切换输入法。 |
| 公共运行设置 | `backend/core/device/comfort.py`、`backend/core/config/defaults.json`、`schema.py` | 增加亮屏、复查和剪贴板选项；媒体音量优先现代系统命令、兼容旧命令，设置与恢复均读回核验，结果进入运行日志。 |
| 公共识别与导航 | `backend/app/common/models/task.py`、`utils/destinations.py`、`backend/core/execution/navigator.py` | 居中面板排除背景文字干扰；跳转词表补充浏览器打开场景；入口处理遮挡后重新观察。 |
| 淘宝共享策略 | `backend/app/taobao/utils/actions.py`、`daily.py`、`search.py`、`verification.py` | 接入后台识别和有效滑动计时；完善可见输入框兜底、弹窗处理与重复任务核验，供淘宝活动复用。 |
| 网页与预览 | `frontend/index.html`、`frontend/js/app.js`、`components/activity-selector.js`、`frontend/css/base.css`、`scripts/site-preview/adapter.js` | 增加清理标记与公共配置交互；缩小活动说明图标，演示页同步相应交互。 |
| 构建与维护 | `scripts/package_activities.py`、`package_windows.py`、`local_release.py`、`release_builder_gui.py`；`tests/`、`docs/` | 按活动布尔配置决定默认预装，保留显式选择及纯框架选项；构建元数据反映实际预装内容；测试归并至 21 个模块，更新接口、使用和贡献规范。 |

## MiaoTask-v0.1.4

活动扩展、资源容灾与执行体验更新。

* 【新增】支付宝「月月赚」浏览活动与可选活动说明；优化淘金币任务识别、弹窗处理及收工重扫。感谢 @ashen0116（#17）与 @Chilzen-smx（#18）的贡献。
* 【修复】活动目录支持官方分支选择；API 限流时尝试静态资源与官方源码归档，新增卸载后离线恢复。仅打开时检查一次，后续手动刷新。
* 【新增】公共配置保存在用户目录；最低媒体音量、亮度选项默认关闭，执行结束后尝试恢复原值。
* 【优化】恢复流水线时跳过当日已确认完成的活动实例；浏览任务按副标题判断，收尾重扫并避免重复执行已核验进度。
* 【修复】退出时清理本次启动的当前安装 ADB。启用 GitHub Pages 在线工作台预览与活动备用下载，main 的界面和活动更新后自动同步。

Windows x64 ZIP 预装全部六项活动，完整解压后运行 `MiaoTask.exe`，保留 `_internal/` 与 `activities/`。升级前从托盘退出旧助手；日记录与公共配置保留在用户目录。安全认证需本人处理，诊断截图分享前请脱敏。GitHub Pages 与官方源码归档提供备用下载，本机保留活动恢复副本。[在线预览](https://h0ll0w-akuzr0guy.github.io/MiaoTask/) 使用演示数据，不连接手机或执行真实任务。

感谢 [MonsterNone/tmall-miao](https://github.com/MonsterNone/tmall-miao) 提供早期实现思路参考。[使用说明](https://github.com/h0ll0w-AkuZr0guY/MiaoTask/blob/main/docs/USAGE.md) · [项目介绍](https://github.com/h0ll0w-AkuZr0guY/MiaoTask#readme)

## MiaoTask-v0.1.3

淘宝日常活动、设备工作台与框架稳定性更新。

* 【新增】淘金币日常、芭芭农场、红包签到与成就中心独立活动，复用浏览、搜索、跳转返回和奖励核验；从活动中心按需获取源码。
* 【优化】设备连接、改名与无线配对集中到左侧弹窗；分身默认关闭，开启后自动串行执行主应用与全部分身，各设备独立配置。
* 【新增】本机排行榜按设备应用或活动汇总累计时长、完成、跳过与失败，沿用系统用户目录的日记录。
* 【优化】紧凑活动计划、公共设置与悬浮预览，提供暖橙、湖蓝、午夜主题；统一弹窗关闭、无线表单与表格对齐。
* 【修复】限制单驱动 UI RPC 与底层连接等待，补充指定显示屏和实例约束的系统输入，完善搜索、跳转确认与返回核验。

Windows x64 ZIP 默认仅预装双11活动；其他活动在活动中心从源码仓库获取。完整解压后运行 `MiaoTask.exe`，保留 `_internal/` 和 `activities/`。升级前从托盘退出旧助手；日记录保留在系统用户目录。安全认证需本人处理，诊断截图分享前请脱敏。

感谢 [MonsterNone/tmall-miao](https://github.com/MonsterNone/tmall-miao) 提供早期实现思路参考。[使用说明](https://github.com/h0ll0w-AkuZr0guY/MiaoTask/blob/main/docs/USAGE.md) · [项目介绍](https://github.com/h0ll0w-AkuZr0guY/MiaoTask#readme)

## MiaoTask-v0.1.2

活动源码按需更新、设备流水线与可视化打包。

* 【新增】框架与活动分开交付，活动中心从官方 GitHub main 按需安装、更新或卸载所选活动，无需随活动修改重打框架。
* 【优化】活动源码独立保存在 exe 旁的 `activities/apps/`，活动中心可查看并打开实际目录；旧安装复制迁移，保留日记录。
* 【新增】每台设备可勾选、排序多个活动，活动与分身串行、不同设备并行；更换设备后清空旧实例计划。
* 【修复】Android 实例与安装信息读取增加有界 ADB 回退，区分读取失败与确认未安装，异常分身空间不阻断主空间。关联 #7。
* 【新增】可视化 Release Builder 接受源码目录、ZIP 或 GitHub 链接，默认递增小版本，可预装官方活动，汇总贡献并生成带环境 ZIP；PR 格式改为提示，本地与云端复用同一预检入口。

下载 Windows x64 ZIP，完整解压后运行 `MiaoTask.exe`，保留 `_internal/`；未预装活动时，在活动中心选择下载。升级前退出旧助手。安全认证需本人处理，诊断截图分享前请脱敏。

感谢 [MonsterNone/tmall-miao](https://github.com/MonsterNone/tmall-miao) 提供早期实现思路参考。[使用说明](https://github.com/h0ll0w-AkuZr0guY/MiaoTask/blob/main/docs/USAGE.md) · [项目介绍](https://github.com/h0ll0w-AkuZr0guY/MiaoTask#readme)

## MiaoTask-v0.1.0

多设备并行、无线调试与可扩展架构。

* 【新增】同页管理多台设备，独立启停、并行执行，也可批量应用配置并启动。
* 【新增】Android 无线调试配对向导，IP 分段输入、自动换框，配对后复用地址。
* 【新增】MuMu 运行实例发现，适配手机、平板及模拟器的横竖屏与不同显示屏。
* 【新增】任务行实时选择“执行 / 跳过”，选择、失败次数与统计按设备、实例、日期保存。
* 【优化】框架、应用与前端分层，支持活动注册和分层配置；更新首页、公共接口及贡献文档，保留原有任务流程和旧记录接续。

下载 Windows x64 ZIP，完整解压后运行 `MiaoTask.exe`，保留 `_internal/`。升级前退出旧助手。安全认证需本人处理，诊断截图分享前请脱敏。

感谢 [MonsterNone/tmall-miao](https://github.com/MonsterNone/tmall-miao) 提供早期实现思路参考。[使用说明](https://github.com/h0ll0w-AkuZr0guY/MiaoTask/blob/main/docs/USAGE.md) · [项目介绍](https://github.com/h0ll0w-AkuZr0guY/MiaoTask#readme)

## MiaoTask-v0.0.9

分身管理、滚动设置与系统托盘。

* 【新增】淘宝主应用/分身选择与串行队列，按包名和用户空间锁定实例，统计独立保存；分身功能默认关闭。
* 【新增】Windows 托盘支持打开控制台、停止并退出。统一图标，任务、滚动和高级设置按需展开。
* 【优化】滚动距离、时长、连续次数和额外间隔范围可调，默认不增加额外等待；随机节奏不保证规避风控。
* 【修复】重启后实例确认失败，限定再次唤起同一实例；搜索键盘、多级跳转优先逐层返回，必要时回到桌面再重启恢复。
* 【修复】首页弹窗遮挡入口时先尝试返回；搜索兜底等待真实输入框。实际确认缺失的应用当天跳过。

下载 Windows x64 ZIP，完整解压后运行 `MiaoTask.exe`，保留 `_internal/`。升级前退出旧助手。安全认证需本人处理，诊断原图分享前请脱敏。

基于 [MonsterNone/tmall-miao next](https://github.com/MonsterNone/tmall-miao/tree/9318d3d138d5fa53cff00601740de012da831c65) 改造。[使用说明](https://github.com/h0ll0w-AkuZr0guY/MiaoTask/blob/main/docs/USAGE.md) · [项目介绍](https://github.com/h0ll0w-AkuZr0guY/MiaoTask#readme)

## v20261020-double11.8

* 兼容「去赚能量 / 赚能量」，同页重新定位后再考虑重启。
* 优化桌面冷启动、截图共享、OCR 缓存与预览刷新。

## v20261019-double11.7

* 分别等待开屏、首页、活动与任务菜单，正常首页路径不下滑，加载中不返回。
* 同时读到「做任务赚能量」标题和任务行后才执行，修复进入活动但未打开任务菜单的问题。

## v20261019-double11.6

* 活动菜单有限补点，搜索官方卡片优先右侧「进入」，避免同名搜索框误点。
* 普通异常限定冷重启恢复；认证停止并弹出 Windows 提醒，保留失败现场。
* 合并保留前版的一键执行、每日记录与诊断功能。

## v20261019-double11.5

此版本的 Release 已撤下，入口问题在后续修复。

* 合并识别与执行，增加自动活动定位、设备日累计和不可用任务缓存。
* 增加首次连接兼容、固定搜索文字及失败现场导出。

## v20261019-double11.4

* 改进搜索框聚焦、搜索提交与内嵌商品结果识别，不安装或切换输入法。
* 增加任务类型选择、前台弹层复核和操作演示。

## v20261018-double11.3

* 从上游 next 改造双11任务流程，提供本地网页、OCR、持续浏览、搜索与跳转核验。
* 支持动态任务和累计领奖，提供 Windows 便携包及公开文件审计。
