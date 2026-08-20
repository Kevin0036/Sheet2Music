# Transkun V2 音频转 MIDI（Step 1）

## 目标

在现有 Sheet2Music 本地工具中新增 MP3 转 MIDI 工作流，同时保留 PDF → HOMR 流程。
本阶段不实现在线 MIDI 编辑，也不实现 MP3 与 PDF 的音准比对。

## Step 1.5：视频 URL 输入

输入区还接受 YouTube 与 Bilibili 的 `http(s)` 视频 URL。后端使用本机
`yt-dlp` 仅提取音频为任务工作区的 `input/score.mp3`，随后完全复用音频
转录流程；不下载或保留视频文件。URL 任务持久化为 `input_kind=video_url`，
并依次显示：

`video_url_uploaded` → `downloading_video_audio` → `converting_audio` →
`detecting_beats` → `running_transkun` → `rendering_mp3` → `completed` / `failed`

非 YouTube/Bilibili URL 在上传时拒绝。平台的登录、地区、版权、限流或网络
错误由 yt-dlp 返回并使任务明确失败，不回退到其他下载器或转录模型。

## 工作流

音频任务按以下顺序执行：

1. 保存 MP3 到任务工作区。
2. 使用 ffmpeg 转换为 44.1 kHz、双声道、PCM s16 WAV；Beat This 和 Transkun 始终读取同一个规范化文件。
3. 使用 Beat This 检测 beats、downbeats 和估算 BPM，并保存 `audio/beats.json`。
4. 调用 Transkun V2 生成 `output/score.mid`。
5. 保留原始 Transkun MIDI 为 `score.raw.mid`。面向用户的 `score.mid` 只写入 Beat This 得到的全局 BPM 和高置信度拍号，保持其他 MIDI 事件的绝对播放秒不变；不量化、不移动、不增删音符，也不写可变速度图。
6. 使用 MuseScore 将 MIDI 渲染为 WAV，再使用 ffmpeg 输出 `output/score.mp3`。
7. 写入 `output/report.json`，清理临时 WAV。

Transkun MIDI 只有在文件不可读取、没有音符、ticks_per_beat 无效或音符结束位置不合法时才会失败；正常节奏不会被修改。

## 输入与 API

- `POST /api/preview`：继续只接受 PDF，并执行首页预览。
- `POST /api/audio`：接受单个 `.mp3`，限制 50 MB，创建 `input_kind=audio` 的任务。
- `POST /api/video-url`：接受 YouTube/Bilibili URL，创建 `input_kind=video_url` 的任务。
- `POST /api/convert`：PDF 使用原有 BPM、拍号和输出选项；音频忽略 BPM/拍号输入，固定生成 MIDI 与回渲染 MP3。
- `GET /api/jobs/{id}` 与 artifacts 下载接口保持不变。

任务状态增加音频阶段：

`audio_uploaded` → `converting_audio` → `detecting_beats` → `running_transkun` → `rendering_mp3` → `completed` / `failed`

## 运行配置

Transkun 默认读取仓库工作区的本机模型目录，大模型不提交到 Git；环境变量可覆盖默认位置：

- `TRANSKUN_ROOT`：可选；默认使用 `vendor/Transkun`。
- `TRANSKUN_PYTHON`：运行 Transkun 的 Python；未设置时使用当前解释器。
- `TRANSKUN_V2_WEIGHT` / `TRANSKUN_V2_CONF`：可选；覆盖原始 V2 的 `checkpoint.pt` / `model.conf`。
- `TRANSKUN_V2_AUG_WEIGHT` / `TRANSKUN_V2_AUG_CONF`：可选；覆盖 V2 Aug 的模型文件。

实际调用格式为 `python -m transkun.transcribe <input.mp3> <output.mid> --weight <checkpoint.pt> --conf <model.conf> --device <cpu|cuda>`。Beat This 的 `beats.json` 是独立节奏审计结果，不作为 Transkun CLI 参数，也不修改 Transkun 输出 MIDI。

Beat This 必须安装在可被当前 Python 或 Transkun 环境访问的环境中。缺少 Beat This、Transkun 源码、模型或外部工具时，任务会在对应阶段失败，不回退到 HOMR。
视频 URL 输入还要求 `yt-dlp` 位于 PATH 中（项目依赖包含 `yt-dlp`）。

## 工作区与产物

```text
<job>/
  input/score.mp3
  audio/score.wav       # 临时文件，完成后删除
  audio/beats.json      # 节奏识别审计结果
  output/score.raw.mid  # 原始 Transkun 结果，仅审计
  output/score.mid      # 写入 BPM/可用拍号的下载 MIDI
  output/score.mp3
  output/report.json
```

原始上传 MP3 保留在任务工作区，但最终 MP3 产物是由 Transkun MIDI 回渲染得到的版本。

## 验收与测试

