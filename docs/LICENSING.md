# 许可与第三方声明

## 项目许可

MiaoTask 采用 [Apache License 2.0](../LICENSE)，项目声明见 [NOTICE](../NOTICE)。第三方代码和工具保留其权利归属，继续遵循各自条款。

感谢 [MonsterNone/tmall-miao](https://github.com/MonsterNone/tmall-miao) 与 [h0ll0w-AkuZr0guY/MiaoTask](https://github.com/h0ll0w-AkuZr0guY/MiaoTask) 原作者与贡献者提供的思路与实现基础。

淘宝日常入口与任务组织思路参考了 [czl0325/coin11-tb](https://github.com/czl0325/coin11-tb)（参考项目附 Apache 2.0 许可）。具体适配使用 MiaoTask 的活动、读屏、搜索、浏览和记录组件实现。

## 随包保留的声明

| 内容 | 许可材料与来源 |
| --- | --- |
| MiaoTask | 包根目录的 `LICENSE`、`NOTICE`，以及 `licenses/MiaoTask/` 副本 |
| Python 及 Python 依赖 | 构建环境提供的许可证、COPYING、LICENCE 和通知文件（含 `uiautomator2`、`adbutils`、`Pillow`、`PyYAML`、`pyobjc-core`、`pyobjc-framework-Vision`、`pyobjc-framework-Quartz` 等），按发行包保存在 `licenses/`；版本见包内 `build-manifest.json` |
| macOS Vision 框架 | 由运行本程序的 macOS 系统原生提供，随包仅通过 PyObjC（MIT 许可）调用系统 API，不分发 Apple 系统库或离线 OCR 模型文件 |
| FlatBuffers 25.12.19（若依赖树包含） | 若构建环境中存在未附独立许可文件的该固定版本 wheel，自动补入[官方固定版本 LICENSE](https://github.com/google/flatbuffers/blob/v25.12.19/LICENSE)及 `docs/licenses/flatbuffers-25.12.19.txt` 到 `licenses/flatbuffers/` |
| Android platform-tools (`adb`) | 保留官方 macOS 发行包的 `NOTICE.txt`；缺失时拒绝构建 |

随包文件用于保留声明，不能据此推断所有依赖具有相同许可证。当前构建器会收集构建环境中的声明，其中也可能包含构建工具的许可；实际程序和环境信息以构建清单为准。

## 后续贡献

新增或替换依赖、图片或上游代码时，在同一 PR 中记录准确版本、官方来源、许可范围和随包声明位置；不能只根据包元数据中的许可简称判断。保留原有权利归属，不将未获授权的第三方内容自动归入项目许可。验证与打包步骤见 [BUILDING](https://github.com/heyunl/MiaoTask-macOS/blob/main/docs/BUILDING.md)。
