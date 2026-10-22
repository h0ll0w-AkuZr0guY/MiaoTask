# 测试、打包与发布

## 工具入口

所有工具随源码一并维护在 GitHub。Windows x64 / Python 3.13 为便携包构建环境；从 Git 克隆仓库并保留干净提交，构建器会记录源码提交号。

| 工具 | 用途 |
| --- | --- |
| `scripts/check_public_files.py` | 审计实际 Git 索引中的公开文件和敏感信息 |
| `scripts/fetch_platform_tools.py` | 获取官方 Android platform-tools |
| `scripts/package_windows.py`、`build.spec` | 独立目录构建 exe、运行自检并生成 ZIP |
| `scripts/smoke_packaged.py` | 对解压后的 exe 检查运行环境、网页和退出 |
| `scripts/prepare_release.py`、`scripts/release_notes.py` | 汇总待发布贡献、校验发布标记、提取已审阅版本正文 |
| `scripts/check_contribution.py` | 检查 PR 标题、正文、贡献保留和版本变更边界 |
| `scripts/export_icons.py` | 从统一母版导出 PNG / ICO |
| `.github/workflows/ci.yml` | PR 审计与回归，不构建 exe |
| `.github/workflows/prepare-release.yml` | 手动汇总贡献并创建发布准备 PR |
| `.github/workflows/release-pipeline.yml` | 发布准备合并到 main 后自动测试、打包，等待确认后发布 |
| `.github/workflows/release.yml`、`publish-release.yml` | 流水线复用的构建与发布；保留手动恢复入口 |

## 验证修改

