"""Reference-compatible MIDI playback rendering for downloads and GUI previews."""

from __future__ import annotations

import os
import subprocess
import tempfile
import wave
from dataclasses import dataclass
from pathlib import Path

import mido

from .settings import ffmpeg_binary, fluidsynth_binary, soundfont_path

SAMPLE_RATE = 44_100
TAIL_SECONDS = 0.03


@dataclass(frozen=True)
class RenderResult:
    midi: Path
    wav: Path
    mp3: Path | None
    sample_rate: int
    channels: int
    duration_seconds: float


def build_fluidsynth_command(executable: str, soundfont: Path, midi: Path, wav: Path) -> list[str]:
    return [executable, "-ni", "-F", str(wav), "-r", str(SAMPLE_RATE), str(soundfont), str(midi)]


def _midi_duration(path: Path) -> float:
    midi = mido.MidiFile(str(path))
    elapsed = 0.0
    for track in midi.tracks:
        track_elapsed = 0.0
        for message in track:
            track_elapsed += mido.tick2second(message.time, midi.ticks_per_beat, 500_000)
        elapsed = max(elapsed, track_elapsed)
    return elapsed


def _validate_wav(path: Path, minimum_duration: float) -> tuple[int, int, float]:
    with wave.open(str(path), "rb") as stream:
        channels = stream.getnchannels()
        sample_rate = stream.getframerate()
        width = stream.getsampwidth()
        duration = stream.getnframes() / sample_rate
    if sample_rate != SAMPLE_RATE or channels != 2 or width != 2:
        raise RuntimeError("FluidSynth WAV 必须是 44.1 kHz、双声道、PCM16")
    if duration + 1e-6 < minimum_duration:
        raise RuntimeError(f"FluidSynth WAV 过短: {duration:.3f}s < {minimum_duration:.3f}s")
    return sample_rate, channels, duration


def render_midi_to_wav(midi_path: Path, wav_path: Path) -> RenderResult:
    midi_path = midi_path.resolve()
    wav_path = wav_path.resolve()
    wav_path.parent.mkdir(parents=True, exist_ok=True)
    executable = fluidsynth_binary()
    soundfont = soundfont_path()
    minimum_duration = _midi_duration(midi_path) + TAIL_SECONDS
    temporary = wav_path.with_name(f".{wav_path.name}.part")
    temporary.unlink(missing_ok=True)
    env = os.environ.copy()
    env["PATH"] = str(Path(executable).parent) + os.pathsep + env.get("PATH", "")
    try:
        subprocess.run(
            build_fluidsynth_command(executable, soundfont, midi_path, temporary),
            check=True,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        sample_rate, channels, duration = _validate_wav(temporary, minimum_duration)
        temporary.replace(wav_path)
    finally:
        temporary.unlink(missing_ok=True)
    return RenderResult(midi_path, wav_path, None, sample_rate, channels, duration)


def render_midi_to_mp3(midi_path: Path, mp3_path: Path) -> RenderResult:
    wav_path = mp3_path.with_suffix(".wav")
    result = render_midi_to_wav(midi_path, wav_path)
    mp3_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = mp3_path.with_name(f".{mp3_path.name}.part")
    try:
        subprocess.run(
            [ffmpeg_binary(), "-y", "-i", str(wav_path), "-codec:a", "libmp3lame", "-qscale:a", "2", str(temporary)],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        temporary.replace(mp3_path)
    finally:
        temporary.unlink(missing_ok=True)
    return RenderResult(result.midi, result.wav, mp3_path.resolve(), result.sample_rate, result.channels, result.duration_seconds)
