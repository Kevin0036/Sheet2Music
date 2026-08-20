"""Preflight analysis for score structure changes and HOMR timing anomalies."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from typing import Any

from .repair import (
    find_measure_divisions,
    preview_deterministic_timing_repair,
    preview_forward_gap_repair,
)
from .structure import ScoreStructurePlan, coerce_structure_plan
from .timeline import analyze_measure, fraction_text, units_to_beats


@dataclass
class ReviewFinding:
    id: str
    part_id: str
    kind: str
    severity: str
    measure_start: int
    measure_end: int
    page_numbers: list[int]
    observed: dict[str, object]
    suggestion: dict[str, object]
    reason: str
    staff: int | None = None
    status: str = "pending"
    available_actions: list[str] = field(
        default_factory=lambda: ["preserve", "correct", "reidentify"]
    )
    offset_units: int = 0
    offset_beats: str = "0"
    hand_region: str | None = None
    measure_ordinal: int | None = None
    display_measure_number: int | None = None
    number_mapping_confidence: str = "unknown"

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


AnalysisFinding = ReviewFinding


@dataclass
class AnalysisReport:
    findings: list[ReviewFinding] = field(default_factory=list)
    structure_plan: dict[str, object] = field(default_factory=dict)
    measures_seen: int = 0
    high_risk_count: int = 0

    @property
    def requires_review(self) -> bool:
        return any(finding.severity == "high" and finding.status == "pending" for finding in self.findings)

    @property
    def high_risk_findings(self) -> list[ReviewFinding]:
        return [finding for finding in self.findings if finding.severity == "high"]

    def to_dict(self) -> dict[str, object]:
        return {
            "findings": [finding.to_dict() for finding in self.findings],
            "structure_plan": self.structure_plan,
            "measures_seen": self.measures_seen,
            "high_risk_count": self.high_risk_count,
            "requires_review": self.requires_review,
        }


def analyze_musicxml_tree(
    root: ET.Element,
    structure_plan: ScoreStructurePlan,
    page_measure_offsets: list[int] | None = None,
) -> AnalysisReport:
    plan = coerce_structure_plan(structure_plan)
    report = AnalysisReport(structure_plan=plan.to_dict())
    parts = root.findall("part")
    report.measures_seen = max(
        (len(part.findall("measure")) for part in parts),
        default=0,
    )

    observed_signatures: dict[int, dict[str, tuple[int, int]]] = {}
    for part in parts:
        part_id = part.get("id", "?")
        effective_signatures, explicit_signature_measures = _analyze_part_time_signatures(
            part,
            part_id,
            plan,
            report,
            page_measure_offsets or [],
        )
        for measure_number, signature in effective_signatures.items():
            observed_signatures.setdefault(measure_number, {})[part_id] = signature
        _analyze_part_clefs(part, part_id, plan, report, page_measure_offsets or [])
        _analyze_part_timing(part, part_id, plan, report, page_measure_offsets or [])
        _report_missing_planned_time_changes(
            part,
            part_id,
            plan,
            explicit_signature_measures,
            report,
            page_measure_offsets or [],
        )

    for measure_number, by_part in observed_signatures.items():
        signatures = set(by_part.values())
        if len(signatures) <= 1:
            continue
        report.findings.append(
            _finding(
                kind="conflicting_time_signature",
                part_id="*",
                measure_start=measure_number,
                measure_end=measure_number,
                page_numbers=_page_numbers(measure_number, page_measure_offsets or []),
                observed={
                    "parts": {
                        part_id: f"{beats}/{beat_type}"
                        for part_id, (beats, beat_type) in by_part.items()
                    }
                },
                suggestion={"action": "review"},
                reason="different parts report different effective time signatures",
            )
        )

    report.high_risk_count = len(report.high_risk_findings)
    return report


def _analyze_part_time_signatures(
    part: ET.Element,
    part_id: str,
    plan: ScoreStructurePlan,
    report: AnalysisReport,
    page_measure_offsets: list[int],
) -> tuple[dict[int, tuple[int, int]], set[int]]:
    effective: dict[int, tuple[int, int]] = {}
    explicit_measures: set[int] = set()
    previous = plan.time_signature_for(1)
    for measure_number, measure in enumerate(part.findall("measure"), start=1):
        time_values: list[tuple[int, int]] = []
        for time in (
            time
            for attributes in measure.findall("attributes")
            for time in attributes.findall("time")
        ):
            try:
                beats = int(time.findtext("beats", "0"))
                beat_type = int(time.findtext("beat-type", "0"))
            except ValueError:
                beats, beat_type = 0, 0
            time_values.append((beats, beat_type))
        if time_values:
            explicit_measures.add(measure_number)
            unique_values = set(time_values)
            if len(unique_values) > 1:
                report.findings.append(
                    _finding(
                        kind="conflicting_time_signature",
                        part_id=part_id,
                        measure_start=measure_number,
                        measure_end=measure_number,
                        page_numbers=_page_numbers(measure_number, page_measure_offsets),
                        observed={
                            "values": [f"{beats}/{beat_type}" for beats, beat_type in time_values]
                        },
                        suggestion={"action": "review"},
                        reason="one part contains multiple time signatures in one measure",
                    )
                )
            current = time_values[0]
            if current != previous and not _plan_declares_time_signature(plan, measure_number, current):
                expected = plan.time_signature_for(measure_number)
                report.findings.append(
                    _finding(
                        kind="time_signature_change",
                        part_id=part_id,
                        measure_start=measure_number,
                        measure_end=measure_number,
                        page_numbers=_page_numbers(measure_number, page_measure_offsets),
                        observed={"signature": f"{current[0]}/{current[1]}"},
                        suggestion={"signature": f"{expected[0]}/{expected[1]}"},
                        reason="unconfirmed time-signature change affects measure boundaries",
                    )
                )
            previous = current
        effective[measure_number] = previous
    return effective, explicit_measures


def _analyze_part_clefs(
    part: ET.Element,
    part_id: str,
    plan: ScoreStructurePlan,
    report: AnalysisReport,
    page_measure_offsets: list[int],
) -> None:
    clef_state: dict[str, tuple[str, int]] = {}
    divisions = 1
    for measure_number, measure in enumerate(part.findall("measure"), start=1):
        divisions = find_measure_divisions(measure, divisions)
        cursor = 0
        for child in measure:
            if child.tag == "attributes":
                for clef in child.findall("clef"):
                    staff = clef.get("number", "1")
                    staff_number = int(staff)
                    current = _clef_signature(clef)
                    previous = clef_state.get(staff)
                    expected = plan.clef_for(staff_number, measure_number)
                    plan_declared = expected == current if expected is not None else False
                    observed = {
                        "previous_clef": (
                            {"sign": previous[0], "line": previous[1]}
                            if previous is not None
                            else None
                        ),
                        "observed_clef": {"sign": current[0], "line": current[1]},
                        "sign": current[0],
                        "line": current[1],
                    }
                    location = {
                        "offset_units": cursor,
                        "offset_beats": fraction_text(units_to_beats(cursor, divisions)),
                        "hand_region": _hand_region(staff_number),
                    }
                    if expected is not None and current != expected:
                        report.findings.append(
                            _finding(
                                kind="clef_mismatch",
                                part_id=part_id,
                                staff=staff_number,
                                measure_start=measure_number,
                                measure_end=measure_number,
                                page_numbers=_page_numbers(measure_number, page_measure_offsets),
                                observed=observed,
                                suggestion={"sign": expected[0], "line": expected[1]},
                                reason="谱号与已确认的结构方案不一致",
                                **location,
                            )
                        )
                    elif previous is not None and current != previous and not plan_declared:
                        report.findings.append(
                            _finding(
                                kind=(
                                    "clef_change_at_measure_start"
                                    if cursor == 0
                                    else "clef_change_mid_measure"
                                ),
                                part_id=part_id,
                                staff=staff_number,
                                measure_start=measure_number,
                                measure_end=measure_number,
                                page_numbers=_page_numbers(measure_number, page_measure_offsets),
                                observed=observed,
                                suggestion=(
                                    {"sign": expected[0], "line": expected[1]}
                                    if expected is not None
                                    else {"action": "review"}
                                ),
                                reason="尚未确认的谱号变化会影响该位置之后的音高解释",
                                available_actions=(
                                    ["preserve", "correct", "reidentify"]
                                    if expected is not None
                                    else ["preserve", "reidentify"]
                                ),
                                **location,
                            )
                        )
                    clef_state[staff] = current
            elif child.tag == "note":
                is_chord = child.find("chord") is not None
                is_grace = child.find("grace") is not None
                if not is_chord and not is_grace:
                    cursor += _safe_duration(child)
            elif child.tag == "backup":
                cursor -= _safe_duration(child)
            elif child.tag == "forward":
                cursor += _safe_duration(child)


def _analyze_part_timing(
    part: ET.Element,
    part_id: str,
    plan: ScoreStructurePlan,
    report: AnalysisReport,
    page_measure_offsets: list[int],
) -> None:
    divisions = 1
    for measure_number, measure in enumerate(part.findall("measure"), start=1):
        divisions = find_measure_divisions(measure, divisions)
        timing_children = [child for child in measure if child.tag in {"note", "backup", "forward"}]
        if not timing_children:
            continue
        beats, beat_type = plan.time_signature_for(measure_number)
        timeline = analyze_measure(measure, divisions, beats, beat_type)
        affected_staffs = sorted(
            {
                event.staff
                for event in timeline.events
                if event.staff is not None and event.end_units > timeline.expected_units
            }
        )
        common_observed = {
            "divisions": divisions,
            "occupied_units": timeline.maximum_note_end_units,
            "expected_units": timeline.expected_units,
            "occupied_beats": fraction_text(
                units_to_beats(timeline.maximum_note_end_units, divisions)
            ),
            "expected_beats": fraction_text(
                units_to_beats(timeline.expected_units, divisions)
            ),
            "difference_beats": fraction_text(
                units_to_beats(
                    timeline.maximum_note_end_units - timeline.expected_units,
                    divisions,
                )
            ),
            "affected_staffs": affected_staffs,
        }
        if timeline.diagnostics:
            gap_repair = preview_forward_gap_repair(measure, timeline.expected_units)
            if gap_repair.applied and gap_repair.measure is not None:
                repaired_timeline = analyze_measure(
                    gap_repair.measure,
                    divisions,
                    beats,
                    beat_type,
                )
                suggestion = {
                    "action": "repair_gap",
                    "reductions": [
                        {"child_index": index, "from_units": old, "to_units": new}
                        for index, old, new in gap_repair.reductions
                    ],
                    "resulting_beats": fraction_text(
                        units_to_beats(
                            repaired_timeline.maximum_note_end_units,
                            divisions,
                        )
                    ),
                }
                available_actions = ["correct", "reidentify"]
            else:
                suggestion = {"action": "reidentify"}
                available_actions = ["reidentify"]
            report.findings.append(
                _finding(
                    kind="timing_cursor_invalid",
                    part_id=part_id,
                    measure_start=measure_number,
                    measure_end=measure_number,
                    page_numbers=_page_numbers(measure_number, page_measure_offsets),
                    observed={
                        **common_observed,
                        "diagnostics": [item.code for item in timeline.diagnostics],
                    },
                    suggestion=suggestion,
                    reason="MusicXML 时间游标结构无效，无法可靠确定音符起点",
                    available_actions=available_actions,
                )
            )
            continue
        if timeline.has_overflow:
            gap_repair = preview_forward_gap_repair(measure, timeline.expected_units)
            if gap_repair.applied and gap_repair.measure is not None:
                suggestion = {
                    "action": "repair_gap",
                    "reductions": [
                        {"child_index": index, "from_units": old, "to_units": new}
                        for index, old, new in gap_repair.reductions
                    ],
                    "resulting_beats": fraction_text(
                        units_to_beats(
                            analyze_measure(
                                gap_repair.measure,
                                divisions,
                                beats,
                                beat_type,
                            ).maximum_note_end_units,
                            divisions,
                        )
                    ),
                }
                available_actions = ["correct", "reidentify"]
            else:
                repair = preview_deterministic_timing_repair(
                    measure,
                    divisions,
                    beats,
                    beat_type,
                )
                if repair.applied and repair.measure is not None:
                    repaired_timeline = analyze_measure(
                        repair.measure,
                        divisions,
                        beats,
                        beat_type,
                    )
                    suggestion = {
                        "action": "compress",
                        "corrected_note_count": len(repair.corrections),
                        "corrections": [
                            {"note_index": index, "from_units": old, "to_units": new}
                            for index, old, new in repair.corrections
                        ],
                        "resulting_beats": fraction_text(
                            units_to_beats(repaired_timeline.maximum_note_end_units, divisions)
                        ),
                    }
                    available_actions = ["correct", "reidentify"]
                else:
                    suggestion = {"action": "reidentify"}
                    available_actions = ["reidentify"]
            report.findings.append(
                _finding(
                    kind="timing_measure_overflow",
                    part_id=part_id,
                    measure_start=measure_number,
                    measure_end=measure_number,
                    page_numbers=_page_numbers(measure_number, page_measure_offsets),
                    observed=common_observed,
                    suggestion=suggestion,
                    reason="音符结束位置超出当前拍号规定的小节容量",
                    available_actions=available_actions,
                )
            )


def _report_missing_planned_time_changes(
    part: ET.Element,
    part_id: str,
    plan: ScoreStructurePlan,
    explicit_measures: set[int],
    report: AnalysisReport,
    page_measure_offsets: list[int],
) -> None:
    measure_count = len(part.findall("measure"))
    for change in plan.time_signature_changes:
        if change.from_measure > measure_count or change.from_measure in explicit_measures:
            continue
        report.findings.append(
            _finding(
                kind="missing_time_signature",
                part_id=part_id,
                measure_start=change.from_measure,
                measure_end=change.from_measure,
                page_numbers=_page_numbers(change.from_measure, page_measure_offsets),
                observed={"signature": "inherited"},
                suggestion={"signature": change.signature},
                reason="reviewed structure plan requires a time-signature declaration here",
            )
        )


def _plan_declares_time_signature(
    plan: ScoreStructurePlan,
    measure_number: int,
    signature: tuple[int, int],
) -> bool:
    return any(
        change.beats == signature[0]
        and change.beat_type == signature[1]
        and change.from_measure <= measure_number
        and (change.to_measure is None or measure_number <= change.to_measure)
        for change in plan.time_signature_changes
    )


def _clef_signature(clef: ET.Element) -> tuple[str, int]:
    sign = clef.findtext("sign", "")
    try:
        line = int(clef.findtext("line", "0"))
    except ValueError:
        line = 0
    return sign, line


def _safe_duration(element: ET.Element) -> int:
    try:
        return max(0, int(element.findtext("duration", "0")))
    except ValueError:
        return 0


def _hand_region(staff: int) -> str | None:
    if staff == 1:
        return "right"
    if staff == 2:
        return "left"
    return None


def _finding(
    *,
    kind: str,
    part_id: str,
    measure_start: int,
    measure_end: int,
    page_numbers: list[int],
    observed: dict[str, object],
    suggestion: dict[str, object],
    reason: str,
    staff: int | str | None = None,
    available_actions: list[str] | None = None,
    offset_units: int = 0,
    offset_beats: str = "0",
    hand_region: str | None = None,
    display_measure_number: int | None = None,
    number_mapping_confidence: str = "unknown",
) -> ReviewFinding:
    staff_key = staff if staff is not None else "-"
    offset_key = f":u{offset_units}" if offset_units else ""
    finding_id = f"{kind}:{part_id}:{staff_key}:{measure_start}:{measure_end}{offset_key}"
    return ReviewFinding(
        id=finding_id,
        part_id=part_id,
        kind=kind,
        severity="high",
        measure_start=measure_start,
        measure_end=measure_end,
        page_numbers=page_numbers,
        observed=observed,
        suggestion=suggestion,
        reason=reason,
        staff=int(staff) if staff is not None else None,
        available_actions=(
            available_actions
            if available_actions is not None
            else ["preserve", "correct", "reidentify"]
        ),
        offset_units=offset_units,
        offset_beats=offset_beats,
        hand_region=hand_region,
        measure_ordinal=measure_start,
        display_measure_number=display_measure_number,
        number_mapping_confidence=number_mapping_confidence,
    )


def _page_numbers(measure_number: int, offsets: list[int]) -> list[int]:
    if not offsets:
        return []
    for index, offset in enumerate(offsets):
        next_offset = offsets[index + 1] if index + 1 < len(offsets) else None
        if measure_number > offset and (next_offset is None or measure_number <= next_offset):
            return [index + 1]
    return []
