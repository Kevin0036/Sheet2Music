"""Map HOMR page layout coordinates back to retained high-resolution pages."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from typing import Mapping, Sequence

import cv2


@dataclass(frozen=True)
class CoordinateTransform:
    raw_size: tuple[int, int]
    input_bounds_in_raw: tuple[int, int, int, int]
    homr_autocrop_bounds: tuple[int, int, int, int]
    recognition_size: tuple[int, int]

    def recognition_bbox_to_raw(
        self, bbox: tuple[int, int, int, int]
    ) -> tuple[int, int, int, int]:
        raw_width, raw_height = self.raw_size
        input_left, input_top, _, _ = self.input_bounds_in_raw
        crop_left, crop_top, crop_right, crop_bottom = self.homr_autocrop_bounds
        recognition_width, recognition_height = self.recognition_size
        if recognition_width <= 0 or recognition_height <= 0:
            raise ValueError("recognition image dimensions must be positive")
        scale_x = (crop_right - crop_left) / recognition_width
        scale_y = (crop_bottom - crop_top) / recognition_height
        left, top, right, bottom = bbox
        mapped = (
            math.floor(input_left + crop_left + left * scale_x),
            math.floor(input_top + crop_top + top * scale_y),
            math.ceil(input_left + crop_left + right * scale_x),
            math.ceil(input_top + crop_top + bottom * scale_y),
        )
        return (
            max(0, min(raw_width, mapped[0])),
            max(0, min(raw_height, mapped[1])),
            max(0, min(raw_width, mapped[2])),
            max(0, min(raw_height, mapped[3])),
        )

    @property
    def raw_y_per_recognition_pixel(self) -> float:
        _, crop_top, _, crop_bottom = self.homr_autocrop_bounds
        return (crop_bottom - crop_top) / self.recognition_size[1]


@dataclass(frozen=True)
class ScoreSystem:
    page_number: int
    system_index: int
    bbox: tuple[int, int, int, int]
    staff_bboxes: tuple[tuple[int, int, int, int], ...]
    global_measure_start: int
    global_measure_end: int
    notehead_counts: tuple[int, ...]
    mapping_confidence: str


@dataclass(frozen=True)
class PageLayout:
    page_number: int
    transform: CoordinateTransform
    systems: tuple[ScoreSystem, ...]


@dataclass(frozen=True)
class SystemBatchSpec:
    batch_id: str
    page_number: int
    system_index: int
    target_measures: tuple[int, ...]
    context_range: tuple[int, int]
    mapping_confidence: str


@dataclass(frozen=True)
class SystemCrop:
    page_number: int
    system_index: int
    source_path: Path
    output_path: Path
    raw_bbox: tuple[int, int, int, int]


def load_page_layout(
    layout_path: Path,
    geometry_path: Path,
    page_number: int,
    measure_offset: int,
) -> PageLayout:
    layout = _read_object(layout_path)
    geometry = _read_object(geometry_path)
    if layout.get("schema_version") != 1 or geometry.get("schema_version") != 1:
        raise ValueError("unsupported page layout schema")

    transform_data = _mapping(layout.get("transform"), "layout transform")
    raw_size_data = _mapping(geometry.get("raw_size"), "raw size")
    input_size_data = _mapping(geometry.get("input_size"), "input size")
    source_size = _int_tuple(transform_data.get("source_size"), 2, "source size")
    input_size = (int(input_size_data.get("width", 0)), int(input_size_data.get("height", 0)))
    if source_size != input_size:
        raise ValueError("HOMR source dimensions do not match the saved page crop")

    transform = CoordinateTransform(
        raw_size=(int(raw_size_data.get("width", 0)), int(raw_size_data.get("height", 0))),
        input_bounds_in_raw=_int_tuple(
            geometry.get("input_bounds_in_raw"), 4, "input bounds"
        ),
        homr_autocrop_bounds=_int_tuple(
            transform_data.get("autocrop_bounds"), 4, "HOMR crop bounds"
        ),
        recognition_size=_int_tuple(
            transform_data.get("recognition_size"), 2, "recognition size"
        ),
    )

    systems_data = layout.get("systems")
    if not isinstance(systems_data, list):
        raise ValueError("layout systems must be a list")
    systems: list[ScoreSystem] = []
    for raw_system in systems_data:
        item = _mapping(raw_system, "layout system")
        mapping_confidence = str(item.get("mapping_confidence", "ambiguous"))
        local_start = _positive_int(item.get("local_measure_start"), "local measure start")
        local_end = _positive_int(item.get("local_measure_end"), "local measure end")
        if local_end < local_start:
            raise ValueError("local measure range is reversed")
        raw_staffs = item.get("staff_bboxes")
        if not isinstance(raw_staffs, list):
            raise ValueError("staff_bboxes must be a list")
        systems.append(
            ScoreSystem(
                page_number=page_number,
                system_index=int(item.get("system_index", -1)),
                bbox=_int_tuple(item.get("bbox"), 4, "system bounds"),
                staff_bboxes=tuple(
                    _int_tuple(staff, 4, "staff bounds") for staff in raw_staffs
                ),
                global_measure_start=measure_offset + local_start,
                global_measure_end=measure_offset + local_end,
                notehead_counts=(
                    _int_tuple(
                        item.get("measure_notehead_counts"),
                        local_end - local_start + 1,
                        "measure notehead counts",
                    )
                    if mapping_confidence == "high"
                    else _int_values(
                        item.get("measure_notehead_counts"), "measure notehead counts"
                    )
                ),
                mapping_confidence=mapping_confidence,
            )
        )
    return PageLayout(page_number=page_number, transform=transform, systems=tuple(systems))


def group_overflow_findings(
    findings: Sequence[Mapping[str, object]],
    pages: Sequence[PageLayout],
) -> tuple[SystemBatchSpec, ...]:
    systems = [system for page in pages for system in page.systems]
    grouped: dict[tuple[int, int], set[int]] = {}
    selected_systems: dict[tuple[int, int], ScoreSystem] = {}
    for finding in findings:
        if (
            finding.get("kind") != "timing_measure_overflow"
            or finding.get("severity") != "high"
        ):
            continue
        measure = _positive_int(finding.get("measure_start"), "finding measure")
        matches = [
            system
            for system in systems
            if system.global_measure_start <= measure <= system.global_measure_end
        ]
        if len(matches) != 1:
            continue
        system = matches[0]
        key = (system.page_number, system.system_index)
        grouped.setdefault(key, set()).add(measure)
        selected_systems[key] = system

    batches = []
    for key in sorted(grouped):
        system = selected_systems[key]
        targets = tuple(sorted(grouped[key]))
        batches.append(
            SystemBatchSpec(
                batch_id=(
                    f"p{system.page_number}-s{system.system_index}-m"
                    + "-".join(str(measure) for measure in targets)
                ),
                page_number=system.page_number,
                system_index=system.system_index,
                target_measures=targets,
                context_range=(system.global_measure_start, system.global_measure_end),
                mapping_confidence=system.mapping_confidence,
            )
        )
    return tuple(batches)


def crop_system_from_raw_page(
    page: PageLayout,
    system: ScoreSystem,
    raw_page_path: Path,
    output_path: Path,
    padding_spaces: float = 4.0,
) -> SystemCrop:
    image = cv2.imread(str(raw_page_path), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError(f"cannot read raw page image: {raw_page_path}")
    expected_width, expected_height = page.transform.raw_size
    if image.shape[1] != expected_width or image.shape[0] != expected_height:
        raise ValueError("raw page dimensions do not match layout metadata")

    left, top, right, bottom = page.transform.recognition_bbox_to_raw(system.bbox)
    staff_heights = [staff[3] - staff[1] for staff in system.staff_bboxes]
    recognition_space = median(staff_heights) / 4 if staff_heights else 0
    padding = math.ceil(
        recognition_space
        * page.transform.raw_y_per_recognition_pixel
        * max(0.0, padding_spaces)
    )
    padded_top = max(0, top - padding)
    padded_bottom = min(image.shape[0], bottom + padding)
    ordered_systems = sorted(page.systems, key=lambda item: item.bbox[1])
    system_position = next(
        (
            index
            for index, candidate in enumerate(ordered_systems)
            if candidate.system_index == system.system_index
        ),
        None,
    )
    if system_position is not None and system_position > 0:
        previous_bottom = page.transform.recognition_bbox_to_raw(
            ordered_systems[system_position - 1].bbox
        )[3]
        padded_top = max(padded_top, (previous_bottom + top) // 2)
    if system_position is not None and system_position + 1 < len(ordered_systems):
        next_top = page.transform.recognition_bbox_to_raw(
            ordered_systems[system_position + 1].bbox
        )[1]
        padded_bottom = min(padded_bottom, (bottom + next_top) // 2)
    raw_bbox = (
        max(0, left - padding),
        padded_top,
        min(image.shape[1], right + padding),
        padded_bottom,
    )
    if raw_bbox[2] <= raw_bbox[0] or raw_bbox[3] <= raw_bbox[1]:
        raise ValueError("mapped system crop is empty")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if not cv2.imwrite(
        str(output_path),
        image[raw_bbox[1] : raw_bbox[3], raw_bbox[0] : raw_bbox[2]],
    ):
        raise OSError(f"failed to write system crop: {output_path}")
    return SystemCrop(
        page_number=page.page_number,
        system_index=system.system_index,
        source_path=raw_page_path,
        output_path=output_path,
        raw_bbox=raw_bbox,
    )


def _read_object(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return value


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    return value


def _int_tuple(value: object, length: int, label: str) -> tuple[int, ...]:
    if not isinstance(value, (list, tuple)) or len(value) != length:
        raise ValueError(f"{label} must contain {length} integers")
    if any(isinstance(item, bool) or not isinstance(item, int) for item in value):
        raise ValueError(f"{label} must contain {length} integers")
    return tuple(value)


def _int_values(value: object, label: str) -> tuple[int, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{label} must be a list of integers")
    if any(isinstance(item, bool) or not isinstance(item, int) for item in value):
        raise ValueError(f"{label} must be a list of integers")
    return tuple(value)


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value
