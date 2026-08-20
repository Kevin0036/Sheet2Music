"""Reference-compatible MIDI playback rendering for downloads and GUI previews."""

from __future__ import annotations

import os
import subprocess
import tempfile
import wave
from dataclasses import dataclass
from pathlib import Path
from bisect import bisect_right
from math import isfinite

import mido

from .settings import ffmpeg_binary, fluidsynth_binary, soundfont_path

SAMPLE_RATE = 44_100
TAIL_SECONDS = 0.03
DEFAULT_TEMPO_US = 500_000


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


def _tempo_sections(midi: mido.MidiFile, track_index: int) -> list[tuple[int, float, int]]:
    """Build the effective tick/second/tempo map for one MIDI timeline."""

    source_tracks = (
        ((track_index, midi.tracks[track_index]),)
        if midi.type == 2
        else tuple(enumerate(midi.tracks))
    )
    tempo_events: list[tuple[int, int, int, int]] = []
    for source_track_index, track in source_tracks:
        absolute_tick = 0
        for sequence, message in enumerate(track):
            absolute_tick += int(message.time)
            if message.is_meta and message.type == "set_tempo":
                tempo_events.append((absolute_tick, source_track_index, sequence, int(message.tempo)))

    collapsed: dict[int, int] = {}
    for tick, _track_index, _sequence, tempo in sorted(tempo_events):
        collapsed[tick] = tempo

    sections: list[tuple[int, float, int]] = [(0, 0.0, DEFAULT_TEMPO_US)]
    for event_tick, event_tempo in sorted(collapsed.items()):
        previous_tick, previous_seconds, previous_tempo = sections[-1]
        if event_tick == previous_tick:
            sections[-1] = (event_tick, previous_seconds, event_tempo)
            continue
        event_seconds = previous_seconds + mido.tick2second(
            event_tick - previous_tick,
            midi.ticks_per_beat,
            previous_tempo,
        )
        sections.append((event_tick, event_seconds, event_tempo))
    return sections


def _tick_to_seconds(
    midi: mido.MidiFile,
    sections: list[tuple[int, float, int]],
    tick: int,
) -> float:
    section_ticks = [start_tick for start_tick, _seconds, _tempo in sections]
    index = bisect_right(section_ticks, int(tick)) - 1
    start_tick, start_seconds, tempo = sections[index]
    return float(
        start_seconds
        + mido.tick2second(tick - start_tick, midi.ticks_per_beat, tempo)
    )


def _midi_duration(path: Path) -> float:
    """Return the last real MIDI event time, respecting the effective tempo map.

    ``end_of_track`` may intentionally be delayed and does not make FluidSynth
    render silence.  Only channel events (notes, pedals, controllers, etc.)
    define the minimum audio boundary.
    """

    midi = mido.MidiFile(str(path))
    elapsed = 0.0
    for track_index, track in enumerate(midi.tracks):
        sections = _tempo_sections(midi, track_index)
        absolute_tick = 0
        for message in track:
            absolute_tick += int(message.time)
            if not message.is_meta:
                elapsed = max(elapsed, _tick_to_seconds(midi, sections, absolute_tick))
    return elapsed


def wav_duration_seconds(path: Path) -> float:
    with wave.open(str(path), "rb") as stream:
        return stream.getnframes() / stream.getframerate()


def _fit_wav_duration(path: Path, target_duration: float) -> None:
    if not isfinite(target_duration) or target_duration < 0.0:
        raise ValueError(f"Invalid WAV target duration: {target_duration!r}")
    temporary = path.with_name(f".{path.name}.fit")
    temporary.unlink(missing_ok=True)
    try:
        with wave.open(str(path), "rb") as source:
            params = source.getparams()
            target_frames = int(round(target_duration * params.framerate))
            source_frames = source.getnframes()
            with wave.open(str(temporary), "wb") as output:
                output.setparams(params)
                remaining = target_frames
                while remaining > 0 and source_frames > 0:
                    chunk = source.readframes(min(remaining, 65_536))
                    if not chunk:
                        break
                    output.writeframesraw(chunk)
                    written = len(chunk) // params.sampwidth // params.nchannels
                    remaining -= written
                    source_frames -= written
                if remaining > 0:
                    output.writeframesraw(b"\0" * remaining * params.sampwidth * params.nchannels)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


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


def render_midi_to_wav(
    midi_path: Path,
    wav_path: Path,
    *,
    target_duration: float | None = None,
) -> RenderResult:
    midi_path = midi_path.resolve()
    wav_path = wav_path.resolve()
    wav_path.parent.mkdir(parents=True, exist_ok=True)
    executable = fluidsynth_binary()
    soundfont = soundfont_path()
    event_duration = _midi_duration(midi_path)
    minimum_duration = event_duration + (0.0 if target_duration is not None else TAIL_SECONDS)
    if target_duration is not None and target_duration + 1e-6 < event_duration:
        raise RuntimeError(
            f"WAV transport 边界早于 MIDI 最后事件: {target_duration:.3f}s < {event_duration:.3f}s"
        )
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
        if target_duration is not None:
            _fit_wav_duration(temporary, target_duration)
            sample_rate, channels, duration = _validate_wav(temporary, minimum_duration)
        temporary.replace(wav_path)
    finally:
        temporary.unlink(missing_ok=True)
    return RenderResult(midi_path, wav_path, None, sample_rate, channels, duration)


def render_midi_to_mp3(
    midi_path: Path,
    mp3_path: Path,
    *,
    target_duration: float | None = None,
) -> RenderResult:
    wav_path = mp3_path.with_suffix(".wav")
    result = render_midi_to_wav(midi_path, wav_path, target_duration=target_duration)
    mp3_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = mp3_path.with_name(f".{mp3_path.stem}.part{mp3_path.suffix}")
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
        wav_path.unlink(missing_ok=True)
    return RenderResult(result.midi, result.wav, mp3_path.resolve(), result.sample_rate, result.channels, result.duration_seconds)
