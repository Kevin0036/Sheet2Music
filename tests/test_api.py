"""FastAPI 层测试（使用 TestClient，需 httpx）。

上传成功路径需要 pdftoppm + 样例 PDF；纯校验/404 用例不依赖外部工具。
"""

import os
import shutil
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from sheet2music.core.settings import pdftoppm_binary

from sheet2music.web import app as web_app


def _pdf_available() -> bool:
    try:
        pdftoppm_binary()
    except Exception:
        return False
    parent_repo = Path(__file__).resolve().parents[2]  # Sheet2Music/ 的上一级
    return any((parent_repo / "assets" / "raw" / "sheets").glob("*.pdf"))


class ApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        base = Path(self.temp_dir.name) / "jobs"
        base.mkdir(parents=True, exist_ok=True)
        web_app.store = _fresh_store(base)
        self.client = TestClient(web_app.app)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()


def _fresh_store(base: Path):
    from sheet2music.web.jobs import JobStore

    return JobStore(base)


class ApiValidationTest(ApiTest):
    def test_upload_rejects_non_pdf(self) -> None:
        resp = self.client.post("/api/preview", files={"file": ("a.txt", b"hello", "text/plain")})
        self.assertEqual(resp.status_code, 400)

    def test_convert_unknown_job_returns_404(self) -> None:
        resp = self.client.post("/api/convert", json={"job_id": "nope", "bpm": 120, "outputs": ["midi"]})
        self.assertEqual(resp.status_code, 404)

    def test_convert_rejects_invalid_bpm(self) -> None:
        # 先造一个已上传但缺预览不重要的 job：直接调用 store。
        record = web_app.store.create("score.pdf")
        web_app.store.mark_preview_ready(record)
        resp = self.client.post(
            "/api/convert",
            json={"job_id": record.job_id, "bpm": 0, "time_signature": "4/4", "outputs": ["midi"]},
        )
        self.assertEqual(resp.status_code, 400)

    def test_reset_unknown_job_returns_404(self) -> None:
        resp = self.client.post("/api/jobs/does-not-exist/reset")
        self.assertEqual(resp.status_code, 404)


@unittest.skipUnless(_pdf_available(), "需要 pdftoppm 与样例 PDF")
class ApiUploadPreviewTest(ApiTest):
    def _sample_pdf(self) -> Path:
        env_pdf = os.environ.get("SHEET2MUSIC_TEST_PDF") or os.environ.get("HOMR_TOOL_TEST_PDF")
        if env_pdf and Path(env_pdf).exists():
            return Path(env_pdf)
        parent_repo = Path(__file__).resolve().parents[2]
        return sorted((parent_repo / "assets" / "raw" / "sheets").glob("*.pdf"))[0]

    def test_upload_then_status(self) -> None:
        pdf = self._sample_pdf()
        with open(pdf, "rb") as fh:
            resp = self.client.post("/api/preview", files={"file": (pdf.name, fh, "application/pdf")})
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()
        job_id = data["job_id"]
        self.assertIn("preview_url", data)

        preview_resp = self.client.get(data["preview_url"])
        self.assertEqual(preview_resp.status_code, 200)
        self.assertEqual(preview_resp.headers["content-type"], "image/png")

        status_resp = self.client.get(f"/api/jobs/{job_id}")
        self.assertEqual(status_resp.status_code, 200)
        self.assertEqual(status_resp.json()["stage"], "preview_ready")

        reset_resp = self.client.post(f"/api/jobs/{job_id}/reset")
        self.assertEqual(reset_resp.status_code, 200)
        gone = self.client.get(f"/api/jobs/{job_id}")
        self.assertEqual(gone.status_code, 404)


if __name__ == "__main__":
    unittest.main()
