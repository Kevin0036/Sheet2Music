"""HOMR 子进程封装（与 `run_homr_trial.py` 的调用方式一致）。

命令：`python -m homr.main --gpu <no|force> [--output-metronome <bpm> --output-tempo <bpm>] <image>`
PYTHONPATH 前置 HOMR_ROOT；HOMR 在当前工作目录输出 `<image>.musicxml`。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from collections.abc import Mapping

from .settings import homr_root


class HomrPageError(RuntimeError):
    """HOMR 在某一页识别失败。

    保留页名与 stderr 尾部，让流水线可以跳过该页继续，并在报告中提示原因，
    而不是让整个任务因最后一页歌词/文本页而失败。
    """

    def __init__(self, page: str, stderr_tail: str):
        self.page = page
        self.stderr_tail = stderr_tail
        super().__init__(f"HOMR 识别失败: {page}")


_DLL_DIRECTORY_HANDLES: list[object] = []
_CUDA_PROBE_RESULT: tuple[bool, str] | None = None


def build_homr_command(
    image: Path,
    debug: bool = False,
    tempo_bpm: int | None = None,
    use_gpu: bool = False,
    layout_output: Path | None = None,
) -> list[str]:
    command = [sys.executable, "-m", "homr.main", "--gpu", "force" if use_gpu else "no"]
    if debug:
        command.append("--debug")
    if tempo_bpm is not None:
        command.extend(["--output-metronome", str(tempo_bpm), "--output-tempo", str(tempo_bpm)])
    if layout_output is not None:
        command.extend(["--layout-output", str(layout_output)])
    command.append(str(image))
    return command


def _windows_cuda_dll_directories() -> list[Path]:
    """Return CUDA/cuDNN DLL directories shipped inside this Python environment."""
    if os.name != "nt":
        return []

    site_packages = Path(sys.prefix) / "Lib" / "site-packages"
    candidates = [
        site_packages / "onnxruntime" / "capi",
        site_packages / "onnxruntime_gpu" / "capi",
        site_packages / "nvidia" / "cu13" / "bin",
        site_packages / "nvidia" / "cu13" / "bin" / "x86_64",
        site_packages / "nvidia" / "cudnn" / "bin",
    ]
    nvidia_root = site_packages / "nvidia"
    if nvidia_root.is_dir():
        candidates.extend(sorted(nvidia_root.glob("*/bin")))
        candidates.extend(sorted(nvidia_root.glob("*/bin/x86_64")))

    result: list[Path] = []
    for candidate in candidates:
        resolved = candidate.resolve()
        if candidate.is_dir() and resolved not in result:
            result.append(resolved)
    return result


def configure_gpu_dlls() -> list[Path]:
    """Load bundled CUDA/cuDNN DLL directories into the current process."""
    directories = _windows_cuda_dll_directories()
    if not directories:
        return []

    add_dll_directory = getattr(os, "add_dll_directory", None)
    if add_dll_directory is not None:
        for directory in directories:
            try:
                _DLL_DIRECTORY_HANDLES.append(add_dll_directory(str(directory)))
            except OSError:
                continue

    try:
        import onnxruntime as ort

        preload_dlls = getattr(ort, "preload_dlls", None)
        if preload_dlls is not None:
            preload_dlls()
    except (ImportError, OSError, RuntimeError):
        pass
    return directories


def gpu_runtime_environment(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """Build a subprocess environment that can load bundled CUDA DLLs on Windows."""
    env = dict(base or os.environ)
    directories = _windows_cuda_dll_directories()
    if not directories:
        return env

    path_entries = [str(path) for path in directories]
    existing_path = env.get("PATH")
    if existing_path:
        path_entries.append(existing_path)
    env["PATH"] = os.pathsep.join(path_entries)
    return env


def probe_cuda_provider() -> tuple[bool, str]:
    """Create a real ORT session to verify CUDA can execute, not just advertise."""
    global _CUDA_PROBE_RESULT
    if _CUDA_PROBE_RESULT is not None:
        return _CUDA_PROBE_RESULT

    configure_gpu_dlls()
    try:
        import onnxruntime as ort

        if "CUDAExecutionProvider" not in ort.get_available_providers():
            result = (False, "onnxruntime 未提供 CUDAExecutionProvider")
        else:
            model_candidates = sorted(
                (homr_root() / "homr" / "segmentation").glob("*_fp16.onnx")
            )
            if not model_candidates:
                result = (False, "缺少 HOMR FP16 分割模型，无法验证 CUDA 会话")
            else:
                session = ort.InferenceSession(
                    str(model_candidates[0]),
                    providers=["CUDAExecutionProvider"],
                )
                active = session.get_providers()
                if "CUDAExecutionProvider" in active:
                    result = (True, ", ".join(active))
                else:
                    result = (False, f"CUDA 会话回退到: {', '.join(active)}")
    except Exception as exc:  # noqa: BLE001 - expose the provider diagnostic
        result = (False, f"CUDA 会话初始化失败: {type(exc).__name__}: {exc}")

    # Cache only a successful probe. A failed probe may become valid after the
    # user downloads weights or restarts a service with corrected DLL paths.
    if result[0]:
        _CUDA_PROBE_RESULT = result
    return result


def _trim_stderr(stderr: str, max_lines: int = 3) -> str:
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    return "\n".join(lines[-max_lines:]) if lines else "(无错误输出)"


def run_homr_on_page(
    image: Path,
    work_dir: Path,
    debug: bool = False,
    tempo_bpm: int | None = None,
    use_gpu: bool = False,
    layout_output: Path | None = None,
) -> Path:
    if use_gpu:
        gpu_ok, gpu_detail = probe_cuda_provider()
        if not gpu_ok:
            raise HomrPageError(image.name, gpu_detail)

    work_dir.mkdir(parents=True, exist_ok=True)
    staged_image = work_dir / image.name
    shutil.copy2(image, staged_image)

    command = build_homr_command(
        staged_image,
        debug=debug,
        tempo_bpm=tempo_bpm,
        use_gpu=use_gpu,
        layout_output=layout_output,
    )
    env = gpu_runtime_environment(os.environ)
    existing_pythonpath = env.get("PYTHONPATH")
    pythonpath_entries = [str(homr_root())]
    if existing_pythonpath:
        pythonpath_entries.append(existing_pythonpath)
    env["PYTHONPATH"] = os.pathsep.join(pythonpath_entries)
    result = subprocess.run(
        command,
        env=env,
        cwd=str(work_dir),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise HomrPageError(staged_image.name, _trim_stderr(result.stderr))
    page_xml = staged_image.with_suffix(".musicxml")
    if not page_xml.exists():
        raise HomrPageError(staged_image.name, "HOMR 未生成 page.musicxml 输出")
    if layout_output is not None and not layout_output.exists():
        raise HomrPageError(staged_image.name, "HOMR 未生成布局 JSON 输出")
    return page_xml
