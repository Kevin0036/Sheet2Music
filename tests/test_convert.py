"""端到端集成冒烟：需要 pdftoppm + HOMR + MuseScore + 一个样例 PDF。

缺少任一条件时自动跳过（单元测试不依赖外部工具）。
"""

import os
import shutil
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from sheet2music.core import repair
from sheet2music.core.convert import (
    ConversionError,
    run_conversion,
    validate_musicxml_boundaries,
)
from sheet2music.core.models import ConvertParams
from sheet2music.core.settings import homr_root, musescore_binary, pdftoppm_binary
from sheet2music.core.workspace import JobWorkspace


NAUTILUS_STRUCTURE_PLAN = {
    "default_time_signature": "4/4",
    "time_signature_changes": [
        {"from_measure": 25, "to_measure": 25, "signature": "2/4"},
        {"from_measure": 26, "signature": "4/4"},
    ],
    "clef_overrides": [
        {"staff": 2, "from_measure": 14, "to_measure": 16, "sign": "G", "line": 2},
        {"staff": 2, "from_measure": 17, "sign": "F", "line": 4},
    ],
    "key_signature": {"fifths": -5},
}


def _tools_available() -> bool:
    try:
        pdftoppm_binary()
        musescore_binary()
        homr_root()
        return True
    except Exception:
        return False


def _sample_pdf() -> Path | None:
    env_pdf = os.environ.get("SHEET2MUSIC_TEST_PDF") or os.environ.get("HOMR_TOOL_TEST_PDF")
    if env_pdf and Path(env_pdf).exists():
        return Path(env_pdf)
    # 在父仓库中运行时，默认用 ai-piano-arranger 的素材。
    parent_repo = Path(__file__).resolve().parents[2]  # Sheet2Music/ 的上一级
    for pattern in (
        parent_repo / "assets" / "raw" / "sheets",
        Path(__file__).resolve().parents[1] / "tests" / "fixtures",
    ):
        candidates = sorted(pattern.glob("*.pdf"))
        if candidates:
            return candidates[0]
    return None


class NautilusStructureFixtureTest(unittest.TestCase):
    def test_structure_fixture_keeps_default_signature_compatible(self) -> None:
        params = ConvertParams.validate(
            80,
            "4/4",
            ["musicxml"],
            structure_plan=NAUTILUS_STRUCTURE_PLAN,
        )
        self.assertEqual(params.time_signature, "4/4")
        self.assertEqual(params.structure_plan["time_signature_changes"][0]["signature"], "2/4")

    def test_unresolved_overflow_is_rejected_before_export(self) -> None:
        root = ET.fromstring(
            """
            <score-partwise version="4.0">
              <part id="P1">
                <measure number="72">
                  <attributes><divisions>4</divisions></attributes>
                  <note><rest/><duration>20</duration><voice>1</voice></note>
                </measure>
              </part>
            </score-partwise>
            """
        )

        with self.assertRaisesRegex(ConversionError, "第 72 小节"):
            validate_musicxml_boundaries(
                root,
                repair.ScoreStructurePlan.from_dict({}),
            )

    def test_underfilled_measure_passes_export_boundary_validation(self) -> None:
        root = ET.fromstring(
            """
            <score-partwise version="4.0">
              <part id="P1">
                <measure number="1">
                  <attributes><divisions>4</divisions></attributes>
                  <note><rest/><duration>8</duration><voice>1</voice></note>
                </measure>
              </part>
            </score-partwise>
            """
        )

        validate_musicxml_boundaries(root, repair.ScoreStructurePlan.from_dict({}))


@unittest.skipUnless(_tools_available() and _sample_pdf(), "缺少外部工具或样例 PDF")
class ConvertIntegrationTest(unittest.TestCase):
    def test_single_page_end_to_end(self) -> None:
        pdf = _sample_pdf()
        assert pdf is not None
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = JobWorkspace(Path(temp_dir) / "job").create()
            shutil.copy2(pdf, workspace.pdf_path)
            params = ConvertParams(bpm=120, time_signature="4/4", outputs=["musicxml", "midi", "zip"])

            stages: list[str] = []
            report = run_conversion(workspace, params, stage=stages.append, max_pages=1)

            self.assertEqual(report["page_count"], 1)
            self.assertGreaterEqual(report["num_parts"], 1)
            self.assertGreater(report["num_measures"], 0)

            self.assertTrue((workspace.output_dir / "score.musicxml").exists())
            self.assertTrue((workspace.output_dir / "score.mid").exists())
            self.assertTrue((workspace.output_dir / "score.zip").exists())
            self.assertTrue((workspace.output_dir / "report.json").exists())

            # 阶段回调按序执行并最终 completed。
            self.assertIn("running_homr", stages)
            self.assertIn("repairing_musicxml", stages)
            self.assertIn("exporting_midi", stages)
            self.assertEqual(stages[-1], "completed")


if __name__ == "__main__":
    unittest.main()
