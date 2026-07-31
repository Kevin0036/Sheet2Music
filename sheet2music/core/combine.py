"""页级 MusicXML 合并为完整乐谱（与 `run_homr_trial.py` 的 combine_page_musicxml 一致）。"""

from __future__ import annotations

import copy
import xml.etree.ElementTree as ET
from pathlib import Path


def _renumber_measures(part: ET.Element) -> None:
    for measure_index, measure in enumerate(part.findall("measure"), start=1):
        measure.set("number", str(measure_index))


def combine_page_musicxml(page_xmls: list[Path], output_path: Path) -> dict[str, int]:
    if not page_xmls:
        raise ValueError("No page-level MusicXML files to combine.")

    first_root = ET.parse(page_xmls[0]).getroot()
    combined_root = copy.deepcopy(first_root)
    combined_parts = {part.get("id", ""): part for part in combined_root.findall("part")}
    part_list = combined_root.find("part-list")
    combined_score_parts = {}
    if part_list is not None:
        combined_score_parts = {
            score_part.get("id", ""): score_part for score_part in part_list.findall("score-part")
        }

    for extra_xml in page_xmls[1:]:
        extra_root = ET.parse(extra_xml).getroot()
        extra_part_list = extra_root.find("part-list")
        extra_score_parts = {}
        if extra_part_list is not None:
            extra_score_parts = {
                score_part.get("id", ""): score_part for score_part in extra_part_list.findall("score-part")
            }

        for extra_part in extra_root.findall("part"):
            part_id = extra_part.get("id", "")
            target_part = combined_parts.get(part_id)
            if target_part is None:
                target_part = ET.Element("part", extra_part.attrib)
                combined_root.append(target_part)
                combined_parts[part_id] = target_part
                if part_list is not None and part_id in extra_score_parts and part_id not in combined_score_parts:
                    part_list.append(copy.deepcopy(extra_score_parts[part_id]))
                    combined_score_parts[part_id] = part_list[-1]

            for measure in extra_part.findall("measure"):
                target_part.append(copy.deepcopy(measure))

    for part in combined_root.findall("part"):
        _renumber_measures(part)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(combined_root).write(output_path, encoding="unicode", xml_declaration=True)
    num_parts = len(combined_root.findall("part"))
    num_measures = max((len(part.findall("measure")) for part in combined_root.findall("part")), default=0)
    return {"num_parts": num_parts, "num_measures": num_measures}
