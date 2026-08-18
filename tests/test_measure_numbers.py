import unittest

import numpy as np

from sheet2music.core.measure_numbers import (
    MeasureNumberAnchor,
    build_number_mapping,
    extract_single_ocr_number,
    parse_ocr_measure_number,
)


class MeasureNumberMappingTest(unittest.TestCase):
    def test_pickup_creates_one_based_ordinal_to_printed_offset(self) -> None:
        mapping = build_number_mapping(
            [
                MeasureNumberAnchor(system_index=0, measure_ordinal=13, display_measure_number=12),
                MeasureNumberAnchor(system_index=1, measure_ordinal=17, display_measure_number=16),
            ],
            ordinal_start=1,
            ordinal_end=20,
        )

        self.assertEqual(mapping.offset, -1)
        self.assertEqual(mapping.display_for_ordinal(13), 12)
        self.assertEqual(mapping.ordinal_for_display(16), 17)
        self.assertEqual(mapping.confidence, "high")

    def test_conflicting_row_anchors_are_not_accepted(self) -> None:
        with self.assertRaises(ValueError):
            build_number_mapping(
                [
                    MeasureNumberAnchor(system_index=0, measure_ordinal=13, display_measure_number=12),
                    MeasureNumberAnchor(system_index=1, measure_ordinal=17, display_measure_number=17),
                ],
                ordinal_start=1,
                ordinal_end=20,
            )

    def test_ocr_parser_accepts_only_a_single_plain_integer(self) -> None:
        self.assertEqual(parse_ocr_measure_number("12"), 12)
        self.assertIsNone(parse_ocr_measure_number("measure 12"))
        self.assertIsNone(parse_ocr_measure_number("12 13"))

    def test_ocr_result_must_contain_one_high_confidence_number(self) -> None:
        class Result:
            txts = ["12"]
            scores = [0.95]

        self.assertEqual(extract_single_ocr_number(Result()), 12)
        self.assertIsNone(
            extract_single_ocr_number(type("Result", (), {"txts": ["12", "13"], "scores": [0.95, 0.95]})())
        )

    def test_row_anchor_ocr_is_clipped_to_requested_header(self) -> None:
        from sheet2music.core.measure_numbers import recognize_row_anchor

        image = np.zeros((20, 30, 3), dtype=np.uint8)
        seen = []

        def reader(crop):
            seen.append(crop.shape[:2])
            return {"txts": ["12"], "scores": [0.95]}

        anchor = recognize_row_anchor(
            image,
            system_index=2,
            measure_ordinal=13,
            bbox=(-5, 2, 15, 12),
            reader=reader,
        )
        self.assertEqual(anchor.display_measure_number, 12)
        self.assertEqual(seen, [(10, 15)])


if __name__ == "__main__":
    unittest.main()
