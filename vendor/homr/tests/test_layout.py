import json
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np

from homr.autocrop import autocrop_with_bounds
from homr.layout import build_page_layout
from homr.main import ProcessingConfig, main, process_image


def fake_multistaff(
    y: int,
    barline_x: tuple[int, ...],
    note_x: tuple[int, ...],
) -> SimpleNamespace:
    def staff(offset: int) -> SimpleNamespace:
        return SimpleNamespace(
            min_x=10,
            max_x=190,
            min_y=y + offset,
            max_y=y + offset + 40,
            average_unit_size=10,
            get_bar_lines=lambda: [
                SimpleNamespace(center=(x, y + offset + 20)) for x in barline_x
            ],
            get_notes=lambda: [
                SimpleNamespace(center=(x, y + offset + 20)) for x in note_x
            ],
        )

    return SimpleNamespace(staffs=[staff(0), staff(80)])


def score_with_two_systems() -> ET.Element:
    return ET.fromstring(
        """
        <score-partwise>
          <part id="P1">
            <measure number="1"/>
            <measure number="2"/>
            <measure number="3"><print new-system="yes"/></measure>
            <measure number="4"/>
          </part>
        </score-partwise>
        """
    )


class LayoutTest(unittest.TestCase):
    def test_autocrop_with_bounds_returns_identity_for_full_page(self) -> None:
        image = np.full((100, 80, 3), 255, dtype=np.uint8)

        cropped, bounds = autocrop_with_bounds(image)

        self.assertEqual(cropped.shape, image.shape)
        self.assertEqual(bounds, (0, 0, 80, 100))

    def test_build_layout_orders_systems_and_deduplicates_barlines(self) -> None:
        lower = fake_multistaff(y=400, barline_x=(10, 99, 101, 190), note_x=(45, 130))
        upper = fake_multistaff(y=100, barline_x=(10, 100, 190), note_x=(40, 140))

        layout = build_page_layout(
            [lower, upper],
            score_with_two_systems(),
            source_size=(2400, 3600),
            autocrop_bounds=(100, 200, 2300, 3400),
            recognition_size=(1920, 2793),
        )

        self.assertEqual([item.system_index for item in layout.systems], [0, 1])
        self.assertEqual(layout.systems[0].barline_x, (10, 100, 190))
        self.assertEqual(layout.systems[0].local_measure_start, 1)
        self.assertEqual(layout.systems[0].local_measure_end, 2)
        self.assertEqual(layout.systems[0].mapping_confidence, "high")
        self.assertEqual(layout.transform.autocrop_bounds, (100, 200, 2300, 3400))

    def test_layout_writer_keeps_transform_and_measure_evidence(self) -> None:
        layout = build_page_layout(
            [fake_multistaff(y=100, barline_x=(10, 100, 190), note_x=(40, 140))],
            ET.fromstring(
                "<score-partwise><part id='P1'><measure number='1'/><measure number='2'/></part></score-partwise>"
            ),
            source_size=(240, 360),
            autocrop_bounds=(0, 0, 240, 360),
            recognition_size=(192, 288),
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "layout.json"

            layout.write(output)

            payload = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["transform"]["source_size"], [240, 360])
        self.assertEqual(payload["systems"][0]["measure_notehead_counts"], [2, 2])

    def test_mismatched_system_count_marks_mapping_ambiguous(self) -> None:
        layout = build_page_layout(
            [fake_multistaff(y=100, barline_x=(10, 100, 190), note_x=(40, 140))],
            score_with_two_systems(),
            source_size=(240, 360),
            autocrop_bounds=(0, 0, 240, 360),
            recognition_size=(192, 288),
        )

        self.assertEqual(layout.systems[0].mapping_confidence, "ambiguous")

    def test_build_layout_uses_detected_page_barlines_when_staff_symbols_are_empty(self) -> None:
        system = fake_multistaff(y=100, barline_x=(), note_x=(40, 90, 140))
        detected_barlines = [
            SimpleNamespace(center=(x, 160)) for x in (10, 70, 130, 190)
        ]
        score = ET.fromstring(
            "<score-partwise><part id='P1'>"
            "<measure number='1'/><measure number='2'/><measure number='3'/>"
            "</part></score-partwise>"
        )

        layout = build_page_layout(
            [system],
            score,
            source_size=(240, 360),
            autocrop_bounds=(0, 0, 240, 360),
            recognition_size=(192, 288),
            detected_barlines=detected_barlines,
        )

        self.assertEqual(layout.systems[0].barline_x, (10, 70, 130, 190))
        self.assertEqual(layout.systems[0].mapping_confidence, "high")

    def test_build_layout_does_not_guess_missing_left_boundary(self) -> None:
        system = fake_multistaff(y=100, barline_x=(), note_x=(40, 90, 140))
        detected_barlines = [
            SimpleNamespace(center=(x, 160)) for x in (70, 130, 190)
        ]
        score = ET.fromstring(
            "<score-partwise><part id='P1'>"
            "<measure number='1'/><measure number='2'/><measure number='3'/>"
            "</part></score-partwise>"
        )

        layout = build_page_layout(
            [system],
            score,
            source_size=(240, 360),
            autocrop_bounds=(0, 0, 240, 360),
            recognition_size=(192, 288),
            detected_barlines=detected_barlines,
        )

        self.assertEqual(layout.systems[0].barline_x, (70, 130, 190))
        self.assertEqual(layout.systems[0].measure_notehead_counts, (2, 2))
        self.assertEqual(layout.systems[0].mapping_confidence, "ambiguous")

    def test_main_forwards_layout_output_to_processing_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            image = Path(temp_dir) / "page.png"
            layout = Path(temp_dir) / "page.layout.json"
            image.touch()
            argv = ["homr", "--gpu", "no", "--layout-output", str(layout), str(image)]

            with (
                mock.patch("sys.argv", argv),
                mock.patch("homr.main.download_weights"),
                mock.patch("homr.main.process_image") as process_image,
            ):
                main()

        config = process_image.call_args.args[1]
        self.assertEqual(config.layout_output, str(layout))

    def test_process_image_forwards_detected_barlines_to_layout(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            image_path = Path(temp_dir) / "page.png"
            layout_path = Path(temp_dir) / "page.layout.json"
            image_path.touch()
            system = fake_multistaff(y=100, barline_x=(), note_x=(40, 140))
            detected_barlines = [SimpleNamespace(center=(10, 160))]
            debug = SimpleNamespace(
                clean_debug_files_from_previous_runs=lambda: None,
                write_teaser=lambda *_: None,
            )
            title = SimpleNamespace(result=lambda *_: "")
            config = ProcessingConfig(
                enable_debug=False,
                enable_cache=False,
                write_staff_positions=False,
                read_staff_positions=False,
                selected_staff=-1,
                transformer_use_gpu=False,
                segnet_use_gpu=False,
                coreml_encoder=False,
                layout_output=str(layout_path),
            )
            score = ET.fromstring(
                "<score-partwise><part id='P1'><measure number='1'/></part></score-partwise>"
            )

            with (
                mock.patch(
                    "homr.main.detect_staffs_in_image",
                    return_value=(
                        [system],
                        np.zeros((288, 192), dtype=np.uint8),
                        debug,
                        title,
                        2,
                        (240, 360),
                        (0, 0, 240, 360),
                        detected_barlines,
                    ),
                ),
                mock.patch("homr.main.parse_staffs", return_value=[[]]),
                mock.patch("homr.main.generate_xml", return_value=score),
                mock.patch("homr.main.build_page_layout") as build_layout,
            ):
                build_layout.return_value.write = mock.Mock()
                process_image(str(image_path), config, mock.sentinel.xml_args)

        self.assertIs(
            build_layout.call_args.kwargs["detected_barlines"], detected_barlines
        )


if __name__ == "__main__":
    unittest.main()
