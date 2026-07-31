# Sheet2Music

把钢琴谱 PDF 转成 `MusicXML` / `MIDI` / `MP3` 的本地浏览器工具。  
它以最新的 [HOMR](https://github.com/liebharc/homr) 为识别引擎，再做一层保守修复：

- 固定整谱拍号，默认 `4/4`
- 固定整谱 BPM
- 只修 `time` / `backup` / MIDI 元数据，不挪动音符到下一小节
- 归一化后来又恢复的短暂错误调号与谱号，不猜测或改写音符音高
- 识别前用 600 DPI 渲染 PDF，并按五线谱间距裁掉上下标题/页脚空白

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

代码里已经做了 Windows 常见安装目录兜底探测，但我目前没有在 Windows 真机上做实跑，只完成了代码级兼容性检查。

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
CPU/FP32 与 GPU/FP16 权重。环境检查还会显示 ONNX Runtime 是否发现
`CUDAExecutionProvider`。

HOMR 的源码和模型权重分别遵循其上游许可证与分发规则；发布时请保留
`vendor/homr/LICENSE`。

## 功能

- 一次上传一份 PDF
- 自动渲染首页预览
- 手动输入 BPM
- 手动输入拍号，默认 `4/4`
- 可勾选 GPU 加速；勾选后使用 HOMR `--gpu auto`，没有可用 GPU 时回退 CPU
- 选择输出 `MusicXML` / `MIDI` / `MP3` / `ZIP`
- 某页 HOMR 失败时跳过该页并继续整谱
- 同一页面可立即继续上传下一份谱子

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

## 转换流程

1. `pdftoppm` 以 600 DPI 把 PDF 逐页导出为高分辨率原图
2. 根据连续五条长水平线估计五线谱范围，上下保留安全边距后生成 HOMR 输入图
3. 逐页调用 HOMR：未勾选 GPU 使用 `--gpu no`，勾选 GPU 使用 `--gpu auto`。
4. 对每页 MusicXML 做保守修复：固定目标拍号和 BPM，并清理后来恢复的短暂调号/谱号错误
5. 合并整谱，再修一次总谱 MusicXML
6. 用 MuseScore 导出 MIDI
7. 对 MIDI 统一拍号 / tempo 元数据
8. 按需渲染 MP3，并输出 `report.json`

任务目录中的 `pages/raw/` 保留 600 DPI 原图，`pages/page-N.png` 是实际送入
HOMR 的裁剪图。如果页面布局无法可靠检测出五线谱，工具会保留整页，不进行激进裁剪。

修复脚本的边界是结构性元数据：如果一个调号或谱号偏离本 part/staff 的首个基线，
但之后又恢复到该基线，才会把这段短暂变化归一化；持续到末尾的变化会保留。
这样可以处理 HOMR 将整首 `5` 个升号短暂识别为 `0`、或把高音谱号短暂识别为低音谱号的情况，
但不会凭空转置已经写入 XML 的 `<pitch>`。

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
