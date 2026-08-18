"""convert.py 单元测试：逐页跳过 / 进度上报 / 权重缺失快速失败（不访问外部工具）。

通过 mock 掉 HOMR 子进程与外部命令，专注验证流水线编排行为。
"""

import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

from sheet2music.core.auto_resolution import AutoResolutionOutcome
from sheet2music.core.convert import (
    ConversionError,
    finalize_conversion,
    prepare_conversion,
    resume_automatic_resolution,
    run_conversion,
)
from sheet2music.core.homr import HomrPageError
from sheet2music.core.models import ConvertParams
from sheet2music.core.workspace import JobWorkspace

MINIMAL_XML = b'<score-partwise version="4.0"></score-partwise>'


class _FakeReport:
    def to_dict(self) -> dict[str, object]:
        return {"fake": True}


def _fake_fix(src: Path, dst: Path, *args: object, **kwargs: object) -> _FakeReport:
    dst.write_bytes(src.read_bytes())
    return _FakeReport()


def _fake_fix_midi(src: Path, dst: Path, *args: object, **kwargs: object) -> _FakeReport:
    dst.write_bytes(src.read_bytes())
    return _FakeReport()


def _fake_export(src: Path, dst: Path, *args: object, **kwargs: object) -> None:
    dst.write_bytes(b"MThd")


def _fake_combine(fixed: list[Path], combined: Path) -> dict[str, int]:
    combined.write_text("<score-partwise/>")
    return {"num_parts": 1, "num_measures": 2}


def _fake_combine_with_time_change(fixed: list[Path], combined: Path) -> dict[str, int]:
    combined.write_text(
        """
        <score-partwise version="4.0">
          <part id="P1">
            <measure number="1">
              <attributes><divisions>4</divisions><time><beats>4</beats><beat-type>4</beat-type></time></attributes>
              <note><rest/><duration>16</duration><voice>1</voice><staff>1</staff></note>
            </measure>
            <measure number="2">
              <attributes><time><beats>2</beats><beat-type>4</beat-type></time></attributes>
              <note><rest/><duration>8</duration><voice>1</voice><staff>1</staff></note>
            </measure>
          </part>
        </score-partwise>
        """,
        encoding="utf-8",
    )
    return {"num_parts": 1, "num_measures": 2}


def _fake_combine_with_overflow(fixed: list[Path], combined: Path) -> dict[str, int]:
    combined.write_text(
        """
        <score-partwise version="4.0"><part id="P1">
          <measure number="1"><attributes><divisions>4</divisions><time><beats>4</beats><beat-type>4</beat-type></time></attributes><note><rest/><duration>20</duration><voice>1</voice><staff>1</staff></note></measure>
        </part></score-partwise>
        """,
        encoding="utf-8",
    )
    return {"num_parts": 1, "num_measures": 1}


def _homr_ok(
    page_image: Path,
    work_dir: Path,
    debug: bool = False,
    tempo_bpm: int | None = None,
    use_gpu: bool = False,
    layout_output: Path | None = None,
) -> Path:
    xml = work_dir / f"{page_image.stem}.musicxml"
    xml.parent.mkdir(parents=True, exist_ok=True)
    xml.write_bytes(MINIMAL_XML)
    if layout_output is not None:
        layout_output.parent.mkdir(parents=True, exist_ok=True)
        layout_output.write_text('{"schema_version": 1, "systems": []}', encoding="utf-8")
    return xml


def _homr_skip_page2(
    page_image: Path,
    work_dir: Path,
    debug: bool = False,
    tempo_bpm: int | None = None,
    use_gpu: bool = False,
    layout_output: Path | None = None,
) -> Path:
    if page_image.name == "page-2.png":
        raise HomrPageError("page-2.png", "cv2.error: inv_scale_x > 0")
    return _homr_ok(
        page_image,
        work_dir,
        debug=debug,
        tempo_bpm=tempo_bpm,
        use_gpu=use_gpu,
        layout_output=layout_output,
    )


def _homr_all_fail(
    page_image: Path,
    work_dir: Path,
    debug: bool = False,
    tempo_bpm: int | None = None,
    use_gpu: bool = False,
    layout_output: Path | None = None,
) -> Path:
    raise HomrPageError(page_image.name, "boom")


