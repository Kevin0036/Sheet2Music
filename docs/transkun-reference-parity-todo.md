# Transkun Step 1 参考项目等价性待办

## 目标

让 Sheet2Music 的钢琴专用 Transkun 路径在相同模型与规范化音频输入下，
输出与 `vendor/music-to-midi` 的钢琴 Transkun 路径事件级等价的 MIDI；随后以
`audio/` 中三首曲目完成实际试听验收。该阶段不实现网页 MIDI 编辑器（Step 2），
不实现 PDF 与 MP3 联合校音（Step 3），也不做自动量化；变速曲仅把 Beat This 的
tempo map 写入 MIDI conductor metadata，绝不改变 Transkun 事件的实际秒数。

## 已验证事实

- 参考项目默认模型是 `vendor/Transkun/transkun/pretrained/2.0.pt` 与
  `2.0.conf`，并非当前 `models/transkun-v2/checkpointMSimpler/` 的模型。
- 相同 V2 Aug 权重、配置、GPU 与输入下，参考 Transkun worker 和当前
  `python -m transkun.transcribe` 的 MIDI 音符事件完全一致；参考项目没有额外
  的 Transkun 自动音符纠错。
- 相同 V2 Aug 下，原 MP3 与 `44.1 kHz / 双声道 / PCM16 WAV` 输入会产生不同
  的事件，因此两条模型路径必须共用同一规范化 WAV。
- 参考项目用 Beat This `final0` 清洗节拍网格、估算全局 BPM、在有把握时推断
  拍号，并将这些元数据写回 MIDI；不会量化、移动、增删 Transkun 音符。
- 当前本机 `final0.ckpt` SHA-256 为
  `8c328b45f59d8dd3dff219253ff6a8d6482be57d0133a29140e2febbf8eb8331`。
- `beat_this.File2Beats` 在当前单一 `.venv` 中因 `torchaudio` 的
  `torchcodec` 读取依赖不可用而不能作为运行时入口。实现必须保留对已规范化
  PCM WAV 的直接读取，不新增第二个虚拟环境或无关解码依赖。

## 实现待办

- [x] 新增一个钢琴音频规范化入口，始终使用 ffmpeg 生成 `44.1 kHz`、双声道、
  `PCM s16` WAV；Beat This 和 Transkun 都只读取该临时 WAV。
- [x] 将默认 `v2` 模型重定义为“Transkun V2（参考兼容）”，固定使用已校验的
  `2.0.pt + 2.0.conf`；保留 `v2_aug` 作为第二个选项。旧
  `checkpointMSimpler` 不再出现在常规界面或作为默认回退。
- [x] 为两套可选模型建立文件身份校验（路径、大小、SHA-256），并在 Transkun
  运行前校验；将生产加载改为 `strict=True`，模型不匹配时明确失败。
- [x] 将 Beat This 从短名称缓存加载改为显式 `final0.ckpt` 路径与 SHA-256 校验，
  继续在独立 PyTorch/CUDA worker 中用 RTX 4060 推理。
- [x] 移植参考项目的 Beat This 网格分析：最少节拍数校验、竞争节拍清理、孤立
  漏拍恢复、全局 BPM 最小二乘拟合与置信度足够时的拍号推断。
- [x] 扩展 `beats.json` 和 `report.json`，记录原始/清洗后的 beats、downbeats、
  BPM、拍号、清理统计和模型身份，供问题追溯。
- [x] 将原始 Transkun 产物保存为 `output/score.raw.mid`；生成面向用户的
  `output/score.mid` 时，写入 Beat This tempo map 与可用拍号，保持所有非 metadata
  事件的绝对秒不变。没有自动量化、音符增删或节拍位置移动。
- [x] 使用规范化后的 `score.mid` 回渲染 `score.mp3`；原始 MIDI 只留作审计，
  不列入下载产物。
- [x] 更新系统状态和模型选择文字，使其准确显示参考兼容 V2、V2 Aug、Beat This
  `final0` 身份校验与 CUDA 可用性。

## 自动化测试待办

- [x] 为 WAV 转换命令增加双声道 PCM16 断言，并验证 Beat This 与 Transkun 收到
  同一路径。
- [x] 覆盖模型身份、未知模型、哈希不匹配和严格加载失败的明确错误。
- [x] 覆盖 Beat This 网格清理、漏拍恢复、全局 BPM 拟合、拍号置信度、可变速度图
  与拒绝无效网格。
- [x] 覆盖 MIDI 元数据重写：音符、控制器和其他非 tempo 事件绝对秒不变；BPM 与
  拍号正确写入；`score.raw.mid` 不被改写。
- [x] 更新音频任务、报告、系统状态和 PDF 回归测试；mock ffmpeg、Beat This、
  Transkun、MuseScore，不依赖真实模型或 GPU。

## 真实验收待办

- [x] 对同一短片段，以“参考兼容 V2”和 V2 Aug 分别运行 Sheet2Music 与参考项目；
  比较 pitch、start、end、velocity 四元组，要求完全一致。
- [x] 对 `audio/` 的三首曲目运行参考兼容 V2，检查 MIDI 可读取、MP3 可下载、
  BPM/拍号符合 Beat This 审计结果，并完成试听记录；按范围约定，V2 Aug 仅完成一首完整曲目验收。
- [x] 确认推理使用项目唯一 `.venv` 与 RTX 4060，且 `pip check` 通过。

## Step 1 完成与文档待办

- [x] 在 `docs/transkun-v2-audio-transcription-design.md` 的里程碑记录中加入本次
  参考等价实现、自动化测试和三首曲目验收结果。
- [x] **仅在以上实现和真实验收均完成后**，更新 `README.md`：说明 PDF、MP3 与
  视频 URL 三种输入；列出参考兼容 V2/V2 Aug；记录统一 WAV、Beat This BPM/拍号
  写入、不自动量化、单 `.venv`/RTX 4060 要求及所需环境变量。
- [x] README 更新后重跑完整测试、静态检查与系统状态检查，再将 Step 1 标记完成。

## 完成标准

Step 1 只有在相同模型和规范化 WAV 下与参考项目音符事件完全一致、三首本地曲目
完成试听与下载验收、里程碑文档已更新且 README 已更新后，才可标记为完成。

## 2026-08-20：运输时长修正

- [x] 复现并修复 FluidSynth `WAV 过短` 的错误：MIDI 时长计算使用有效 tempo map，
  只以真实 channel event 为边界，忽略延迟的 `end_of_track`。
- [x] 音频/视频回渲染按输入 WAV transport 时长裁剪或补零，避免 CC64 自然释放把
  4:04 的曲目导出为 4:19。
- [x] 在 Yorushika 实曲上验证：修正 MIDI `242.497s`，下载 WAV `244.035918s`，
  Beat This tempo map 有 `197` 个点，MIDI 非 metadata 事件秒数保持不变。
- [x] 使用同一完整曲目完成 MP3 导出验收：源音频 `244.036s`，下载 MP3 `244.04s`；
  临时输出命名固定为 `.score.part.mp3`，确保 ffmpeg 通过扩展名识别 MP3 容器。