在开发环境运行：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -q
.\.venv\Scripts\python.exe -m compileall -q main.py backend scripts tests
node --check frontend/js/app.js
node --check frontend/js/api.js
node --check frontend/js/components/activity-selector.js
node --check frontend/js/components/wifi-connect.js
node --check frontend/js/components/device-manager.js
```

Python 用例使用模拟设备，覆盖解析、任务更新、搜索提交、返回核验、重试、累计领奖、实例隔离、日记录、连接与公开文件边界。涉及界面脚本时再执行 Node 语法检查，并在浏览器检查相应交互。

改变任务行为、配置字段或数据格式时，应验证原配置仍可加载、旧记录仍可延续，并补充对应回归。导航、分身或驱动修改宜补充真机复现和停止/恢复检查；不消耗无关任务，也不将未核验的访问计作成功。PR 中写明命令、结果与覆盖限制，要求见[贡献规范](CONTRIBUTING.md)。

提交前明确选择要公开的文件，再运行：

```powershell
.\.venv\Scripts\python.exe scripts/check_public_files.py
git diff --cached --check
git diff --cached --stat
```

审计读取暂存内容；仅修改工作区而未暂存的新文件不会纳入检查。本地已知设备标识可放在仓库外的 JSON 字符串数组，通过 `--forbid-file 文件路径` 额外检查。截图的视觉脱敏仍需人工核对。

## 构建 Windows 便携包

使用独立的锁定环境，避免混入开发环境的临时依赖：

```powershell
python -m venv build/release-env
.\build\release-env\Scripts\python.exe -m pip install -r requirements-windows.lock
.\build\release-env\Scripts\python.exe scripts/fetch_platform_tools.py
# 审阅并提交源码，确认 git status 无待提交修改后构建
.\build\release-env\Scripts\python.exe scripts/package_windows.py --adb-dir build/vendor/platform-tools
```

已有官方 platform-tools 可传入对应目录；必须包含 adb.exe、必要 DLL 和 NOTICE.txt。需要 HDC 时可传 `--hdc-dir`，默认包只包含 Android ADB。从源码 ZIP 开始构建时需先建立 Git 仓库并提交；不需要构建的用户直接下载便携 ZIP。

构建器先审计、运行用例，再从新目录执行 PyInstaller，包含当前入口可达代码、网页、公共配置、离线 OCR、ADB 和第三方许可。不会整体压缩项目目录或携带开发虚拟环境、个人日志、设备配置、旧 exe 和手机截图。

输出为 `dist/<version>-windows-x64.zip`。包内包含：

* `MiaoTask.exe` 和 `_internal/` 运行环境。
* `README.md`、`docs/` 使用、风险、发布与许可说明，以及根目录 `LICENSE`、`NOTICE` 和 `licenses/` 第三方声明。
* `build-manifest.json` 的源码提交与依赖信息，`self-test.json` 的离线自检结果。

## 检查实际 ZIP

完整解压到新目录，不将新 exe 复制到旧运行库中：

```powershell
$releaseVersion = (Get-Content version.json -Raw | ConvertFrom-Json).version
$verifyFolder = "dist/verify-$releaseVersion"
if (Test-Path -LiteralPath $verifyFolder) { throw '请改用新的验证目录' }
Expand-Archive -LiteralPath "dist/$releaseVersion-windows-x64.zip" -DestinationPath $verifyFolder
.\build\release-env\Scripts\python.exe scripts/smoke_packaged.py "$verifyFolder/MiaoTask/MiaoTask.exe"
```

自检会从 PATH 中移除 Python / ADB，检查内置 ADB、离线 OCR、图标、HTTP、浏览器来源头、重复启动和退出，不操作手机。另检查随包文档和构建清单与发布提交一致；升级、托盘和涉及手机的变更按影响范围补充检查。SHA256 为可选完整性附件。

## 重构验证

0.1.0 的原有回归用例保留业务断言，仅调整迁移后的导入和资源路径。`tests/fixtures/v009_contract.json` 来自基线提交的合成页面，比较完整配置及任务解析结果；不含真实设备或账号数据。架构测试覆盖新活动注册、生命周期、活动切换限制、命名空间、旧记录接续和坐标回映接口。

多设备回归覆盖真实工作线程重叠、单台暂停和停止隔离、重复物理设备租约、失效工作区与部分批量失败、全局退出及日志归属。屏幕回归覆盖旋转、横屏 OCR 回映、系统安全区域、非主显示屏输入、逻辑/物理 ID 映射及黑屏登录保护。队列回归覆盖通用失败预算的重启延续、按天和实例隔离、任务替换、实时选择及认证/停止不计普通失败。无线接入回归覆盖地址校验、配对码不落盘、广播解析、受保护的 HTTP 接口、已有 USB 设备时指定无线连接，以及执行期间禁止换设备。实机验证记录保存在本地，手机地址、标识、配对码和原始截图不提交。本地通过不能代替各机型的入口、搜索、跳转、暂停恢复与分身验收；实验分支不发布正式 Release。

## 多设备实测清单

* 先用「唤醒所选应用」确认各设备前台与预览匹配，再将本轮上限设为 1～2 次进行批量启动。记录各线程同时运行的阶段，不能用串行剪辑代替并行证据。
* 未登录模拟器只验证公开页面和入口，登录或保护页出现后停止；不代替用户接受协议、授权或安全验证。
* 已登录设备验证任务点击、完成反馈、返回及进度增加。访问或跳转次数不等于完成数。
* 切换设备确认设置、日志、画面和诊断归属；单台停止、断连或异常时，其他设备应继续运行。
* 实测证据放在忽略的本地目录，PR 只描述设备类别、比例、覆盖路径及结果。附图前脱敏。

## 版本规则

版本与标签使用 `MiaoTask-v0.0.9` 形式，去掉日期和活动限定；两个版本字段保持一致。功能优化递增末位（如 `0.0.9 → 0.0.10`），架构升级或扩展应用递增中位（如 `0.0.x → 0.1.0`）。`0.1.0` 以兼容原功能和配置的架构调整为基线，无线接入、多设备和队列选择先在本地候选包验证；暂不规划 `1.0.0` 定稿版本。历史标签保留。

## 发布

普通 PR 自动运行只读检查，维护者审阅合并后，贡献记录累积在 [UNRELEASED.md](UNRELEASED.md)。不按普通 PR 发版，推送标签也不会自动构建。发布由维护者决定收录范围，云端完成后续构建：

1. **Prepare release for review**：在 Actions 选择 `main`，填写 `MiaoTask-v0.x.y` 和一句摘要。脚本将已合并的待发布条目按类别汇总到 `RELEASES.md` 顶部，清空已收录贡献，同步 `version.json`，并生成 `.github/release-plan.json`。创建独立发布准备 PR，不生成 exe。该 PR 不收录尚未合并的贡献。
2. 审阅准备 PR 的内容和版本。必要时精简重复条目；若 main 又合入贡献，将其一并更新到准备 PR，避免打包代码与说明不一致。准备 PR 仅改以上四个文件，版本不能回退或覆盖历史版本。
3. 审阅合并到 main 后，**Release reviewed version** 自动检查版本确实变化、标记与前后版本一致、说明存在且待发布区为空；随后锁定依赖、审计与回归、获取官方 ADB、构建带环境 ZIP 并执行 exe 自检。失败时停止，不创建 Release。
4. 构建成功后，运行停在 **Review deployments → release**。维护者下载 `MiaoTask-windows-x64` 附件检查后点 **Approve and deploy**。流水线核对构建提交、包内清单、版本、完整性和已完成的构建任务，再发布 ZIP；标签只指向本次提交，已有公开版本不覆盖。

只有 `main` 的版本变化会进入自动发布；普通代码合并、工作流修改、推送标签或单独修改活动不会触发框架发布。发布标记随源码保留供追溯，只有与版本变化配对的新标记才生效。构建、公开发布分离，因此不需要维护者本机安装 Python 或打包环境。

### 仓库首次设置

* **Settings → Actions → General**：允许所用官方 Actions；工作流默认权限使用只读，并勾选 **Allow GitHub Actions to create and approve pull requests**。创建发布准备 PR 的工作流按 job 显式申请必要的写权限，不向普通 PR 提供 secrets。
* **Settings → Rules → Rulesets / Branches**：保护 `main`，要求 PR、通过 `check` 状态、解决评审对话和 CODEOWNERS 评审；禁止强推和删除。合并方式建议 squash。`.github/CODEOWNERS` 指向维护者，CI 通过不代表自动合并。管理员处理自己发起的 PR 时可保留明确的管理员旁路，其他贡献者不能绕过。
* **Settings → Environments → release**：添加维护者为 Required reviewer；仅允许 `main` 部署。维护者既发起准备又确认发布时，关闭 Prevent self-review。公开仓库支持这项审批。没有配置审批时环境本身不会自动提供确认关卡，因此首次发布前必须完成该设置。

仓库本次使用上述保护。`pull_request` 工作流保持 `contents: read` 且 checkout 不保留凭据，不使用 `pull_request_target` 执行贡献者代码。外部贡献者第一次运行可能仍需 GitHub 的工作流批准；机器人创建的准备 PR 也可能显示 **Approve workflows to run**，由维护者批准检查即可，无需个人访问令牌。

### 恢复失败的发布

优先在失败运行中 **Re-run failed jobs**，仍保留相同提交和环境确认。也可在 main 手动运行 **Release reviewed version**，它仍要求已审阅发布标记。已公开的版本会拒绝重复发布，避免意外替换旧附件。

保留的分步入口适合构建与发布之间需要较长验收的情况：先运行 **Build Windows portable release**；验收附件后运行 **Publish verified release assets**，填写版本、成功构建运行 ID、完整 40 位提交 SHA 并勾选确认，公开前仍经过 `release` 环境审批。

工作流需先合并到默认分支，GitHub 才显示手动入口。若准备阶段因创建 PR 权限失败，下载 `release-preparation` 补丁附件，审阅后本地应用并提 PR。参考 [手动运行工作流](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/manually-run-a-workflow)、[工作流触发与机器人 PR](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow)、[环境审批](https://docs.github.com/en/actions/reference/workflows-and-actions/deployments-and-environments)。

本地等效准备命令：

```powershell
.\.venv\Scripts\python.exe scripts/prepare_release.py --version MiaoTask-v0.1.1 --summary "一句本次更新摘要"
# 审阅四个修改文件，以发布准备 PR 提交并合并；然后按前文流程检查附件。
```

版本已存在、回退版本、空待发布区或条目格式错误时脚本拒绝覆盖。公开版本的同版本替换仅在维护者明确授权后单独处理，不由普通工作流自动覆盖附件或移动标签。当前本地候选包不意味着已经完成发布准备或远端验收。

## 发布说明写法

* 开头一句概括主题，随后用「【新增】」「【优化】」「【修复】」列出主要用户可见变化，通常不超过 5 条。
* 结尾保留简短的下载、解压和启动说明，以及致谢、使用文档链接。必要的使用限制客观说明。
* 不写调试过程、测试数量、重复版本命名说明或过时的临时停用提醒；不承诺随机滚动能够规避风控。
* 同版本修复合并进现有小节，GitHub Release 正文始终从该小节提取，避免两处说明不一致。
