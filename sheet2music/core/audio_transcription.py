"""Audio-to-MIDI pipeline adapters for Beat This and Transkun V2."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Callable

import miditoolkit

from .audio_midi import rewrite_midi_metadata_preserving_seconds
from .export import export_pdf, render_mp3
from .fluidsynth_renderer import wav_duration_seconds
from .notation_midi import split_midi_for_piano_notation
from .settings import beat_this_checkpoint, ffmpeg_binary, transkun_model_files, transkun_python, transkun_root
from .workspace import JobWorkspace, write_report
from .video_audio import download_video_audio


def transcode_input_to_wav(source: Path, target: Path) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            ffmpeg_binary(),
            "-y",
            "-i",
            str(source),
            "-ar",
            "44100",
            "-ac",
            "2",
            "-sample_fmt",
            "s16",
            str(target),
        ],
        check=True,
    )
    if not target.exists():
        raise FileNotFoundError(f"ffmpeg did not emit {target}")
    return target


def write_beats(path: Path, beats: object, downbeats: object, estimated_bpm: float | None) -> Path:
    payload = {"schema_version": 1, "beats": [float(value) for value in beats], "downbeats": [float(value) for value in downbeats], "estimated_bpm": estimated_bpm}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def build_beat_this_command(*, python: str, wav_path: Path, output_path: Path, device: str, checkpoint_path: Path) -> list[str]:
    return [
        python,
        "-m",
        "sheet2music.core.audio_worker",
        "detect-beats",
        str(wav_path),
        str(output_path),
        "--device",
        device,
        "--checkpoint",
        str(checkpoint_path),
    ]


def detect_beats(wav_path: Path, output_path: Path, use_gpu: bool = False) -> Path:
    command = build_beat_this_command(
        python=transkun_python(),
        wav_path=wav_path,
        output_path=output_path,
        device="cuda" if use_gpu else "cpu",
        checkpoint_path=beat_this_checkpoint(),
    )
    subprocess.run(command, check=True)
    if not output_path.is_file():
        raise FileNotFoundError(f"Beat This 未生成节拍结果: {output_path}")
    return output_path


def build_transkun_command(*, python: str, weight_path: Path, conf_path: Path, audio_path: Path, midi_path: Path, device: str) -> list[str]:
    return [python, "-m", "transkun.transcribe", str(audio_path), str(midi_path), "--weight", str(weight_path), "--conf", str(conf_path), "--device", device]


def run_transkun(audio_path: Path, midi_path: Path, *, model: str, use_gpu: bool) -> Path:
    audio_path = audio_path.resolve()
    midi_path = midi_path.resolve()
    root = transkun_root()
    weight_path, conf_path = transkun_model_files(model)
    command = build_transkun_command(python=transkun_python(), weight_path=weight_path, conf_path=conf_path, audio_path=audio_path, midi_path=midi_path, device="cuda" if use_gpu else "cpu")
    environment = os.environ.copy()
    # pydub invokes both ffmpeg and ffprobe by executable name.
    ffmpeg_dir = str(Path(ffmpeg_binary()).resolve().parent)
    environment["PATH"] = ffmpeg_dir + os.pathsep + environment.get("PATH", "")
    environment["PYTHONPATH"] = str(root) + os.pathsep + environment.get("PYTHONPATH", "")
    midi_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(command, check=True, cwd=root, env=environment)
    validate_transkun_midi(midi_path)
    return midi_path


def validate_transkun_midi(path: Path) -> None:
    if not path.is_file():
        raise FileNotFoundError(f"Transkun 未生成 MIDI: {path}")
    midi = miditoolkit.MidiFile(str(path))
    notes = [note for instrument in midi.instruments for note in instrument.notes]
    if not notes:
        raise ValueError("Transkun MIDI 不包含音符")
    if midi.ticks_per_beat <= 0 or any(note.start < 0 or note.end <= note.start for note in notes):
        raise ValueError("Transkun MIDI 包含无效音符时值")


def run_audio_transcription(
    workspace: JobWorkspace,
    *,
    use_gpu: bool,
    transkun_model: str = "v2",
    generate_pdf: bool = False,
    stage: Callable[[str], None] | None = None,
) -> dict[str, object]:
    emit = stage or (lambda _name: None)
    source_url = None
    if workspace.source_url_path.exists():
        source_url = workspace.source_url_path.read_text(encoding="utf-8").strip()
        emit("downloading_video_audio")
        download_video_audio(source_url, workspace.audio_path)
    emit("converting_audio")
    transcode_input_to_wav(workspace.audio_path, workspace.audio_wav_path)
    source_duration = wav_duration_seconds(workspace.audio_wav_path)
    emit("detecting_beats")
    detect_beats(workspace.audio_wav_path, workspace.beats_path, use_gpu=use_gpu)
    emit("running_transkun")
    raw_midi_path = workspace.output_dir / "score.raw.mid"
    midi_path = workspace.output_dir / "score.mid"
    run_transkun(workspace.audio_wav_path, raw_midi_path, model=transkun_model, use_gpu=use_gpu)
    beat_detection = json.loads(workspace.beats_path.read_text(encoding="utf-8"))
    rewrite_midi_metadata_preserving_seconds(
        raw_midi_path,
        midi_path,
        bpm=float(beat_detection["estimated_bpm"]),
        time_signature=tuple(beat_detection["time_signature"]) if beat_detection.get("time_signature") else None,
        tempo_map=[tuple(entry) for entry in beat_detection.get("tempo_map", [])],
    )
    validate_transkun_midi(midi_path)
    emit("rendering_mp3")
    render_mp3(
        midi_path,
        workspace.output_dir / "score.mp3",
        target_duration=source_duration,
    )
    if generate_pdf:
        emit("rendering_pdf")
        notation_midi_path = workspace.output_dir / "score.notation.mid"
        notation_hand_split = split_midi_for_piano_notation(midi_path, notation_midi_path)
        export_pdf(notation_midi_path, workspace.output_dir / "score.pdf")
    workspace.audio_wav_path.unlink(missing_ok=True)
    weight_path, conf_path = transkun_model_files(transkun_model)
    report = {
        "status": "completed",
        "input_kind": "video_url" if source_url else "audio",
        "source_url": source_url,
        "transkun_model": transkun_model,
        "transkun_model_files": {"weight": str(weight_path), "conf": str(conf_path)},
        "beat_detection": beat_detection,
        "source_duration_seconds": source_duration,
        "raw_midi": "output/score.raw.mid",
        "midi": "output/score.mid",
        "mp3": "output/score.mp3",
    }
    if generate_pdf:
        report["pdf"] = "output/score.pdf"
        report["notation_midi"] = "output/score.notation.mid"
        report["notation_hand_split"] = notation_hand_split.to_dict()
    write_report(workspace, report)
    emit("completed")
    return report
