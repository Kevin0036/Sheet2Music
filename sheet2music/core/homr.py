"""HOMR 子进程封装（与 `run_homr_trial.py` 的调用方式一致）。

命令：`python -m homr.main --gpu <no|auto> [--output-metronome <bpm> --output-tempo <bpm>] <image>`
PYTHONPATH 前置 HOMR_ROOT；HOMR 在当前工作目录输出 `<image>.musicxml`。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

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


def build_homr_command(
    image: Path,
    debug: bool = False,
    tempo_bpm: int | None = None,
    use_gpu: bool = False,
    layout_output: Path | None = None,
) -> list[str]:
    command = [sys.executable, "-m", "homr.main", "--gpu", "auto" if use_gpu else "no"]
    if debug:
        command.append("--debug")
    if tempo_bpm is not None:
        command.extend(["--output-metronome", str(tempo_bpm), "--output-tempo", str(tempo_bpm)])
    if layout_output is not None:
        command.extend(["--layout-output", str(layout_output)])
    command.append(str(image))
    return command


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
    env = os.environ.copy()
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
