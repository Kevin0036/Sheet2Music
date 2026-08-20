"""Isolated PyTorch worker for Beat This and CUDA runtime probing."""

from __future__ import annotations

import argparse
import json
import wave
from pathlib import Path


def detect_beats_in_process(wav_path: Path, output_path: Path, device: str, checkpoint_path: Path) -> Path:
    try:
        import numpy as np
        from beat_this.inference import Audio2Beats
    except ImportError as exc:
        raise RuntimeError("未安装 Beat This；请在 Transkun 环境中安装 beat_this") from exc

    try:
        with wave.open(str(wav_path), "rb") as stream:
            sample_rate = stream.getframerate()
            channels = stream.getnchannels()
            frames = stream.readframes(stream.getnframes())
        audio = np.frombuffer(frames, dtype="<i2").astype("float64") / 32768.0
        if channels > 1:
            audio = audio.reshape(-1, channels).mean(axis=1)
    except (wave.Error, OSError) as exc:
        raise RuntimeError(f"无法读取 Beat This 输入 WAV: {wav_path}") from exc

    beats, downbeats = Audio2Beats(checkpoint_path=str(checkpoint_path), device=device, dbn=False)(audio, sample_rate)

    from .beat_grid import analyze_beat_this_grid

    payload = analyze_beat_this_grid(beats, downbeats)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return output_path


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    beat_parser = subparsers.add_parser("detect-beats")
    beat_parser.add_argument("wav_path", type=Path)
    beat_parser.add_argument("output_path", type=Path)
    beat_parser.add_argument("--device", choices=("cpu", "cuda"), required=True)
    beat_parser.add_argument("--checkpoint", type=Path, required=True)
    subparsers.add_parser("torch-status")
    args = parser.parse_args()

    if args.command == "detect-beats":
        detect_beats_in_process(args.wav_path, args.output_path, args.device, args.checkpoint)
        return

    from .system import pytorch_cuda_status

    print(json.dumps(pytorch_cuda_status(), ensure_ascii=True))


if __name__ == "__main__":
    main()
