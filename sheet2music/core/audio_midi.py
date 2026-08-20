"""MIDI conductor metadata rewrites that preserve playback seconds."""

from __future__ import annotations

from bisect import bisect_right
from math import isfinite
from pathlib import Path

import mido


def rewrite_midi_metadata_preserving_seconds(
    source: Path,
    destination: Path,
    *,
    bpm: float,
    time_signature: tuple[int, int] | None,
    tempo_map: list[tuple[float, float]] | None = None,
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

    sections: list[tuple[float, int, int]] = []
    if tempo_map:
        normalized = sorted(
            (max(0.0, float(seconds)), float(local_bpm))
            for seconds, local_bpm in tempo_map
        )
        for seconds, local_bpm in normalized:
            if not isfinite(seconds) or not isfinite(local_bpm) or local_bpm <= 0.0:
                raise ValueError(f"Invalid tempo map entry: {(seconds, local_bpm)!r}")
            if sections and abs(local_bpm - mido.tempo2bpm(sections[-1][2])) < 1e-9:
                continue
            sections.append((seconds, 0, mido.bpm2tempo(local_bpm)))
        if not sections:
            sections = [(0.0, 0, target_tempo)]
        elif sections[0][0] > 0.0:
            sections.insert(0, (0.0, 0, sections[0][2]))
        tick_cursor = 0
        previous_seconds = 0.0
        previous_tempo = sections[0][2]
        resolved: list[tuple[float, int, int]] = []
        for seconds, _unused, local_tempo in sections:
            tick_cursor += int(
                round(
                    mido.second2tick(
                        seconds - previous_seconds,
                        source_midi.ticks_per_beat,
                        previous_tempo,
                    )
                )
            )
            resolved.append((seconds, tick_cursor, local_tempo))
            previous_seconds, previous_tempo = seconds, local_tempo
        sections = resolved

    def source_tempo_sections(track_index: int) -> list[tuple[int, float, int]]:
        tracks = (
            ((track_index, source_midi.tracks[track_index]),)
            if source_midi.type == 2
            else tuple(enumerate(source_midi.tracks))
        )
        events: list[tuple[int, int, int, int]] = []
        for source_index, track in tracks:
            absolute = 0
            for sequence, message in enumerate(track):
                absolute += int(message.time)
                if message.is_meta and message.type == "set_tempo":
                    events.append((absolute, source_index, sequence, int(message.tempo)))
        collapsed: dict[int, int] = {}
        for tick, _source, _sequence, value in sorted(events):
            collapsed[tick] = value
        result: list[tuple[int, float, int]] = [(0, 0.0, 500_000)]
        for tick, value in sorted(collapsed.items()):
            previous_tick, previous_seconds, previous_tempo = result[-1]
            if tick == previous_tick:
                result[-1] = (tick, previous_seconds, value)
            else:
                result.append(
                    (
                        tick,
                        previous_seconds
                        + mido.tick2second(
                            tick - previous_tick,
                            source_midi.ticks_per_beat,
                            previous_tempo,
                        ),
                        value,
                    )
                )
        return result

    def source_seconds(track_index: int, tick: int) -> float:
        source_sections = source_tempo_sections(track_index)
        index = bisect_right([item[0] for item in source_sections], tick) - 1
        start_tick, start_seconds, source_tempo = source_sections[index]
        return start_seconds + mido.tick2second(tick - start_tick, source_midi.ticks_per_beat, source_tempo)

    def target_tick(seconds: float) -> int:
        if not sections:
            return int(round(mido.second2tick(seconds, source_midi.ticks_per_beat, target_tempo)))
        index = bisect_right([item[0] for item in sections], seconds) - 1
        start_seconds, start_tick, local_tempo = sections[index]
        return int(round(start_tick + mido.second2tick(seconds - start_seconds, source_midi.ticks_per_beat, local_tempo)))

    for index, source_track in enumerate(source_midi.tracks):
        entries: list[tuple[int, int, int, mido.Message | mido.MetaMessage]] = []
        if index == 0:
            if sections:
                for sequence, (_seconds, tick, local_tempo) in enumerate(sections):
                    entries.append((tick, 0, sequence, mido.MetaMessage("set_tempo", tempo=local_tempo, time=0)))
            else:
                entries.append((0, 0, 0, mido.MetaMessage("set_tempo", tempo=target_tempo, time=0)))
            if time_signature is not None:
                entries.append(
                    (
                        0,
                        0,
                        len(entries),
                        mido.MetaMessage(
                            "time_signature",
                            numerator=numerator,
                            denominator=denominator,
                            time=0,
                        ),
                    )
                )
        absolute_tick = 0
        for sequence, message in enumerate(source_track, start=len(entries) + 1):
            absolute_tick += int(message.time)
            if message.is_meta and message.type in {"set_tempo", "time_signature"}:
                continue
            if message.is_meta and message.type == "end_of_track":
                continue
            seconds = source_seconds(index, absolute_tick)
            entries.append((target_tick(seconds), 1, sequence, message.copy(time=0)))
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
