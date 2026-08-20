"""FastAPI 层测试（使用 TestClient，需 httpx）。

上传成功路径需要 pdftoppm + 样例 PDF；纯校验/404 用例不依赖外部工具。
"""

import json
import hashlib
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from sheet2music.core.auto_resolution import (
    AutoResolutionBatch,
    AutoResolutionStore,
    BatchStatus,
)
from sheet2music.core.settings import pdftoppm_binary
from sheet2music.core.models import ConvertParams, JobStatus

from sheet2music.web import app as web_app
from sheet2music.web.jobs import ReviewError, normalize_review_decisions, summarize_review_changes


PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"0" * 16


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


class JobPersistenceTest(unittest.TestCase):
    def test_restart_refreshes_exhausted_automatic_batches_in_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir) / "jobs"
            first = _fresh_store(base)
            record = first.create("score.pdf")
            record.status = JobStatus.AWAITING_REVIEW
            record.stage = JobStatus.AWAITING_REVIEW.value
            record.params = ConvertParams(bpm=80, outputs=["midi"])
            candidate = record.workspace.output_dir / "score.auto.musicxml"
            candidate.write_text(
                """<score-partwise version=\"4.0\"><part id=\"P1\"><measure number=\"1\">
                <attributes><divisions>4</divisions><time><beats>4</beats><beat-type>4</beat-type></time></attributes>
                <note><rest/><duration>20</duration><voice>1</voice><staff>1</staff></note>
                </measure></part></score-partwise>""",
                encoding="utf-8",
            )
            batch = AutoResolutionBatch(
                batch_id="p1-s0-m1",
                page_number=1,
                system_index=0,
                target_measures=(1,),
                context_range=(1, 1),
                status=BatchStatus.NEEDS_UPLOAD,
                attempts=[
                    {"variant": name, "status": "succeeded"}
                    for name in ("standard", "contrast", "context")
                ],
            )
            AutoResolutionStore(record.workspace.auto_resolution_dir / "batches.json").save([batch])
            record.report = {"page_measure_offsets": [], "analysis": {"findings": []}}
            record.analysis = {"findings": []}
            first._persist_record(record)

            restored = _fresh_store(base).get(record.job_id)

        assert restored is not None
        assert restored.report is not None
        auto_report = restored.report["auto_resolution"]
        assert isinstance(auto_report, dict)
        self.assertEqual(auto_report["batches"][0]["status"], "accepted_original")
        self.assertEqual(auto_report["needs_upload_count"], 0)
        self.assertFalse(restored.report["analysis"]["requires_review"])

    def test_start_clears_previous_automatic_resolution_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir) / "jobs"
            store = _fresh_store(base)
            record = store.create("score.pdf")
            batches = record.workspace.auto_resolution_dir / "batches.json"
            cached = record.workspace.auto_resolution_dir / "homr_work" / "cached.bin"
            candidate = record.workspace.output_dir / "score.auto.musicxml"
            batches.write_text("{}", encoding="utf-8")
            cached.parent.mkdir(parents=True, exist_ok=True)
            cached.write_bytes(b"old")
            candidate.write_text("<old/>", encoding="utf-8")

            with mock.patch.object(store, "_run"):
                store.start(record, ConvertParams(bpm=80, outputs=["midi"]))

            self.assertFalse(batches.exists())
            self.assertFalse(cached.exists())
            self.assertFalse(candidate.exists())
            self.assertTrue(record.workspace.pdf_path.parent.is_dir())

    def test_automatic_stage_snapshots_the_preparation_report_for_resume(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir) / "jobs"
            first = _fresh_store(base)
            record = first.create("score.pdf")
            preparation = {
                "status": "automatic_reidentification",
                "combined_musicxml_raw": "output/score.raw.musicxml",
                "page_layouts": ["layout/page-1.json"],
                "page_measure_offsets": [0],
                "analysis": {"findings": []},
            }
            (record.workspace.output_dir / "report.json").write_text(
                json.dumps(preparation), encoding="utf-8"
            )

            first.set_stage(record, "automatic_reidentification")
            restored = _fresh_store(base).get(record.job_id)

            self.assertEqual(restored.report, preparation)

    def test_automatic_progress_persists_system_and_resolution_counts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir) / "jobs"
            first = _fresh_store(base)
            record = first.create("score.pdf")

            first.set_progress(
                record,
                2,
                5,
                "page-3",
                system=1,
                resolved=2,
                needs_review=1,
            )
            restored = _fresh_store(base).get(record.job_id)

            self.assertEqual(
                restored.progress,
                {
                    "current": 2,
                    "total": 5,
                    "page": "page-3",
                    "system": 1,
                    "resolved": 2,
                    "needs_review": 1,
                },
            )

    def test_store_restores_awaiting_review_job_after_reload(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir) / "jobs"
            first = _fresh_store(base)
            record = first.create("score.pdf")
            record.params = ConvertParams(bpm=80, time_signature="4/4", outputs=["midi"])
            record.analysis = {
                "requires_review": True,
                "findings": [{"id": "timing:P1:-:1:1", "severity": "high"}],
            }
            record.report = {
                "status": "awaiting_review",
                "combined_musicxml_candidate": "regions/merged/candidate.musicxml",
            }
            record.review_decisions = []
            record.status = JobStatus.AWAITING_REVIEW
            record.stage = JobStatus.AWAITING_REVIEW.value
            first.save(record)

            restored = _fresh_store(base).get(record.job_id)

            self.assertIsNotNone(restored)
            assert restored is not None
            self.assertEqual(restored.status, JobStatus.AWAITING_REVIEW)
            self.assertEqual(restored.stage, "awaiting_review")
            self.assertEqual(restored.params.bpm, 80)
            self.assertEqual(restored.analysis, record.analysis)
            self.assertEqual(restored.report, record.report)

    def test_restore_preserves_automatic_report_when_recovery_inputs_are_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir) / "jobs"
            first = _fresh_store(base)
            record = first.create("score.pdf")
            record.params = ConvertParams(bpm=80, time_signature="4/4", outputs=["midi"])
            record.status = JobStatus.RUNNING
            record.stage = "automatic_reidentification"
            record.report = {
                "status": "automatic_reidentification",
                "auto_resolution": {
                    "batches": [
                        {
                            "batch_id": "p1-s0-m1",
                            "status": "auto_resolved",
                            "attempts": [{"variant": "standard", "status": "succeeded"}],
                        },
                        {"batch_id": "p1-s1-m2", "status": "recognizing", "attempts": []},
                    ]
                },
            }
            first.save(record)

            restored = _fresh_store(base).get(record.job_id)

            self.assertIsNotNone(restored)
            assert restored is not None
            self.assertEqual(restored.status, JobStatus.FAILED)
            self.assertEqual(restored.stage, "failed")
            batches = restored.report["auto_resolution"]["batches"]
            self.assertEqual(batches[0]["status"], "auto_resolved")
            self.assertEqual(batches[0]["attempts"][0]["variant"], "standard")

    def test_restore_resumes_eligible_automatic_job(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir) / "jobs"
            first = _fresh_store(base)
            record = first.create("score.pdf")
            record.params = ConvertParams(bpm=80, time_signature="4/4", outputs=["midi"])
            record.status = JobStatus.RUNNING
            record.stage = "automatic_reidentification"
            record.report = {
                "status": "automatic_reidentification",
                "combined_musicxml_raw": "output/score.raw.musicxml",
                "page_layouts": ["layout/page-1.json"],
                "page_measure_offsets": [0],
                "analysis": {"findings": []},
            }
            first.save(record)
            resumed_report = {
                **record.report,
                "status": "awaiting_review",
                "analysis": {"requires_review": True, "findings": []},
            }

            with mock.patch(
                "sheet2music.web.jobs.resume_automatic_resolution",
                return_value=resumed_report,
            ) as resume:
                restored_store = _fresh_store(base)
                deadline = time.time() + 2
                restored = restored_store.get(record.job_id)
                while restored.status == JobStatus.RUNNING and time.time() < deadline:
                    time.sleep(0.01)

            self.assertEqual(restored.status, JobStatus.AWAITING_REVIEW)
            self.assertEqual(restored.stage, "awaiting_review")
            resume.assert_called_once()

    def test_restore_fails_unrecoverable_automatic_job(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir) / "jobs"
            first = _fresh_store(base)
            record = first.create("score.pdf")
            record.params = ConvertParams(bpm=80, time_signature="4/4", outputs=["midi"])
            record.status = JobStatus.RUNNING
            record.stage = "automatic_reidentification"
            record.report = {
                "status": "automatic_reidentification",
                "combined_musicxml_raw": "output/score.raw.musicxml",
                "page_layouts": [],
            }
            first.save(record)

            restored = _fresh_store(base).get(record.job_id)

            self.assertIsNotNone(restored)
            assert restored is not None
            self.assertEqual(restored.status, JobStatus.FAILED)
            self.assertEqual(restored.stage, "failed")
            self.assertIn("无法恢复", restored.error or "")

    def test_restore_finalizes_pending_manual_candidate_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir) / "jobs"
            first = _fresh_store(base)
            record = first.create("score.pdf")
            record.params = ConvertParams(bpm=80, time_signature="4/4", outputs=["midi"])
            record.status = JobStatus.RUNNING
            record.stage = "automatic_upload_recognition"
            record.report = {
                "status": "awaiting_review",
                "page_measure_offsets": [],
                "auto_resolution": {"batches": []},
            }
            official = record.workspace.output_dir / "score.auto.musicxml"
            prepared = record.workspace.auto_resolution_validation_dir / "batch.prepared.musicxml"
            result = b"<score-partwise><part id='P1'><measure number='1'/></part></score-partwise>"
            official.write_bytes(result)
            prepared.write_bytes(result)
            batch = AutoResolutionBatch(
                batch_id="batch",
                page_number=1,
                system_index=0,
                target_measures=(1,),
                context_range=(1, 1),
                status=BatchStatus.COMMITTING,
                commit={
                    "base_digest": hashlib.sha256(b"old").hexdigest(),
                    "result_digest": hashlib.sha256(result).hexdigest(),
                    "prepared_xml": str(prepared),
                    "candidate_id": "standard",
                },
            )
            AutoResolutionStore(
                record.workspace.auto_resolution_dir / "batches.json"
            ).save([batch])
            first.save(record)

            restored_store = _fresh_store(base)
            restored = restored_store.get(record.job_id)

            self.assertIsNotNone(restored)
            assert restored is not None
            self.assertEqual(restored.status, JobStatus.AWAITING_REVIEW)
            self.assertEqual(restored.stage, JobStatus.AWAITING_REVIEW.value)
            restored_batch = AutoResolutionStore(
                record.workspace.auto_resolution_dir / "batches.json"
            ).load()[0]
            self.assertEqual(restored_batch.status, BatchStatus.AUTO_RESOLVED)
            self.assertEqual(restored_batch.selected_candidate, "standard")


