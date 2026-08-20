"""MIDI conductor metadata rewrites that preserve playback seconds."""

from __future__ import annotations

from pathlib import Path

import mido


def _event_seconds(track: mido.MidiTrack, ticks_per_beat: int) -> list[tuple[mido.Message | mido.MetaMessage, float]]:
    tempo = 500_000
    seconds = 0.0
    events: list[tuple[mido.Message | mido.MetaMessage, float]] = []
    for message in track:
        seconds += mido.tick2second(message.time, ticks_per_beat, tempo)
        if message.is_meta and message.type == "set_tempo":
            tempo = message.tempo
        if message.is_meta and message.type in {"set_tempo", "time_signature", "end_of_track"}:
            continue
        events.append((message.copy(time=0), seconds))
    return events


def rewrite_midi_metadata_preserving_seconds(
    source: Path,
    destination: Path,
    *,
    bpm: float,
    time_signature: tuple[int, int] | None,
) -> Path:
    """Write global tempo/meter while retaining every non-metadata event second."""

    if bpm <= 0.0:
        raise ValueError(f"Invalid BPM: {bpm}")
    if time_signature is not None:
        numerator, denominator = time_signature
        if numerator <= 0 or denominator <= 0 or denominator & (denominator - 1):
            raise ValueError(f"Invalid time signature: {time_signature}")
    source_midi = mido.MidiFile(str(source))
    if source_midi.ticks_per_beat <= 0:
        raise ValueError("MIDI ticks_per_beat must be positive")
    target_tempo = mido.bpm2tempo(bpm)
    output = mido.MidiFile(type=source_midi.type, ticks_per_beat=source_midi.ticks_per_beat)
    for index, source_track in enumerate(source_midi.tracks):
        events = _event_seconds(source_track, source_midi.ticks_per_beat)
        entries: list[tuple[int, int, int, mido.Message | mido.MetaMessage]] = []
        if index == 0:
            entries.append((0, 0, 0, mido.MetaMessage("set_tempo", tempo=target_tempo, time=0)))
            if time_signature is not None:
                entries.append(
                    (0, 0, 1, mido.MetaMessage("time_signature", numerator=numerator, denominator=denominator, time=0))
                )
        for sequence, (message, seconds) in enumerate(events, start=2):
            tick = int(round(mido.second2tick(seconds, source_midi.ticks_per_beat, target_tempo)))
            entries.append((tick, 1, sequence, message))
        final_tick = max((entry[0] for entry in entries), default=0)
        entries.append((final_tick, 2, len(entries), mido.MetaMessage("end_of_track", time=0)))
        entries.sort(key=lambda item: item[:3])
        target_track = mido.MidiTrack()
        previous_tick = 0
        for tick, _priority, _sequence, message in entries:
            target_track.append(message.copy(time=tick - previous_tick))
            previous_tick = tick
        output.tracks.append(target_track)
    destination.parent.mkdir(parents=True, exist_ok=True)
    output.save(str(destination))
    return destination
