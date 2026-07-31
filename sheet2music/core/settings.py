"""运行时设置：外部工具发现与路径解析。

HOMR 根目录解析顺序：
1. `HOMR_ROOT` 环境变量
2. 当前 monorepo 的 `third_party/homr`（唯一 canonical 源码）
3. 当前 monorepo / 独立仓库兼容入口 `vendor/homr`
4. 工具目录内的 `third_party/homr`

MuseScore / pdftoppm / ffmpeg 通过 `shutil.which` 在 PATH 上探测。
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

# Sheet2Music/sheet2music/core/settings.py → parents[2] == Sheet2Music/
TOOL_ROOT = Path(__file__).resolve().parents[2]


def _env(*names: str, default: str | None = None) -> str | None:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return default


def homr_root() -> Path:
    explicit = _env("HOMR_ROOT")
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    candidates.extend(
        [
            TOOL_ROOT.parent / "third_party" / "homr",
            TOOL_ROOT.parent / "vendor" / "homr",
            TOOL_ROOT / "vendor" / "homr",
            TOOL_ROOT / "third_party" / "homr",
        ]
    )
    for candidate in candidates:
        if candidate.exists():
            return candidate.resolve()
    attempted = ", ".join(str(candidate) for candidate in candidates)
    raise FileNotFoundError(
        "无法定位 HOMR 源码根目录。请设置环境变量 HOMR_ROOT，或将 HOMR 放到 "
        f"third_party/homr 或 vendor/homr。已尝试: {attempted}"
    )


#: Windows 上常见安装路径（未加入 PATH 时兜底探测）。
_WINDOWS_KNOWN_PATHS: dict[str, tuple[str, ...]] = {
    "musescore": (
        r"C:\Program Files\MuseScore 3\bin\MuseScore3.exe",
        r"C:\Program Files\MuseScore 4\bin\MuseScore4.exe",
        r"C:\Program Files (x86)\MuseScore 3\bin\MuseScore3.exe",
    ),
    "pdftoppm": (r"C:\Program Files\poppler\Library\bin\pdftoppm.exe",),
    "ffmpeg": (r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",),
}


def find_tool(name: str, *candidates: str) -> str:
    for candidate in candidates:
        resolved = shutil.which(candidate)
        if resolved is not None:
            return resolved
    if os.name == "nt":
        for known in _WINDOWS_KNOWN_PATHS.get(name.lower(), ()):
            if Path(known).exists():
                return known
    joined = ", ".join(candidates) if candidates else name
    raise FileNotFoundError(f"找不到 {name} 可执行文件，请在 PATH 中安装（已尝试: {joined}）")


def musescore_binary() -> str:
    return find_tool(
        "MuseScore",
        "musescore3",
        "musescore",
        "mscore3",
        "mscore",
        "MuseScore3",
        "MuseScore4",
        "musescore4",
    )


def pdftoppm_binary() -> str:
    return find_tool("pdftoppm", "pdftoppm")


def ffmpeg_binary() -> str:
    return find_tool("ffmpeg", "ffmpeg")


def work_dir() -> Path:
    """任务工作目录根（每个任务一个子目录）。"""
    configured = _env("SHEET2MUSIC_WORK_DIR", "HOMR_TOOL_WORK_DIR")
    base = Path(configured) if configured else Path(tempfile.gettempdir()) / "sheet2music"
    jobs_dir = base / "jobs"
    jobs_dir.mkdir(parents=True, exist_ok=True)
    return jobs_dir


def port() -> int:
    return int(_env("SHEET2MUSIC_PORT", "HOMR_TOOL_PORT", default="8610"))


def host() -> str:
    """监听地址。默认仅本机；暴露到局域网时设为 0.0.0.0。"""
    return _env("SHEET2MUSIC_HOST", "HOMR_TOOL_HOST", default="127.0.0.1") or "127.0.0.1"
