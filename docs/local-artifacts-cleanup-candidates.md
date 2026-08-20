# 本地冗余文件删除清单

本清单只记录测试、安装和参考分析期间产生的本地资源。当前不会自动删除其中任何项目；请确认后再逐项处理。GitHub 提交不包含这些资源，`.gitignore` 已忽略其中的仓库内目录和压缩包。

## 可优先删除

| 路径 | 约占用 | 说明 |
| --- | ---: | --- |
| `smoke-test/` | 488 MiB | 短音频、参考 MIDI/WAV、API 工作目录、服务日志和 `pdf-export-verification-20260820/` 的 MuseScore PDF 验收文件；仅用于验收回归，删除后不影响运行时。 |
| `checkpointTransformer.zip` | 45.6 MiB | Transkun V2 压缩包；已解压并完成模型校验，确认不再需要重新解压后可删除。 |
| `checkpointTransformerAug.zip` | 48.4 MiB | Transkun V2 Aug 压缩包；已解压并完成模型校验，确认不再需要重新解压后可删除。 |
| `models/checkpointMSimpler/` | 51.0 MiB | 旧的 V2 权重副本；当前默认流程使用 `vendor/Transkun/transkun/pretrained/2.0.pt`，可删除前请确认不再需要旧模型。 |
| `models/checkpointMSimplerAug/` | 53.8 MiB | V2 Aug 的重复目录；当前流程使用 `models/transkun-v2-aug/checkpointMSimplerAug/`，可删除前请确认两个文件完全一致。 |
| `nested/` | 0 bytes | 空的临时目录，可删除。 |

## 删除前需要确认用途

| 路径 | 约占用 | 说明 |
| --- | ---: | --- |
| `audio/` | 28.9 MiB | 用户提供的三首测试 MP3；删除后无法复现当前 Step 1 的真实验收。 |
| `sheets/` | 6.3 MiB | 用户提供的 PDF 测试谱；删除后无法复现 PDF/HOMR 验收。 |
| `vendor/music-to-midi/` | 5.6 MiB | 参考项目源码；Step 1 已完成参考一致性分析，但 Step 2 的试听/编辑实现可能仍需查阅。 |
| `vendor/Transkun/` | 54.3 MiB | 当前默认 V2 源码和配套 `2.0.pt`；运行 MP3/视频转录前必须保留，除非迁移到外部 `TRANSKUN_ROOT`。 |

## 不建议删除

| 路径 | 原因 |
| --- | --- |
| `.venv/` | 项目唯一 Python/CUDA 推理环境；删除后需要重新安装依赖和 GPU 运行时。 |
| `models/transkun-v2-aug/` | 当前 V2 Aug 的默认模型目录；界面仍支持 V2 Aug。 |
| `%USERPROFILE%\\.cache\\torch\\hub\\checkpoints\\beat_this-final0.ckpt` | Beat This 默认检查点；删除后音频任务环境检查会失败。 |
| `%USERPROFILE%\\.cache\\music_ai_models\\fluidsynth\\2.5.6/` | FluidSynth 运行时；删除后 MIDI 回渲染和 Step 2 试听无法工作。 |
| `%USERPROFILE%\\.cache\\music_ai_models\\soundfonts\\MuseScore_General.sf2` | 官方钢琴 SoundFont；删除后 FluidSynth 无法生成音频。 |

## 仓库外临时任务

以下目录位于系统临时目录，不影响 Git 工作区：

- `%TEMP%\\sheet2music-yorushika-e2e-20260820\\`：首次失败的端到端任务，约 9.4 MiB，仅有输入 MP3。
- `%TEMP%\\sheet2music-yorushika-e2e-20260820-rerun1\\`：修复后完整验收产物，约 54.2 MiB，包含 `score.mid`、`score.mp3`、`score.wav` 和 `report.json`；建议保留到最终试听确认后再删除。

确认删除时请优先使用回收站或明确路径逐项移除，不要对工作区根目录执行递归删除。
