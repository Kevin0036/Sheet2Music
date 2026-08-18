from __future__ import annotations

import json
import math
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Sequence

from homr.bounding_boxes import RotatedBoundingBox
from homr.model import MultiStaff


@dataclass(frozen=True)
class HomrTransform:
    source_size: tuple[int, int]
    autocrop_bounds: tuple[int, int, int, int]
    recognition_size: tuple[int, int]


@dataclass(frozen=True)
class HomrSystemLayout:
    system_index: int
    bbox: tuple[int, int, int, int]
    staff_bboxes: tuple[tuple[int, int, int, int], ...]
    barline_x: tuple[int, ...]
    notehead_x: tuple[int, ...]
    local_measure_start: int | None
    local_measure_end: int | None
    measure_notehead_counts: tuple[int, ...]
    mapping_confidence: str


@dataclass(frozen=True)
class HomrPageLayout:
    schema_version: int
    transform: HomrTransform
    systems: tuple[HomrSystemLayout, ...]

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")


def musicxml_system_ranges(root: ET.Element) -> tuple[tuple[int, int], ...]:
    part = root.find("part")
    if part is None:
        return ()
    measures = part.findall("measure")
    if not measures:
        return ()

    starts = [1]
    for ordinal, measure in enumerate(measures, start=1):
        if ordinal == 1:
            continue
        system_break = measure.find("print")
        if system_break is not None and system_break.get("new-system") == "yes":
            starts.append(ordinal)
    return tuple(
        (start, starts[index + 1] - 1 if index + 1 < len(starts) else len(measures))
        for index, start in enumerate(starts)
    )


def build_page_layout(
    multi_staffs: Sequence[MultiStaff],
    musicxml: ET.Element,
    source_size: tuple[int, int],
    autocrop_bounds: tuple[int, int, int, int],
    recognition_size: tuple[int, int],
    detected_barlines: Sequence[RotatedBoundingBox] = (),
) -> HomrPageLayout:
    ordered = sorted(multi_staffs, key=_system_min_y)
    ranges = musicxml_system_ranges(musicxml)
    ranges_match = len(ranges) == len(ordered)
    systems = tuple(
        _build_system_layout(
            index,
            system,
            ranges[index] if index < len(ranges) else None,
            ranges_match,
            detected_barlines,
        )
        for index, system in enumerate(ordered)
    )
    return HomrPageLayout(
        schema_version=1,
        transform=HomrTransform(
            source_size=source_size,
            autocrop_bounds=autocrop_bounds,
            recognition_size=recognition_size,
        ),
        systems=systems,
    )


def _build_system_layout(
    index: int,
    system: MultiStaff,
    measure_range: tuple[int, int] | None,
    ranges_match: bool,
    detected_barlines: Sequence[RotatedBoundingBox],
) -> HomrSystemLayout:
    staffs = tuple(system.staffs)
    staff_bboxes = tuple(
        (
            math.floor(staff.min_x),
            math.floor(staff.min_y),
            math.ceil(staff.max_x),
            math.ceil(staff.max_y),
        )
        for staff in staffs
    )
    bbox = (
        min(item[0] for item in staff_bboxes),
        min(item[1] for item in staff_bboxes),
        max(item[2] for item in staff_bboxes),
        max(item[3] for item in staff_bboxes),
    )
    tolerance = max(1.0, sum(float(staff.average_unit_size) for staff in staffs) / len(staffs))
    staff_barlines = [barline for staff in staffs for barline in staff.get_bar_lines()]
    page_barlines = [
        barline for barline in detected_barlines if bbox[1] <= barline.center[1] <= bbox[3]
    ]
    detected_barline_x = _merge_x_positions(
        [float(barline.center[0]) for barline in (*staff_barlines, *page_barlines)], tolerance
    )
    expected_measure_count = (
        measure_range[1] - measure_range[0] + 1 if measure_range is not None else None
    )
    barline_x = detected_barline_x
    notehead_x = tuple(
        sorted(round(float(note.center[0])) for staff in staffs for note in staff.get_notes())
    )
    counts = _count_positions_between_boundaries(notehead_x, barline_x)
    confidence = (
        "high"
        if ranges_match
        and expected_measure_count is not None
        and len(barline_x) >= 2
        and len(barline_x) - 1 == expected_measure_count
        else "ambiguous"
    )
    return HomrSystemLayout(
        system_index=index,
        bbox=bbox,
        staff_bboxes=staff_bboxes,
        barline_x=barline_x,
        notehead_x=notehead_x,
        local_measure_start=measure_range[0] if measure_range is not None else None,
        local_measure_end=measure_range[1] if measure_range is not None else None,
        measure_notehead_counts=counts,
        mapping_confidence=confidence,
    )


def _merge_x_positions(values: Sequence[float], tolerance: float) -> tuple[int, ...]:
    groups: list[list[float]] = []
    for value in sorted(values):
        if not groups or value - (sum(groups[-1]) / len(groups[-1])) > tolerance:
            groups.append([value])
        else:
            groups[-1].append(value)
    return tuple(round(sum(group) / len(group)) for group in groups)


def _count_positions_between_boundaries(
    positions: Sequence[int], boundaries: Sequence[int]
) -> tuple[int, ...]:
    return tuple(
        sum(
            1
            for position in positions
            if left <= position < right or (index == len(boundaries) - 2 and position == right)
        )
        for index, (left, right) in enumerate(zip(boundaries, boundaries[1:]))
    )


def _system_min_y(system: MultiStaff) -> float:
    return min(staff.min_y for staff in system.staffs)
