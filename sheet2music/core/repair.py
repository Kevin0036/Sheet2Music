"""MusicXML / MIDI 保守修复。

本模块是独立副本，逻辑与仓库内 `scripts/phase1/fix_homr_musicxml.py` 和
`scripts/phase1/fix_homr_midi.py` 保持一致，供独立发布使用：

- 修复 HOMR 输出的不稳拍号与时值布局（只动 `time` 声明和 `backup` 布局，音符原位不动）；
- 注入恒定拍号与恒定 BPM 元数据；
- 归一化 MIDI 的 time signature / tempo 元数据（不改变音符 tick 时序）。
"""

from __future__ import annotations

import copy
import xml.etree.ElementTree as ET
from collections import OrderedDict, defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path

import miditoolkit

TIMING_TAGS = {"note", "backup", "forward"}


@dataclass
class FixReport:
    source_path: str
    output_path: str
    target_time_signature: str
    target_tempo_bpm: int | None = None
    measures_seen: int = 0
    measures_rebuilt: int = 0
    measures_already_valid: int = 0
    duplicate_time_signatures_removed: int = 0
    tempo_markings_removed: int = 0
    tempo_markings_inserted: int = 0
    transient_key_signatures_normalized: int = 0
    transient_clefs_normalized: int = 0
    unsupported_measures: int = 0
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass
class MidiFixReport:
    source_path: str
    output_path: str
    target_time_signature: str
    original_time_signature_count: int
    removed_time_signature_count: int
    inserted_time_signature_count: int = 1
    target_tempo_bpm: int | None = None
    original_tempo_change_count: int = 0
    removed_tempo_change_count: int = 0
    inserted_tempo_change_count: int = 0

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def parse_time_signature(value: str) -> tuple[int, int]:
    beats_text, beat_type_text = value.split("/", maxsplit=1)
    beats = int(beats_text)
    beat_type = int(beat_type_text)
    if beats <= 0 or beat_type <= 0:
        raise ValueError(f"Invalid time signature: {value}")
    return beats, beat_type


# ---------------------------------------------------------------------------
# MusicXML 修复
# ---------------------------------------------------------------------------


def find_measure_divisions(measure: ET.Element, current_divisions: int) -> int:
    for attributes in measure.findall("attributes"):
        divisions = attributes.find("divisions")
        if divisions is not None and divisions.text:
            current_divisions = int(divisions.text)
    return current_divisions


def expected_measure_ticks(divisions: int, beats: int, beat_type: int) -> int:
    numerator = divisions * beats * 4
    if numerator % beat_type != 0:
        raise ValueError(
            f"Time signature {beats}/{beat_type} is incompatible with divisions={divisions}"
        )
    return numerator // beat_type


def measure_cursor_extent(measure: ET.Element) -> int:
    cursor = 0
    for child in measure:
        if child.tag == "note":
            if child.find("chord") is not None:
                continue
            duration = child.findtext("duration")
            if duration:
                cursor += int(duration)
        elif child.tag == "backup":
            cursor -= int(child.findtext("duration", "0"))
        elif child.tag == "forward":
            cursor += int(child.findtext("duration", "0"))
    return cursor


def ensure_single_time_signature(measure: ET.Element, beats: int, beat_type: int) -> int:
    attributes = measure.findall("attributes")
    if not attributes:
        first_attributes = ET.Element("attributes")
        measure.insert(0, first_attributes)
        attributes = [first_attributes]
    first_attributes = attributes[0]

    time_nodes: list[tuple[ET.Element, ET.Element]] = []
    for attributes_node in attributes:
        for time in list(attributes_node.findall("time")):
            time_nodes.append((attributes_node, time))

    for attributes_node, time in time_nodes:
        attributes_node.remove(time)

    time = ET.Element("time")
    ET.SubElement(time, "beats").text = str(beats)
    ET.SubElement(time, "beat-type").text = str(beat_type)
    first_attributes.append(time)

    return max(0, len(time_nodes) - 1)


def _note_lane_key(note: ET.Element) -> tuple[str, str]:
    return note.findtext("staff", "1"), note.findtext("voice", "1")


def _leading_and_trailing_children(measure: ET.Element) -> tuple[list[ET.Element], list[ET.Element]]:
    children = list(measure)
    first_timing_index = next((i for i, child in enumerate(children) if child.tag in TIMING_TAGS), len(children))
    last_timing_index = max(
        (i for i, child in enumerate(children) if child.tag in TIMING_TAGS),
        default=first_timing_index - 1,
    )
    leading = children[:first_timing_index]
    trailing = children[last_timing_index + 1 :] if last_timing_index >= first_timing_index else []
    return leading, trailing


