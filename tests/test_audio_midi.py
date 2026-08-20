import tempfile
import unittest
from pathlib import Path

import mido


def event_times(path: Path) -> list[tuple[str, int, float]]:
    midi = mido.MidiFile(str(path))
    tempo = 500_000
    absolute_tick = 0
    seconds = 0.0
    events: list[tuple[str, int, float]] = []
    for message in midi.tracks[0]:
        seconds += mido.tick2second(message.time, midi.ticks_per_beat, tempo)
        absolute_tick += message.time
        if message.type == "set_tempo":
            tempo = message.tempo
        elif message.type in {"note_on", "note_off", "control_change"}:
            events.append((message.type, absolute_tick, seconds))
    return events


class AudioMidiTest(unittest.TestCase):
    def test_rewrite_preserves_non_tempo_event_seconds_and_writes_metadata(self) -> None:
        from sheet2music.core.audio_midi import rewrite_midi_metadata_preserving_seconds

        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "source.mid"
            target = Path(temp_dir) / "target.mid"
            midi = mido.MidiFile(ticks_per_beat=480)
            track = mido.MidiTrack()
            track.extend(
                [
                    mido.MetaMessage("set_tempo", tempo=500_000, time=0),
                    mido.Message("note_on", note=60, velocity=80, time=480),
                    mido.Message("control_change", control=64, value=127, time=240),
                    mido.Message("note_off", note=60, velocity=0, time=240),
                ]
            )
            midi.tracks.append(track)
            midi.save(source)

            rewrite_midi_metadata_preserving_seconds(source, target, bpm=90.0, time_signature=(3, 4))
            rewritten = mido.MidiFile(str(target))
            expected = event_times(source)
            actual = event_times(target)
            self.assertEqual([item[0] for item in actual], [item[0] for item in expected])
            for before, after in zip(expected, actual):
                self.assertAlmostEqual(before[2], after[2], places=3)
            metadata = [message for message in rewritten.tracks[0] if message.is_meta]
            self.assertEqual([message.tempo for message in metadata if message.type == "set_tempo"], [mido.bpm2tempo(90.0)])
            self.assertEqual([(message.numerator, message.denominator) for message in metadata if message.type == "time_signature"], [(3, 4)])

    def test_rewrite_can_preserve_seconds_with_a_variable_tempo_map(self) -> None:
        from sheet2music.core.audio_midi import rewrite_midi_metadata_preserving_seconds

        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "source.mid"
            target = Path(temp_dir) / "target.mid"
            midi = mido.MidiFile(ticks_per_beat=480)
            track = mido.MidiTrack()
            track.extend(
                [
                    mido.MetaMessage("set_tempo", tempo=500_000, time=0),
                    mido.Message("note_on", note=60, velocity=80, time=480),
                    mido.Message("note_off", note=60, velocity=0, time=960),
                ]
            )
            midi.tracks.append(track)
            midi.save(source)

            rewrite_midi_metadata_preserving_seconds(
                source,
                target,
                bpm=120.0,
                time_signature=None,
                tempo_map=[(0.0, 120.0), (1.0, 60.0)],
            )
            rewritten = mido.MidiFile(str(target))
            tempos = [message.tempo for message in rewritten.tracks[0] if message.type == "set_tempo"]
            self.assertEqual(tempos, [mido.bpm2tempo(120.0), mido.bpm2tempo(60.0)])
            self.assertAlmostEqual(rewritten.length, mido.MidiFile(str(source)).length, places=3)