- 单元测试覆盖 ffmpeg WAV 命令、Beat This JSON、Transkun 命令构造和 MIDI 非修改式校验。
- API 测试覆盖 MP3 上传、非 MP3 拒绝、输入类型持久化和旧 PDF 校验。
- 集成测试通过 mock Beat This、Transkun、MuseScore 和 ffmpeg 运行，不依赖真实 GPU/模型。
- 真实验收时设置上述环境变量，使用短 MP3 验证 `score.mid` 可读取且 `score.mp3` 可下载。

## 后续步骤

- Step 2：接入 MIDI 在线试听与编辑器。
- Step 3：MP3 与 PDF 同时上传时，以 Transkun MIDI 为基底，用 HOMR 音高结果生成可解释的音准修正提示。

## 里程碑记录

每完成一个可独立验收的阶段，必须同步更新本节，记录完成内容、验证结果和仍需人工处理的外部资源。

### 2026-08-19：Step 1.5 与推理环境

- Step 1.5 已加入 YouTube/Bilibili URL 输入、yt-dlp 音频提取和 `video_url` 任务分派。
- Beat This 1.1.0 已安装到项目 `.venv`，`beat_this.inference.Audio2Beats` 可导入。默认 `final0` 检查点已下载到 Torch 缓存并成功反序列化（81,058,141 bytes）。
- 本地 `vendor/Transkun` 已以 editable 方式安装为 Transkun 2.0.1；官方 CLI `python -m transkun.transcribe -h` 可运行。
- setuptools 固定为 80.9.0，因为 Transkun 2.0.1 仍依赖 `pkg_resources`。
- Transkun 使用最小推理依赖安装。配置解析、模型构建和默认检查点 `strict=True` 加载已通过（13,613,711 个参数）。完整训练/评估依赖中的 `ncls` 在 Windows/Python 3.11 需要 Microsoft Visual C++ 14+，当前未安装；该缺项不在转录入口的执行路径上。
- 仓库自带 `transkun/pretrained/2.0.pt` 已通过 SHA256 `50a80010effc2a59ffcd068a95cd2b29bd7f23a27a3515bc3ccd209c89a3d44c` 校验。它是官方说明中的默认 No Ext 包模型，不作为产品所需的原始 Transkun V2 模型。
- 产品的两个独立检查点已解压并接入：`Transkun V2`（默认选择）与 `Transkun V2 Aug`。任务持久化 `v2` / `v2_aug`，不得自动回退。
- 原始 V2 权重：Google Drive 文件 ID `1pxGpO8eCdFxMRrXi_YUh7_uC0Ae26coB`。
- V2 Aug 权重：Google Drive 文件 ID `1Hg5ua8vYdtg1Y-MnXD0mLyhRK9Srd7hm`。
- 原始 V2 `checkpoint.pt`：53,406,550 bytes，SHA256 `76469be487911e110e0e661fadb583645e89c35364faa1ef8897d5600ba6ce20`，严格加载成功（12,859,535 参数）。
- V2 Aug `checkpoint.pt`：56,423,254 bytes，SHA256 `8bd6b4b5ddf9ce8c5f296a57859eec9f166cd337c35245ec2a2576d90be68c4c`，严格加载成功（13,613,711 参数）。
- 系统状态已能同时报告两套模型；前端音频任务可选择 V2 或 V2 Aug，默认 V2。
- 尚待验收：使用真实短 MP3 分别完成两套模型的 MIDI 转录与 MP3 回渲染。

### 2026-08-19：RTX 4060 CUDA 验收

- Beat This 与 Transkun 继续复用项目现有 `.venv`，未创建第二个虚拟环境。
- `.venv` 已验证为 CUDA 版 PyTorch：`torch 2.11.0+cu128`、CUDA runtime `12.8`，设备为 `NVIDIA GeForce RTX 4060 Laptop GPU`（8,188 MiB 显存）。
- Beat This `final0` 已在 CUDA 上加载并完成 20 秒音频节拍识别；结果为 37 beats、估计 BPM `118.03278688524591`。
- Transkun V2 与 V2 Aug 均已在 CUDA 上完成 20 秒音频推理、MIDI 有效性检查及 MuseScore/ffmpeg MP3 回渲染：分别生成 299 与 272 个音符，均为 960 ticks/beat。
- 系统状态接口新增 `pytorch_cuda`，独立显示音频模型的 PyTorch/CUDA/设备状态；前端对 PDF 使用 HOMR ONNX GPU 状态，对 MP3/视频音频使用 PyTorch CUDA 状态。
- `audio/` 中已放入三首真实测试曲目，时长约 244 秒、297 秒和 331 秒；短片段验收已通过，完整三曲后台任务仍待新服务进程验收。
- 当前已知非阻塞告警：Transkun 的 `pkg_resources` 与 Torch checkpoint `use_reentrant` 弃用警告；不会阻止推理。

### 2026-08-19：环境基线收敛