class ApiValidationTest(ApiTest):
    def test_video_url_upload_creates_video_job(self) -> None:
        response = self.client.post("/api/video-url", json={"url": "https://youtu.be/abc"})
        self.assertEqual(response.status_code, 200, response.text)
        record = web_app.store.get(response.json()["job_id"])
        assert record is not None
        self.assertEqual(record.input_kind, "video_url")
        self.assertEqual(record.stage, "video_url_uploaded")

    def test_video_url_upload_rejects_other_hosts(self) -> None:
        response = self.client.post("/api/video-url", json={"url": "https://example.com/video"})
        self.assertEqual(response.status_code, 400)

    def test_audio_upload_creates_an_audio_job(self) -> None:
        response = self.client.post(
            "/api/audio",
            files={"file": ("song.mp3", b"ID3" + b"0" * 32, "audio/mpeg")},
        )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        record = web_app.store.get(payload["job_id"])
        assert record is not None
        self.assertEqual(record.input_kind, "audio")
        self.assertEqual(record.stage, "audio_uploaded")
        self.assertTrue(record.workspace.audio_path.exists())

    def test_audio_upload_rejects_non_mp3(self) -> None:
        response = self.client.post(
            "/api/audio",
            files={"file": ("song.wav", b"RIFF", "audio/wav")},
        )
        self.assertEqual(response.status_code, 400)

    def test_audio_conversion_uses_audio_pipeline_and_fixed_outputs(self) -> None:
        record = web_app.store.create("song.mp3", input_kind="audio")
        record.workspace.audio_path.write_bytes(b"ID3")
        web_app.store.save(record)
        with mock.patch("sheet2music.web.jobs.run_audio_transcription", return_value={"status": "completed"}) as run_audio:
            response = self.client.post("/api/convert", json={"job_id": record.job_id})
            self.assertEqual(response.status_code, 200, response.text)
            for _ in range(100):
                if record.status == JobStatus.COMPLETED:
                    break
                time.sleep(0.01)

        run_audio.assert_called_once()
        self.assertEqual(record.status, JobStatus.COMPLETED)

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

    def test_page_preview_returns_recognized_page_image(self) -> None:
        record = web_app.store.create("score.pdf")
        page = record.workspace.pages_dir / "page-2.png"
        page.write_bytes(PNG_BYTES)
        resp = self.client.get(f"/api/jobs/{record.job_id}/pages/2")
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.headers["content-type"], "image/png")