def rebuild_measure_timing(measure: ET.Element, expected_ticks: int) -> bool:
    timing_children = [child for child in measure if child.tag in TIMING_TAGS]
    if not timing_children:
        return False
    if any(child.tag == "forward" for child in timing_children):
        return False

    lanes: OrderedDict[tuple[str, str], list[ET.Element]] = OrderedDict()
    lane_durations: defaultdict[tuple[str, str], int] = defaultdict(int)

    for child in timing_children:
        if child.tag != "note":
            continue
        lane = _note_lane_key(child)
        lanes.setdefault(lane, []).append(child)
        if child.find("chord") is None:
            duration = child.findtext("duration")
            if duration:
                lane_durations[lane] += int(duration)

    if len(lanes) < 2:
        return False
    if not all(duration == expected_ticks for duration in lane_durations.values()):
        return False

    leading, trailing = _leading_and_trailing_children(measure)
    rebuilt_children = list(leading)
    for index, lane_notes in enumerate(lanes.values()):
        if index > 0:
            backup = ET.Element("backup")
            ET.SubElement(backup, "duration").text = str(expected_ticks)
            rebuilt_children.append(backup)
        rebuilt_children.extend(lane_notes)
    rebuilt_children.extend(trailing)
    measure[:] = rebuilt_children
    return True


def _is_tempo_direction(direction: ET.Element) -> bool:
    if direction.find("./direction-type/metronome") is not None:
        return True
    return any(sound.get("tempo") is not None for sound in direction.findall("sound"))


def _remove_existing_tempo_markings(root: ET.Element) -> int:
    removed = 0
    for measure in root.findall("./part/measure"):
        for direction in list(measure.findall("direction")):
            if not _is_tempo_direction(direction):
                continue
            measure.remove(direction)
            removed += 1
        for sound in list(measure.findall("sound")):
            if sound.get("tempo") is None:
                continue
            measure.remove(sound)
            removed += 1
    return removed


def _insert_tempo_marking(root: ET.Element, tempo_bpm: int) -> int:
    first_part = root.find("part")
    if first_part is None:
        return 0
    first_measure = first_part.find("measure")
    if first_measure is None:
        return 0

    direction = ET.Element("direction", {"placement": "above"})
    direction_type = ET.SubElement(direction, "direction-type")
    metronome = ET.SubElement(direction_type, "metronome")
    ET.SubElement(metronome, "beat-unit").text = "quarter"
    ET.SubElement(metronome, "per-minute").text = str(tempo_bpm)
    ET.SubElement(direction, "sound", {"tempo": str(tempo_bpm)})

    insert_at = next(
        (index for index, child in enumerate(list(first_measure)) if child.tag in TIMING_TAGS),
        len(first_measure),
    )
    first_measure.insert(insert_at, direction)
    return 1


def _normalize_transient_key_signatures(part: ET.Element, report: FixReport) -> None:
    events: list[tuple[ET.Element, ET.Element, str]] = []
    for measure in part.findall("measure"):
        for attributes in measure.findall("attributes"):
            for key in attributes.findall("key"):
                fifths = key.find("fifths")
                if fifths is not None and fifths.text is not None:
                    events.append((measure, key, fifths.text))

    if not events:
        return

    baseline = events[0][2]
    pending: list[tuple[ET.Element, ET.Element]] = []
    for measure, key, value in events:
        if value == baseline:
            if pending:
                for pending_measure, pending_key in pending:
                    pending_key.find("fifths").text = baseline
                report.transient_key_signatures_normalized += len(pending)
                first = pending[0][0].get("number", "?")
                last = pending[-1][0].get("number", "?")
                report.warnings.append(
                    f"part {part.get('id', '?')}: normalized transient key signature "
                    f"in measures {first}-{last} to fifths={baseline}"
                )
                pending = []
        else:
            pending.append((measure, key))


def _clef_value(clef: ET.Element) -> tuple[tuple[str, str | None, tuple[tuple[str, str], ...]], ...]:
    return tuple(
        (child.tag, child.text, tuple(sorted(child.attrib.items()))) for child in clef
    )


def _copy_clef_content(source: ET.Element, target: ET.Element) -> None:
    target[:] = [copy.deepcopy(child) for child in source]


