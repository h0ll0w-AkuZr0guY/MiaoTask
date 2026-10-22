# 许可与第三方声明

## 项目许可

MiaoTask 采用 [Apache License 2.0](../LICENSE)，项目声明见 [NOTICE](../NOTICE)。第三方代码、模型和工具保留其权利归属，继续遵循各自条款。

感谢 [MonsterNone/tmall-miao](https://github.com/MonsterNone/tmall-miao) 原作者与贡献者提供的思路参考。

## 随包保留的声明

| 内容 | 许可材料与来源 |
| --- | --- |
| MiaoTask | 包根目录的 `LICENSE`、`NOTICE`，以及 `licenses/MiaoTask/` 副本 |
| Python 及 Python 依赖 | 构建环境提供的许可证、COPYING、LICENCE 和通知文件，按发行包保存在 `licenses/`；版本见包内 `build-manifest.json` |
| ONNX Runtime | 同时保留包内 `LICENSE` 与 `ThirdPartyNotices.txt`，不只收集 `.dist-info` 文件 |
| RapidOCR 与模型 | 保留发行包的 `LICENSE` 与 `MODEL_LICENSES.md`；模型权利归属与转换说明独立保留，不能只标注推理程序的许可 |
| FlatBuffers 25.12.19 | 该 Python wheel 未带独立许可文件，补入[官方固定版本 LICENSE](https://github.com/google/flatbuffers/blob/v25.12.19/LICENSE)及 [Python 文件中的原始声明](https://github.com/google/flatbuffers/blob/v25.12.19/python/flatbuffers/__init__.py)，来源记录随源码 `docs/licenses/flatbuffers-25.12.19.txt` 维护，随包放入 `licenses/flatbuffers/` |
| Android platform-tools | 保留官方发行包的 `NOTICE.txt`；缺失时拒绝构建 |

随包文件用于保留声明，不能据此推断所有依赖具有相同许可证。当前构建器会收集构建环境中的声明，其中也可能包含构建工具的许可；实际程序和环境信息以构建清单为准。

## 后续贡献

新增或替换依赖、模型、图片或上游代码时，在同一 PR 中记录准确版本、官方来源、许可范围和随包声明位置；不能只根据包元数据中的许可简称判断。FlatBuffers 补充材料仅对应已核查的固定版本，升级时重新核对。保留原有权利归属，不将未获授权的第三方内容自动归入项目许可。验证与打包步骤见 [BUILDING](https://github.com/h0ll0w-AkuZr0guY/MiaoTask/blob/main/docs/BUILDING.md)。