- 运行环境收敛为一套项目 `.venv`：Python 3.11、`torch 2.11.0+cu128`、`torchaudio 2.11.0+cu128`、`beat-this 1.1.0`、`onnxruntime-gpu 1.28.0`。`pip check` 已通过。
- `vendor/Transkun` 保持唯一源码副本，不再以 editable package 方式安装到 `.venv`。运行命令通过 `cwd` 和 `PYTHONPATH` 从该目录加载模块，因此不会把上游训练/评估依赖（`ncls`、`seaborn`、`tensorboard`、`torch-optimizer`、`sox`）误当作产品推理依赖。
- Windows 上 HOMR 的 ONNX Runtime CUDA 13.x 与音频模型的 PyTorch CUDA 12.8 不在同一进程混载：音频 CUDA 自检与 Beat This 推理使用独立 worker，Transkun 继续使用独立 CLI 子进程。系统状态分别显示 HOMR GPU 与 `pytorch_cuda`。
- 外部工具基线为一套 MuseScore、一套 Poppler，及同目录的一对 `ffmpeg.exe`/`ffprobe.exe`。新增 `SHEET2MUSIC_FFMPEG` 覆盖项；Transkun 子进程把该目录前置到 PATH，使 pydub 解析 MP3 时不会单独找到其他 ffprobe。
- 本轮验收中创建的 `smoke-test/ffmpeg.exe` 已删除，未作为正式工具副本保留。完整 MP3 后台验收应在用户本机账户下，以正式 WinGet ffmpeg 目录执行；Codex 沙箱账户对该目录存在 ACL 限制，不能代表用户账户的工具可执行性。
- 基线回归验证完成：`python -m unittest discover -s tests -v` 为 194 通过、2 个既有外部样例/工具测试跳过；`node --check sheet2music/web/static/app.js`、`python -m compileall -q sheet2music`、`pip check` 也均通过。Transkun 在不安装 editable package 的前提下，以 `vendor/Transkun` 源码直载模式完成 CLI 帮助命令验证。

### 2026-08-20：参考项目等价实现与 Step 1 验收

- 音频路径已对齐 `vendor/music-to-midi` 的钢琴专用 Transkun 工作流：`ffmpeg` 生成同一份 `44.1 kHz / 双声道 / PCM s16` WAV，Beat This 与 Transkun 均读取该文件。
- 默认模型为“Transkun V2（参考兼容）”，固定对应 `vendor/Transkun/transkun/pretrained/2.0.pt + 2.0.conf`；V2 Aug 为可选模型。两组权重及配置在运行前按路径、大小和 SHA-256 验证，Transkun 严格加载。
- Beat This 固定使用经校验的 `final0.ckpt`，仅完成节拍网格清理、全局 BPM 拟合与高置信度拍号推断。它不参与 Transkun 解码，且不量化或改变音符事件。
- 对同一段规范化 WAV，Sheet2Music 与 `music-to-midi` 的参考 worker 已分别完成 V2 与 V2 Aug 逐事件比对：V2 为 674 个音符、V2 Aug 为 710 个音符；两者的 `(pitch, start, end, velocity)` 四元组均完全一致。
- RTX 4060 与唯一项目 `.venv` 已完成真实 CUDA 推理验收。`audio/` 三首本地 MP3 已使用默认 V2 端到端生成可读取 MIDI、回渲染 MP3 和审计报告；V2 Aug 按项目范围只完成一首完整曲目验收（2,928 个音符，检测 BPM 127.370，未达到拍号推断置信度，因此未写入拍号）。
- 本机随 `2.0.pt` 提供的 `2.0.conf` 为 819 bytes、SHA-256 `edc237514eb7881f0f96b5769b20225c056c5c4e52f3804d77d8f6e39ebdbb33`，与参考仓库中旧的 782-byte 哈希记录不同，但 JSON 语义相同，且上述真实逐事件等价验证以该物理配套文件完成。

### 2026-08-20：统一 FluidSynth 回渲染管线

- PDF、MP3 和视频 URL 的 MIDI 音频输出统一改为 `FluidSynth 2.5.6 + MuseScore_General.sf2 -> 44.1 kHz PCM16 WAV -> ffmpeg MP3`；MuseScore 仍只负责 PDF 的 MusicXML -> MIDI 导出。
- 新增 `sheet2music.core.fluidsynth_renderer` 作为独立渲染接口。`render_midi_to_wav()` 可直接被后续 GUI 在线试听/编辑任务调用，不依赖 Web JobStore；`render_midi_to_mp3()` 负责下载产物。
- FluidSynth runtime、SoundFont 版本/大小/SHA-256 均在环境状态中校验。渲染器不重写 MIDI，因此 `CC64` 延音踏板和其他控制器会原样传给 FluidSynth；同时检查 44.1 kHz、双声道、PCM16 和末尾尾音覆盖。
- 本机已下载并验证 FluidSynth 2.5.6 和官方 SoundFont。真实 FluidSynth WAV 渲染已成功；当前 Codex 沙箱账户执行 WinGet ffmpeg 时受 Windows ACL 拒绝，MP3 转码须由用户账户运行或通过 `SHEET2MUSIC_FFMPEG` 指向可执行的同目录 `ffmpeg.exe`。
