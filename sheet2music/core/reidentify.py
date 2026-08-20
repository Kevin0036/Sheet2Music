"""Validation and safe replacement helpers for user-selected score regions."""

from __future__ import annotations

import copy
import xml.etree.ElementTree as ET
from pathlib import Path

from .homr import run_homr_on_page


MAX_REGION_UPLOAD_BYTES = 20 * 1024 * 1024


def validate_region_request(
    image_path: Path,
    measure_start: int,
    measure_end: int,
    score_measure_count: int,
) -> None:
    """Validate a region image and its inclusive full-score measure range."""
    if not image_path.exists() or not image_path.is_file():
        raise ValueError("region image does not exist")
    if image_path.stat().st_size > MAX_REGION_UPLOAD_BYTES:
        raise ValueError("region image exceeds the 20MB limit")
    if not isinstance(score_measure_count, int) or isinstance(score_measure_count, bool):
        raise ValueError("score_measure_count must be an integer")
    if score_measure_count <= 0:
        raise ValueError("score_measure_count must be positive")
    if not _is_supported_image(image_path):
        raise ValueError("region image must be PNG, JPEG, or WEBP")
    if not _is_positive_int(measure_start) or not _is_positive_int(measure_end):
        raise ValueError("measure range must use positive integers")
    if measure_start > measure_end:
        raise ValueError("measure_start must not be after measure_end")
    if measure_end > score_measure_count:
        raise ValueError("measure range is outside the score")


def replace_musicxml_measure_range(
    base_root: ET.Element,
    replacement_root: ET.Element,
    measure_start: int,
    measure_end: int,
) -> None:
    """Replace an inclusive range while retaining full-score numbering and metadata.

    A cropped HOMR result normally numbers its first measure as ``1``. When its
    measure numbers already match the requested full-score range, those numbers
    are used directly; otherwise the replacement measures are mapped in order
    onto the requested range.
    """
    if not _is_positive_int(measure_start) or not _is_positive_int(measure_end):
        raise ValueError("measure range must use positive integers")
    if measure_start > measure_end:
        raise ValueError("measure_start must not be after measure_end")

    base_parts = _parts_by_id(base_root)
    replacement_parts = _parts_by_id(replacement_root)
    if not replacement_parts:
        raise ValueError("replacement MusicXML contains no parts")

    span = measure_end - measure_start + 1
    target_numbers = list(range(measure_start, measure_end + 1))
    for part_id, replacement_part in replacement_parts.items():
        base_part = base_parts.get(part_id)
        if base_part is None:
            raise ValueError(f"replacement contains unknown part id: {part_id}")

        base_measures = base_part.findall("measure")
        base_by_number = _measure_map(base_measures, "base")
        if any(number not in base_by_number for number in target_numbers):
            raise ValueError(f"base MusicXML does not contain the requested range for part {part_id}")
        following_measure = base_by_number.get(measure_end + 1)
        following_divisions = _effective_divisions_at(base_measures, following_measure)

        replacement_measures = replacement_part.findall("measure")
        if len(replacement_measures) != span:
            raise ValueError(
                f"replacement part {part_id} contains {len(replacement_measures)} measures; "
                f"expected {span}"
            )
        replacement_by_number = _measure_map(replacement_measures, "replacement")
        if all(number in replacement_by_number for number in target_numbers):
            ordered_replacements = [replacement_by_number[number] for number in target_numbers]
        else:
            ordered_replacements = replacement_measures

        positions = {id(measure): index for index, measure in enumerate(base_measures)}
        for target_number, replacement_measure in zip(target_numbers, ordered_replacements, strict=True):
            old_measure = base_by_number[target_number]
            new_measure = copy.deepcopy(replacement_measure)
            new_measure.set("number", str(target_number))
            index = positions[id(old_measure)]
            base_part.remove(old_measure)
            base_part.insert(index, new_measure)
            base_measures[index] = new_measure
            base_by_number[target_number] = new_measure
            positions = {id(measure): index for index, measure in enumerate(base_measures)}

        # A crop may use a different divisions value. Its scope ends with the
        # replacement range, so restore the original effective value for the
        # first untouched measure when it would otherwise inherit the crop's.
        if following_measure is not None and following_divisions is not None:
            current_divisions = _effective_divisions_at(base_measures, following_measure)
            if current_divisions != following_divisions:
                _set_measure_divisions(following_measure, following_divisions)


