"""运行时设置：外部工具发现与路径解析。

HOMR 根目录解析顺序：
1. `HOMR_ROOT` 环境变量
2. 当前 monorepo 的 `third_party/homr`（唯一 canonical 源码）
3. 当前 monorepo / 独立仓库兼容入口 `vendor/homr`
4. 工具目录内的 `third_party/homr`

MuseScore / pdftoppm / ffmpeg 通过 `shutil.which` 在 PATH 上探测。
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

# Sheet2Music/sheet2music/core/settings.py → parents[2] == Sheet2Music/
TOOL_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class FileIdentity:
    size: int
    sha256: str


TRANSKUN_MODEL_IDENTITIES: dict[str, tuple[FileIdentity, FileIdentity]] = {
    "v2": (
        FileIdentity(56_408_978, "50a80010effc2a59ffcd068a95cd2b29bd7f23a27a3515bc3ccd209c89a3d44c"),
        FileIdentity(819, "edc237514eb7881f0f96b5769b20225c056c5c4e52f3804d77d8f6e39ebdbb33"),
    ),
    "v2_aug": (
        FileIdentity(56_423_254, "8bd6b4b5ddf9ce8c5f296a57859eec9f166cd337c35245ec2a2576d90be68c4c"),
        FileIdentity(782, "d3d989214eb148230ee5df476d994dcde6af595904d3f968f1221d2e3bea5ac6"),
    ),
}

BEAT_THIS_FINAL0_IDENTITY = FileIdentity(
    81_058_141,
    "8c328b45f59d8dd3dff219253ff6a8d6482be57d0133a29140e2febbf8eb8331",
)

FLUIDSYNTH_VERSION = "2.5.6"
MUSICSCORE_GENERAL_SF2_IDENTITY = FileIdentity(
    215_614_036,
    "ee51d2c4b1525e70f19a45909c4fd7a2e26d91d115fa89dbf5a6bc413d8b9bf3",
)


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


def _windows_poppler_binary() -> str | None:
    candidates = [Path(path) for path in _WINDOWS_KNOWN_PATHS["pdftoppm"]]
    local_app_data = _local_app_data()
    if local_app_data:
        packages_dir = Path(local_app_data) / "Microsoft" / "WinGet" / "Packages"
        candidates.extend(
            sorted(
                packages_dir.glob("*Poppler_*/poppler-*/Library/bin/pdftoppm.exe"),
                reverse=True,
            )
        )
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return None


def _windows_ffmpeg_binary() -> str | None:
    roots: list[Path] = []
    for root in (
        _local_app_data(),
        Path(os.environ["USERPROFILE"]) / "AppData" / "Local"
        if os.environ.get("USERPROFILE")
        else None,
        Path.home() / "AppData" / "Local" if os.name == "nt" else None,
    ):
        if root is not None and root not in roots:
            roots.append(root)

    for local_app_data in roots:
        winget_root = Path(local_app_data) / "Microsoft" / "WinGet"
        # WinGet may expose package executables through its links directory.
        link = winget_root / "Links" / "ffmpeg.exe"
        try:
            if link.is_file():
                return str(link)
        except OSError:
            pass

        packages_dir = winget_root / "Packages"
        try:
            package_dirs = sorted(
                (path for path in packages_dir.iterdir() if "ffmpeg_" in path.name.lower()),
                key=lambda path: path.name,
                reverse=True,
            )
        except OSError:
            continue
        for package_dir in package_dirs:
            # Avoid a recursive glob: ACLs on WinGet package contents can make
            # glob() silently return no matches even when the executable exists.
            try:
                version_dirs = sorted(
                    (path for path in package_dir.iterdir() if path.name.lower().startswith("ffmpeg-")),
                    key=lambda path: path.name,
                    reverse=True,
                )
            except OSError:
                continue
            for version_dir in version_dirs:
                candidate = version_dir / "bin" / "ffmpeg.exe"
                try:
                    if candidate.is_file():
                        return str(candidate)
                except OSError:
                    # The path layout itself is authoritative; defer the final
                    # access check to CreateProcess when the export runs.
                    return str(candidate)
    return None


def _local_app_data() -> Path | None:
    configured = os.environ.get("LOCALAPPDATA")
    if configured:
        return Path(configured)
    if os.name == "nt":
        return Path.home() / "AppData" / "Local"
    return None


def pdftoppm_binary() -> str:
    windows_poppler = _windows_poppler_binary()
    if windows_poppler is not None:
        return windows_poppler
    # Prefer a real executable over PATH shims such as pdftoppm.CMD.
    return find_tool("pdftoppm", "pdftoppm.exe", "pdftoppm")


def ffmpeg_binary() -> str:
    configured = _env("SHEET2MUSIC_FFMPEG")
    if configured:
        return configured
    windows_ffmpeg = _windows_ffmpeg_binary()
    if windows_ffmpeg is not None:
        return windows_ffmpeg
    return find_tool("ffmpeg", "ffmpeg.exe", "ffmpeg")


def fluidsynth_binary() -> str:
    configured = _env("SHEET2MUSIC_FLUIDSYNTH")
    candidates = [Path(configured)] if configured else []
    cache = Path.home() / ".cache" / "music_ai_models" / "fluidsynth" / FLUIDSYNTH_VERSION
    candidates.append(cache / "bin" / ("fluidsynth.exe" if os.name == "nt" else "fluidsynth"))
    discovered = shutil.which("fluidsynth")
    if discovered:
        candidates.append(Path(discovered))
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate.resolve())
    raise FileNotFoundError(
        f"找不到 FluidSynth {FLUIDSYNTH_VERSION}；请设置 SHEET2MUSIC_FLUIDSYNTH 或安装固定版本"
    )


def soundfont_path() -> Path:
    configured = _env("SHEET2MUSIC_SOUNDFONT")
    candidates = [Path(configured)] if configured else []
    candidates.extend(
        [
            TOOL_ROOT / "resources" / "MuseScore_General.sf2",
            Path.home() / ".cache" / "music_ai_models" / "soundfonts" / "MuseScore_General.sf2",
        ]
    )
    for candidate in candidates:
        if candidate.is_file():
            return validate_file_identity(
                candidate.resolve(),
                expected_size=MUSICSCORE_GENERAL_SF2_IDENTITY.size,
                expected_sha256=MUSICSCORE_GENERAL_SF2_IDENTITY.sha256,
                label="MuseScore General SoundFont",
            )
    raise FileNotFoundError(
        "MuseScore General SoundFont 不存在；请设置 SHEET2MUSIC_SOUNDFONT 或下载 MuseScore_General.sf2"
    )


def transkun_root() -> Path:
    configured = _env("TRANSKUN_ROOT")
    candidates = [Path(configured)] if configured else []
    candidates.extend([TOOL_ROOT / "vendor" / "Transkun", TOOL_ROOT / "vendor" / "transkun"])
    for candidate in candidates:
        root = candidate.expanduser().resolve()
        if root.is_dir():
            return root
    raise FileNotFoundError("无法定位 Transkun V2 源码；请设置 TRANSKUN_ROOT 或放入 vendor/Transkun")


def transkun_python() -> str:
    return _env("TRANSKUN_PYTHON", default=sys.executable) or sys.executable


def beat_this_checkpoint() -> Path:
    default = Path.home() / ".cache" / "torch" / "hub" / "checkpoints" / "beat_this-final0.ckpt"
    path = Path(_env("BEAT_THIS_CHECKPOINT", default=str(default)) or "").expanduser().resolve()
    return validate_file_identity(
        path,
        expected_size=BEAT_THIS_FINAL0_IDENTITY.size,
        expected_sha256=BEAT_THIS_FINAL0_IDENTITY.sha256,
        label="Beat This final0 checkpoint",
    )


def validate_file_identity(path: Path, *, expected_size: int, expected_sha256: str, label: str) -> Path:
    if not path.is_file():
        raise FileNotFoundError(f"{label} 不存在: {path}")
    actual_size = path.stat().st_size
    if actual_size != expected_size:
        raise RuntimeError(f"{label} 文件大小不匹配: {actual_size} != {expected_size} ({path})")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    actual_sha256 = digest.hexdigest()
    if actual_sha256 != expected_sha256:
        raise RuntimeError(f"{label} SHA-256 不匹配: {actual_sha256} != {expected_sha256} ({path})")
    return path


def transkun_model_files(model: str = "v2") -> tuple[Path, Path]:
    if model not in {"v2", "v2_aug"}:
        raise ValueError(f"未知的 Transkun 模型: {model}")
    prefix = "TRANSKUN_V2_AUG" if model == "v2_aug" else "TRANSKUN_V2"
    if model == "v2_aug":
        default_weight = TOOL_ROOT / "models" / "transkun-v2-aug" / "checkpointMSimplerAug" / "checkpoint.pt"
        default_conf = default_weight.with_name("model.conf")
    else:
        default_weight = transkun_root() / "transkun" / "pretrained" / "2.0.pt"
        default_conf = default_weight.with_suffix(".conf")
    weight = Path(_env(f"{prefix}_WEIGHT", default=str(default_weight)) or "")
    conf = Path(_env(f"{prefix}_CONF", default=str(default_conf)) or "")
    weight = weight.expanduser().resolve()
    conf = conf.expanduser().resolve()
    weight_identity, conf_identity = TRANSKUN_MODEL_IDENTITIES[model]
    validate_file_identity(
        weight,
        expected_size=weight_identity.size,
        expected_sha256=weight_identity.sha256,
        label=f"Transkun {model} 权重",
    )
    validate_file_identity(
        conf,
        expected_size=conf_identity.size,
        expected_sha256=conf_identity.sha256,
        label=f"Transkun {model} 配置",
    )
    return weight, conf


def transkun_model_dir() -> Path:
    """Compatibility helper returning the default V2 checkpoint directory."""
    return transkun_model_files("v2")[0].parent


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
