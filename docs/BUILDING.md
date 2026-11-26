# 测试、打包与发布

## 两条独立路径

* 用户下载带运行环境的框架 ZIP；活动中心从官方 GitHub 仓库 main 按需获取应用与活动源码。活动更新不要求框架或活动 Release。
* 维护者使用本地 **MiaoTask Release Builder**，提供最新源码 ZIP、目录或 GitHub 链接，生成可交付框架 ZIP。工具在副本汇总贡献、测试和构建，默认自动递增末位版本；不上传、不修改远端或原始源码。

GitHub PR CI 与维护者评审负责合入质量，具体活动的入口、任务和兼容仍应有对应回归。

## 本地 Release Builder

工具实现随源码放在 `scripts/`，本机独立的 `MiaoTask Release Builder/` 是启动与产物目录。在 macOS Apple Silicon 终端运行：

```bash
./.venv/bin/python scripts/release_builder_gui.py --home "Builder目录"
```

1. 选择源码文件夹、ZIP 或 GitHub 链接。源码无需 `.git`，工具使用这份源码自己的业务与构建配置。
2. 默认自动递增末位，例如 `0.1.1 → 0.1.2`。可选递增中位、保留版本或明确填写目标版本；成功记录参与下一次递增，预览和失败不占用版本，原始输入保持不变。
3. 点击加载官方活动，勾选希望预装的项目；不选则生成纯框架包。目录与源码下载锁定同一 GitHub 提交，预装活动位于包根的 `activities/apps/`，仍可在客户端独立更新，不塞入 `_internal/`。
4. 点击开始构建。工具清洁复制源码到 `runs/<编号>/source`，在副本设置目标版本、汇总 `UNRELEASED.md` 到 `RELEASES.md`，清空已收录条目，再运行回归、构建和实际可执行文件自检。窗口持续显示日志。
5. 完成后打开 `outputs/<编号>/`，检查带环境 `macos-arm64.zip`、整理后源码 ZIP、发布正文、更新文档与 `build-result.json`。完整日志和源码副本留在 `runs/`，旧产物保留。

命令行同样可用，具体可选参数以 `--help` 为准：

```bash
./.venv/bin/python scripts/local_release.py "源码ZIP、目录或GitHub链接" --home "Builder目录" --summary "一句更新摘要"
```

默认复用当前 Python 3.12+（`arm64`）环境；可用 `--python` 指定解释器、`--adb-dir` 指定官方 macOS `platform-tools`，或用 `--create-env` 按输入源码的 `requirements.txt` 与 `requirements-build.txt` 创建独立环境。Node.js 用于前端语法检查，Git 命令用于仓库工具的合成回归，输入源码本身无需提交或 GitHub 登录。

默认不回写原始输入。目录输入可显式加 `--write-back-notes`，仅在成功后回写两份更新文档；目标版本与整理后源码保存在构建输出。发布前审阅这些记录，避免下次构建丢失历史。工具只处理已在所选源码中合并的贡献，不自动合并远端 PR。

构建会执行输入源码及其测试，请使用已审阅的可信源码；独立工作目录用于隔离文件，不提供代码沙箱。源码、环境和产物分别管理，不收集开发机已下载的活动、设备信息、旧构建或个人日志。源码来源与验证结果记录在清单，无 Git 提交时不伪造提交号。

## 验证修改

开发环境运行统一预检，GitHub PR CI 使用同一个入口：

```bash
./.venv/bin/python scripts/check_project.py
```

该命令检查当前公开源码、待发布区结构，执行全部回归和 Python / JavaScript 语法检查；不会构建发行包或创建 Release。修改尚未暂存也可检查，运行前按开发说明安装依赖与 Node.js。

用例覆盖解析、搜索、返回、重试、领奖、分身、日记录、USB 连接过滤（拒绝 Wi-Fi / mDNS / 模拟器）、Apple Silicon Vision OCR 坐标转换、多设备和活动安装；涉及界面交互再在浏览器核对。行为变化补回归，不删除或削弱原有断言；真机结果、模拟用例和冻结可执行文件自检分开描述。设备码、地址和原始截图不提交。

前端交互回归使用 Node 内置模块检查弹窗外侧关闭、设备切换后的预览隔离、偏好存储失败回退与活动中心分组，不增加 npm 依赖。浏览器另核对桌面与窄窗口布局、活动勾选与排序、日志切换、弹窗滚动及侧栏跟随；合成工作区只能证明界面交互，不能作为手机任务完成证据。冻结包还需确认新增脚本可通过静态白名单加载。

排行榜回归覆盖跨日、跨实例、跨活动与设备汇总、重复刷新不重复累计、旧迁移副本不重复计入、损坏文件原样保留、匿名键及 HTTP 参数。

