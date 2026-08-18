import base64
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

from sheet2music.core.reidentify import (
    MAX_REGION_UPLOAD_BYTES,
    replace_musicxml_measure_range,
    replace_selected_musicxml_measures,
    run_region_reidentification,
    validate_region_request,
)


PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)
JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"0" * 32
WEBP_BYTES = b"RIFF" + b"\x00" * 4 + b"WEBP" + b"0" * 16


def _score_xml() -> ET.Element:
    return ET.fromstring(
        """
        <score-partwise version="4.0">
          <work><work-title>Keep metadata</work-title></work>
          <part-list><score-part id="P1"><part-name>Piano</part-name></score-part></part-list>
          <part id="P1">
            <measure number="1"><barline><bar-style>regular</bar-style></barline></measure>
            <measure number="2"><note><rest/><duration>8</duration></note></measure>
            <measure number="3"><note><rest/><duration>12</duration></note></measure>
            <measure number="4"><note><rest/><duration>16</duration></note></measure>
          </part>
        </score-partwise>
        """
    )


class RegionValidationTest(unittest.TestCase):
    def test_accepts_png_jpeg_and_webp(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            for name, contents in (
                ("crop.png", PNG_BYTES),
                ("crop.jpg", JPEG_BYTES),
                ("crop.webp", WEBP_BYTES),
            ):
                path = Path(temp_dir) / name
                path.write_bytes(contents)
                validate_region_request(path, 2, 2, 4)

    def test_rejects_reversed_or_out_of_score_range(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "crop.png"
            path.write_bytes(PNG_BYTES)
            for start, end in ((2, 1), (0, 1), (1, 5)):
                with self.assertRaises(ValueError):
                    validate_region_request(path, start, end, 4)

    def test_rejects_oversized_upload(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "crop.png"
            with path.open("wb") as handle:
                handle.write(PNG_BYTES)
                handle.truncate(MAX_REGION_UPLOAD_BYTES + 1)
            with self.assertRaises(ValueError):
                validate_region_request(path, 1, 1, 4)


class MusicXMLReplacementTest(unittest.TestCase):
    def test_replaces_only_requested_range_and_restores_global_numbers(self) -> None:
        replacement = ET.fromstring(
            """
            <score-partwise version="4.0">
              <part id="P1">
                <measure number="1"><note><rest/><duration>20</duration></note></measure>
                <measure number="2"><note><rest/><duration>24</duration></note></measure>
              </part>
            </score-partwise>
            """
        )
        base = _score_xml()

        replace_musicxml_measure_range(base, replacement, 2, 3)

        part = base.find("part")
        self.assertIsNotNone(part)
        measures = part.findall("measure")
        self.assertEqual([measure.get("number") for measure in measures], ["1", "2", "3", "4"])
        self.assertEqual([measure.findtext("note/duration") for measure in measures], [None, "20", "24", "16"])
        self.assertEqual(base.findtext("work/work-title"), "Keep metadata")
        self.assertEqual(base.find("part-list/score-part").get("id"), "P1")

    def test_restores_prior_divisions_after_a_reidentified_range(self) -> None:
        base = ET.fromstring(
            """
            <score-partwise version="4.0">
              <part id="P1">
                <measure number="1"><attributes><divisions>24</divisions></attributes><note><rest/><duration>96</duration></note></measure>
                <measure number="2"><note><rest/><duration>96</duration></note></measure>
                <measure number="3"><note><rest/><duration>96</duration></note></measure>
              </part>
            </score-partwise>
            """
        )
        replacement = ET.fromstring(
            """
            <score-partwise version="4.0">
              <part id="P1">
                <measure number="1"><attributes><divisions>12</divisions></attributes><note><rest/><duration>48</duration></note></measure>
              </part>
            </score-partwise>
            """
        )

        replace_musicxml_measure_range(base, replacement, 2, 2)

        measures = base.find("part").findall("measure")
        self.assertEqual(measures[1].findtext("attributes/divisions"), "12")
        self.assertEqual(measures[2].findtext("attributes/divisions"), "24")

    def test_sparse_targets_leave_context_measure_unchanged(self) -> None:
        base = _score_xml()
        replacement = ET.fromstring(
            """
            <score-partwise version="4.0">
              <part id="P1">
                <measure number="1"><note><rest/><duration>20</duration></note></measure>
                <measure number="2"><note><rest/><duration>24</duration></note></measure>
                <measure number="3"><note><rest/><duration>28</duration></note></measure>
              </part>
            </score-partwise>
            """
        )

        replace_selected_musicxml_measures(
            base,
            replacement,
            candidate_global_start=2,
            target_measure_numbers=(2, 4),
        )

        measures = base.find("part").findall("measure")
        self.assertEqual(
            [measure.findtext("note/duration") for measure in measures],
            [None, "20", "12", "28"],
        )

    def test_sparse_target_keeps_candidate_inherited_divisions(self) -> None:
        base = ET.fromstring(
            """
            <score-partwise><part id="P1">
              <measure number="1"><attributes><divisions>24</divisions></attributes><note><rest/><duration>96</duration></note></measure>
              <measure number="2"><note><rest/><duration>96</duration></note></measure>
              <measure number="3"><note><rest/><duration>96</duration></note></measure>
            </part></score-partwise>
            """
        )
        candidate = ET.fromstring(
            """
            <score-partwise><part id="P1">
              <measure number="1"><attributes><divisions>12</divisions></attributes><note><rest/><duration>48</duration></note></measure>
              <measure number="2"><note><rest/><duration>48</duration></note></measure>
            </part></score-partwise>
            """
        )

        replace_selected_musicxml_measures(
            base,
            candidate,
            candidate_global_start=1,
            target_measure_numbers=(2,),
        )

        measures = base.find("part").findall("measure")
        self.assertEqual(measures[1].findtext("attributes/divisions"), "12")
        self.assertEqual(measures[2].findtext("attributes/divisions"), "24")

    def test_region_runner_keeps_raw_and_writes_merged_xml(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            base_path = root / "score.raw.musicxml"
            image_path = root / "crop.png"
            homr_work_dir = root / "region-homr"
            raw_path = root / "regions" / "raw" / "finding.musicxml"
            merged_path = root / "regions" / "merged" / "finding.musicxml"
            ET.ElementTree(_score_xml()).write(base_path, encoding="unicode")
            image_path.write_bytes(PNG_BYTES)

            replacement_path = root / "replacement.musicxml"
            replacement_path.write_text(
                """
                <score-partwise version="4.0">
                  <part id="P1">
                    <measure number="1"><note><rest/><duration>20</duration></note></measure>
                    <measure number="2"><note><rest/><duration>24</duration></note></measure>
                  </part>
                </score-partwise>
                """,
                encoding="utf-8",
            )

            with mock.patch(
                "sheet2music.core.reidentify.run_homr_on_page",
                return_value=replacement_path,
            ) as run_homr:
                result = run_region_reidentification(
                    base_path,
                    image_path,
                    raw_path,
                    merged_path,
                    homr_work_dir,
                    measure_start=2,
                    measure_end=3,
                    score_measure_count=4,
                    tempo_bpm=80,
                    use_gpu=True,
                )

            self.assertEqual(result["raw_xml"], raw_path)
            self.assertEqual(result["merged_xml"], merged_path)
            self.assertEqual(raw_path.read_bytes(), replacement_path.read_bytes())
            merged = ET.parse(merged_path).getroot()
            durations = [
                measure.findtext("note/duration")
                for measure in merged.find("part").findall("measure")
            ]
            self.assertEqual(durations, [None, "20", "24", "16"])
            self.assertEqual(run_homr.call_args.kwargs["work_dir"], homr_work_dir)
            self.assertTrue(run_homr.call_args.kwargs["use_gpu"])


if __name__ == "__main__":
    unittest.main()
