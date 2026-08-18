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
from collections.abc import Mapping
from collections import OrderedDict, defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path

import miditoolkit

from .structure import ScoreStructurePlan, coerce_structure_plan
from .timeline import analyze_measure, notated_duration_units

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
    structure_plan_applied: bool = False
    structure_plan: dict[str, object] | None = None
    time_signature_changes: list[dict[str, object]] = field(default_factory=list)
    key_signature_changes: int = 0
    clef_overrides_applied: int = 0
    clef_override_ranges: list[dict[str, object]] = field(default_factory=list)
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
    time_signature_changes: list[dict[str, object]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class DeterministicTimingRepair:
    applied: bool
    corrections: tuple[tuple[int, int, int], ...] = ()
    measure: ET.Element | None = None
    reason: str | None = None


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


def preview_deterministic_timing_repair(
    measure: ET.Element,
    divisions: int,
    beats: int,
    beat_type: int,
) -> DeterministicTimingRepair:
    """Build a corrected copy only when notation proves exact duration values."""
    candidate = copy.deepcopy(measure)
    corrections: list[tuple[int, int, int]] = []
    for note_index, note in enumerate(candidate.findall("note")):
        if note.find("grace") is not None:
            continue
        duration_node = note.find("duration")
        if duration_node is None or duration_node.text is None:
            continue
        try:
            observed = int(duration_node.text)
        except ValueError:
            continue
        expected = notated_duration_units(note, divisions)
        if expected is None or observed == expected:
            continue
        duration_node.text = str(expected)
        corrections.append((note_index, observed, expected))

    if not corrections:
        return DeterministicTimingRepair(
            applied=False,
            reason="没有可由记谱信息唯一确定的时值更正",
        )

    timeline = analyze_measure(candidate, divisions, beats, beat_type)
    if timeline.diagnostics or timeline.has_overflow:
        return DeterministicTimingRepair(
            applied=False,
            corrections=tuple(corrections),
            reason="按记谱信息更正后，小节时间结构仍然无效或越界",
        )
    return DeterministicTimingRepair(
        applied=True,
        corrections=tuple(corrections),
        measure=candidate,
    )


def apply_deterministic_timing_decisions(
    root: ET.Element,
    structure_plan: ScoreStructurePlan | Mapping[str, object] | None,
    decisions: list[dict[str, object]],
) -> int:
    """Apply approved timing corrections to their exact part and measure."""
    plan = coerce_structure_plan(structure_plan)
    applied = 0
    for decision in decisions:
        if decision.get("action") != "correct" or decision.get("kind") not in {
            "timing_measure_overflow",
            "timing_notation_mismatch",
        }:
            continue
        part_id = decision.get("part_id")
        measure_number = decision.get("measure_start")
        if not isinstance(part_id, str) or not isinstance(measure_number, int):
            raise ValueError("timing repair decision is missing part or measure information")
        part = next((item for item in root.findall("part") if item.get("id") == part_id), None)
        if part is None:
            raise ValueError(f"timing repair target part does not exist: {part_id}")
        measures = part.findall("measure")
        if measure_number < 1 or measure_number > len(measures):
            raise ValueError(
                f"timing repair target measure does not exist: {part_id} measure {measure_number}"
            )
        divisions = 1
        for measure in measures[:measure_number]:
            divisions = find_measure_divisions(measure, divisions)
        measure = measures[measure_number - 1]
        beats, beat_type = plan.time_signature_for(measure_number)
        repair = preview_deterministic_timing_repair(
            measure,
            divisions,
            beats,
            beat_type,
        )
        if not repair.applied or repair.measure is None:
            raise ValueError(
                f"timing repair is no longer deterministic: {part_id} measure {measure_number}"
            )
        child_index = list(part).index(measure)
        part.remove(measure)
        part.insert(child_index, repair.measure)
        applied += 1
    return applied


def apply_reviewed_clef_decisions(
    root: ET.Element,
    decisions: list[dict[str, object]],
) -> int:
    """Apply a reviewed clef correction at its exact MusicXML cursor offset."""
    applied = 0
    for decision in decisions:
        kind = str(decision.get("kind", ""))
        action = decision.get("action")
        if not kind.startswith("clef_") or action == "preserve":
            continue
        if action != "correct":
            continue
        part_id = decision.get("part_id")
        measure_number = decision.get("measure_start")
        staff = decision.get("staff")
        offset_units = decision.get("offset_units", 0)
        suggestion = decision.get("suggestion")
        if (
            not isinstance(part_id, str)
            or not isinstance(measure_number, int)
            or not isinstance(staff, int)
            or not isinstance(offset_units, int)
            or not isinstance(suggestion, Mapping)
            or not isinstance(suggestion.get("sign"), str)
            or not isinstance(suggestion.get("line"), int)
        ):
            raise ValueError("clef repair decision is missing target or replacement information")
        part = next((item for item in root.findall("part") if item.get("id") == part_id), None)
        if part is None:
            raise ValueError(f"clef repair target part does not exist: {part_id}")
        measures = part.findall("measure")
        if measure_number < 1 or measure_number > len(measures):
            raise ValueError(
                f"clef repair target measure does not exist: {part_id} measure {measure_number}"
            )
        measure = measures[measure_number - 1]
        target = _clef_at_offset(measure, staff, offset_units)
        if target is None:
            raise ValueError(
                f"clef event no longer exists: {part_id} measure {measure_number} offset {offset_units}"
            )
        sign = target.find("sign")
        if sign is None:
            sign = ET.SubElement(target, "sign")
        line = target.find("line")
        if line is None:
            line = ET.SubElement(target, "line")
        sign.text = str(suggestion["sign"])
        line.text = str(suggestion["line"])
        applied += 1
    return applied


def _clef_at_offset(
    measure: ET.Element,
    staff: int,
    target_offset: int,
) -> ET.Element | None:
    cursor = 0
    for child in measure:
        if child.tag == "attributes" and cursor == target_offset:
            for clef in child.findall("clef"):
                if clef.get("number", "1") == str(staff):
                    return clef
        elif child.tag == "note":
            if child.find("chord") is None and child.find("grace") is None:
                cursor += _timing_duration(child)
        elif child.tag == "backup":
            cursor -= _timing_duration(child)
        elif child.tag == "forward":
            cursor += _timing_duration(child)
    return None


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


@dataclass
class _LaneGroup:
    notes: list[ET.Element]
    gap_before: int
    duration: int


@dataclass
class _LaneLayout:
    lanes: OrderedDict[tuple[str, str], list[_LaneGroup]]
    trailing_gaps: defaultdict[tuple[str, str], int]
    lane_lengths: dict[tuple[str, str], int]


def _timing_duration(element: ET.Element) -> int:
    raw_duration = element.findtext("duration")
    if raw_duration is None:
        return 0
    try:
        duration = int(raw_duration)
    except ValueError as exc:
        raise ValueError(f"invalid {element.tag} duration: {raw_duration!r}") from exc
    if duration < 0:
        raise ValueError(f"negative {element.tag} duration: {duration}")
    return duration


def _collect_lane_layout(measure: ET.Element) -> tuple[_LaneLayout | None, str | None]:
    children = list(measure)
    next_note_lane: list[tuple[str, str] | None] = [None] * len(children)
    upcoming_lane: tuple[str, str] | None = None
    for index in range(len(children) - 1, -1, -1):
        next_note_lane[index] = upcoming_lane
        if children[index].tag == "note":
            upcoming_lane = _note_lane_key(children[index])

    lanes: OrderedDict[tuple[str, str], list[_LaneGroup]] = OrderedDict()
    pending_gaps: defaultdict[tuple[str, str], int] = defaultdict(int)
    trailing_gaps: defaultdict[tuple[str, str], int] = defaultdict(int)
    active_lane: tuple[str, str] | None = None
    unassigned_forward = 0

    try:
        for index, child in enumerate(children):
            if child.tag == "note":
                lane = _note_lane_key(child)
                groups = lanes.setdefault(lane, [])
                is_chord = child.find("chord") is not None
                duration = _timing_duration(child)
                if is_chord and groups:
                    groups[-1].notes.append(child)
                else:
                    groups.append(
                        _LaneGroup(
                            notes=[child],
                            gap_before=pending_gaps.pop(lane, 0),
                            duration=duration,
                        )
                    )
                active_lane = lane
            elif child.tag == "forward":
                duration = _timing_duration(child)
                target_lane = next_note_lane[index] or active_lane
                if target_lane is None:
                    unassigned_forward += duration
                else:
                    pending_gaps[target_lane] += duration
            elif child.tag == "backup":
                _timing_duration(child)
    except ValueError as exc:
        return None, str(exc)

    for lane, gap in pending_gaps.items():
        if lane not in lanes:
            return None, f"forward has no note lane: staff={lane[0]}, voice={lane[1]}"
        trailing_gaps[lane] += gap

    if unassigned_forward:
        return None, f"forward has no staff/voice context: duration={unassigned_forward}"
    if not lanes:
        return None, "measure has no note lanes"

    lane_lengths: dict[tuple[str, str], int] = {}
    for lane, groups in lanes.items():
        lane_lengths[lane] = sum(
            group.gap_before + group.duration for group in groups
        ) + trailing_gaps[lane]

    return _LaneLayout(lanes, trailing_gaps, lane_lengths), None


def _rebuild_measure_timing_with_reason(
    measure: ET.Element,
    expected_ticks: int,
) -> tuple[bool, str | None]:
    timing_children = [child for child in measure if child.tag in TIMING_TAGS]
    if not timing_children:
        return False, "measure has no timing elements"

    layout, error = _collect_lane_layout(measure)
    if layout is None:
        return False, error
    overflowing = [
        (lane, length)
        for lane, length in layout.lane_lengths.items()
        if length > expected_ticks
    ]
    if overflowing:
        lane, length = overflowing[0]
        return (
            False,
            f"lane staff={lane[0]}, voice={lane[1]} exceeds measure boundary: "
            f"duration={length}, expected={expected_ticks}",
        )

    leading, trailing = _leading_and_trailing_children(measure)
    children = list(measure)
    first_timing_index = next(
        (index for index, child in enumerate(children) if child.tag in TIMING_TAGS),
        len(children),
    )
    last_timing_index = max(
        (index for index, child in enumerate(children) if child.tag in TIMING_TAGS),
        default=first_timing_index - 1,
    )
    middle_non_timing = [
        child
        for child in children[first_timing_index : last_timing_index + 1]
        if child.tag not in TIMING_TAGS
    ]

    rebuilt_children = list(leading)
    for lane_index, (lane, groups) in enumerate(layout.lanes.items()):
        if lane_index > 0:
            backup = ET.Element("backup")
            ET.SubElement(backup, "duration").text = str(expected_ticks)
            rebuilt_children.append(backup)
        for group in groups:
            if group.gap_before:
                forward = ET.Element("forward")
                ET.SubElement(forward, "duration").text = str(group.gap_before)
                rebuilt_children.append(forward)
            rebuilt_children.extend(group.notes)

        remaining = expected_ticks - layout.lane_lengths[lane]
        trailing_forward = layout.trailing_gaps[lane] + remaining
        if trailing_forward:
            forward = ET.Element("forward")
            ET.SubElement(forward, "duration").text = str(trailing_forward)
            rebuilt_children.append(forward)

    rebuilt_children.extend(middle_non_timing)
    rebuilt_children.extend(trailing)
    measure[:] = rebuilt_children
    return True, None


def rebuild_measure_timing(measure: ET.Element, expected_ticks: int) -> bool:
    """Rebuild legal multi-lane timing while preserving the original notes."""

    rebuilt, _ = _rebuild_measure_timing_with_reason(measure, expected_ticks)
    return rebuilt


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


def _remove_time_signatures(measure: ET.Element) -> int:
    removed = 0
    for attributes in measure.findall("attributes"):
        for time in list(attributes.findall("time")):
            attributes.remove(time)
            removed += 1
    return removed


def _set_time_signature(
    measure: ET.Element,
    beats: int,
    beat_type: int,
    include: bool,
) -> int:
    existing_count = sum(
        len(attributes.findall("time")) for attributes in measure.findall("attributes")
    )
    if not include:
        return _remove_time_signatures(measure)
    duplicate_count = max(0, existing_count - 1)
    ensure_single_time_signature(measure, beats, beat_type)
    return duplicate_count


def _first_attributes(measure: ET.Element) -> ET.Element:
    attributes = measure.find("attributes")
    if attributes is None:
        attributes = ET.Element("attributes")
        measure.insert(0, attributes)
    return attributes


def _apply_key_signature_plan(
    part: ET.Element,
    plan: ScoreStructurePlan,
    report: FixReport,
    measure_offset: int,
) -> None:
    if plan.key_signature_fifths is None or measure_offset != 0:
        return

    target = str(plan.key_signature_fifths)
    measures = part.findall("measure")
    for measure in measures:
        for attributes in measure.findall("attributes"):
            for key in attributes.findall("key"):
                fifths = key.find("fifths")
                if fifths is None:
                    fifths = ET.SubElement(key, "fifths")
                if fifths.text != target:
                    fifths.text = target
                    report.key_signature_changes += 1

    if not measures:
        return
    first_measure = measures[0]
    existing_keys = [
        key
        for attributes in first_measure.findall("attributes")
        for key in attributes.findall("key")
    ]
    if existing_keys:
        return
    attributes = _first_attributes(first_measure)
    key = ET.SubElement(attributes, "key")
    ET.SubElement(key, "fifths").text = target
    report.key_signature_changes += 1


def _apply_clef_overrides(
    part: ET.Element,
    plan: ScoreStructurePlan,
    report: FixReport,
    measure_offset: int,
) -> None:
    if not plan.clef_overrides:
        return

    report.clef_override_ranges.extend(
        {
            "staff": override.staff,
            "from_measure": override.from_measure,
            "to_measure": override.to_measure,
            "sign": override.sign,
            "line": override.line,
        }
        for override in plan.clef_overrides
    )
    for local_index, measure in enumerate(part.findall("measure"), start=1):
        score_measure_number = measure_offset + local_index
        for override in plan.clef_overrides:
            if not (
                override.from_measure <= score_measure_number
                and (
                    override.to_measure is None
                    or score_measure_number <= override.to_measure
                )
            ):
                continue

            clefs = [
                clef
                for attributes in measure.findall("attributes")
                for clef in attributes.findall("clef")
                if clef.get("number", "1") == str(override.staff)
            ]
            if not clefs:
                clef = ET.SubElement(_first_attributes(measure), "clef")
                clef.set("number", str(override.staff))
                clefs = [clef]
            for clef in clefs:
                old_value = _clef_value(clef)
                sign = clef.find("sign")
                if sign is None:
                    sign = ET.SubElement(clef, "sign")
                line = clef.find("line")
                if line is None:
                    line = ET.SubElement(clef, "line")
                sign.text = override.sign
                line.text = str(override.line)
                if old_value != _clef_value(clef):
                    report.clef_overrides_applied += 1


def _lane_layout_needs_rebuild(
    layout: _LaneLayout | None,
    expected_ticks: int,
) -> bool:
    if layout is None:
        return True
    return any(length != expected_ticks for length in layout.lane_lengths.values())


def fix_musicxml_tree(
    root: ET.Element,
    target_time_signature: str,
    target_tempo_bpm: int | None = None,
    structure_plan: ScoreStructurePlan | Mapping[str, object] | None = None,
    measure_offset: int = 0,
    normalize_transient: bool = True,
) -> FixReport:
    plan_mode = structure_plan is not None
    plan = coerce_structure_plan(structure_plan, target_time_signature)
    report = FixReport(
        source_path="",
        output_path="",
        target_time_signature=target_time_signature,
        target_tempo_bpm=target_tempo_bpm,
        structure_plan_applied=plan_mode,
        structure_plan=plan.to_dict() if plan_mode else None,
    )

    for part in root.findall("part"):
        if plan_mode:
            _apply_key_signature_plan(part, plan, report, measure_offset)
            _apply_clef_overrides(part, plan, report, measure_offset)
        elif normalize_transient:
            _normalize_transient_key_signatures(part, report)
            _normalize_transient_clefs(part, report)
        divisions = 1
        previous_signature = (
            plan.time_signature_for(measure_offset) if measure_offset > 0 else None
        )
        for local_index, measure in enumerate(part.findall("measure"), start=1):
            report.measures_seen += 1
            divisions = find_measure_divisions(measure, divisions)
            score_measure_number = measure_offset + local_index
            beats, beat_type = plan.time_signature_for(score_measure_number)
            expected_ticks = expected_measure_ticks(divisions, beats, beat_type)
            signature = (beats, beat_type)
            signature_changed = previous_signature is None or signature != previous_signature
            report.duplicate_time_signatures_removed += _set_time_signature(
                measure,
                beats,
                beat_type,
                include=signature_changed,
            )
            if signature_changed:
                report.time_signature_changes.append(
                    {
                        "measure": score_measure_number,
                        "signature": f"{beats}/{beat_type}",
                    }
                )
            previous_signature = signature

            cursor_before = measure_cursor_extent(measure)
            layout, layout_error = _collect_lane_layout(measure)
            if (
                cursor_before == expected_ticks
                and layout_error is None
                and not _lane_layout_needs_rebuild(layout, expected_ticks)
            ):
                report.measures_already_valid += 1
                continue

            rebuilt, rebuild_reason = _rebuild_measure_timing_with_reason(
                measure,
                expected_ticks,
            )
            if not rebuilt:
                report.unsupported_measures += 1
                reason = rebuild_reason or layout_error or "timing layout is not safely rebuildable"
                report.warnings.append(
                    f"measure {measure.get('number', '?')}: {reason}; "
                    f"cursor={cursor_before}, expected={expected_ticks}"
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
    structure_plan: ScoreStructurePlan | Mapping[str, object] | None = None,
    measure_offset: int = 0,
    normalize_transient: bool = True,
) -> FixReport:
    tree = ET.parse(input_path)
    report = fix_musicxml_tree(
        tree.getroot(),
        target_time_signature,
        target_tempo_bpm,
        structure_plan=structure_plan,
        measure_offset=measure_offset,
        normalize_transient=normalize_transient,
    )
    report.source_path = str(input_path)
    report.output_path = str(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tree.write(output_path, encoding="unicode", xml_declaration=True)
    return report


# ---------------------------------------------------------------------------
# MIDI 修复
# ---------------------------------------------------------------------------


def measure_start_ticks(
    plan: ScoreStructurePlan,
    measure_count: int,
    ticks_per_beat: int,
) -> list[int]:
    if measure_count <= 0:
        raise ValueError("measure_count must be positive")
    if ticks_per_beat <= 0:
        raise ValueError("ticks_per_beat must be positive")

    starts = [0]
    current_tick = 0
    for measure_number in range(1, measure_count + 1):
        beats, beat_type = plan.time_signature_for(measure_number)
        numerator = ticks_per_beat * beats * 4
        if numerator % beat_type != 0:
            raise ValueError(
                f"time signature {beats}/{beat_type} is incompatible with "
                f"ticks_per_beat={ticks_per_beat}"
            )
        current_tick += numerator // beat_type
        if measure_number < measure_count:
            starts.append(current_tick)
    return starts


def _structure_time_signature_events(
    plan: ScoreStructurePlan,
    measure_count: int,
    ticks_per_beat: int,
) -> tuple[list[miditoolkit.TimeSignature], list[dict[str, object]]]:
    starts = measure_start_ticks(plan, measure_count, ticks_per_beat)
    events: list[miditoolkit.TimeSignature] = []
    changes: list[dict[str, object]] = []
    previous_signature: tuple[int, int] | None = None
    for index, start_tick in enumerate(starts):
        measure_number = index + 1
        signature = plan.time_signature_for(measure_number)
        if signature == previous_signature:
            continue
        beats, beat_type = signature
        events.append(miditoolkit.TimeSignature(beats, beat_type, start_tick))
        changes.append(
            {
                "measure": measure_number,
                "tick": start_tick,
                "signature": f"{beats}/{beat_type}",
            }
        )
        previous_signature = signature
    return events, changes


def fix_midi_file(
    input_path: Path,
    output_path: Path,
    target_time_signature: str,
    target_tempo_bpm: int | None = None,
    structure_plan: ScoreStructurePlan | Mapping[str, object] | None = None,
    measure_count: int | None = None,
) -> MidiFixReport:
    midi_obj = miditoolkit.MidiFile(str(input_path))
    original_count = len(midi_obj.time_signature_changes)
    original_tempo_count = len(midi_obj.tempo_changes)
    plan_mode = structure_plan is not None
    plan = coerce_structure_plan(structure_plan, target_time_signature)
    warnings: list[str] = []
    if plan_mode:
        if measure_count is None:
            max_tick = getattr(midi_obj, "max_tick", 0)
            default_beats, default_beat_type = plan.time_signature_for(1)
            default_numerator = midi_obj.ticks_per_beat * default_beats * 4
            default_measure_ticks = max(1, default_numerator // default_beat_type)
            measure_count = max(1, (max_tick + default_measure_ticks - 1) // default_measure_ticks)
            warnings.append("measure_count was inferred from MIDI max_tick")
        time_signature_events, time_signature_changes = _structure_time_signature_events(
            plan,
            measure_count,
            midi_obj.ticks_per_beat,
        )
    else:
        beats, beat_type = parse_time_signature(target_time_signature)
        time_signature_events = [miditoolkit.TimeSignature(beats, beat_type, 0)]
        time_signature_changes = [
            {"measure": 1, "tick": 0, "signature": f"{beats}/{beat_type}"}
        ]
    midi_obj.time_signature_changes = time_signature_events
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
        removed_time_signature_count=(
            original_count if plan_mode else max(0, original_count - 1)
        ),
        inserted_time_signature_count=len(time_signature_events),
        original_tempo_change_count=original_tempo_count,
        removed_tempo_change_count=max(0, original_tempo_count - 1) if target_tempo_bpm is not None else 0,
        inserted_tempo_change_count=1 if target_tempo_bpm is not None else 0,
        time_signature_changes=time_signature_changes,
        warnings=warnings,
    )