连接编辑与主题修改还需覆盖原工作区改名不改执行状态、新建后等待正确工作区再提交 USB 序列号、迟到选择拒绝、连续预览失败提示和成功恢复，以及主题允许值、存储失败和跨标签同步。

HyperOS 元数据回归检查 SDK shell 失效时的有界只读回退、未知状态与未安装区分、坏分身空间不阻断主空间、失效选择不换账号；本地用例通过不能写成已在问题设备真机复现。

淘宝日常回归覆盖独立活动注册、手机/平板/横屏的合成任务布局、奖励圆钮与卡片定位、答题行边界、推广遮挡、UI/OCR 标签变化不误计、余额证据及有界返回。

驱动回归包含真正挂起的本地 socket、每台驱动的独立超时、指定显示屏的 ASCII 输入及实例不符时拒绝输入。

提交前明确选择公开文件，再运行：

```bash
./.venv/bin/python scripts/check_public_files.py
git diff --cached --check
git diff --cached --stat
```

仓库审计读取实际 Git 索引。本地已知标识可用仓库外 JSON 数组配合 `--forbid-file` 检查，图像仍需人工脱敏。Release Builder 对其输入副本进行独立文件审计，不依赖输入含 Git 索引。

根目录 `README.md` 和 `docs/assets/`、`frontend/assets/` 下的图片由维护者直接审阅维护，不做内容、文件名白名单、格式签名或大小校验；可直接更新，不要求经过 PR。源码审计继续检查其他文件，保留路径越界、符号链接、私人目录、凭据与非图片构建产物的限制。

