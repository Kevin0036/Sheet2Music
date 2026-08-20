"""Create a two-hand MIDI derivative for piano notation.

The downloadable playback MIDI is intentionally left untouched. This module only
routes the original note events to two named piano tracks so MuseScore does not
have to guess a hand split while importing a single Transkun track.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict, deque
from dataclasses import dataclass
from pathlib import Path

import mido


@dataclass(frozen=True)
class NoteEvent:
    track_index: int
    sequence: int
    channel: int
    pitch: int
    velocity: int
    start_tick: int
    end_tick: int


@dataclass(frozen=True)
class HandSplitResult:
    split_note: int
    left_note_count: int
    right_note_count: int

    def to_dict(self) -> dict[str, int | str]:
        return {
            "policy": "weighted_pitch_distribution",
            "split_note": self.split_note,
            "left_note_count": self.left_note_count,
            "right_note_count": self.right_note_count,
        }


def _is_note_on(message: mido.Message | mido.MetaMessage) -> bool:
    return message.type == "note_on" and message.velocity > 0


def _is_note_off(message: mido.Message | mido.MetaMessage) -> bool:
    return message.type == "note_off" or (message.type == "note_on" and message.velocity == 0)


def _read_note_events(midi: mido.MidiFile) -> list[NoteEvent]:
    events: list[NoteEvent] = []
    for track_index, track in enumerate(midi.tracks):
        absolute_tick = 0
        active: dict[tuple[int, int], deque[tuple[int, int, int, int]]] = defaultdict(deque)
        for sequence, message in enumerate(track):
            absolute_tick += int(message.time)
            if _is_note_on(message):
                active[(message.channel, message.note)].append(
                    (sequence, absolute_tick, message.velocity, message.note)
                )
            elif _is_note_off(message):
                key = (message.channel, message.note)
                if not active[key]:
                    raise ValueError(
                        f"MIDI note-off has no matching note-on: track={track_index}, "
                        f"sequence={sequence}, pitch={message.note}"
                    )
                onset_sequence, onset_tick, velocity, pitch = active[key].popleft()
                if absolute_tick <= onset_tick:
                    raise ValueError(
                        f"MIDI note duration is not positive: track={track_index}, "
                        f"sequence={sequence}, pitch={message.note}"
                    )
                events.append(
                    NoteEvent(
                        track_index=track_index,
                        sequence=onset_sequence,
                        channel=message.channel,
                        pitch=pitch,
                        velocity=velocity,
                        start_tick=onset_tick,
                        end_tick=absolute_tick,
                    )
                )
        dangling = sum(len(queue) for queue in active.values())
        if dangling:
            raise ValueError(f"MIDI contains {dangling} note-on event(s) without note-off")
    return events


def _weighted_variance_score(
    count: int,
    pitch_sum: int,
    pitch_square_sum: int,
) -> float:
    if count <= 1:
        return 0.0
    return pitch_square_sum - (pitch_sum * pitch_sum) / count


def _optimal_pitch_boundary(events: list[NoteEvent]) -> int:
    if not events:
        raise ValueError("MIDI 不包含音符")
    counts = Counter(event.pitch for event in events)
    pitches = sorted(counts)
    if len(pitches) == 1:
        return pitches[0] - 1

    total_count = len(events)
    total_sum = sum(pitch * count for pitch, count in counts.items())
    total_square_sum = sum(pitch * pitch * count for pitch, count in counts.items())
    left_count = 0
    left_sum = 0
    left_square_sum = 0
    candidates: list[tuple[float, int, int]] = []
    for pitch in pitches[:-1]:
        count = counts[pitch]
        left_count += count
        left_sum += pitch * count
        left_square_sum += pitch * pitch * count
        right_count = total_count - left_count
        right_sum = total_sum - left_sum
        right_square_sum = total_square_sum - left_square_sum
        score = _weighted_variance_score(left_count, left_sum, left_square_sum)
        score += _weighted_variance_score(right_count, right_sum, right_square_sum)
        # Middle C is only a deterministic tie-breaker, never a hard threshold.
        candidates.append((score, abs(pitch - 60), pitch))
    return min(candidates)[2]


def _absolute_track_events(
    track: mido.MidiTrack,
) -> list[tuple[int, int, mido.Message | mido.MetaMessage]]:
    absolute_tick = 0
    events: list[tuple[int, int, mido.Message | mido.MetaMessage]] = []
    for sequence, message in enumerate(track):
        absolute_tick += int(message.time)
        events.append((absolute_tick, sequence, message))
    return events


def _write_track(
    events: list[tuple[int, int, int, mido.Message | mido.MetaMessage]],
    *,
    end_tick: int,
) -> mido.MidiTrack:
    track = mido.MidiTrack()
    previous_tick = 0
    for tick, priority, sequence, message in sorted(events, key=lambda item: (item[0], item[1], item[2])):
        del priority
        track.append(message.copy(time=tick - previous_tick))
        previous_tick = tick
    track.append(mido.MetaMessage("end_of_track", time=max(0, end_tick - previous_tick)))
    return track


def split_midi_for_piano_notation(source: Path, destination: Path) -> HandSplitResult:
    """Write a two-hand notation MIDI while preserving all source note events."""

    source_midi = mido.MidiFile(str(source))
    note_events = _read_note_events(source_midi)
    split_note = _optimal_pitch_boundary(note_events)
    assignments: dict[tuple[int, int], str] = {}
    note_off_assignments: dict[tuple[int, int], str] = {}
    active: dict[tuple[int, int, int], deque[str]] = defaultdict(deque)
    for track_index, track in enumerate(source_midi.tracks):
        for sequence, message in enumerate(track):
            if _is_note_on(message):
                hand = "right" if message.note > split_note else "left"
                assignments[(track_index, sequence)] = hand
                active[(track_index, message.channel, message.note)].append(hand)
            elif _is_note_off(message):
                key = (track_index, message.channel, message.note)
                if not active[key]:
                    raise ValueError(
                        f"MIDI note-off has no matching note-on: track={track_index}, "
                        f"sequence={sequence}, pitch={message.note}"
                    )
                note_off_assignments[(track_index, sequence)] = active[key].popleft()

    end_tick = max(
        (tick for track in source_midi.tracks for tick, _sequence, _message in _absolute_track_events(track)),
        default=0,
    )
    conductor_events: list[tuple[int, int, int, mido.Message | mido.MetaMessage]] = []
    hand_events: dict[str, list[tuple[int, int, int, mido.Message | mido.MetaMessage]]] = {
        "left": [],
        "right": [],
    }
    for track_index, track in enumerate(source_midi.tracks):
        for tick, sequence, message in _absolute_track_events(track):
            if _is_note_on(message):
                hand = assignments[(track_index, sequence)]
                hand_events[hand].append((tick, 2, sequence, message.copy(time=0)))
                continue
            if _is_note_off(message):
                hand = note_off_assignments[(track_index, sequence)]
                hand_events[hand].append((tick, 1, sequence, message.copy(time=0)))
                continue
            if message.is_meta:
                if message.type != "end_of_track":
                    # Track names are replaced by explicit hand names below.
                    if message.type != "track_name" or track_index == 0:
                        conductor_events.append((tick, 0, track_index * 1_000_000 + sequence, message.copy(time=0)))
                continue
            # Controllers and program changes affect the piano performance, so
            # duplicate them into each notation hand track without changing them.
            order = track_index * 1_000_000 + sequence
            for hand in hand_events:
                hand_events[hand].append((tick, 0, order, message.copy(time=0)))

    for hand, events in hand_events.items():
        name = "Piano Left Hand" if hand == "left" else "Piano Right Hand"
        named_events = [(0, 0, -2, mido.MetaMessage("track_name", name=name, time=0))]
        named_events.extend(events)
        # MuseScore uses the program when choosing the imported instrument.
        if not any(message.type == "program_change" for _tick, _priority, _seq, message in events):
            named_events.append((0, 0, -1, mido.Message("program_change", channel=0, program=0, time=0)))
        conductor_events.sort(key=lambda item: (item[0], item[1], item[2]))
        hand_events[hand] = named_events

    output = mido.MidiFile(type=1, ticks_per_beat=source_midi.ticks_per_beat)
    output.tracks.append(_write_track(conductor_events, end_tick=end_tick))
    output.tracks.append(_write_track(hand_events["right"], end_tick=end_tick))
    output.tracks.append(_write_track(hand_events["left"], end_tick=end_tick))
    destination.parent.mkdir(parents=True, exist_ok=True)
    output.save(str(destination))
    return HandSplitResult(
        split_note=split_note,
        left_note_count=sum(1 for event in note_events if event.pitch <= split_note),
        right_note_count=sum(1 for event in note_events if event.pitch > split_note),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Split a piano MIDI into left/right notation tracks")
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    result = split_midi_for_piano_notation(args.source, args.destination)
    print(result.to_dict())


if __name__ == "__main__":
    main()