def replace_selected_musicxml_measures(
    base_root: ET.Element,
    candidate_root: ET.Element,
    candidate_global_start: int,
    target_measure_numbers: tuple[int, ...],
) -> None:
    """Replace selected global measures from a candidate system recognition.

    Candidate MusicXML uses local ordinal measure numbers. Measures that are
    present only to provide recognition context are deliberately left intact.
    """
    if not _is_positive_int(candidate_global_start):
        raise ValueError("candidate_global_start must be a positive integer")
    if not target_measure_numbers:
        raise ValueError("target_measure_numbers must not be empty")
    if any(not _is_positive_int(number) for number in target_measure_numbers):
        raise ValueError("target measures must use positive integers")
    if len(set(target_measure_numbers)) != len(target_measure_numbers):
        raise ValueError("target measures must be unique")

    candidate_parts = _parts_by_id(candidate_root)
    if not candidate_parts:
        raise ValueError("candidate MusicXML contains no parts")
    candidate_measure_counts = {
        len(part.findall("measure")) for part in candidate_parts.values()
    }
    if len(candidate_measure_counts) != 1:
        raise ValueError("candidate parts must contain the same number of measures")
    candidate_measure_count = candidate_measure_counts.pop()
    candidate_global_end = candidate_global_start + candidate_measure_count - 1
    if any(
        number < candidate_global_start or number > candidate_global_end
        for number in target_measure_numbers
    ):
        raise ValueError("target measure is outside the candidate system")

    for target_number in sorted(target_measure_numbers):
        local_index = target_number - candidate_global_start
        single_root = ET.Element(candidate_root.tag, candidate_root.attrib)
        for part_id, candidate_part in candidate_parts.items():
            candidate_measures = candidate_part.findall("measure")
            single_part = ET.SubElement(single_root, "part", {"id": part_id})
            selected_measure = copy.deepcopy(candidate_measures[local_index])
            candidate_divisions = _effective_divisions_at(
                candidate_measures,
                candidate_measures[local_index],
            )
            if candidate_divisions is not None:
                _set_measure_divisions(selected_measure, candidate_divisions)
            single_part.append(selected_measure)
        replace_musicxml_measure_range(
            base_root,
            single_root,
            target_number,
            target_number,
        )


def run_region_reidentification(
    base_musicxml_path: Path,
    image_path: Path,
    raw_xml_path: Path,
    merged_xml_path: Path,
    homr_work_dir: Path,
    measure_start: int,
    measure_end: int,
    score_measure_count: int,
    tempo_bpm: int | None = None,
    use_gpu: bool = False,
    debug: bool = False,
) -> dict[str, Path]:
    """Recognize a crop and write both raw and merged MusicXML artifacts."""
    validate_region_request(image_path, measure_start, measure_end, score_measure_count)
    raw_result = run_homr_on_page(
        image_path,
        work_dir=homr_work_dir,
        debug=debug,
        tempo_bpm=tempo_bpm,
        use_gpu=use_gpu,
    )
    raw_xml_path.parent.mkdir(parents=True, exist_ok=True)
    raw_xml_path.write_bytes(raw_result.read_bytes())

    base_root = ET.parse(base_musicxml_path).getroot()
    replacement_root = ET.parse(raw_xml_path).getroot()
    replace_musicxml_measure_range(
        base_root,
        replacement_root,
        measure_start,
        measure_end,
    )
    merged_xml_path.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(base_root).write(
        merged_xml_path,
        encoding="utf-8",
        xml_declaration=True,
    )
    return {"raw_xml": raw_xml_path, "merged_xml": merged_xml_path}


def _parts_by_id(root: ET.Element) -> dict[str, ET.Element]:
    parts: dict[str, ET.Element] = {}
    for part in root.findall("part"):
        part_id = part.get("id")
        if not part_id:
            raise ValueError("MusicXML part is missing an id")
        if part_id in parts:
            raise ValueError(f"duplicate MusicXML part id: {part_id}")
        parts[part_id] = part
    return parts


def _measure_map(measures: list[ET.Element], label: str) -> dict[int, ET.Element]:
    result: dict[int, ET.Element] = {}
    for measure in measures:
        raw_number = measure.get("number")
        try:
            number = int(raw_number or "")
        except ValueError as exc:
            raise ValueError(f"{label} measure number must be an integer") from exc
        if number in result:
            raise ValueError(f"duplicate {label} measure number: {number}")
        result[number] = measure
    return result


def _effective_divisions_at(
    measures: list[ET.Element],
    target_measure: ET.Element | None,
) -> int | None:
    if target_measure is None:
        return None
    divisions: int | None = None
    for measure in measures:
        for attributes in measure.findall("attributes"):
            raw_value = attributes.findtext("divisions")
            if raw_value is None:
                continue
            try:
                divisions = int(raw_value)
            except ValueError:
                continue
        if measure is target_measure:
            return divisions
    return None


def _set_measure_divisions(measure: ET.Element, divisions: int) -> None:
    attributes = measure.find("attributes")
    if attributes is None:
        attributes = ET.Element("attributes")
        measure.insert(0, attributes)
    division_element = attributes.find("divisions")
    if division_element is None:
        division_element = ET.Element("divisions")
        attributes.insert(0, division_element)
    division_element.text = str(divisions)


def _is_supported_image(path: Path) -> bool:
    header = path.read_bytes()[:16]
    return (
        header.startswith(b"\x89PNG\r\n\x1a\n")
        or header.startswith(b"\xff\xd8\xff")
        or (header.startswith(b"RIFF") and header[8:12] == b"WEBP")
    )


def _is_positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0
