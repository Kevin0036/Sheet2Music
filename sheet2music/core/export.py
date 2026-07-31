"""MuseScore 导出与 MP3 渲染。

MIDI 导出复用了 `run_homr_trial.py` 的 MuseScore 参数；MP3 先由 MuseScore
渲染 wav，再用 ffmpeg 转码，保证输出稳定。
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

from .settings import ffmpeg_binary, musescore_binary


def _musescore_env() -> dict[str, str]:
    env = os.environ.copy()
    env["QT_QPA_PLATFORM"] = "offscreen"
    return env


def _musescore_config_dir() -> Path:
    config_dir = Path(tempfile.gettempdir()) / "mscorecfg-sheet2music"
    config_dir.mkdir(parents=True, exist_ok=True)
    return config_dir


def _run_musescore(args: list[str]) -> None:
    subprocess.run(
        [musescore_binary(), *args],
        check=True,
        env=_musescore_env(),
    )


def export_midi(input_musicxml: Path, midi_path: Path) -> None:
    midi_path.parent.mkdir(parents=True, exist_ok=True)
    _run_musescore(
        [
            "-s",
            "-m",
            "-w",
            "-f",
            "-F",
            "-R",
            "-c", str(_musescore_config_dir()),
            "-o", str(midi_path),
            str(input_musicxml),
        ]
    )
    if not midi_path.exists():
        raise FileNotFoundError(f"MuseScore did not emit {midi_path}")


def render_wav(input_score: Path, wav_path: Path) -> None:
    wav_path.parent.mkdir(parents=True, exist_ok=True)
    _run_musescore(["-o", str(wav_path), str(input_score)])
    if not wav_path.exists():
        raise FileNotFoundError(f"MuseScore did not emit {wav_path}")


def transcode_mp3(wav_path: Path, mp3_path: Path) -> None:
    mp3_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            ffmpeg_binary(),
            "-y",
            "-i", str(wav_path),
            "-codec:a", "libmp3lame",
            "-qscale:a", "2",
            str(mp3_path),
        ],
        check=True,
    )
    if not mp3_path.exists():
        raise FileNotFoundError(f"ffmpeg did not emit {mp3_path}")


def render_mp3(input_score: Path, mp3_path: Path) -> Path:
    """渲染 MP3：MuseScore 出 wav，再 ffmpeg 转 mp3。"""
    wav_path = mp3_path.with_suffix(".wav")
    render_wav(input_score, wav_path)
    transcode_mp3(wav_path, mp3_path)
    return mp3_path