README 与图片的新旧由合并评审按共同祖先、提交历史和差异处理：保留合并当时 main 的较新内容，剔除 PR 中的旧版回退，允许经审阅的较新贡献。具体约定见 [CONTRIBUTING](CONTRIBUTING.md#同步文档)。

## 仓库内直接构建（macOS Apple Silicon）

在 macOS Apple Silicon（`arm64`）开发机从干净 Git 工作区构建时，可使用：

```bash
python3 -m venv build/release-env
./build/release-env/bin/python -m pip install -r requirements.txt -r requirements-build.txt
./build/release-env/bin/python scripts/fetch_platform_tools.py
./build/release-env/bin/python scripts/package_macos.py --adb-dir build/vendor/platform-tools
```

默认直接构建要求干净提交；本地未提交候选可加 `--preview`，清单标记预览与实际文件摘要，输出 `-macos-arm64-preview.zip`。Release Builder 使用独立的 `--local-source` 构建接口，对清洁副本审计并记录文件摘要，输出正常命名的 `MiaoTask-v…-macos-arm64.zip`。

框架内部包含网页、设备与执行组件、共用代码、公共配置、PyObjC Vision 接口、macOS `adb` 及许可声明，不内置具体活动源码；默认按 `bundle_by_default` 在可执行文件旁外置预装，`--framework-only` 生成不预装活动的包。`--include-activities` 仅用于内置模式的离线兼容验收，生成 `-macos-arm64-with-activities.zip`。

包内有 `MiaoTask` 可执行文件、`_internal/`、可选的独立 `activities/` 源码目录、使用与风险文档、许可和第三方声明、`build-manifest.json` 及 `self-test.json`（保留 Unix 可执行权限位）。

## 检查实际 ZIP 与活动获取

完整解压到新目录，保留 `_internal/`，不要混用新旧运行库。执行 `python3 scripts/smoke_packaged.py <MiaoTask可执行文件路径>` 可在 PATH 中移除 Python/ADB 后检查内置 `adb`、macOS 系统 Vision OCR、HTTP、静态资源、重复启动和退出；活动样本也需在真正的冻结可执行文件中安装、导入工厂，避免只验证源码环境。

活动分发应覆盖：从官方 main 读取列表、一次安装固定到同一源码提交、只下载目标应用共享文件和所选活动、内容与路径核验、暂存导入失败保留原活动、全局忙碌时拒绝变更、卸载保留记录、断网仍能使用已安装活动。离线合成归档可供单元测试和打包自检使用，不作为用户需部署的下载服务。

多活动/多设备实测保持少量任务：先唤醒各设备确认预览归属，再限定每类 1～2 次。设备之间并行，同设备活动与实例串行；单台认证、停止或异常不继续该台后续活动，也不干扰其他设备。任务成功仍看返回核验，不以访问或跳转次数代替。未登录设备只验证公开入口，不代办授权。

## GitHub 检查与可选云构建

仓库的普通 PR 自动运行只读 `check`，复用本地预检。公开文件、代码测试、待发布区结构与发布文件保护属于必须通过的检查；标题、正文模板和新增贡献条目等写作要求给出提示，供维护者评审时补齐。只修改 PR 文字不重跑完整检查。main 要求维护者/CODEOWNERS 评审、必需检查和已解决对话；唯一维护者的管理员旁路保留（`enforce_admins=false`）。CI 通过后仍需本人审核，不自动合并。

GitHub 侧保留既有的发布准备、Windows 构建与审批发布工作流，作为维护者可选方式，与客户端源码活动下载无关，也不是本地 Builder 的依赖。只有维护者明确准备版本、满足发布标记并确认后才允许公开，普通 PR、活动更新或推送标签不会直接发布。

`release` 环境仅 main 可部署，需要维护者批准，允许批准本人发起的运行；Actions 默认只读并允许工作流建准备 PR。普通 PR 不持有发布 secrets，不用 `pull_request_target` 执行贡献者代码。外部首次贡献遵循 GitHub 工作流审批；本地尚未上传的显式准备分支 CI 调度优化不视为线上行为。

本地 Builder 不调用这些工作流，不创建分支、PR、标签或 Release，不移动标签、不覆盖附件。首次和后续活动更新都从 main 获取，无需建立额外活动 Release 渠道。官方机制参考 [工作流触发](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow) 与 [环境审批](https://docs.github.com/en/actions/reference/workflows-and-actions/deployments-and-environments)。

## 版本与说明写法

版本使用 `MiaoTask-v0.x.y`，Release Builder 默认递增末位并在构建副本同步两个字段；维护者也可选择递增中位、保留或手动指定。功能优化通常递增末位，架构或应用扩展可递增中位，暂不规划 1.0.0。普通 PR 不夹带版本变更，贡献写在 `UNRELEASED.md`；发布时在构建副本集中汇总，已发布历史不被无关修改覆盖。

说明开头一句主题，随后用「【新增】」「【优化】」「【修复】」等列主要变化，通常不超过 5 条，保留简短下载解压、使用链接和致谢。不写调试流水、测试数量或过时停用提醒，不承诺随机滚动可以规避风控。历史版本仅在明确授权时修改。


GitHub macOS 发布构建运行在 `macos-14`（Apple Silicon `arm64`）Runner 上，使用 `scripts/package_macos.py --preinstall-all`，从本次源码生成并校验全部活动，放入可执行文件旁的 `activities/`，同时保留离线恢复副本。只预装部分活动时改用可重复的 `--preinstall-activity taobao/double11`；两种预装方式不能同时使用，也不能和 `--include-activities` 混用。不传预装参数默认读取 `bundle_by_default`，`--framework-only` 生成纯框架。普通 PR 不构建 Release。

框架候选还需验证：分支切换不会复用另一分支离线缓存、打开活动中心可发现新应用及更新；配置跨安装路径重启保留但不跨工作区改写运行快照；日完成标记跨进程保留并按日/实例隔离；浏览一次预算重启保留，认证不消耗次数；收工重扫不重复计成功；ADB 只清理本次新增同路径进程。

### 静态活动资源与 GitHub Pages

`python3 scripts/build_activity_site.py --output _site/activities` 从干净提交生成静态目录、固定提交索引和全部活动 ZIP；不构建发行包，不创建 Release，不修改源码。

`.github/workflows/activity-pages.yml` 提供活动源码更新后的构建/Pages 部署流程。Pages 首页由 `scripts/build_preview_site.py` 复制当前前端，并仅在部署副本加载 `scripts/site-preview/` 的内存演示适配器。演示不进入桌面入口，不连接后端或手机；更新前端 API 契约时同步维护演示适配与浏览器回归。

## 测试类别与活动预装策略

回归按类别归并至 `tests/test_*.py`（总数保持低于 30），覆盖 USB 连接过滤、Apple Silicon Vision OCR、实例、任务、存储、活动分发、前端及发布隔离用例；相近测试直接合并，不用转发文件维持重复入口。脚本保留独立的检查、构建和站点职责，总数低于 30。

直接 macOS 构建默认读取各活动配置的 `bundle_by_default`；`--preinstall-all` 同样遵循该策略；`--framework-only` 明确生成纯框架，显式活动列表仍可覆盖默认。Release Builder 未提供活动列表时采用源码默认策略；显式空列表保留纯框架选项。

### 主屏截图兼容回归

常规截图优先位于设备适配层，活动调度与多设备工作线程保持既有模型。离线验证须覆盖有效 PNG 不查询映射、普通路径图片流含警告时显式选屏、已知多屏即使默认 PNG 有效也显式选屏、厂商格式缺失/读取失败后主屏 RPC 回退、回退使用新身份、MTK 格式解析、歧义拒绝、停止取消，以及两个设备并行和同设备跨应用连续截图。回退截图保持原尺寸，使用 JPEG 质量 95，不把任意物理 ID 当作主屏。
