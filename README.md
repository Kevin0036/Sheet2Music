# Sheet2Music

Sheet2Music 是一个本地浏览器工具，用于把钢琴音频或 PDF 琴谱转换为 MIDI，并按需生成 MP3。
项目优先推荐 MP3 输入：音频包含真实演奏节奏，Transkun + Beat This 通常比从静态谱面推断节奏更稳定。PDF 琴谱保留为没有音频时的备用方案。

## 概览

### 音频路线（推荐）

```text
MP3 / YouTube / Bilibili
  -> ffmpeg 规范化 WAV
  -> Beat This final0 节拍与速度分析
  -> Transkun V2 或 V2 Aug 钢琴转录
  -> MIDI 非修改式校验与 tempo map 写入
  -> FluidSynth 钢琴 WAV
  -> ffmpeg MP3
```

Beat This 只负责节拍网格、BPM 和高置信度拍号元数据，不量化、移动、增删 Transkun 音符。原始 MIDI 会保留为 `score.raw.mid` 供审计，下载的 `score.mid` 保持非 metadata 事件的绝对播放秒不变。音频任务以输入 WAV 的 transport 时长作为边界，避免延音踏板或混响尾部把歌曲错误拉长。

### PDF 路线（备用）

```text
PDF
  -> 600 DPI 页面图与谱表裁剪
  -> HOMR MusicXML 识别
  -> 结构检查与必要的浏览器审批
  -> MuseScore 导出 MIDI
  -> FluidSynth 钢琴 WAV
  -> ffmpeg MP3
```

PDF 路线继续使用现有的拍号、谱号和时值安全检查。高风险结构不会静默修改，会先进入人工审查。

项目当前只支持单次提交一个 PDF、MP3 或视频 URL。在线 MIDI 试听/编辑和 PDF+MP3 联合音准核查属于后续 Step 2、Step 3。

## 部署指南

### 1. 获取代码

```powershell
git clone https://github.com/Kevin0036/Sheet2Music.git
cd Sheet2Music
py -3 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
```

Windows PowerShell 若禁止激活脚本，可以直接使用 `.venv\Scripts\python.exe` 执行后续命令，不需要创建第二个虚拟环境。

### 2. 安装运行时工具

所有 Python 依赖、Beat This 和 Transkun 推理都使用项目唯一的 `.venv`。先按 PyTorch 官方安装选择器安装与 NVIDIA 驱动匹配的 CUDA 版 `torch`/`torchaudio`，再安装本项目及音频推理依赖：

