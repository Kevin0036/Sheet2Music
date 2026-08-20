import json
import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np

from sheet2music.core.layout import (
    CoordinateTransform,
    annotate_page_layout_with_printed_numbers,
    crop_system_from_raw_page,
    group_overflow_findings,
    load_page_layout,
)
from sheet2music.core.measure_numbers import MeasureNumberAnchor


def write_json(path: Path, payload: dict[str, object]) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def layout_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "transform": {
            "source_size": [1000, 1000],
            "autocrop_bounds": [100, 50, 900, 950],
            "recognition_size": [400, 450],
        },
        "systems": [
            {
                "system_index": 0,
                "bbox": [50, 50, 350, 250],
                "staff_bboxes": [[50, 60, 350, 100], [50, 170, 350, 210]],
                "barline_x": [50, 200, 350],
                "notehead_x": [80, 220],
                "local_measure_start": 1,
                "local_measure_end": 2,
                "measure_notehead_counts": [1, 1],
                "mapping_confidence": "high",
            },
            {
                "system_index": 1,
                "bbox": [50, 270, 350, 430],
                "staff_bboxes": [[50, 280, 350, 320], [50, 370, 350, 410]],
                "barline_x": [50, 200, 350],
                "notehead_x": [90, 230],
                "local_measure_start": 3,
                "local_measure_end": 4,
                "measure_notehead_counts": [1, 1],
                "mapping_confidence": "high",
            },
        ],
    }


def geometry_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "raw_size": {"width": 1000, "height": 1200},
        "input_bounds_in_raw": [0, 100, 1000, 1100],
        "input_size": {"width": 1000, "height": 1000},
    }


def overflow(measure: int) -> dict[str, object]:
    return {
        "id": f"timing_measure_overflow:P1:-:{measure}:{measure}",
        "kind": "timing_measure_overflow",
        "severity": "high",
        "measure_start": measure,
        "measure_end": measure,
    }


class CoordinateTransformTest(unittest.TestCase):
    def test_reverses_homr_resize_autocrop_and_page_crop(self) -> None:
        transform = CoordinateTransform(
            raw_size=(1000, 1200),
            input_bounds_in_raw=(0, 100, 1000, 1100),
            homr_autocrop_bounds=(100, 50, 900, 950),
            recognition_size=(400, 450),
        )

        self.assertEqual(
            transform.recognition_bbox_to_raw((50, 50, 350, 250)),
            (200, 250, 800, 650),
        )


class PageLayoutTest(unittest.TestCase):
    def test_maps_global_measures_and_groups_findings_on_the_same_system(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            page = load_page_layout(
                write_json(root / "layout.json", layout_payload()),
                write_json(root / "geometry.json", geometry_payload()),
                page_number=6,
                measure_offset=64,
            )

            batches = group_overflow_findings(
                [overflow(65), overflow(66), overflow(68)],
                [page],
            )

        self.assertEqual(
            [
                (batch.page_number, batch.system_index, batch.target_measures, batch.context_range)
                for batch in batches
            ],
            [
                (6, 0, (65, 66), (65, 66)),
                (6, 1, (68,), (67, 68)),
            ],
        )

    def test_preserves_ambiguous_mapping_confidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            payload = layout_payload()
            payload["systems"][0]["mapping_confidence"] = "ambiguous"
            page = load_page_layout(
                write_json(root / "layout.json", payload),
                write_json(root / "geometry.json", geometry_payload()),
                page_number=2,
                measure_offset=12,
            )

        self.assertEqual(page.systems[0].mapping_confidence, "ambiguous")

    def test_number_anchor_maps_printed_measure_without_changing_target_ordinal(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            payload = layout_payload()
            payload["systems"][0]["display_measure_start"] = 1
            payload["systems"][0]["display_measure_end"] = 1
            payload["systems"][0]["number_mapping_confidence"] = "high"
            page = load_page_layout(
                write_json(root / "layout.json", payload),
                write_json(root / "geometry.json", geometry_payload()),
                page_number=2,
                measure_offset=12,
            )

        self.assertEqual(page.systems[0].global_measure_start, 13)
        self.assertEqual(page.systems[0].display_measure_start, 1)
        self.assertEqual(page.systems[0].number_mapping_confidence, "high")

    def test_ocr_row_anchors_add_display_numbers_but_keep_ordinals(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            page = load_page_layout(
                write_json(root / "layout.json", layout_payload()),
                write_json(root / "geometry.json", geometry_payload()),
                page_number=2,
                measure_offset=12,
            )
            raw_path = root / "raw.png"
            cv2.imwrite(str(raw_path), np.zeros((1200, 1000, 3), dtype=np.uint8))

            numbers = iter(("12", "14"))

            def reader(_crop):
                return {"txts": [next(numbers)], "scores": [0.95]}

            enriched = annotate_page_layout_with_printed_numbers(page, raw_path, reader)

        self.assertEqual(enriched.systems[0].global_measure_start, 13)
        self.assertEqual(enriched.systems[0].display_measure_start, 12)
        self.assertEqual(enriched.systems[0].display_measure_end, 13)
        self.assertEqual(enriched.systems[0].number_mapping_confidence, "high")

    def test_ambiguous_layout_accepts_missing_measure_notehead_counts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            payload = layout_payload()
            payload["systems"][0]["mapping_confidence"] = "ambiguous"
            payload["systems"][0]["barline_x"] = []
            payload["systems"][0]["measure_notehead_counts"] = []

            page = load_page_layout(
                write_json(root / "layout.json", payload),
                write_json(root / "geometry.json", geometry_payload()),
                page_number=2,
                measure_offset=12,
            )
            batches = group_overflow_findings([overflow(13)], [page])

        self.assertEqual(page.systems[0].notehead_counts, ())
        self.assertEqual(batches[0].mapping_confidence, "ambiguous")

    def test_crop_maps_system_back_to_retained_raw_page(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            page = load_page_layout(
                write_json(root / "layout.json", layout_payload()),
                write_json(root / "geometry.json", geometry_payload()),
                page_number=6,
                measure_offset=64,
            )
            raw_path = root / "raw.png"
            output_path = root / "crop.png"
            raw = np.zeros((1200, 1000, 3), dtype=np.uint8)
            raw[:, :, 0] = np.arange(1000, dtype=np.uint16) % 256
            cv2.imwrite(str(raw_path), raw)

            crop = crop_system_from_raw_page(
                page,
                page.systems[0],
                raw_path,
                output_path,
                padding_spaces=0,
            )

            image = cv2.imread(str(output_path))
        self.assertEqual(crop.raw_bbox, (200, 250, 800, 650))
        self.assertEqual(image.shape[:2], (400, 600))

    def test_crop_padding_stops_at_the_midpoint_to_the_next_system(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            page = load_page_layout(
                write_json(root / "layout.json", layout_payload()),
                write_json(root / "geometry.json", geometry_payload()),
                page_number=6,
                measure_offset=64,
            )
            raw_path = root / "raw.png"
            output_path = root / "crop.png"
            cv2.imwrite(str(raw_path), np.zeros((1200, 1000, 3), dtype=np.uint8))

            crop = crop_system_from_raw_page(
                page,
                page.systems[0],
                raw_path,
                output_path,
                padding_spaces=10,
            )

        self.assertEqual(crop.raw_bbox[3], 670)


if __name__ == "__main__":
    unittest.main()
