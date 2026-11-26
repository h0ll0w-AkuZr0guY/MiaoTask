<div align="center">

<img src="frontend/assets/miaotask-128.png" width="76" alt="MiaoTask 图标">

# MiaoTask (macOS Apple Silicon)

**把重复浏览，交给助手。**

专为 **macOS Apple Silicon（M 系列芯片）** 定制的本地网页控制台，通过 **ADB USB** 管理多台 Android 手机与平板的重复任务。

[![下载](https://img.shields.io/github/v/release/heyunl/MiaoTask-macOS?label=Download&color=f97316&filter=MiaoTask-v*&sort=date)](https://github.com/heyunl/MiaoTask-macOS/releases/latest)
![平台](https://img.shields.io/badge/macOS_Apple_Silicon_(arm64)-Vision_OCR_%2B_ADB_USB-315f56)
[![License](https://img.shields.io/badge/License-Apache_2.0-2563eb)](LICENSE)

[下载使用](#开始使用) · [功能与界面](#功能与界面) · [参与开发](docs/CONTRIBUTING.md) · [发布记录](docs/RELEASES.md)

</div>

![MiaoTask 手绘概览：一个本地控制台，通过 ADB USB 管理独立设备工作区；活动适配执行观察、执行、返回和核验循环](docs/assets/miaotask-overview.png)

连接 USB 设备后，一键进入活动、读取任务、浏览或搜索，再返回核验进度。**淘宝的双11赚能量、淘金币日常、芭芭农场、红包签到和成就中心分别作为独立活动**，按需选择与排序；另支持京东「东东农场」、闲鱼与支付宝「月月赚」浏览任务。可选实物打卡的初始化由本人处理。底层使用 **uiautomator2 + macOS ADB USB**，UI 控件识别优先、**macOS 系统原生 Vision 框架（Apple Silicon 神经网络引擎 / GPU 加速）** 辅助；已移除第三方本地大模型依赖与 Wi-Fi 无线调试控制，截图与 OCR 全部在 Mac 本地处理。

框架 ZIP 提供网页、设备控制和运行环境；**活动中心默认读取官方仓库 main，也可选择其他分支，按需安装活动源码**。活动适配更新合并后即可获取，无需为每次活动修改重新下载框架。

## 定制特性

- [x] **固定 macOS Apple Silicon 系统 OCR**：直接调用 macOS `Vision`（`VNRecognizeTextRequest` 高精度中英文识别），由 M 系列芯片 ANE / Metal 原生加速，无需下载安装 RapidOCR / ONNX / CUDA / DirectML 模型。
- [x] **纯 macOS ADB USB 直连**：仅保留稳定的 Android ADB USB 有线连接，全面移除 Wi-Fi 无线配对/连接、模拟器端口扫描与 HDC 通道。
- [x] **多设备 USB 并行**：一个页面管理多个独立 USB 设备工作区，分别启停，也可批量应用设置；同一物理设备独占租约，同机应用分身串行执行。
- [x] **横竖屏与安全区域适配**：动态刷新屏幕尺寸与挖孔/系统栏安全边距，OCR 坐标自动回映至设备物理像素。
- [x] **可调整的任务队列**：实时选择执行或跳过；同日失败预算、选择与统计按设备、应用、活动、实例、日期独立保留。
- [x] **框架与活动分离**：从官方源码按应用安装、更新或卸载活动，设备上的活动按流水线串行执行。
- [ ] **人工认证与安全边界**：遇到滑块、登录、支付或安全认证自动停止并弹出 macOS 系统提醒，由本人手动完成。

## 开始使用

### 环境要求

* **电脑系统**：搭载 Apple Silicon（M1 / M2 / M3 / M4 等 `arm64` 芯片）的 macOS 13 及以上。
* **手机连接**：Android 手机或平板，开启「开发者选项 → USB 调试」，使用支持数据传输的 USB-C 数据线连接 Mac。

### 下载并启动

![MiaoTask 首页](docs/assets/miaotask-home.png)

1. **便携版启动**：打开 [Releases](https://github.com/heyunl/MiaoTask-macOS/releases/latest)，下载 `MiaoTask-…-macos-arm64.zip`，完整解压后在终端运行 `./MiaoTask`（保留同目录的 `_internal/`）。
2. **源码启动**：克隆仓库后双击 `start.command`（或在终端执行 `./start.command`），脚本会自动检查 Apple Silicon 架构、创建 `.venv`、安装依赖并启动本地控制台（若系统尚未安装 `adb`，可执行 `brew install android-platform-tools`）。
3. 浏览器打开本地控制台，可在 **活动中心** 安装或更新活动，再通过下方步骤连接 USB 设备，勾选本设备的活动并点击 **开始自动执行**。初次可在「任务设置」把本轮上限设为 2 次。活动源码单独保存在程序旁的 `activities/apps/`，可从活动中心直接在访达（Finder）中打开。

### macOS ADB USB 连接

1. 手机保持解锁，在系统设置中开启「开发者选项 → USB 调试」（部分机型需同时开启「USB 调试（安全设置）」以允许模拟点击），通过支持数据传输的 USB 线连接 Mac。
2. 手机弹出「允许 USB 调试吗？」提示时，勾选并点击 **允许**。
3. 在网页控制台点击左侧设备齿轮或 **添加设备**，点击「刷新 USB 列表」，选择已识别的 USB 设备序列号后点击 **连接 USB 设备**。

更多多设备、应用分身和异常处理说明见[使用文档](docs/USAGE.md)。

> 调试授权允许程序操作手机。页面变化与平台规则可能带来误操作、认证或账号风险；随机滚动不保证规避风控。登录、安全认证与真实交易由本人处理。使用前请阅读[风险与隐私](docs/SAFETY.md)。

## 功能与界面

### ✅ 活动按需安装，单台设备顺序执行

活动中心按应用展示所选官方分支的活动，安装后以勾选卡片加入当前设备的流水线，可调整顺序或移除。**勾选只保存计划，点击开始才执行**。同一设备的活动和分身顺序处理，不同设备继续并行。认证或手动停止仍暂停当前设备；恢复流水线时跳过当天已确认完成的活动实例。任务完成仍以页面反馈核验。有说明的活动旁可点「！」查看介绍。

活动安装、更新和卸载需所有设备操作结束。卸载保留日记录；活动包包含可执行 Python 代码，只安装经过审阅的可信活动，校验不提供沙箱隔离。

淘宝日常活动的入口、执行范围和人工处理边界见[活动说明](docs/USAGE.md#淘宝活动)。签到与浏览不包含施肥、浇水、收货地址设置、抽奖或真实交易；页面不符合已识别规则时保留现场并停止。

### ✅ 同页管理，多台 USB 设备并行

点击左侧虚线 **添加设备** 创建工作区，点击设备卡片切换设置、任务、日志和画面；勾选后用底部 **一键启动** 批量发送当前计划。每台 USB 设备独立开始、暂停和停止。同一物理设备的重复连接会被拦截，同一手机内的分身仍按队列串行执行。

![设备工作区：添加设备、切换标签与批量启动；图中为尚未连接的示例工作区](docs/assets/feature-devices.png)

### ✅ 任务由程序识别，也能随时调整

浏览、搜索、跳转及累计奖励按活动规则处理，每次开始和返回都会重读列表。行内 **执行 / 跳过** 调整可执行队列，**不会立即启动任务**；当前步骤先安全结束，下次选择时生效。已完成或必须人工处理的任务不能强制加入，右侧保留实际处理状态。

![任务队列的交互示例：执行与跳过并排，完成及人工任务不可选择；图中为合成任务演示](docs/assets/feature-queue.png)

任务选择、失败次数、已确认缺失的应用与统计按 **设备、应用、活动、实例、日期** 保存在 `~/Library/Application Support/MiaoTask`（便携版）或仓库 `runtime/`（源码运行）。同日重启继续使用，次日重新判断；失败现场可在本地查看和导出，公开前需自行脱敏。

### ✅ 按需设置，保持简单的开始路径

**任务设置 / 滚动节奏 / 高级选项** 分别管理执行范围、滑动参数和识别恢复；OCR 固定为 **macOS Apple Silicon 系统 Vision OCR**，无需配置后端或额外扩展包。搜索使用原生填字与提交兜底，不安装或替换输入法。跨应用返回优先逐层检查，必要时有限重启恢复所选实例。

![滚动节奏设置：距离、时长、额外间隔和同方向连续次数均可调整范围](docs/assets/feature-scoll.png)

## 致谢与对比

感谢 [MonsterNone/tmall-miao](https://github.com/MonsterNone/tmall-miao) 与 [h0ll0w-AkuZr0guY/MiaoTask](https://github.com/h0ll0w-AkuZr0guY/MiaoTask) 原作者与贡献者提供的架构与活动实现基础，以及 [czl0325/coin11-tb](https://github.com/czl0325/coin11-tb) 的入口与任务组织思路参考。

| 模块 | 上游 Windows 版 | 本仓库（macOS Apple Silicon 定制版） |
| --- | --- | --- |
| 运行平台 | Windows x64 | macOS Apple Silicon（M 系列 `arm64` 专属） |
| 本地 OCR | RapidOCR + ONNX Runtime（可选 CUDA / DirectML 扩展） | 固定使用 macOS 系统原生 `Vision` 框架（零额外模型体积） |
| 设备连接 | USB、Android 无线调试配对向导、MuMu 模拟器、预留 HDC | 仅保留 macOS Android ADB USB 有线直连，彻底移除 Wi-Fi 控制 |
| 多设备与分身 | 单页多工作区并行，包名与用户空间锁定 | 保持多台 USB 真机并行、独占租约与同机应用分身串行 |
| 数据与活动 | `%LOCALAPPDATA%/MiaoTask` + `activities/` | `~/Library/Application Support/MiaoTask` + `activities/` |

## 文档

| 文档 | 适合查什么 |
| --- | --- |
| [使用说明](docs/USAGE.md) | macOS 启动、ADB USB 连接、多设备、分身、设置、记录与升级 |
| [公共配置表](docs/CONFIGURATION.md) | 公共设置参数、词表规则与固定 Vision OCR 配置说明 |
| [风险与隐私](docs/SAFETY.md) | 调试权限、平台风险、本地数据与反馈脱敏 |
| [架构设计](docs/ARCHITECTURE.md) | 分层理由、依赖方向、设备隔离、Vision OCR 与存储模型 |
| [公共接口手册](docs/REUSABLE_INTERFACES.md) | 设备、读屏、坐标、会话、任务策略、活动分发与流水线的调用约束 |
| [开发指南](docs/DEVELOPMENT.md) | macOS Apple Silicon 源码环境、活动扩展步骤、配置与调试入口 |
| [贡献规范](docs/CONTRIBUTING.md) · [AGENTS](AGENTS.md) | Issue/PR 格式、AI 协作、共享资源维护与兼容要求 |
| [测试、打包与发布](docs/BUILDING.md) | 预检脚本、`package_macos.py` 打包与本地 Release Builder |
| [待发布贡献](docs/UNRELEASED.md) · [发布记录](docs/RELEASES.md) | 贡献累积与集中版本记录 |

## License

MiaoTask 采用 [Apache License 2.0](LICENSE)。第三方组件与工具遵循各自许可，相关声明见 [NOTICE](NOTICE) 和[许可说明](docs/LICENSING.md)。
