import json
import os
import tempfile
import unittest
import sys
import types
import wave
from pathlib import Path
from unittest import mock

import miditoolkit


class AudioTranscriptionTest(unittest.TestCase):
    def test_builds_beat_this_worker_command_for_cuda(self) -> None:
        from sheet2music.core.audio_transcription import build_beat_this_command

        command = build_beat_this_command(
            python="C:/sheet2music/python.exe",
            wav_path=Path("C:/job/audio/score.wav"),
            output_path=Path("C:/job/audio/beats.json"),
            device="cuda",
            checkpoint_path=Path("C:/models/beat_this-final0.ckpt"),
        )

        self.assertEqual(command[:4], ["C:/sheet2music/python.exe", "-m", "sheet2music.core.audio_worker", "detect-beats"])
        self.assertEqual(command[-4:], ["--device", "cuda", "--checkpoint", "C:\\models\\beat_this-final0.ckpt"])

    def test_beat_this_uses_minimal_postprocessor_without_madmom(self) -> None:
        from sheet2music.core.audio_worker import detect_beats_in_process

        calls = []

        class FakeAudio2Beats:
            def __init__(self, **kwargs):
                calls.append(kwargs)

            def __call__(self, audio, sample_rate):
                return [index * 0.5 for index in range(8)], [0.0, 2.0, 3.5]

        inference = types.ModuleType("beat_this.inference")
        inference.Audio2Beats = FakeAudio2Beats
        package = types.ModuleType("beat_this")
        with tempfile.TemporaryDirectory() as temp_dir:
            wav_path = Path(temp_dir) / "input.wav"
            with wave.open(str(wav_path), "wb") as stream:
                stream.setnchannels(1)
                stream.setsampwidth(2)
                stream.setframerate(44100)
                stream.writeframes(b"\0\0" * 44100)
            with mock.patch.dict(sys.modules, {"beat_this": package, "beat_this.inference": inference}):
                checkpoint = Path(temp_dir) / "final0.ckpt"
                checkpoint.touch()
                detect_beats_in_process(wav_path, Path(temp_dir) / "beats.json", device="cuda", checkpoint_path=checkpoint)

        self.assertEqual(calls, [{"checkpoint_path": str(checkpoint), "device": "cuda", "dbn": False}])

    def test_video_url_accepts_youtube_and_bilibili_only(self) -> None:
        from sheet2music.core.video_audio import validate_video_url

        self.assertEqual(validate_video_url("https://www.youtube.com/watch?v=abc"), "https://www.youtube.com/watch?v=abc")
        self.assertEqual(validate_video_url("https://www.bilibili.com/video/BVabc"), "https://www.bilibili.com/video/BVabc")
        with self.assertRaises(ValueError):
            validate_video_url("https://example.com/video")

    def test_builds_ytdlp_audio_extraction_command(self) -> None:
        from sheet2music.core.video_audio import build_ytdlp_command

        command = build_ytdlp_command("yt-dlp", "https://youtu.be/abc", Path("C:/job/input/score.mp3"))
        self.assertEqual(command[0], "yt-dlp")
        self.assertIn("--extract-audio", command)
        self.assertIn("--audio-format", command)
        self.assertEqual(command[-1], "https://youtu.be/abc")
    def test_audio_job_runs_without_homr_conversion(self) -> None:
        from sheet2music.web.jobs import JobStore

        with tempfile.TemporaryDirectory() as temp_dir:
            store = JobStore(Path(temp_dir) / "jobs")
            record = store.create("song.mp3", input_kind="audio")
            params = mock.MagicMock(use_gpu=False)
            with mock.patch("sheet2music.web.jobs.run_audio_transcription", return_value={"status": "completed"}) as audio_run:
                with mock.patch("sheet2music.web.jobs.run_conversion") as homr_run:
                    store._run(record, params, debug=False)

        audio_run.assert_called_once()
        homr_run.assert_not_called()
        self.assertEqual(record.status.value, "completed")

    def test_transcodes_source_audio_to_stereo_pcm16_wav(self) -> None:
        from sheet2music.core import audio_transcription

        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "song.mp3"
            target = Path(temp_dir) / "song.wav"
            source.touch()
            with mock.patch.object(audio_transcription, "ffmpeg_binary", return_value="ffmpeg"):
                with mock.patch.object(audio_transcription.subprocess, "run", side_effect=lambda *args, **kwargs: target.touch()) as run:
                    audio_transcription.transcode_input_to_wav(source, target)

        self.assertEqual(
            run.call_args.args[0],
            [
                "ffmpeg",
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
        )

    def test_writes_beat_this_result_as_auditable_json(self) -> None:
        from sheet2music.core.audio_transcription import write_beats

        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "beats.json"
            write_beats(target, [0.0, 0.5, 1.0], [0.0], 120.0)
            payload = json.loads(target.read_text(encoding="utf-8"))

        self.assertEqual(payload["beats"], [0.0, 0.5, 1.0])
        self.assertEqual(payload["downbeats"], [0.0])
        self.assertEqual(payload["estimated_bpm"], 120.0)

    def test_audio_pipeline_gives_normalized_wav_to_both_models(self) -> None:
        from sheet2music.core import audio_transcription
        from sheet2music.core.workspace import JobWorkspace

        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = JobWorkspace(Path(temp_dir) / "job").create()
            workspace.audio_path.touch()
            seen: dict[str, Path] = {}

            def transcode(_source: Path, target: Path) -> Path:
                target.touch()
                return target

            def detect(wav_path: Path, output_path: Path, **_kwargs: object) -> Path:
                seen["beats"] = wav_path
                output_path.write_text('{"estimated_bpm": 120}', encoding="utf-8")
                return output_path

            def transkun(audio_path: Path, midi_path: Path, **_kwargs: object) -> Path:
                seen["transkun"] = audio_path
                midi_path.touch()
                return midi_path

            with mock.patch.object(audio_transcription, "transcode_input_to_wav", side_effect=transcode):
                with mock.patch.object(audio_transcription, "detect_beats", side_effect=detect):
                    with mock.patch.object(audio_transcription, "run_transkun", side_effect=transkun):
                        with mock.patch.object(audio_transcription, "rewrite_midi_metadata_preserving_seconds", side_effect=lambda _source, destination, **_kwargs: destination.touch() or destination):
                            with mock.patch.object(audio_transcription, "validate_transkun_midi"):
                                with mock.patch.object(audio_transcription, "render_mp3"):
                                    audio_transcription.run_audio_transcription(workspace, use_gpu=False)

        self.assertEqual(seen, {"beats": workspace.audio_wav_path, "transkun": workspace.audio_wav_path})

    def test_audio_pipeline_preserves_raw_midi_and_renders_normalized_midi(self) -> None:
        from sheet2music.core import audio_transcription
        from sheet2music.core.workspace import JobWorkspace

        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = JobWorkspace(Path(temp_dir) / "job").create()
            workspace.audio_path.touch()
            rendered: list[Path] = []

            def detect(_wav: Path, output: Path, **_kwargs: object) -> Path:
                output.write_text('{"estimated_bpm": 90, "time_signature": [3, 4]}', encoding="utf-8")
                return output

            def transkun(_audio: Path, midi: Path, **_kwargs: object) -> Path:
                midi.touch()
                return midi

            def normalize(source: Path, destination: Path, **_kwargs: object) -> Path:
                self.assertEqual(source, workspace.output_dir / "score.raw.mid")
                destination.touch()
                return destination

            with mock.patch.object(audio_transcription, "transcode_input_to_wav", side_effect=lambda _source, target: target.touch() or target):
                with mock.patch.object(audio_transcription, "detect_beats", side_effect=detect):
                    with mock.patch.object(audio_transcription, "run_transkun", side_effect=transkun):
                        with mock.patch.object(audio_transcription, "rewrite_midi_metadata_preserving_seconds", side_effect=normalize):
                            with mock.patch.object(audio_transcription, "validate_transkun_midi"):
                                with mock.patch.object(audio_transcription, "render_mp3", side_effect=lambda midi, _mp3: rendered.append(midi)):
                                    report = audio_transcription.run_audio_transcription(workspace, use_gpu=False)

            self.assertTrue((workspace.output_dir / "score.raw.mid").exists())
            self.assertEqual(rendered, [workspace.output_dir / "score.mid"])
            self.assertEqual(report["midi"], "output/score.mid")

    def test_builds_real_transkun_v2_command(self) -> None:
        from sheet2music.core.audio_transcription import build_transkun_command

        command = build_transkun_command(
            python="C:/transkun/python.exe",
            weight_path=Path("C:/models/v2/checkpoint.pt"),
            conf_path=Path("C:/models/v2/model.conf"),
            audio_path=Path("C:/job/input/score.mp3"),
            midi_path=Path("C:/job/output/score.mid"),
            device="cuda",
        )

        self.assertEqual(command[:3], ["C:/transkun/python.exe", "-m", "transkun.transcribe"])
        self.assertEqual(command[3:5], [str(Path("C:/job/input/score.mp3")), str(Path("C:/job/output/score.mid"))])
        self.assertIn("--weight", command)
        self.assertIn("--conf", command)
        self.assertEqual(command[-2:], ["--device", "cuda"])

    def test_transkun_prefers_configured_ffmpeg_directory_for_pydub(self) -> None:
        from sheet2music.core import audio_transcription

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            audio = root / "input.mp3"
            midi = root / "output.mid"
            audio.touch()
            with mock.patch.object(audio_transcription, "transkun_root", return_value=root):
                with mock.patch.object(audio_transcription, "transkun_model_files", return_value=(root / "model.pt", root / "model.conf")):
                    with mock.patch.object(audio_transcription, "ffmpeg_binary", return_value=str(root / "tools" / "ffmpeg.exe")):
                        with mock.patch.object(audio_transcription, "validate_transkun_midi"):
                            with mock.patch.object(audio_transcription.subprocess, "run") as run:
                                audio_transcription.run_transkun(audio, midi, model="v2", use_gpu=True)

        self.assertEqual(run.call_args.kwargs["env"]["PATH"].split(os.pathsep)[0], str(root / "tools"))

    def test_transkun_passes_absolute_audio_and_output_paths_to_its_child_process(self) -> None:
        from sheet2music.core import audio_transcription

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            audio = root / "nested" / "input.wav"
            midi = root / "nested" / "output.mid"
            audio.parent.mkdir()
            audio.touch()
            with mock.patch.object(audio_transcription, "transkun_root", return_value=root):
                with mock.patch.object(audio_transcription, "transkun_model_files", return_value=(root / "model.pt", root / "model.conf")):
                    with mock.patch.object(audio_transcription, "ffmpeg_binary", return_value=str(root / "ffmpeg.exe")):
                        with mock.patch.object(audio_transcription, "validate_transkun_midi"):
                            with mock.patch.object(audio_transcription.subprocess, "run") as run:
                                audio_transcription.run_transkun(audio.relative_to(root), midi.relative_to(root), model="v2", use_gpu=False)

        self.assertEqual(
            run.call_args.args[0][3:5],
            [str((Path.cwd() / "nested" / "input.wav").resolve()), str((Path.cwd() / "nested" / "output.mid").resolve())],
        )

    def test_rejects_midi_without_notes_without_modifying_the_file(self) -> None:
        from sheet2music.core.audio_transcription import validate_transkun_midi

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "empty.mid"
            miditoolkit.MidiFile().dump(str(path))
            original = path.read_bytes()
            with self.assertRaisesRegex(ValueError, "音符"):
                validate_transkun_midi(path)
            self.assertEqual(path.read_bytes(), original)