class AutoResolutionApiTest(ApiTest):
    batch_id = "p1-s0-m1"

    def _record(self, status: BatchStatus, attempts: list[dict[str, object]] | None = None):
        record = web_app.store.create("score.pdf")
        record.params = ConvertParams(bpm=80, outputs=["midi"])
        record.status = JobStatus.AWAITING_REVIEW
        record.stage = JobStatus.AWAITING_REVIEW.value
        record.report = {
            "status": "awaiting_review",
            "combined_musicxml_raw": "output/score.raw.musicxml",
            "analysis": {"requires_review": True, "findings": []},
            "auto_resolution": {
                "resolved_count": 0,
                "needs_choice_count": int(status == BatchStatus.NEEDS_CHOICE),
                "needs_upload_count": int(status == BatchStatus.NEEDS_UPLOAD),
                "batches": [],
            },
        }
        batch = AutoResolutionBatch(
            batch_id=self.batch_id,
            page_number=1,
            system_index=0,
            target_measures=(1,),
            context_range=(1, 2),
            status=status,
            attempts=attempts or [],
        )
        AutoResolutionStore(
            record.workspace.auto_resolution_dir / "batches.json"
        ).save([batch])
        crop = record.workspace.auto_resolution_crop_dir / self.batch_id / "source.png"
        crop.parent.mkdir(parents=True, exist_ok=True)
        crop.write_bytes(PNG_BYTES)
        web_app.store.save(record)
        return record

    def test_serves_only_crop_inside_requested_job(self) -> None:
        record = self._record(BatchStatus.NEEDS_CHOICE)

        response = self.client.get(
            f"/api/jobs/{record.job_id}/auto-resolution/{self.batch_id}/crop"
        )
        other_job = web_app.store.create("other.pdf")
        rejected = self.client.get(
            f"/api/jobs/{other_job.job_id}/auto-resolution/{self.batch_id}/crop"
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "image/png")
        self.assertEqual(rejected.status_code, 404)

    def test_select_candidate_routes_through_revalidation(self) -> None:
        record = self._record(BatchStatus.NEEDS_CHOICE)
        record.report["auto_resolution"]["needs_choice_count"] = 0

        with mock.patch.object(
            web_app.store,
            "select_auto_candidate",
            return_value=None,
        ) as select:
            response = self.client.post(
                f"/api/jobs/{record.job_id}/auto-resolution/{self.batch_id}/select",
                json={"candidate_id": "contrast"},
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["report"]["auto_resolution"]["needs_choice_count"], 0)
        select.assert_called_once_with(record, self.batch_id, "contrast")

    def test_manual_candidate_selection_uses_shared_worker_lock(self) -> None:
        record = self._record(BatchStatus.NEEDS_CHOICE)
        guard = mock.MagicMock()
        original = web_app.store._worker_lock
        web_app.store._worker_lock = guard
        try:
            with self.assertRaises(ReviewError):
                web_app.store.select_auto_candidate(record, self.batch_id, "missing")
        finally:
            web_app.store._worker_lock = original

        guard.__enter__.assert_called_once()

    def test_retry_rejects_fourth_automatic_attempt(self) -> None:
        record = self._record(
            BatchStatus.NEEDS_UPLOAD,
            attempts=[
                {"variant": name, "status": "failed"}
                for name in ("standard", "contrast", "context")
            ],
        )

        response = self.client.post(
            f"/api/jobs/{record.job_id}/auto-resolution/{self.batch_id}/retry"
        )

        self.assertEqual(response.status_code, 409)

    def test_batch_upload_uses_persisted_context_range(self) -> None:
        record = self._record(BatchStatus.NEEDS_UPLOAD)
        captured: list[tuple[int, int]] = []

        def fake_submit(current, batch_id: str, image_path: Path) -> None:
            batch = web_app.store.get_auto_batch(current, batch_id)
            captured.append(batch.context_range)

        with mock.patch.object(
            web_app.store,
            "submit_auto_upload",
            side_effect=fake_submit,
        ):
            response = self.client.post(
                f"/api/jobs/{record.job_id}/auto-resolution/{self.batch_id}/upload",
                files={"file": ("system.png", PNG_BYTES, "image/png")},
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(captured, [(1, 2)])


class ApiReviewStateTest(ApiTest):
    def test_preparation_result_enters_awaiting_review(self) -> None:
        record = web_app.store.create("score.pdf")
        params = ConvertParams(bpm=80, outputs=["midi"])
        pending_report = {
            "status": "awaiting_review",
            "analysis": {
                "requires_review": True,
                "findings": [{"id": "time_signature_change:P1:-:25:25", "severity": "high"}],
            },
        }
        with mock.patch("sheet2music.web.jobs.run_conversion", return_value=pending_report):
            web_app.store._run(record, params, debug=False)

        self.assertEqual(record.status, JobStatus.AWAITING_REVIEW)
        payload = record.to_dict()
        self.assertEqual(payload["analysis"], pending_report["analysis"])
        self.assertEqual(payload["review_decisions"], [])
        self.assertEqual(payload["report"]["status"], "awaiting_review")


class ReviewChangeSummaryTest(unittest.TestCase):
    def test_identifies_new_and_resolved_high_risk_findings_after_reidentification(self) -> None:
        previous = {
            "findings": [
                {"id": "timing_structure:P1:-:76:76", "severity": "high", "measure_start": 76},
                {"id": "clef_change:P1:2:79:79", "severity": "high", "measure_start": 79},
            ]
        }
        current = {
            "findings": [
                {"id": "clef_change:P1:2:79:79", "severity": "high", "measure_start": 79},
                {"id": "timing_structure:P1:-:77:77", "severity": "high", "measure_start": 77},
            ]
        }

        summary = summarize_review_changes(previous, current)

        self.assertEqual(summary["before_high_risk_count"], 2)
        self.assertEqual(summary["after_high_risk_count"], 2)
        self.assertEqual(summary["unchanged_high_risk_count"], 1)
        self.assertEqual(
            [finding["id"] for finding in summary["new_findings"]],
            ["timing_structure:P1:-:77:77"],
        )
        self.assertEqual(
            [finding["id"] for finding in summary["resolved_findings"]],
            ["timing_structure:P1:-:76:76"],
        )


class ApiReviewWorkflowTest(ApiTest):
    finding_id = "time_signature_change:P1:-:2:2"

    def test_accepted_original_findings_do_not_require_review_decisions(self) -> None:
        analysis = {
            "findings": [
                {
                    "id": "timing_measure_overflow:P1:-:14:14",
                    "severity": "high",
                    "status": "accepted_original",
                },
                {
                    "id": self.finding_id,
                    "severity": "high",
                    "status": "pending",
                    "available_actions": ["preserve", "ignore"],
                },
            ]
        }

        normalized = normalize_review_decisions(
            analysis,
            [{"id": self.finding_id, "action": "preserve"}],
        )

        self.assertEqual([item["id"] for item in normalized], [self.finding_id])

    def test_ignore_is_allowed_for_findings_with_other_limited_actions(self) -> None:
        analysis = {
            "findings": [
                {
                    "id": self.finding_id,
                    "severity": "high",
                    "status": "pending",
                    "available_actions": ["preserve", "reidentify"],
                }
            ]
        }

        normalized = normalize_review_decisions(
            analysis,
            [{"id": self.finding_id, "action": "ignore"}],
        )

        self.assertEqual(normalized[0]["action"], "ignore")

    def _pending_record(self):
        record = web_app.store.create("score.pdf")
        analysis = {
            "requires_review": True,
            "findings": [
                {
                    "id": self.finding_id,
                    "kind": "time_signature_change",
                    "severity": "high",
                    "measure_start": 2,
                    "measure_end": 2,
                    "page_numbers": [1],
                    "observed": {"signature": "2/4"},
                    "suggestion": {"signature": "4/4"},
                    "reason": "unconfirmed change",
                    "status": "pending",
                }
            ],
        }
        report = {
            "status": "awaiting_review",
            "analysis": analysis,
            "combined_musicxml_raw": "output/score.raw.musicxml",
            "page_measure_offsets": [],
            "num_measures": 2,
        }
        record.params = ConvertParams(bpm=80, outputs=["midi"])
        record.report = report
        record.analysis = analysis
        record.status = JobStatus.AWAITING_REVIEW
        record.stage = JobStatus.AWAITING_REVIEW.value
        return record

    def test_analysis_endpoint_returns_pending_findings(self) -> None:
        record = self._pending_record()
        resp = self.client.get(f"/api/jobs/{record.job_id}/analysis")
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["analysis"]["findings"][0]["id"], self.finding_id)

    def test_review_requires_a_decision_for_every_high_risk_finding(self) -> None:
        record = self._pending_record()
        resp = self.client.post(
            f"/api/jobs/{record.job_id}/review",
            json={"decisions": []},
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(record.status, JobStatus.AWAITING_REVIEW)

    def test_review_rejects_action_not_offered_by_finding(self) -> None:
        record = self._pending_record()
        assert record.analysis is not None
        record.analysis["findings"][0]["available_actions"] = ["reidentify"]

        resp = self.client.post(
            f"/api/jobs/{record.job_id}/review",
            json={"decisions": [{"id": self.finding_id, "action": "preserve"}]},
        )

        self.assertEqual(resp.status_code, 400)
        self.assertIn("not available", resp.json()["detail"])
        self.assertEqual(record.status, JobStatus.AWAITING_REVIEW)

    def test_preserve_review_queues_finalization(self) -> None:
        record = self._pending_record()
        completed_report = {"status": "completed", "review_decisions": []}
        with mock.patch(
            "sheet2music.web.jobs.finalize_conversion",
            return_value=completed_report,
        ):
            resp = self.client.post(
                f"/api/jobs/{record.job_id}/review",
                json={"decisions": [{"id": self.finding_id, "action": "preserve"}]},
            )
            self.assertEqual(resp.status_code, 200, resp.text)
            for _ in range(100):
                if record.status == JobStatus.COMPLETED:
                    break
                time.sleep(0.01)

        self.assertEqual(record.status, JobStatus.COMPLETED)
        self.assertEqual(record.review_decisions[0]["action"], "preserve")

    def test_region_rejects_invalid_range_and_non_image(self) -> None:
        record = self._pending_record()
        bad_range = self.client.post(
            f"/api/jobs/{record.job_id}/review/{self.finding_id}/region",
            data={"measure_start": "2", "measure_end": "1"},
            files={"file": ("crop.png", PNG_BYTES, "image/png")},
        )
        self.assertEqual(bad_range.status_code, 400)

        bad_image = self.client.post(
            f"/api/jobs/{record.job_id}/review/{self.finding_id}/region",
            data={"measure_start": "2", "measure_end": "2"},
            files={"file": ("crop.txt", b"not an image", "text/plain")},
        )
        self.assertEqual(bad_image.status_code, 400)

    def test_region_upload_returns_a_new_pending_analysis(self) -> None:
        record = self._pending_record()
        base_xml = """
        <score-partwise version="4.0">
          <part id="P1">
            <measure number="1"><attributes><time><beats>4</beats><beat-type>4</beat-type></time></attributes><note><rest/><duration>16</duration></note></measure>
            <measure number="2"><attributes><time><beats>2</beats><beat-type>4</beat-type></time></attributes><note><rest/><duration>8</duration></note></measure>
          </part>
        </score-partwise>
        """
        replacement_xml = base_xml.replace(
            '<measure number="2">',
            '<measure number="2"><!-- replacement -->',
        )
        (record.workspace.output_dir / "score.raw.musicxml").write_text(base_xml, encoding="utf-8")

        def fake_region(*args, **kwargs):
            raw_path = args[2]
            merged_path = args[3]
            raw_path.parent.mkdir(parents=True, exist_ok=True)
            merged_path.parent.mkdir(parents=True, exist_ok=True)
            raw_path.write_text(base_xml, encoding="utf-8")
            merged_path.write_text(replacement_xml, encoding="utf-8")
            return {"raw_xml": raw_path, "merged_xml": merged_path}

        with mock.patch("sheet2music.web.jobs.run_region_reidentification", side_effect=fake_region):
            resp = self.client.post(
                f"/api/jobs/{record.job_id}/review/{self.finding_id}/region",
                data={"measure_start": "1", "measure_end": "2"},
                files={"file": ("crop.png", PNG_BYTES, "image/png")},
            )
            self.assertEqual(resp.status_code, 200, resp.text)
            for _ in range(100):
                if record.status == JobStatus.AWAITING_REVIEW and record.analysis is not None:
                    break
                time.sleep(0.01)

        self.assertEqual(record.status, JobStatus.AWAITING_REVIEW)
        self.assertTrue(record.analysis["requires_review"])
        self.assertTrue(record.workspace.region_raw_xml_dir.exists())
        self.assertEqual(
            (record.workspace.output_dir / "score.raw.musicxml").read_text(encoding="utf-8"),
            base_xml,
        )
        self.assertEqual(
            record.report["region_reidentification"]["finding_id"],
            self.finding_id,
        )
        self.assertEqual(
            record.report["combined_musicxml_candidate"],
            record.report["region_reidentification"]["merged_xml"],
        )

    def test_failed_region_keeps_base_xml_and_marks_finding_for_retry(self) -> None:
        record = self._pending_record()
        base_xml = "<score-partwise><part id='P1'><measure number='1'/><measure number='2'/></part></score-partwise>"
        base_path = record.workspace.output_dir / "score.raw.musicxml"
        base_path.write_text(base_xml, encoding="utf-8")

        with mock.patch(
            "sheet2music.web.jobs.run_region_reidentification",
            side_effect=RuntimeError("region HOMR failed"),
        ):
            resp = self.client.post(
                f"/api/jobs/{record.job_id}/review/{self.finding_id}/region",
                data={"measure_start": "2", "measure_end": "2"},
                files={"file": ("crop.png", PNG_BYTES, "image/png")},
            )
            self.assertEqual(resp.status_code, 200, resp.text)
            for _ in range(100):
                if record.error:
                    break
                time.sleep(0.01)

        self.assertEqual(record.status, JobStatus.AWAITING_REVIEW)
        self.assertEqual(record.analysis["findings"][0]["status"], "retry")
        self.assertIn("region HOMR failed", record.report["region_error"])
        self.assertEqual(base_path.read_text(encoding="utf-8"), base_xml)


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