按 [PyTorch 官方安装选择器](https://pytorch.org/get-started/locally/) 为你的 NVIDIA 驱动选择 CUDA 版本并安装 `torch`、`torchaudio`，然后在同一个环境安装项目：

```powershell
# 先执行 PyTorch 官方页面为你的系统生成的 CUDA 安装命令
.venv\Scripts\python.exe -m pip install -e ".[audio]"
.venv\Scripts\python.exe -m pip check
```

`audio` 依赖组会在同一个 `.venv` 安装 Beat This 以及 Transkun 转写入口所需的 `moduleconf`、`pretty-midi`、`mir-eval`、`pydub`。不要执行 `pip install vendor\Transkun`：该包的完整训练依赖会尝试编译 Windows 推理不需要的组件。程序通过 `TRANSKUN_ROOT` 直接从已克隆源码调用转写入口。

另外准备以下系统工具，并确保 `ffmpeg.exe` 与同目录的 `ffprobe.exe` 来自同一套发行包：

- `ffmpeg` / `ffprobe`：音频规范化、MP3 编码和视频音频抽取
- `FluidSynth 2.5.6`：MIDI 钢琴回渲染
- `MuseScore 4`：PDF 路线的 MusicXML → MIDI
- `pdftoppm`：PDF 页面渲染
- `yt-dlp`：YouTube/Bilibili 音频抽取（已作为 Python 依赖安装）

Windows 常用来源：

- [ffmpeg](https://www.gyan.dev/ffmpeg/builds/)
- [FluidSynth 2.5.6 releases](https://github.com/FluidSynth/fluidsynth/releases)
- [MuseScore](https://musescore.org/zh-hans/download)
- [Poppler for Windows](https://github.com/oschwartz10612/poppler-windows)

从 [MuseScore SoundFont 使用说明](https://musescore.org/en/handbook/3/soundfonts-and-sfz-files) 获取与 FluidSynth 配套的 `MuseScore_General.sf2` 后，放到 `resources\MuseScore_General.sf2` 或 `%USERPROFILE%\.cache\music_ai_models\soundfonts\MuseScore_General.sf2`，也可以通过 `SHEET2MUSIC_SOUNDFONT` 指定。系统状态页会校验 SoundFont 的文件身份，并检查所有工具、CUDA provider、Beat This 检查点和两套 Transkun 模型。

### 3. 准备外部源码与模型

Transkun 源码和大模型不提交到 GitHub。默认源码位置是 `vendor/Transkun`：

```powershell
git clone https://github.com/Yujia-Yan/Transkun.git vendor\Transkun
```

准备以下文件：

```text
vendor\Transkun\transkun\pretrained\2.0.pt
vendor\Transkun\transkun\pretrained\2.0.conf
models\transkun-v2-aug\checkpointMSimplerAug\checkpoint.pt
models\transkun-v2-aug\checkpointMSimplerAug\model.conf
```

其中前两项是默认的“Transkun V2（参考兼容）”，后两项是可选的 Transkun V2 Aug。权重文件请从 Transkun 官方发布渠道下载；项目不会把大文件打包进仓库。Beat This `final0` 检查点默认位置为：

```text
%USERPROFILE%\.cache\torch\hub\checkpoints\beat_this-final0.ckpt
```

### 4. 可选环境变量

```powershell
$env:TRANSKUN_ROOT = "C:\path\to\Sheet2Music\vendor\Transkun"
$env:TRANSKUN_PYTHON = "C:\path\to\Sheet2Music\.venv\Scripts\python.exe"
$env:TRANSKUN_V2_WEIGHT = "C:\path\to\2.0.pt"
$env:TRANSKUN_V2_CONF = "C:\path\to\2.0.conf"
$env:TRANSKUN_V2_AUG_WEIGHT = "C:\path\to\checkpoint.pt"
$env:TRANSKUN_V2_AUG_CONF = "C:\path\to\model.conf"
$env:BEAT_THIS_CHECKPOINT = "C:\path\to\beat_this-final0.ckpt"
$env:SHEET2MUSIC_FFMPEG = "C:\path\to\ffmpeg.exe"
$env:SHEET2MUSIC_FLUIDSYNTH = "C:\path\to\fluidsynth.exe"
$env:SHEET2MUSIC_SOUNDFONT = "C:\path\to\MuseScore_General.sf2"
```

不设置时，程序按仓库 `vendor/Transkun`、项目 `.venv` 和用户缓存目录自动探测。不要为 Transkun 或 Beat This 创建第二个虚拟环境；Windows 上 HOMR 的 ONNX Runtime CUDA 与音频模型的 PyTorch CUDA 会在独立子进程中运行。

### 5. 启动服务

```powershell
.venv\Scripts\python.exe -m sheet2music.web.app
```

浏览器打开 <http://127.0.0.1:8610>。也可以执行 `sheet2music` 启动同一个服务。启动后先查看页面顶部“环境检查”，确认 `all_ok`、音频模型 CUDA、Beat This、Transkun、ffmpeg、FluidSynth 和 MuseScore 均可用。

## 功能介绍

- **MP3 钢琴转录**：使用 Transkun V2（默认）或 V2 Aug，输出可下载的 MIDI、钢琴 MP3 和审计报告；可选生成基于 MIDI 的钢琴谱 PDF。制谱时会额外生成内部的左右手分轨 MIDI：它只按整曲音高分布路由音符，保留音符的音高、起止和力度，不改动下载的 `score.mid` 或 MP3。
- **视频 URL 音频转录**：支持 YouTube 与 Bilibili，后台用 `yt-dlp` 只提取任务所需音频，不保存视频文件。
- **Beat This 节奏分析**：检测 beats、downbeats、BPM、可用拍号和 tempo map；结果保存到 `beats.json` 供审计。
- **PDF 琴谱识别**：使用 HOMR 生成 MusicXML，经结构检查与审批后导出 MIDI/MP3。
- **统一 MIDI 回渲染**：所有路线使用 FluidSynth 2.5.6 + MuseScore General SoundFont 生成 44.1 kHz 双声道 PCM WAV，再由 ffmpeg 编码 MP3。
- **GPU 推理**：音频模型使用 PyTorch CUDA；PDF 模型使用 HOMR ONNX Runtime CUDA。当前项目已在 RTX 4060 上验证。
- **任务状态与下载**：后台任务串行执行，页面轮询状态，支持失败提示、重置和产物下载。
- **可追溯产物**：音频任务保留 `score.raw.mid`、`score.mid`、`score.mp3`、可选的 `score.pdf` 和 `report.json`；临时 WAV 在完成后清理。

## 如何使用

### MP3 转 MIDI（推荐）

1. 启动服务并打开浏览器。
2. 在上传区选择或拖入 `.mp3`。
3. 确认环境检查通过；音频任务的 BPM 和拍号由 Beat This 自动检测。
4. 选择 `Transkun V2（参考兼容，默认）` 或 `Transkun V2 Aug`，需要时勾选 GPU。
5. 需要打印或查看钢琴谱时，勾选“同时生成钢琴谱 PDF”。该选项只对 MP3 和视频 URL 显示，PDF 输入不会重复生成 PDF。PDF 会从内部的左右手分轨 MIDI 制作，`score.mid` 保持为原始 Transkun 播放 MIDI。
6. 点击“开始转换”，等待 `MP3 转 WAV`、`Beat This 节奏识别`、`Transkun V2 转 MIDI`、`渲染 MP3` 和（如已选择）`生成钢琴谱 PDF` 完成。
7. 在结果区下载 `score.mid`、`score.mp3`、可选的 `score.pdf` 或 `report.json`。`score.raw.mid` 只用于本地审计，不作为下载产物。

### YouTube / Bilibili 视频转 MIDI

1. 将视频 URL 粘贴到上传区。
2. 提交后等待后台下载音频；平台登录、地区限制、版权和限流错误会直接显示为任务失败。
3. 后续步骤与 MP3 转 MIDI 相同，也可以选择同时生成钢琴谱 PDF。

### PDF 琴谱转 MIDI（备用）

1. 在上传区选择 `.pdf`，等待首页预览。
2. 填写 BPM 和拍号，选择需要的输出格式；没有明确拍号时使用 `4/4` 仅作为默认值。
3. 启动转换，查看 HOMR 识别和结构分析结果。
4. 如果页面提示人工审查，逐项选择保留谱面变化、采用修复建议或上传区域放大图，再提交审批。
5. 审批通过后下载 MusicXML、MIDI、MP3 或 ZIP。

### 常见问题

- **环境检查提示 Transkun V2 权重不存在**：检查 `vendor\Transkun\transkun\pretrained\2.0.pt`，或设置 `TRANSKUN_V2_WEIGHT` / `TRANSKUN_V2_CONF`。
- **Beat This 检查点不存在或校验失败**：准备 `beat_this-final0.ckpt`，或设置 `BEAT_THIS_CHECKPOINT`；不要用其他 checkpoint 替换。
- **找不到 ffmpeg / ffprobe**：将同一发行包的 `ffmpeg.exe` 和 `ffprobe.exe` 放入 PATH，或设置 `SHEET2MUSIC_FFMPEG`。
- **FluidSynth WAV 过短**：确认使用当前渲染器和同一份 FluidSynth/SoundFont；音频路线会按输入 transport 时长裁剪自然尾音。
- **GPU 不可用**：确认 NVIDIA 驱动、CUDA 版 PyTorch 和 ONNX Runtime GPU 均安装在项目 `.venv`，再查看 `/api/system/status`。

测试与实现记录：

- [Step 1 设计与里程碑](docs/transkun-v2-audio-transcription-design.md)
- [Transkun 参考一致性待办](docs/transkun-reference-parity-todo.md)
- [项目设计说明](docs/design.md)
- [本地冗余文件删除清单](docs/local-artifacts-cleanup-candidates.md)

## 许可证

本项目以 AGPL-3.0 发布。项目会调用 HOMR、Transkun、Beat This、FluidSynth、MuseScore 和 yt-dlp；请同时遵守这些组件各自的许可证、模型权重条款及音频平台使用规则。