def _homr_page_with_two_measures(
    page_image: Path,
    work_dir: Path,
    debug: bool = False,
    tempo_bpm: int | None = None,
    use_gpu: bool = False,
    layout_output: Path | None = None,
) -> Path:
    xml = work_dir / f"{page_image.stem}.musicxml"
    xml.parent.mkdir(parents=True, exist_ok=True)
    xml.write_text(
        """
        <score-partwise version="4.0">
          <part id="P1">
            <measure number="1" />
            <measure number="2" />
          </part>
        </score-partwise>
        """,
        encoding="utf-8",
    )
    if layout_output is not None:
        layout_output.parent.mkdir(parents=True, exist_ok=True)
        layout_output.write_text('{"schema_version": 1, "systems": []}', encoding="utf-8")
    return xml


def _homr_with_layout(
    page_image: Path,
    work_dir: Path,
    debug: bool = False,
    tempo_bpm: int | None = None,
    use_gpu: bool = False,
    layout_output: Path | None = None,
) -> Path:
    xml = _homr_ok(
        page_image,
        work_dir,
        debug=debug,
        tempo_bpm=tempo_bpm,
        use_gpu=use_gpu,
        layout_output=layout_output,
    )
    return xml


class ConvertUnitTest(unittest.TestCase):
    def _workspace(self) -> JobWorkspace:
        self._temp_dir = tempfile.TemporaryDirectory()
        workspace = JobWorkspace(Path(self._temp_dir.name) / "job").create()
        workspace.pdf_path.write_bytes(b"%PDF-1.4 fake")
        return workspace

    def _patch_pipeline(self, homr_side_effect) -> ExitStack:
        stack = ExitStack()
        stack.enter_context(
            mock.patch("sheet2music.core.convert.run_homr_on_page", side_effect=homr_side_effect)
        )
        stack.enter_context(
            mock.patch(
                "sheet2music.core.convert.export_numbered_pages",
                return_value=[Path("page-1.png"), Path("page-2.png"), Path("page-3.png")],
            )
        )
        stack.enter_context(mock.patch("sheet2music.core.convert.missing_model_files", return_value=[]))
        stack.enter_context(mock.patch("sheet2music.core.convert.fix_musicxml_file", side_effect=_fake_fix))
        stack.enter_context(mock.patch("sheet2music.core.convert.combine_page_musicxml", side_effect=_fake_combine))
        stack.enter_context(mock.patch("sheet2music.core.convert.export_midi", side_effect=_fake_export))
        stack.enter_context(mock.patch("sheet2music.core.convert.fix_midi_file", side_effect=_fake_fix_midi))
        return stack

    def test_skips_failed_page_and_reports_progress(self) -> None:
        workspace = self._workspace()
        try:
            with self._patch_pipeline(_homr_skip_page2) as stack:
                progress_calls: list[tuple[int, int, str]] = []
                params = ConvertParams(bpm=120, time_signature="4/4", outputs=["musicxml", "midi"])
                report = run_conversion(
                    workspace, params, progress=lambda c, t, p: progress_calls.append((c, t, p))
                )
            self.assertEqual(report["page_count"], 3)
            self.assertEqual(report["page_count_recognized"], 2)
            self.assertEqual(len(report["skipped_pages"]), 1)
            self.assertEqual(report["skipped_pages"][0]["page"], "page-2.png")
            self.assertIn("inv_scale_x", report["skipped_pages"][0]["error"])
            self.assertTrue((workspace.output_dir / "score.musicxml").exists())
            self.assertTrue((workspace.output_dir / "score.mid").exists())
            self.assertEqual(
                progress_calls,
                [(1, 3, "page-1.png"), (2, 3, "page-2.png"), (3, 3, "page-3.png")],
            )
        finally:
            workspace.cleanup()

    def test_raises_when_all_pages_fail(self) -> None:
        workspace = self._workspace()
        try:
            with self._patch_pipeline(_homr_all_fail):
                params = ConvertParams(bpm=120, time_signature="4/4", outputs=["midi"])
                with self.assertRaises(ConversionError):
                    run_conversion(workspace, params)
        finally:
            workspace.cleanup()

    def test_fails_fast_when_weights_missing(self) -> None:
        workspace = self._workspace()
        try:
            stack = ExitStack()
            stack.enter_context(
                mock.patch(
                    "sheet2music.core.convert.missing_model_files",
                    return_value=[Path("segnet.onnx")],
                )
            )
            # 权重缺失时不应走到页面导出 / HOMR。
            stack.enter_context(
                mock.patch(
                    "sheet2music.core.convert.export_numbered_pages",
                    side_effect=AssertionError("should not be reached"),
                )
            )
            with stack:
                params = ConvertParams(bpm=120, time_signature="4/4", outputs=["midi"])
                with self.assertRaisesRegex(ConversionError, "模型权重缺失"):
                    run_conversion(workspace, params)
        finally:
            workspace.cleanup()

    def test_forwards_gpu_preference_to_homr(self) -> None:
        workspace = self._workspace()
        try:
            with self._patch_pipeline(_homr_ok) as stack:
                params = ConvertParams(
                    bpm=120,
                    time_signature="4/4",
                    outputs=["musicxml"],
                    use_gpu=True,
                )
                with mock.patch(
                    "sheet2music.core.convert.run_homr_on_page",
                    side_effect=_homr_ok,
                ) as run_homr:
                    run_conversion(workspace, params)
            self.assertTrue(run_homr.call_args.kwargs["use_gpu"])
        finally:
            workspace.cleanup()

    def test_preparation_collects_page_layout_sidecars(self) -> None:
        workspace = self._workspace()
        try:
            with self._patch_pipeline(_homr_with_layout):
                params = ConvertParams(
                    bpm=120,
                    time_signature="4/4",
                    outputs=["musicxml"],
                    use_gpu=True,
                )
                preparation = prepare_conversion(workspace, params, max_pages=1)

            self.assertEqual(preparation["page_layouts"], ["layout/page-1.json"])
            self.assertTrue((workspace.layout_dir / "page-1.json").exists())
        finally:
            workspace.cleanup()

    def test_applies_structure_plan_with_full_score_page_offsets(self) -> None:
        workspace = self._workspace()
        try:
            structure_plan = {
                "default_time_signature": "4/4",
                "time_signature_changes": [
                    {"from_measure": 5, "to_measure": 5, "signature": "2/4"},
                ],
            }
            with self._patch_pipeline(_homr_page_with_two_measures):
                with mock.patch(
                    "sheet2music.core.convert.fix_musicxml_file",
                    side_effect=_fake_fix,
                ) as fix_musicxml:
                    with mock.patch(
                        "sheet2music.core.convert.fix_midi_file",
                        side_effect=_fake_fix_midi,
                    ) as fix_midi:
                        params = ConvertParams(
                            bpm=80,
                            time_signature="4/4",
                            outputs=["midi"],
                            structure_plan=structure_plan,
                        )
                        run_conversion(workspace, params)

            self.assertEqual(
                [call.kwargs["measure_offset"] for call in fix_musicxml.call_args_list[:3]],
                [0, 2, 4],
            )
            self.assertEqual(
                [call.kwargs["structure_plan"] for call in fix_musicxml.call_args_list[:3]],
                [structure_plan, structure_plan, structure_plan],
            )
            self.assertEqual(fix_musicxml.call_args_list[3].kwargs["measure_offset"], 0)
            self.assertEqual(fix_midi.call_args.kwargs["measure_count"], 2)
            self.assertEqual(fix_midi.call_args.kwargs["structure_plan"], structure_plan)
        finally:
            workspace.cleanup()

    def test_prepare_stops_before_final_exports_when_analysis_requires_review(self) -> None:
        workspace = self._workspace()
        try:
            stack = self._patch_pipeline(_homr_ok)
            stack.enter_context(
                mock.patch(
                    "sheet2music.core.convert.combine_page_musicxml",
                    side_effect=_fake_combine_with_time_change,
                )
            )
            stack.enter_context(
                mock.patch(
                    "sheet2music.core.convert.export_midi",
                    side_effect=AssertionError("final MIDI export must wait for review"),
                )
            )
            stack.enter_context(
                mock.patch(
                    "sheet2music.core.convert.fix_midi_file",
                    side_effect=AssertionError("MIDI repair must wait for review"),
                )
            )
            with stack:
                params = ConvertParams(bpm=80, time_signature="4/4", outputs=["midi"])
                preparation = prepare_conversion(workspace, params)

            self.assertEqual(preparation["status"], "awaiting_review")
            self.assertTrue(preparation["analysis"]["requires_review"])
            self.assertFalse((workspace.output_dir / "score.mid").exists())
            self.assertFalse((workspace.output_dir / "score.mp3").exists())
        finally:
            workspace.cleanup()

    def test_preparation_analyzes_combined_raw_xml_before_candidate_repair(self) -> None:
        workspace = self._workspace()
        try:
            with self._patch_pipeline(_homr_ok) as stack:
                with mock.patch(
                    "sheet2music.core.convert.combine_page_musicxml",
                    side_effect=_fake_combine,
                ) as combine:
                    params = ConvertParams(bpm=80, time_signature="4/4", outputs=["midi"])
                    preparation = prepare_conversion(workspace, params, max_pages=1)

            combined_inputs = combine.call_args.args[0]
            self.assertEqual(len(combined_inputs), 1)
            self.assertEqual(combined_inputs[0].parent.name, "homr_raw")
            self.assertEqual(
                Path(preparation["combined_musicxml_raw"]),
                Path("output") / "score.raw.musicxml",
            )
        finally:
            workspace.cleanup()

    def test_prepare_runs_auto_resolution_before_manual_review(self) -> None:
        workspace = self._workspace()
        try:
            candidate_path = workspace.output_dir / "score.auto.musicxml"
            candidate_path.write_text(
                """
                <score-partwise version="4.0"><part id="P1">
                  <measure number="1"><attributes><divisions>4</divisions><time><beats>4</beats><beat-type>4</beat-type></time></attributes><note><rest/><duration>16</duration><voice>1</voice><staff>1</staff></note></measure>
                </part></score-partwise>
                """,
                encoding="utf-8",
            )
            stack = self._patch_pipeline(_homr_ok)
            stack.enter_context(
                mock.patch(
                    "sheet2music.core.convert.combine_page_musicxml",
                    side_effect=_fake_combine_with_overflow,
                )
            )
            outcome = AutoResolutionOutcome(
                candidate_path=candidate_path,
                batches=(),
                resolved_count=1,
                needs_choice_count=0,
                needs_upload_count=0,
            )
            resolve = stack.enter_context(
                mock.patch(
                    "sheet2music.core.convert.resolve_timing_overflows",
                    return_value=outcome,
                )
            )
            stages: list[str] = []
            with stack:
                preparation = prepare_conversion(
                    workspace,
                    ConvertParams(bpm=80, time_signature="4/4", outputs=["musicxml"]),
                    stage=stages.append,
                    max_pages=1,
                )

            self.assertIn("automatic_reidentification", stages)
            self.assertEqual(preparation["auto_resolution"]["resolved_count"], 1)
            self.assertFalse(preparation["analysis"]["requires_review"])
            self.assertEqual(
                preparation["combined_musicxml_candidate"],
                "output/score.auto.musicxml",
            )
            resolve.assert_called_once()
        finally:
            workspace.cleanup()

    def test_resume_automatic_resolution_uses_persisted_preparation(self) -> None:
        workspace = self._workspace()
        try:
            raw_path = workspace.output_dir / "score.raw.musicxml"
            candidate_path = workspace.output_dir / "score.auto.musicxml"
            _fake_combine_with_overflow([], raw_path)
            candidate_path.write_text(
                """
                <score-partwise version="4.0"><part id="P1">
                  <measure number="1"><attributes><divisions>4</divisions></attributes><note><rest/><duration>16</duration><voice>1</voice><staff>1</staff></note></measure>
                </part></score-partwise>
                """,
                encoding="utf-8",
            )
            preparation = {
                "status": "automatic_reidentification",
                "combined_musicxml_raw": "output/score.raw.musicxml",
                "page_layouts": ["layout/page-1.json"],
                "page_measure_offsets": [0],
                "analysis": {
                    "findings": [
                        {
                            "id": "timing_measure_overflow:P1:-:1:1",
                            "kind": "timing_measure_overflow",
                            "severity": "high",
                            "measure_start": 1,
                            "measure_end": 1,
                        }
                    ]
                },
            }
            outcome = AutoResolutionOutcome(candidate_path, (), 1, 0, 0)

            with mock.patch(
                "sheet2music.core.convert.resolve_timing_overflows",
                return_value=outcome,
            ) as resolve:
                resumed = resume_automatic_resolution(
                    workspace,
                    ConvertParams(bpm=80, time_signature="4/4", outputs=["musicxml"]),
                    preparation,
                )

            self.assertEqual(resumed["status"], "prepared")
            self.assertEqual(resumed["auto_resolution"]["resolved_count"], 1)
            self.assertEqual(
                resumed["combined_musicxml_candidate"],
                "output/score.auto.musicxml",
            )
            resolve.assert_called_once()
        finally:
            workspace.cleanup()

    def test_finalize_preserve_adds_observed_change_to_structure_plan(self) -> None:
        workspace = self._workspace()
        try:
            raw_path = workspace.output_dir / "score.raw.musicxml"
            raw_path.write_text("<score-partwise><part id='P1'><measure number='1'/></part></score-partwise>", encoding="utf-8")
            params = ConvertParams(bpm=80, time_signature="4/4", outputs=["midi"])
            preparation = {
                "status": "awaiting_review",
                "num_parts": 1,
                "num_measures": 1,
                "combined_musicxml_raw": "output/score.raw.musicxml",
                "analysis": {"requires_review": True},
            }
            decision = {
                "id": "time_signature_change:P1:-:25:25",
                "kind": "time_signature_change",
                "measure_start": 25,
                "measure_end": 25,
                "observed": {"signature": "2/4"},
                "suggestion": {"signature": "4/4"},
                "action": "preserve",
            }
            with mock.patch(
                "sheet2music.core.convert.fix_musicxml_file",
                side_effect=_fake_fix,
            ) as fix_musicxml:
                with mock.patch(
                    "sheet2music.core.convert.export_midi",
                    side_effect=_fake_export,
                ):
                    with mock.patch(
                        "sheet2music.core.convert.fix_midi_file",
                        side_effect=_fake_fix_midi,
                    ):
                        finalize_conversion(workspace, params, preparation, [decision])

            effective_plan = fix_musicxml.call_args.kwargs["structure_plan"]
            self.assertEqual(
                effective_plan["time_signature_changes"],
                [{"from_measure": 25, "to_measure": 25, "signature": "2/4"}],
            )
        finally:
            workspace.cleanup()

    def test_finalize_uses_region_candidate_and_keeps_raw_xml(self) -> None:
        workspace = self._workspace()
        try:
            raw_path = workspace.output_dir / "score.raw.musicxml"
            candidate_path = workspace.region_merged_xml_dir / "candidate.musicxml"
            raw_xml = "<score-partwise><part id='P1'><measure number='1'/></part></score-partwise>"
            candidate_xml = (
                "<score-partwise><part id='P1'><measure number='1'>"
                "<attributes><divisions>4</divisions></attributes>"
                "<note><rest/><duration>16</duration><voice>1</voice></note>"
                "</measure></part></score-partwise>"
            )
            raw_path.write_text(raw_xml, encoding="utf-8")
            candidate_path.write_text(candidate_xml, encoding="utf-8")
            params = ConvertParams(bpm=80, time_signature="4/4", outputs=["midi"])
            preparation = {
                "status": "awaiting_review",
                "num_parts": 1,
                "num_measures": 1,
                "combined_musicxml_raw": "output/score.raw.musicxml",
                "combined_musicxml_candidate": "regions/merged/candidate.musicxml",
                "analysis": {"requires_review": True},
            }
            with mock.patch(
                "sheet2music.core.convert.fix_musicxml_file",
                side_effect=_fake_fix,
            ) as fix_musicxml:
                with mock.patch(
                    "sheet2music.core.convert.export_midi",
                    side_effect=_fake_export,
                ):
                    with mock.patch(
                        "sheet2music.core.convert.fix_midi_file",
                        side_effect=_fake_fix_midi,
                    ):
                        finalize_conversion(workspace, params, preparation)

            self.assertEqual(fix_musicxml.call_args.args[0], candidate_path)
            self.assertEqual(raw_path.read_text(encoding="utf-8"), raw_xml)
        finally:
            workspace.cleanup()


if __name__ == "__main__":
    unittest.main()
