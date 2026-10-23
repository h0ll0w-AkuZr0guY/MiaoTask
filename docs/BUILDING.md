# 测试、打包与发布

## 两条独立路径

* 用户下载带运行环境的框架 ZIP；活动中心从官方 GitHub 仓库 main 按需获取应用与活动源码。活动更新不要求框架或活动 Release。
* 维护者使用本地 **MiaoTask Release Builder**，提供最新源码 ZIP、目录或 GitHub 链接，生成可交付框架 ZIP。工具在副本汇总贡献、测试和构建，默认自动递增末位版本；不上传、不修改远端或原始源码。

GitHub PR CI 与维护者评审负责合入质量，具体活动的入口、任务和兼容仍应有对应回归。

## 本地 Release Builder

工具实现随源码放在 `scripts/`，本机独立的 `MiaoTask Release Builder/` 是启动与产物目录。双击 `Build.cmd` 打开窗口，或运行：

```powershell
.\.venv\Scripts\python.exe scripts/release_builder_gui.py --home "Builder目录"
```

1. 选择源码文件夹、ZIP 或 GitHub 链接。源码无需 `.git`，工具使用这份源码自己的业务与构建配置。
2. 默认自动递增末位，例如 `0.1.1 → 0.1.2`。可选递增中位、保留版本或明确填写目标版本；成功记录参与下一次递增，预览和失败不占用版本，原始输入保持不变。
3. 点击加载官方活动，勾选希望预装的项目；不选则生成纯框架包。目录与源码下载锁定同一 GitHub 提交，预装活动位于包根的 `activities/apps/`，仍可在客户端独立更新，不塞入 `_internal/`。
4. 点击开始构建。工具清洁复制源码到 `runs/<编号>/source`，在副本设置目标版本、汇总 `UNRELEASED.md` 到 `RELEASES.md`，清空已收录条目，再运行回归、构建和实际 exe 自检。窗口持续显示日志。
5. 完成后打开 `outputs/<编号>/`，检查带环境 ZIP、整理后源码 ZIP、发布正文、更新文档与 `build-result.json`。完整日志和源码副本留在 `runs/`，旧产物保留。

命令行同样可用，具体可选参数以 `--help` 为准：

```powershell
.\.venv\Scripts\python.exe scripts/local_release.py "源码ZIP、目录或GitHub链接" --home "Builder目录" --summary "一句更新摘要"
```

默认复用配置的 Python 3.13 环境；可用 `--python` 指定解释器、`--adb-dir` 指定官方 platform-tools，或用 `--create-env` 按输入源码的 `requirements-windows.lock` 创建独立环境。Node.js 用于前端语法检查，Git 命令用于仓库工具的合成回归，输入源码本身无需提交或 GitHub 登录。

默认不回写原始输入。目录输入可显式加 `--write-back-notes`，仅在成功后回写两份更新文档；目标版本与整理后源码保存在构建输出。发布前审阅这些记录，避免下次构建丢失历史。工具只处理已在所选源码中合并的贡献，不自动合并远端 PR。

构建会执行输入源码及其测试，请使用已审阅的可信源码；独立工作目录用于隔离文件，不提供代码沙箱。源码、环境和产物分别管理，不收集开发机已下载的活动、设备信息、旧构建或个人日志。源码来源与验证结果记录在清单，无 Git 提交时不伪造提交号。

## 验证修改

开发环境运行统一预检，GitHub PR CI 使用同一个入口：

```powershell
.\.venv\Scripts\python.exe scripts/check_project.py
```

该命令检查当前公开源码、待发布区结构，执行全部回归和 Python / JavaScript 语法检查；不会构建 exe 或创建 Release。修改尚未暂存也可检查，运行前按开发说明安装依赖与 Node.js。

用例覆盖解析、搜索、返回、重试、领奖、分身、日记录、连接、坐标、多设备和活动安装；涉及界面交互再在浏览器核对。行为变化补回归，不删除或削弱原有断言；真机结果、模拟用例和冻结 exe 自检分开描述。设备码、地址、配对码和原始截图不提交。

HyperOS 元数据回归检查 SDK shell 失效时的有界只读回退、未知状态与未安装区分、坏分身空间不阻断主空间、失效选择不换账号；本地用例通过不能写成已在问题设备真机复现。

提交前明确选择公开文件，再运行：

```powershell
.\.venv\Scripts\python.exe scripts/check_public_files.py
git diff --cached --check
git diff --cached --stat
```

仓库审计读取实际 Git 索引。本地已知标识可用仓库外 JSON 数组配合 `--forbid-file` 检查，图像仍需人工脱敏。Release Builder 对其输入副本进行独立文件审计，不依赖输入含 Git 索引。

## 仓库内直接构建

已有开发环境且从 Git 工作区构建时，可继续使用底层工具：

```powershell
python -m venv build/release-env
.\build\release-env\Scripts\python.exe -m pip install -r requirements-windows.lock
.\build\release-env\Scripts\python.exe scripts/fetch_platform_tools.py
.\build\release-env\Scripts\python.exe scripts/package_windows.py --adb-dir build/vendor/platform-tools
```

默认直接构建要求干净提交；本地未提交候选可加 `--preview`，清单标记预览与实际文件摘要，输出 `-preview.zip`。Release Builder 使用独立的 `--local-source` 构建接口，对清洁副本审计并记录文件摘要，不要求 Git、提交或发布计划，也不伪造提交号；输出正常命名的带环境 ZIP。

默认框架包含网页、设备与执行组件、共用代码、公共配置、离线 OCR、ADB 及许可声明，排除具体应用与活动源码。`--include-activities` 仅用于离线兼容验收，生成 `-with-activities.zip`。不会整体压缩项目或塞入虚拟环境、旧 exe、个人日志和设备截图。HDC 可通过 `--hdc-dir` 另行提供，默认只带 Android ADB。

工具也支持已核验构建入口的 0.1.0 源码：仅适配无 Git 来源检查，仍使用该源码自身的业务、构建配置和自检，生成它原有的完整包。不会为旧源码注入新框架或在线活动能力；未知旧入口需使用对应构建器。

包内有 `MiaoTask.exe`、`_internal/`、可选的独立 `activities/` 源码目录、使用与风险文档、许可和第三方声明、`build-manifest.json` 及 `self-test.json`。`bundle-policy.json` 记录框架/完整包模式。

## 检查实际 ZIP 与活动获取

完整解压到新目录，保留 `_internal/`，不要混用新旧运行库。执行 `scripts/smoke_packaged.py <exe路径>` 可在 PATH 中移除 Python/ADB 后检查内置工具、OCR、HTTP、图标、重复启动和退出；活动样本也需在真正的冻结 exe 中安装、导入工厂，避免只验证源码环境。

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
