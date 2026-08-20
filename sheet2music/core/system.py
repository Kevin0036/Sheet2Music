"""运行环境自检 + 模型权重后台下载。

提供 `system_status()` 供前端「环境检查」面板使用：HOMR 源码 / 权重 /
Python 依赖 / 外部命令。权重缺失时可通过 `start_weight_download()` 从
HOMR 官方 release 后台下载（带进度），无需用户手动装权重。
"""

from __future__ import annotations

import importlib
import json
import os
import re
import subprocess
import threading
import zipfile
from pathlib import Path

import requests

from .homr import configure_gpu_dlls, probe_cuda_provider
from .settings import beat_this_checkpoint, ffmpeg_binary, find_tool, homr_root, pdftoppm_binary, transkun_model_files, transkun_python, transkun_root

#: HOMR 官方权重下载地址（与 homr/download_utils.py 一致）。
_BASE_URL = "https://github.com/liebharc/homr/releases/download/onnx_checkpoints/"

#: 前端展示的 Python 依赖检查项（HOMR 推理 + 本工具）。
REQUIRED_PYTHON_MODULES: tuple[str, ...] = (
    "numpy",
    "cv2",
    "onnxruntime",
    "musicxml",
    "rapidocr",
    "PIL",
    "miditoolkit",
    "fastapi",
    "uvicorn",
)

#: 系统命令检查项：(探测名, 候选可执行名列表)。探测复用 settings.find_tool，
#: 已内置 Windows 常见安装路径兜底。
_BINARY_CHECKS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("pdftoppm", ("pdftoppm",)),
    (
        "MuseScore",
        ("musescore3", "musescore", "mscore3", "mscore", "MuseScore3", "MuseScore4", "musescore4"),
    ),
    ("ffmpeg", ("ffmpeg",)),
)


def _install_hint(name: str) -> str:
    if os.name == "nt":
        return "请安装该软件，确保其可执行文件在 PATH 中（或安装到默认目录后重启工具）"
    return {
        "pdftoppm": "sudo apt-get install -y poppler-utils",
        "MuseScore": "sudo apt-get install -y musescore3",
        "ffmpeg": "sudo apt-get install -y ffmpeg",
    }[name]


def _model_name_from_config(path: Path) -> str:
    if not path.exists():
        raise FileNotFoundError(f"缺少 HOMR 配置文件: {path}")
    match = re.search(r'model_name\s*=\s*"([^"]+)"', path.read_text(encoding="utf-8"))
    if match is None:
        raise RuntimeError(f"无法从 {path} 解析 model_name")
    return match.group(1)


def required_model_specs(
    root: Path,
    include_fp16: bool = False,
) -> tuple[tuple[str, str], ...]:
    """从 HOMR 当前源码配置动态推导需要的 ONNX 权重文件名。"""
    segnet_name = _model_name_from_config(root / "homr" / "segmentation" / "config.py")
    transformer_name = _model_name_from_config(root / "homr" / "transformer" / "configs.py")
    fp32_models = (
        ("segmentation", f"{segnet_name}.onnx"),
        ("transformer", f"encoder_{transformer_name}.onnx"),
        ("transformer", f"decoder_{transformer_name}.onnx"),
    )
    if not include_fp16:
        return fp32_models
    return fp32_models + tuple(
        (subdir, f"{filename[:-5]}_fp16.onnx") for subdir, filename in fp32_models
    )


def model_files(root: Path, include_fp16: bool = False) -> list[Path]:
    """HOMR 推理需要的 3 个权重文件路径（不判断是否存在）。"""
    return [
        root / "homr" / subdir / name
        for subdir, name in required_model_specs(root, include_fp16=include_fp16)
    ]


def available_gpu_providers() -> list[str]:
    """Return ONNX Runtime providers that can activate HOMR's GPU path."""
    configure_gpu_dlls()
    try:
        import onnxruntime as ort
    except ImportError:
        return []
    return [
        provider
        for provider in ort.get_available_providers()
        if provider in {"CUDAExecutionProvider", "CoreMLExecutionProvider"}
    ]


def pytorch_cuda_status(torch_module: object | None = None) -> dict[str, object]:
    """Report the CUDA runtime used by Beat This and Transkun."""
    try:
        torch = torch_module or importlib.import_module("torch")
        cuda = torch.cuda
        available = bool(cuda.is_available())
        return {
            "ok": available,
            "torch_version": str(torch.__version__),
            "cuda_version": str(torch.version.cuda or ""),
            "device": str(cuda.get_device_name(0)) if available else "",
            "device_count": int(cuda.device_count()) if available else 0,
            "hint": None if available else "PyTorch CUDA 不可用，音频模型只能使用 CPU",
        }
    except (ImportError, AttributeError, OSError, RuntimeError) as exc:
        return {
            "ok": False,
            "torch_version": "",
            "cuda_version": "",
            "device": "",
            "device_count": 0,
            "hint": f"PyTorch CUDA 探测失败: {type(exc).__name__}: {exc}",
        }


