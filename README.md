# Sheet2Music

把钢琴谱 PDF 或钢琴音频转成 `MusicXML` / `MIDI` / `MP3` 的本地浏览器工具。
它以最新的 [HOMR](https://github.com/liebharc/homr) 为识别引擎，再做一层保守修复：

- 默认整谱拍号为 `4/4`；检测到拍号变化时先进入人工审批，不会按出现频率过滤
- 固定整谱 BPM
- 按 `staff + voice` 修复安全的 `time` / `backup` / MIDI 元数据，不挪动音符到下一小节
- 保留 raw MusicXML；高风险 timing、拍号或谱号疑点在浏览器中审批后才导出最终 MIDI/MP3
- 识别前用 600 DPI 渲染 PDF，并按五线谱间距裁掉上下标题/页脚空白
- MP3 或 YouTube/Bilibili 视频 URL 可走专用 Transkun 钢琴转录流程；PDF 继续使用 HOMR

设计文档见 [docs/design.md](docs/design.md)。

## 快速结论

只装 Python 还不够。`Sheet2Music` 还依赖 3 个系统工具：

- `pdftoppm`：PDF 转页面图
- MuseScore CLI：MusicXML 导出 MIDI / 渲染音频
- `ffmpeg`：wav 转 MP3

Python 依赖可以通过 `pip` 一次装完；这 3 个系统工具必须按操作系统单独安装。

## Linux 安装

在这台机器上我已经确认：直接写 `python -m ...` 不够稳，因为很多 Linux 环境只有 `python3`，甚至没有 `python` 命令。下面这套更可靠：

```bash
git clone <repo> && cd Sheet2Music
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
sudo apt-get install -y poppler-utils musescore3 ffmpeg
sheet2music
```

启动后打开 `http://127.0.0.1:8610`。

## Windows 安装

PowerShell 下推荐：

```powershell
git clone <repo>
cd Sheet2Music
py -3 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e .
sheet2music
```

然后额外安装这 3 个系统工具，并确保可执行文件可被 `PATH` 找到：

- [MuseScore](https://musescore.org/zh-hans/download)
- [ffmpeg](https://www.gyan.dev/ffmpeg/builds/)
- [poppler for Windows](https://github.com/oschwartz10612/poppler-windows)（提供 `pdftoppm`）

代码里已经做了 Windows 常见安装目录兜底探测，并已在 Windows RTX 4060 Laptop 环境完成真实 GPU 流程验证：CUDA 会话的 active provider 为 `CUDAExecutionProvider`（同时保留 CPU provider 处理少量不支持的算子）。

## HOMR 源码与权重

`Sheet2Music` 只保留一套最新 HOMR 流程。独立仓库内的 `vendor/homr/` 包含当前工具使用的 HOMR 源码；模型权重不进入 Git。

- 优先探测 `HOMR_ROOT`
- 然后探测当前工具目录的 `vendor/homr`
- 在父项目中运行时，再兼容探测 `../third_party/homr` 和 `../vendor/homr`

如果使用外部 HOMR 源码，可以显式设置：

```bash
HOMR_ROOT=/absolute/path/to/homr sheet2music
```

模型权重不进入 Git。缺失时，页面顶部“环境检查”会提供一键下载；下载会同时准备
CPU/FP32 与 GPU/FP16 权重。环境检查会实际创建一次 ONNX Runtime CUDA
会话，只有会话激活 `CUDAExecutionProvider` 时才显示 GPU 可用。

HOMR 的源码和模型权重分别遵循其上游许可证与分发规则；发布时请保留
`vendor/homr/LICENSE`。

## 功能

- 一次上传一份 PDF
- 一次上传一份 MP3，或输入 YouTube/Bilibili 视频 URL；视频仅提取任务所需音频
- 自动渲染首页预览
- 手动输入 BPM
- 手动输入拍号，默认 `4/4`
- 可勾选 GPU 加速；勾选后使用 HOMR `--gpu force`，CUDA 会话不可用时明确报错，不静默回退 CPU
- 选择输出 `MusicXML` / `MIDI` / `MP3` / `ZIP`
- 某页 HOMR 失败时跳过该页并继续整谱
- 同一页面可立即继续上传下一份谱子

## 结构审批

转换先生成 raw MusicXML 和分析报告。出现拍号变化、谱号变化、声部越过小节边界、负游标或音符重叠等高风险 finding 时，任务会进入 `awaiting_review`，最终 MIDI/MP3 在审批前不会生成。

每个 finding 支持三种处理：保留谱面变化、采用修复建议，或上传指定小节范围的放大图进行区域二次识别。区域识别会按 `part id` 和全谱小节范围替换 XML，保留范围外内容，并在再次审批后才最终导出。

## 当前命名

- 仓库目录：`Sheet2Music/`
- Python 包：`sheet2music`
- 启动命令：`sheet2music`
- 备用启动方式：`python -m sheet2music.web.app`

兼容性上，旧环境变量仍可读取，但文档与默认名称已统一到 `Sheet2Music`。

## 可选环境变量

- `HOMR_ROOT`：显式指定 HOMR 源码目录
- `SHEET2MUSIC_PORT`：服务端口，默认 `8610`
- `SHEET2MUSIC_HOST`：监听地址，默认 `127.0.0.1`
- `SHEET2MUSIC_WORK_DIR`：任务工作目录，默认系统临时目录下 `sheet2music/jobs`
- `SHEET2MUSIC_FFMPEG`：可选；显式指定统一管理的 `ffmpeg.exe`。Transkun 子进程会自动将其所在目录加入 PATH，以便 pydub 同时找到同目录的 `ffprobe.exe`。
- `TRANSKUN_ROOT`：可选；默认 `vendor/Transkun`。Transkun 源码直接从该目录运行，不需要额外安装为第二个 Python 包。
- `TRANSKUN_PYTHON`：可选；默认当前 `.venv` 的 Python。必须与 Beat This、PyTorch 共享同一 `.venv`。

## 转换流程

1. `pdftoppm` 以 600 DPI 把 PDF 逐页导出为高分辨率原图
2. 根据连续五条长水平线估计五线谱范围，上下保留安全边距后生成 HOMR 输入图
3. 逐页调用 HOMR：未勾选 GPU 使用 `--gpu no`，勾选 GPU 使用 `--gpu force`；Windows 子进程会显式加载 venv 内的 CUDA/cuDNN DLL。
4. 保留 raw MusicXML，同时生成候选页级修复 XML
5. 合并 raw 全谱并运行结构预分析；候选修复不能覆盖未经确认的拍号/谱号变化
6. 无高风险 finding 时自动继续；有高风险 finding 时进入浏览器审批
7. 审批决定或区域二次识别完成后，按最终结构计划修复全谱 MusicXML
8. 用 MuseScore 导出 MIDI，按结构计划写入拍号 / tempo 元数据
9. 通过验证后按需渲染 MP3，并输出 `report.json`

任务目录中的 `pages/raw/` 保留 600 DPI 原图，`pages/page-N.png` 是实际送入
HOMR 的裁剪图。如果页面布局无法可靠检测出五线谱，工具会保留整页，不进行激进裁剪。

修复脚本只在结构计划或审批决定支持时改变拍号、谱号和调号元数据；不会凭空转置已经写入 XML 的 `<pitch>`。

## 音频推理环境

MP3/视频音频路径使用与 HOMR 相同的项目 `.venv`。本项目当前的 CUDA 运行时组合为 PyTorch CUDA（Beat This/Transkun）与 ONNX Runtime CUDA（HOMR）；它们在 Windows 上加载不同 CUDA DLL，因此音频模型的 CUDA 探测与 Beat This 推理在独立 Python 子进程中运行。请不要额外创建 Transkun venv，也不要把上游训练依赖（例如 `ncls`）混入推理环境。

外部工具只需集中管理一套：`ffmpeg.exe` 与 `ffprobe.exe` 必须来自同一目录，另配一套 MuseScore 与 Poppler。若系统 PATH 不稳定，请通过 `SHEET2MUSIC_FFMPEG` 固定 ffmpeg 路径，而不是复制二进制文件到项目目录。

### Transkun 转录流程

音频输入统一由 ffmpeg 转为 `44.1 kHz`、双声道、PCM s16 WAV。Beat This `final0` 只用于清洗节拍网格、推断全局 BPM 和高置信度拍号；不会量化、移动、增删 Transkun 的任何音符，也不会写可变速度图。

默认模型为“Transkun V2（参考兼容）”，使用仓库 `vendor/Transkun/transkun/pretrained/2.0.pt` 和 `2.0.conf`。界面可选 `Transkun V2 Aug`。两组模型会按文件大小和 SHA-256 校验；任务输出保留原始 `score.raw.mid` 供审计，下载的 `score.mid` 仅加入 BPM/可用拍号并保持其他事件的绝对播放秒不变，`score.mp3` 由该 MIDI 回渲染。

音频模型必须与 Beat This 共享项目唯一 `.venv`；本机已经以 RTX 4060 CUDA 验证。必要配置如下：

- `TRANSKUN_ROOT`：可选，默认 `vendor/Transkun`
- `TRANSKUN_PYTHON`：可选，默认当前项目 `.venv` 的 Python
- `TRANSKUN_V2_WEIGHT` / `TRANSKUN_V2_CONF`：可选，覆盖默认 V2 权重和配置
- `TRANSKUN_V2_AUG_WEIGHT` / `TRANSKUN_V2_AUG_CONF`：可选，覆盖 V2 Aug 权重和配置
- `SHEET2MUSIC_FFMPEG`：可选，固定 `ffmpeg.exe`，同目录需要有 `ffprobe.exe`

### 本机环境恢复清单

Transkun 源码和大模型不进入 Git。新机器、清理 `vendor`，或从压缩包恢复项目后，必须同时准备源码和权重；只安装 Python 依赖不会通过环境检查。

PowerShell 示例：

```powershell
# 进入 Sheet2Music 根目录
git clone https://github.com/Yujia-Yan/Transkun.git vendor\Transkun

# 原始 Transkun V2 随官方源码放置
# vendor\Transkun\transkun\pretrained\2.0.pt
# vendor\Transkun\transkun\pretrained\2.0.conf

# V2 Aug：将下载的压缩包解压为以下目录结构
# models\transkun-v2-aug\checkpointMSimplerAug\checkpoint.pt
# models\transkun-v2-aug\checkpointMSimplerAug\model.conf

# 检查项目环境
.venv\Scripts\python.exe -m pip check
sheet2music
```

如果使用本项目目录中的压缩包，可直接执行：

```powershell
tar -xf checkpointTransformer.zip -C models
New-Item -ItemType Directory -Force models\transkun-v2-aug | Out-Null
tar -xf checkpointTransformerAug.zip -C models\transkun-v2-aug
```

解压后如果目录名不是 `checkpointMSimplerAug`，请整理为上面列出的完整路径，或通过
`TRANSKUN_V2_AUG_WEIGHT` 和 `TRANSKUN_V2_AUG_CONF` 显式指定文件。环境检查会校验文件大小和 SHA-256，不接受重命名但内容不匹配的权重。

Beat This 还需要经过校验的 `final0` 检查点，默认位置为：

```text
%USERPROFILE%\.cache\torch\hub\checkpoints\beat_this-final0.ckpt
```

系统工具要求 `ffmpeg.exe`/`ffprobe.exe` 来自同一目录，另外需要 MuseScore 和 Poppler。音频模型使用项目唯一 `.venv` 的 PyTorch CUDA；当前已验证设备为 RTX 4060。环境检查中的 `transkun`、`beat_this`、`pytorch_cuda` 和 `binaries` 均通过后，音频任务才会进入推理阶段。

## MIDI 回渲染差异分析

`vendor/music-to-midi` 的钢琴专用转写完成后，播放音频走的是独立的 FluidSynth 路径，核心逻辑位于 `src/core/muscriptor_result_assets.py`：

1. 使用固定的 FluidSynth `2.5.6` Windows runtime，而不是调用 MuseScore。
2. 使用 MuScriptor 官方 `MuseScore_General.sf2` SoundFont，并对文件身份做大小和 SHA-256 校验。
3. 以 `44,100 Hz` 直接生成 WAV：`fluidsynth -ni -F output.wav -r 44100 soundfont.sf2 input.mid`。
4. 单钢琴轨直接合成完整 MIDI，因此 MIDI 中的控制器事件（尤其是 `CC64` 延音踏板）会进入最终声音；多乐器时才按乐器拆分后分别合成。
5. 生成的是无损 PCM WAV，并在 GUI 中做长度覆盖、淡出、通道混音和原声/转写声道对齐；它的“在线试听”并不等同于把 WAV 再压成 MP3。

Sheet2Music 当前使用 MuseScore CLI 将 MIDI 渲染为 WAV，再用 ffmpeg `libmp3lame -qscale:a 2` 转成 MP3。两条路径的音符可以完全一致，但仍会有以下听感差异：

- 使用的钢琴 SoundFont、滤波器、起音/释音包络不同；MuseScore 默认音色不等于官方 MuseScore General SoundFont 的 FluidSynth 渲染。
- FluidSynth 直接消费 MIDI 控制器，当前 Sheet2Music 的回渲染链路没有针对 `CC64`、声道增益和乐器总线做参考项目同等的混音处理。
- 参考项目试听的是 44.1 kHz PCM WAV；Sheet2Music 的下载产物经过 MP3 有损编码，瞬态和延音尾部会进一步变化。
- 参考项目对最终持续时间、尾音覆盖和淡出有显式边界检查；我们的 MuseScore 导出依赖其自身的 MIDI 播放边界。

因此，Transkun 识别结果相同并不保证 MP3 波形相同。若要达到 `music-to-midi` 的试听效果，Step 2 之后应增加独立的 FluidSynth 渲染适配器，固定 FluidSynth 版本和官方 SoundFont，先输出 44.1 kHz PCM WAV，再按需要生成 MP3；同时保留 CC64、长度覆盖和淡出/混音策略。仅调整当前 ffmpeg 的 MP3 码率不能解决音色和踏板差异。

## 测试

```bash
python -m pip install -e ".[dev]"
python -m unittest discover -s tests -t .
```

集成测试可用环境变量指定样例 PDF：

```bash
SHEET2MUSIC_TEST_PDF=/path/to/sample.pdf python -m unittest tests.test_convert
```

## 许可证

本工具调用并分发 [HOMR](https://github.com/liebharc/homr)（AGPL-3.0），因此仓库整体以 **AGPL-3.0** 使用与发布。
