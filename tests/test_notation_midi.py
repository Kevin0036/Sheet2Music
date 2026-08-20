import tempfile
import unittest
from collections import Counter
from pathlib import Path

import mido


def _note_events(path: Path) -> list[tuple[int, int, int, int, int]]:
    midi = mido.MidiFile(str(path))
    result: list[tuple[int, int, int, int, int]] = []
    for track in midi.tracks:
        tick = 0
        active: dict[tuple[int, int], list[tuple[int, int, int]]] = {}
        for message in track:
            tick += message.time
            if message.type == "note_on" and message.velocity > 0:
                active.setdefault((message.channel, message.note), []).append(
                    (tick, message.velocity, message.note)
                )
            elif message.type == "note_off" or (message.type == "note_on" and message.velocity == 0):
                onset_tick, velocity, pitch = active[(message.channel, message.note)].pop(0)
                result.append((pitch, onset_tick, tick, velocity, message.channel))
    return sorted(result)


def _track_notes(path: Path, name: str) -> list[int]:
    midi = mido.MidiFile(str(path))
    for track in midi.tracks:
        if any(message.type == "track_name" and message.name == name for message in track):
            return [
                message.note
                for message in track
                if message.type == "note_on" and message.velocity > 0
            ]
    return []


class NotationMidiTest(unittest.TestCase):
    def test_split_preserves_every_note_and_routes_by_computed_boundary(self) -> None:
        from sheet2music.core.notation_midi import split_midi_for_piano_notation

        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "source.mid"
            destination = Path(temp_dir) / "notation.mid"
            midi = mido.MidiFile(type=1, ticks_per_beat=480)
            conductor = mido.MidiTrack()
            conductor.extend(
                [
                    mido.MetaMessage("track_name", name="Conductor", time=0),
                    mido.MetaMessage("set_tempo", tempo=500_000, time=0),
                    mido.MetaMessage("end_of_track", time=1920),
                ]
            )
            notes = mido.MidiTrack()
            notes.extend(
                [
                    mido.MetaMessage("track_name", name="Transkun", time=0),
                    mido.Message("program_change", channel=0, program=0, time=0),
                    mido.Message("note_on", channel=0, note=48, velocity=70, time=0),
                    mido.Message("note_on", channel=0, note=55, velocity=75, time=0),
                    mido.Message("note_on", channel=0, note=57, velocity=78, time=0),
                    mido.Message("note_on", channel=0, note=60, velocity=80, time=0),
                    mido.Message("note_off", channel=0, note=48, velocity=0, time=480),
                    mido.Message("note_off", channel=0, note=55, velocity=0, time=0),
                    mido.Message("note_off", channel=0, note=57, velocity=0, time=0),
                    mido.Message("note_off", channel=0, note=60, velocity=0, time=0),
                    mido.Message("note_on", channel=0, note=64, velocity=90, time=240),
                    mido.Message("note_on", channel=0, note=72, velocity=100, time=0),
                    mido.Message("note_off", channel=0, note=64, velocity=0, time=480),
                    mido.Message("note_off", channel=0, note=72, velocity=0, time=0),
                    mido.MetaMessage("end_of_track", time=0),
                ]
            )
            midi.tracks.extend([conductor, notes])
            midi.save(source)
            original_bytes = source.read_bytes()

            result = split_midi_for_piano_notation(source, destination)

            self.assertEqual(result.split_note, 60)
            self.assertEqual(source.read_bytes(), original_bytes)
            self.assertEqual(_note_events(destination), _note_events(source))
            self.assertEqual(Counter(_track_notes(destination, "Piano Left Hand")), Counter([48, 55, 57, 60]))
            self.assertEqual(Counter(_track_notes(destination, "Piano Right Hand")), Counter([64, 72]))

    def test_split_preserves_pedal_events_in_both_hand_tracks(self) -> None:
        from sheet2music.core.notation_midi import split_midi_for_piano_notation

        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "source.mid"
            destination = Path(temp_dir) / "notation.mid"
            midi = mido.MidiFile(type=1, ticks_per_beat=480)
            midi.tracks.append(mido.MidiTrack([mido.MetaMessage("end_of_track", time=0)]))
            midi.tracks.append(
                mido.MidiTrack(
                    [
                        mido.Message("control_change", channel=0, control=64, value=127, time=0),
                        mido.Message("note_on", channel=0, note=48, velocity=70, time=0),
                        mido.Message("note_off", channel=0, note=48, velocity=0, time=480),
                        mido.MetaMessage("end_of_track", time=0),
                    ]
                )
            )
            midi.save(source)

            split_midi_for_piano_notation(source, destination)
            notation = mido.MidiFile(str(destination))
            pedal_tracks = [
                track
                for track in notation.tracks
                if any(message.type == "control_change" and message.control == 64 for message in track)
            ]
            self.assertEqual(len(pedal_tracks), 2)


if __name__ == "__main__":
    unittest.main()
