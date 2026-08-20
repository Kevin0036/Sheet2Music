import tempfile
import unittest
from pathlib import Path
from unittest import mock

import mido

from sheet2music.core import fluidsynth_renderer


class FluidSynthRendererTest(unittest.TestCase):
    def test_builds_reference_command(self) -> None:
        command = fluidsynth_renderer.build_fluidsynth_command(
            "fluidsynth.exe", Path("piano.sf2"), Path("score.mid"), Path("score.wav")
        )
        self.assertEqual(command, ["fluidsynth.exe", "-ni", "-F", "score.wav", "-r", "44100", "piano.sf2", "score.mid"])

    def test_rejects_non_reference_wav_format(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "bad.wav"
            with fluidsynth_renderer.wave.open(str(path), "wb") as stream:
                stream.setnchannels(1)
                stream.setsampwidth(2)
                stream.setframerate(22050)
                stream.writeframes(b"\0" * 100)
            with self.assertRaisesRegex(RuntimeError, "44.1 kHz"):
                fluidsynth_renderer._validate_wav(path, 0.0)

    def test_render_uses_fluid_synth_and_validates_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            midi = mido.MidiFile(ticks_per_beat=480)
            track = mido.MidiTrack()
            midi.tracks.append(track)
            track.append(mido.Message("note_on", note=60, velocity=90, time=0))
            track.append(mido.Message("note_off", note=60, velocity=0, time=480))
            source = root / "score.mid"
            destination = root / "score.wav"
            midi.save(source)
            with mock.patch.object(fluidsynth_renderer, "fluidsynth_binary", return_value="fluidsynth.exe"), mock.patch.object(
                fluidsynth_renderer, "soundfont_path", return_value=root / "piano.sf2"
            ), mock.patch.object(fluidsynth_renderer.subprocess, "run") as run:
                def fake_run(command, **kwargs):
                    wav = Path(command[command.index("-F") + 1])
                    with fluidsynth_renderer.wave.open(str(wav), "wb") as stream:
                        stream.setnchannels(2)
                        stream.setsampwidth(2)
                        stream.setframerate(44100)
                        stream.writeframes(b"\0" * 44100 * 2 * 2)
                run.side_effect = fake_run
                result = fluidsynth_renderer.render_midi_to_wav(source, destination)
            self.assertEqual(result.sample_rate, 44100)
            self.assertEqual(result.channels, 2)
            self.assertIn("-ni", run.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
