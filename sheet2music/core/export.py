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
from .fluidsynth_renderer import render_midi_to_mp3


def _musescore_env() -> dict[str, str]:
    env = os.environ.copy()
    env["QT_QPA_PLATFORM"] = "windows" if os.name == "nt" else "offscreen"
    if os.name == "nt":
        # MuseScore writes logs and settings during CLI exports. Keep those
        # writes isolated from a possibly inaccessible interactive profile.
        profile_dir = Path(tempfile.gettempdir()) / "sheet2music" / "musescore"
        (profile_dir / "AppData" / "Local").mkdir(parents=True, exist_ok=True)
        (profile_dir / "AppData" / "Roaming").mkdir(parents=True, exist_ok=True)
        env.update(
            {
                "USERPROFILE": str(profile_dir),
                "LOCALAPPDATA": str(profile_dir / "AppData" / "Local"),
                "APPDATA": str(profile_dir / "AppData" / "Roaming"),
            }
        )
    return env


def _run_musescore(args: list[str]) -> None:
    subprocess.run(
        [musescore_binary(), *args],
        check=True,
        env=_musescore_env(),
    )


def export_midi(input_musicxml: Path, midi_path: Path) -> None:
    midi_path.parent.mkdir(parents=True, exist_ok=True)
    _run_musescore(["-f", "-o", str(midi_path), str(input_musicxml)])
    if not midi_path.exists():
        raise FileNotFoundError(f"MuseScore did not emit {midi_path}")


def export_pdf(input_midi: Path, pdf_path: Path) -> None:
    """Render a MIDI file as a printable piano score with MuseScore."""
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    _run_musescore(["-f", "-o", str(pdf_path), str(input_midi)])
    if not pdf_path.exists():
        raise FileNotFoundError(f"MuseScore did not emit {pdf_path}")


def render_wav(input_score: Path, wav_path: Path) -> None:
    wav_path.parent.mkdir(parents=True, exist_ok=True)
    _run_musescore(["-f", "-o", str(wav_path), str(input_score)])
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


def render_mp3(
    input_score: Path,
    mp3_path: Path,
    *,
    target_duration: float | None = None,
) -> Path:
    """Render a MIDI file with the shared FluidSynth playback pipeline."""
    render_midi_to_mp3(input_score, mp3_path, target_duration=target_duration)
    return mp3_path
