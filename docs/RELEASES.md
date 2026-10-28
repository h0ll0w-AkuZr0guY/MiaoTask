# 发布记录

按新到旧维护用户可见变化。GitHub Release 正文从对应的 `## 版本号` 小节提取，发布状态以 [Releases](https://github.com/h0ll0w-AkuZr0guY/MiaoTask/releases) 为准。

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