def probe_pytorch_cuda() -> dict[str, object]:
    """Probe PyTorch in a clean process to avoid mixing ORT and Torch CUDA DLLs."""
    try:
        result = subprocess.run(
            [transkun_python(), "-m", "sheet2music.core.audio_worker", "torch-status"],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
        return json.loads(result.stdout.strip().splitlines()[-1])
    except (FileNotFoundError, IndexError, json.JSONDecodeError, subprocess.SubprocessError) as exc:
        return {
            "ok": False,
            "torch_version": "",
            "cuda_version": "",
            "device": "",
            "device_count": 0,
            "hint": f"PyTorch CUDA 子进程探测失败: {type(exc).__name__}: {exc}",
        }
def missing_model_files(use_gpu: bool = False) -> list[Path]:
    """当前解析到的 HOMR 根目录下缺失的权重文件。"""
    # GPU mode requires FP16 files only when a CUDA provider is actually present.
    include_fp16 = use_gpu and bool(available_gpu_providers())
    return [
        path for path in model_files(homr_root(), include_fp16=include_fp16) if not path.exists()
    ]


def system_status() -> dict[str, object]:
    """返回前端「环境检查」面板所需的状态。"""
    try:
        root = homr_root()
        homr_root_info: dict[str, object] = {"ok": True, "path": str(root)}
        missing = [path.name for path in missing_model_files()]
        fp16_missing = [
            path.name
            for path in model_files(root, include_fp16=True)
            if path.name.endswith("_fp16.onnx") and not path.exists()
        ]
    except (FileNotFoundError, RuntimeError) as exc:
        homr_root_info = {"ok": False, "error": str(exc)}
        missing = []
        fp16_missing = []

    providers: list[str] = []
    try:
        import onnxruntime as ort

        providers = list(ort.get_available_providers())
    except ImportError:
        pass
    accelerator_providers = available_gpu_providers()
    if homr_root_info["ok"] and not fp16_missing and accelerator_providers:
        gpu_probe_ok, gpu_probe_detail = probe_cuda_provider()
    else:
        gpu_probe_ok = False
        gpu_probe_detail = "FP16 权重缺失或未发现 CUDA provider"
    gpu_ok = bool(gpu_probe_ok and homr_root_info["ok"])
    gpu_hint = None
    if not gpu_probe_ok:
        gpu_hint = gpu_probe_detail
    elif not accelerator_providers:
        gpu_hint = "未检测到 CUDA/CoreML provider，将使用 CPU"
    elif fp16_missing:
        gpu_hint = "GPU provider 已检测到，但 FP16 权重尚未就绪"

    deps: list[dict[str, object]] = []
    for module in REQUIRED_PYTHON_MODULES:
        try:
            importlib.import_module(module)
            deps.append({"name": module, "ok": True})
        except ImportError:
            deps.append({"name": module, "ok": False})

    binaries: list[dict[str, object]] = []
    for name, candidates in _BINARY_CHECKS:
        try:
            resolved = (
                pdftoppm_binary()
                if name == "pdftoppm"
                else ffmpeg_binary()
                if name == "ffmpeg"
                else find_tool(name, *candidates)
            )
            binaries.append({"name": name, "ok": True, "path": resolved, "hint": None})
        except FileNotFoundError:
            binaries.append({"name": name, "ok": False, "path": None, "hint": _install_hint(name)})

    try:
        v2_weight, v2_conf = transkun_model_files("v2")
        aug_weight, aug_conf = transkun_model_files("v2_aug")
        transkun_info = {
            "ok": True,
            "root": str(transkun_root()),
            "model_dir": str(v2_weight.parent),
            "models": {
                "v2": {"ok": True, "identity_verified": True, "weight": str(v2_weight), "conf": str(v2_conf)},
                "v2_aug": {"ok": True, "identity_verified": True, "weight": str(aug_weight), "conf": str(aug_conf)},
            },
        }
    except (FileNotFoundError, OSError, RuntimeError) as exc:
        transkun_info = {"ok": False, "error": str(exc)}

    beat_this_info: dict[str, object] = {"ok": False, "identity_verified": False}
    try:
        importlib.import_module("beat_this")
        checkpoint = beat_this_checkpoint()
        beat_this_info = {"ok": True, "identity_verified": True, "checkpoint": str(checkpoint)}
    except (ImportError, FileNotFoundError, OSError, RuntimeError) as exc:
        beat_this_info["error"] = str(exc)

    pytorch_cuda = probe_pytorch_cuda()
    all_ok = bool(
        homr_root_info["ok"]
        and not missing
        and all(item["ok"] for item in deps)
        and all(item["ok"] for item in binaries)
        and transkun_info["ok"]
        and bool(beat_this_info["ok"])
    )
    return {
        "homr_root": homr_root_info,
        "weights": {
            "ok": not missing,
            "missing": missing,
            "missing_fp16": fp16_missing,
            "download_needed": bool(missing or fp16_missing),
        },
        "gpu": {
            "ok": gpu_ok,
            "providers": providers,
            "cuda_available": "CUDAExecutionProvider" in providers,
            "active_providers": gpu_probe_detail if gpu_probe_ok else "",
            "session_ok": gpu_probe_ok,
            "fp16_weights_ok": not fp16_missing and bool(homr_root_info["ok"]),
            "missing_fp16": fp16_missing,
            "hint": gpu_hint,
        },
        "pytorch_cuda": pytorch_cuda,
        "python_deps": deps,
        "binaries": binaries,
        "transkun": transkun_info,
        "beat_this": beat_this_info,
        "all_ok": all_ok,
    }


# ---------------------------------------------------------------------------
# 权重后台下载
# ---------------------------------------------------------------------------

WEIGHT_DOWNLOAD_STATE: dict[str, object] = {
    "running": False,
    "current_file": None,
    "percent": 0,
    "downloaded_bytes": 0,
    "total_bytes": 0,
    "error": None,
    "done": False,
}
_DOWNLOAD_LOCK = threading.Lock()


def weight_download_state() -> dict[str, object]:
    with _DOWNLOAD_LOCK:
        return dict(WEIGHT_DOWNLOAD_STATE)


def start_weight_download() -> bool:
    """启动后台权重下载；若已在下载则返回 False。"""
    with _DOWNLOAD_LOCK:
        if WEIGHT_DOWNLOAD_STATE["running"]:
            return False
        WEIGHT_DOWNLOAD_STATE.update(
            running=True,
            current_file=None,
            percent=0,
            downloaded_bytes=0,
            total_bytes=0,
            error=None,
            done=False,
        )
    threading.Thread(target=_download_weights_worker, daemon=True).start()
    return True


def _set_state(**updates: object) -> None:
    with _DOWNLOAD_LOCK:
        WEIGHT_DOWNLOAD_STATE.update(updates)


def _download_weights_worker() -> None:
    try:
        root = homr_root()
        for subdir, filename in required_model_specs(root, include_fp16=True):
            target = root / "homr" / subdir / filename
            if target.exists():
                continue
            _set_state(current_file=filename)
            _download_and_unzip(filename, target)
        _set_state(running=False, done=True, percent=100, current_file=None)
    except Exception as exc:  # noqa: BLE001 - 失败要暴露给前端
        _set_state(running=False, done=False, error=f"{type(exc).__name__}: {exc}")


def _download_and_unzip(filename: str, target: Path) -> None:
    zip_name = filename.rsplit(".", 1)[0] + ".zip"
    target.parent.mkdir(parents=True, exist_ok=True)
    downloaded_zip = target.parent / zip_name
    try:
        total = _download_file(_BASE_URL + zip_name, downloaded_zip)
        _set_state(total_bytes=total, downloaded_bytes=total)
        # 兼容 zip 内含顶层目录的情况：只取 <name>.onnx 这一项解压到目标路径。
        with zipfile.ZipFile(downloaded_zip) as archive:
            names = {Path(member).name: member for member in archive.namelist() if not member.endswith("/")}
            if filename not in names:
                raise RuntimeError(f"压缩包内未找到 {filename}")
            with archive.open(names[filename]) as source, open(target, "wb") as dest:
                while chunk := source.read(1024 * 1024):
                    dest.write(chunk)
    finally:
        downloaded_zip.unlink(missing_ok=True)


def _download_file(url: str, dest: Path, chunk_size: int = 256 * 1024) -> int:
    with requests.get(url, stream=True, timeout=30) as response:
        response.raise_for_status()
        total = int(response.headers.get("content-length", 0))
        downloaded = 0
        with open(dest, "wb") as handle:
            for chunk in response.iter_content(chunk_size=chunk_size):
                if not chunk:
                    continue
                handle.write(chunk)
                downloaded += len(chunk)
                if total:
                    _set_state(
                        downloaded_bytes=downloaded,
                        total_bytes=total,
                        percent=int(100 * downloaded // total),
                    )
                else:
                    _set_state(downloaded_bytes=downloaded, total_bytes=downloaded)
    return total if total else downloaded
