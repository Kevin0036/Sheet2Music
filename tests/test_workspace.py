import tempfile
import unittest
from pathlib import Path

from sheet2music.core.workspace import JobWorkspace, create_job_workspace, make_zip_bundle, write_report


class JobWorkspaceTest(unittest.TestCase):
    def test_create_layout(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = JobWorkspace(Path(temp_dir) / "job").create()
            self.assertTrue(workspace.input_dir.is_dir())
            self.assertTrue(workspace.preview_dir.is_dir())
            self.assertTrue(workspace.pages_dir.is_dir())
            self.assertTrue(workspace.raw_page_xml_dir.is_dir())
            self.assertTrue(workspace.fixed_page_xml_dir.is_dir())
            self.assertTrue(workspace.output_dir.is_dir())
            self.assertTrue(workspace.homr_work_dir.is_dir())

    def test_artifacts_collection_and_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = JobWorkspace(Path(temp_dir) / "job").create()
            self.assertEqual(workspace.artifacts(), [])

            (workspace.output_dir / "score.musicxml").write_text("<score/>", encoding="utf-8")
            artifacts = workspace.artifacts()
            self.assertEqual(len(artifacts), 1)
            self.assertEqual(artifacts[0].name, "score.musicxml")
            self.assertEqual(artifacts[0].kind, "musicxml")

            workspace.cleanup()
            self.assertFalse(workspace.root.exists())

    def test_create_job_workspace_generates_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir) / "jobs"
            first = create_job_workspace(base)
            second = create_job_workspace(base)
            self.assertNotEqual(first.root.name, second.root.name)
            self.assertTrue(first.root.is_dir())
            self.assertTrue(second.root.is_dir())

    def test_report_and_zip_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = JobWorkspace(Path(temp_dir) / "job").create()
            (workspace.output_dir / "score.musicxml").write_text("<score/>", encoding="utf-8")

            report_path = write_report(workspace, {"num_measures": 5})
            self.assertTrue(report_path.exists())

            zip_path = make_zip_bundle(workspace)
            self.assertTrue(zip_path.exists())
            self.assertTrue(zip_path.name, "score.zip")

            import zipfile

            with zipfile.ZipFile(zip_path) as archive:
                names = archive.namelist()
            self.assertIn("score.musicxml", names)
            self.assertIn("report.json", names)
            self.assertNotIn("score.zip", names)


if __name__ == "__main__":
    unittest.main()
