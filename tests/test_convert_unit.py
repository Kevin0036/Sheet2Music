"""convert.py 单元测试：逐页跳过 / 进度上报 / 权重缺失快速失败（不访问外部工具）。

通过 mock 掉 HOMR 子进程与外部命令，专注验证流水线编排行为。
"""

import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

from sheet2music.core.convert import ConversionError, run_conversion
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


def _homr_ok(
    page_image: Path,
    work_dir: Path,
    debug: bool = False,
    tempo_bpm: int | None = None,
    use_gpu: bool = False,
) -> Path:
    xml = work_dir / f"{page_image.stem}.musicxml"
    xml.parent.mkdir(parents=True, exist_ok=True)
    xml.write_bytes(MINIMAL_XML)
    return xml


def _homr_skip_page2(
    page_image: Path,
    work_dir: Path,
    debug: bool = False,
    tempo_bpm: int | None = None,
    use_gpu: bool = False,
) -> Path:
    if page_image.name == "page-2.png":
        raise HomrPageError("page-2.png", "cv2.error: inv_scale_x > 0")
    return _homr_ok(
        page_image,
        work_dir,
        debug=debug,
        tempo_bpm=tempo_bpm,
        use_gpu=use_gpu,
    )


def _homr_all_fail(
    page_image: Path,
    work_dir: Path,
    debug: bool = False,
    tempo_bpm: int | None = None,
    use_gpu: bool = False,
) -> Path:
    raise HomrPageError(page_image.name, "boom")


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


if __name__ == "__main__":
    unittest.main()