def _normalize_transient_clefs(part: ET.Element, report: FixReport) -> None:
    events_by_staff: dict[str, list[tuple[ET.Element, ET.Element, tuple]]] = defaultdict(list)
    for measure in part.findall("measure"):
        for attributes in measure.findall("attributes"):
            for clef in attributes.findall("clef"):
                staff = clef.get("number", "1")
                events_by_staff[staff].append((measure, clef, _clef_value(clef)))

    for staff, events in events_by_staff.items():
        if not events:
            continue
        baseline_node = events[0][1]
        baseline = events[0][2]
        pending: list[tuple[ET.Element, ET.Element]] = []
        for measure, clef, value in events:
            if value == baseline:
                if pending:
                    for _, pending_clef in pending:
                        _copy_clef_content(baseline_node, pending_clef)
                    report.transient_clefs_normalized += len(pending)
                    first = pending[0][0].get("number", "?")
                    last = pending[-1][0].get("number", "?")
                    report.warnings.append(
                        f"part {part.get('id', '?')} staff {staff}: normalized transient clef "
                        f"in measures {first}-{last}"
                    )
                    pending = []
            else:
                pending.append((measure, clef))


def fix_musicxml_tree(
    root: ET.Element,
    target_time_signature: str,
    target_tempo_bpm: int | None = None,
) -> FixReport:
    beats, beat_type = parse_time_signature(target_time_signature)
    report = FixReport(
        source_path="",
        output_path="",
        target_time_signature=target_time_signature,
        target_tempo_bpm=target_tempo_bpm,
    )

    for part in root.findall("part"):
        _normalize_transient_key_signatures(part, report)
        _normalize_transient_clefs(part, report)
        divisions = 1
        for measure in part.findall("measure"):
            report.measures_seen += 1
            divisions = find_measure_divisions(measure, divisions)
            expected_ticks = expected_measure_ticks(divisions, beats, beat_type)
            report.duplicate_time_signatures_removed += ensure_single_time_signature(
                measure, beats, beat_type
            )

            cursor_before = measure_cursor_extent(measure)
            if cursor_before == expected_ticks:
                report.measures_already_valid += 1
                continue

            rebuilt = rebuild_measure_timing(measure, expected_ticks)
            if not rebuilt:
                report.unsupported_measures += 1
                report.warnings.append(
                    f"measure {measure.get('number', '?')}: cursor={cursor_before}, expected={expected_ticks}"
                )
                continue

            cursor_after = measure_cursor_extent(measure)
            if cursor_after == expected_ticks:
                report.measures_rebuilt += 1
            else:
                report.unsupported_measures += 1
                report.warnings.append(
                    f"measure {measure.get('number', '?')}: rebuild ended at cursor={cursor_after}, expected={expected_ticks}"
                )

    if target_tempo_bpm is not None:
        report.tempo_markings_removed = _remove_existing_tempo_markings(root)
        report.tempo_markings_inserted = _insert_tempo_marking(root, target_tempo_bpm)
        if report.tempo_markings_inserted == 0:
            report.warnings.append("no part/measure available for tempo injection")

    return report


def fix_musicxml_file(
    input_path: Path,
    output_path: Path,
    target_time_signature: str,
    target_tempo_bpm: int | None = None,
) -> FixReport:
    tree = ET.parse(input_path)
    report = fix_musicxml_tree(tree.getroot(), target_time_signature, target_tempo_bpm)
    report.source_path = str(input_path)
    report.output_path = str(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tree.write(output_path, encoding="unicode", xml_declaration=True)
    return report


# ---------------------------------------------------------------------------
# MIDI 修复
# ---------------------------------------------------------------------------


def fix_midi_file(
    input_path: Path,
    output_path: Path,
    target_time_signature: str,
    target_tempo_bpm: int | None = None,
) -> MidiFixReport:
    beats, beat_type = parse_time_signature(target_time_signature)
    midi_obj = miditoolkit.MidiFile(str(input_path))
    original_count = len(midi_obj.time_signature_changes)
    original_tempo_count = len(midi_obj.tempo_changes)
    midi_obj.time_signature_changes = [miditoolkit.TimeSignature(beats, beat_type, 0)]
    if target_tempo_bpm is not None:
        midi_obj.tempo_changes = [miditoolkit.TempoChange(float(target_tempo_bpm), 0)]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    midi_obj.dump(str(output_path))
    return MidiFixReport(
        source_path=str(input_path),
        output_path=str(output_path),
        target_time_signature=target_time_signature,
        target_tempo_bpm=target_tempo_bpm,
        original_time_signature_count=original_count,
        removed_time_signature_count=max(0, original_count - 1),
        original_tempo_change_count=original_tempo_count,
        removed_tempo_change_count=max(0, original_tempo_count - 1) if target_tempo_bpm is not None else 0,
        inserted_tempo_change_count=1 if target_tempo_bpm is not None else 0,
    )
