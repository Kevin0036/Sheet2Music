"""内存任务存储 + 后台执行。

任务串行执行（本地单任务，避免 CPU 密集的 HOMR 互相争抢）。
"""

from __future__ import annotations

import json
import threading
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from collections.abc import Mapping

from ..core.analysis import analyze_musicxml_tree
from ..core.auto_resolution import (
    AutoResolutionBatch,
    AutoResolutionOutcome,
    AutoResolutionStore,
    BatchStatus,
    CandidateArtifact,
    TransactionContext,
    apply_candidate_transactionally,
    recover_pending_commits,
    validate_batch_candidate_artifact,
)
from ..core.convert import finalize_conversion, resume_automatic_resolution, run_conversion
from ..core.audio_transcription import run_audio_transcription
from ..core.homr import run_homr_on_page
from ..core.models import ConvertParams, JobStatus
from ..core.reidentify import run_region_reidentification, validate_region_request
from ..core.structure import coerce_structure_plan
from ..core.workspace import JobWorkspace, create_job_workspace, write_report


class ReviewError(ValueError):
    """Raised when a review decision cannot be applied to a job."""


class AutoResolutionConflict(ReviewError):
    """Raised when an automatic-resolution action cannot be retried."""


def summarize_review_changes(
    previous_analysis: Mapping[str, object],
    current_analysis: Mapping[str, object],
) -> dict[str, object]:
    """Describe how high-risk findings changed after a region replacement."""
    previous = _high_risk_findings(previous_analysis)
    current = _high_risk_findings(current_analysis)
    previous_ids = {finding["id"] for finding in previous}
    current_ids = {finding["id"] for finding in current}
    return {
        "before_high_risk_count": len(previous),
        "after_high_risk_count": len(current),
        "unchanged_high_risk_count": len(previous_ids & current_ids),
        "new_findings": [finding for finding in current if finding["id"] not in previous_ids],
        "resolved_findings": [finding for finding in previous if finding["id"] not in current_ids],
    }


def _high_risk_findings(analysis: Mapping[str, object]) -> list[dict[str, object]]:
    raw_findings = analysis.get("findings", [])
    if not isinstance(raw_findings, list):
        return []
    return [
        dict(finding)
        for finding in raw_findings
        if isinstance(finding, Mapping)
        and finding.get("severity") == "high"
        and isinstance(finding.get("id"), str)
    ]


def _can_resume_automatic(record: "JobRecord") -> bool:
    report = record.report
    return bool(
        record.params is not None
        and isinstance(report, Mapping)
        and isinstance(report.get("combined_musicxml_raw"), str)
        and isinstance(report.get("page_layouts"), list)
        and isinstance(report.get("page_measure_offsets"), list)
        and isinstance(report.get("analysis"), Mapping)
    )


def normalize_review_decisions(
    analysis: Mapping[str, object],
    decisions: object,
) -> list[dict[str, object]]:
    """Validate decisions and fill their structural fields from the report."""
    if not isinstance(decisions, list):
        raise ReviewError("decisions must be a list")
    findings_raw = analysis.get("findings", [])
    if not isinstance(findings_raw, list):
        raise ReviewError("analysis findings must be a list")
    findings = {
        finding.get("id"): finding
        for finding in findings_raw
        if isinstance(finding, Mapping) and isinstance(finding.get("id"), str)
    }
    required = {
        finding_id
        for finding_id, finding in findings.items()
        if finding.get("severity") == "high"
        and finding.get("status", "pending") not in {"resolved", "accepted_original"}
    }
    normalized: list[dict[str, object]] = []
    seen: set[str] = set()
    for raw_decision in decisions:
        if not isinstance(raw_decision, Mapping):
            raise ReviewError("each decision must be an object")
        finding_id = raw_decision.get("id")
        action = raw_decision.get("action")
        if not isinstance(finding_id, str) or finding_id not in findings:
            raise ReviewError("decision refers to an unknown finding")
        if finding_id in seen:
            raise ReviewError(f"duplicate decision for finding: {finding_id}")
        if action not in {"preserve", "correct", "reidentify", "ignore"}:
            raise ReviewError("action must be preserve, correct, reidentify, or ignore")
        finding = findings[finding_id]
        available_actions = finding.get("available_actions")
        if (
            action != "ignore"
            and isinstance(available_actions, list)
            and action not in available_actions
        ):
            raise ReviewError(
                f"action {action!r} is not available for finding: {finding_id}"
            )
        if action == "reidentify":
            raise ReviewError("reidentify decisions require a region upload")
        decision = dict(finding)
        decision.update({"id": finding_id, "action": action})
        seen.add(finding_id)
        normalized.append(decision)

    missing = sorted(required - seen)
    if missing:
        raise ReviewError(f"missing decisions for findings: {', '.join(missing)}")
    return normalized


@dataclass
class JobRecord:
    job_id: str
    workspace: JobWorkspace
    status: JobStatus = JobStatus.PENDING
    stage: str = "uploaded"
    error: str | None = None
    params: ConvertParams | None = None
    report: dict[str, object] | None = None
    analysis: dict[str, object] | None = None
    review_decisions: list[dict[str, object]] = field(default_factory=list)
    progress: dict[str, object] | None = None
    filename: str = ""
    input_kind: str = "pdf"
    created_at: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "job_id": self.job_id,
            "status": self.status.value,
            "stage": self.stage,
            "error": self.error,
            "filename": self.filename,
            "input_kind": self.input_kind,
            "created_at": self.created_at,
            "params": self.params.to_dict() if self.params else None,
            "report": self.report,
            "analysis": self.analysis,
            "review_decisions": self.review_decisions,
            "progress": self.progress,
            "artifacts": [a.to_dict() for a in self.workspace.artifacts()],
        }


class JobStore:
    def __init__(self, base_dir: Path) -> None:
        self.base_dir = base_dir
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._jobs: dict[str, JobRecord] = {}
        self._lock = threading.Lock()
        self._worker_lock = threading.Lock()  # 串行化转换任务
        self._restore_jobs()

    def _restore_jobs(self) -> None:
        resumable_records: list[JobRecord] = []
        recovered_commits: list[tuple[JobRecord, list[AutoResolutionBatch], Path]] = []
        for state_path in self.base_dir.glob("*/job-state.json"):
            try:
                payload = json.loads(state_path.read_text(encoding="utf-8"))
                workspace = JobWorkspace(state_path.parent)
                params_payload = payload.get("params")
                params = None
                if isinstance(params_payload, Mapping):
                    params = ConvertParams.validate(
                        params_payload.get("bpm"),
                        params_payload.get("time_signature", "4/4"),
                        params_payload.get("outputs", []),
                        params_payload.get("use_gpu", False),
                        params_payload.get("structure_plan"),
                        transkun_model=params_payload.get("transkun_model", "v2"),
                    )
                status = JobStatus(str(payload.get("status", JobStatus.PENDING.value)))
                error = payload.get("error") if isinstance(payload.get("error"), str) else None
                stage = str(payload.get("stage", "uploaded"))
                interrupted_upload_recognition = (
                    status == JobStatus.RUNNING
                    and stage == "automatic_upload_recognition"
                )
                resumable_automatic_stage = (
                    status == JobStatus.RUNNING and stage == "automatic_reidentification"
                )
                if (
                    status == JobStatus.RUNNING
                    and not resumable_automatic_stage
                    and not interrupted_upload_recognition
                ):
                    status = JobStatus.FAILED
                    stage = "failed"
                    error = "服务重启中断了正在执行的任务，请重新开始转换或二次识别"
                record = JobRecord(
                    job_id=state_path.parent.name,
                    workspace=workspace,
                    status=status,
                    stage=stage,
                    error=error,
                    params=params,
                    report=payload.get("report") if isinstance(payload.get("report"), dict) else None,
                    analysis=payload.get("analysis") if isinstance(payload.get("analysis"), dict) else None,
                    review_decisions=(
                        payload.get("review_decisions")
                        if isinstance(payload.get("review_decisions"), list)
                        else []
                    ),
                    progress=payload.get("progress") if isinstance(payload.get("progress"), dict) else None,
                    filename=str(payload.get("filename", "")),
                    input_kind=str(payload.get("input_kind", "pdf")),
                    created_at=str(payload.get("created_at", "")),
                )
                self._jobs[record.job_id] = record
                batch_state = record.workspace.auto_resolution_dir / "batches.json"
                candidate_path = record.workspace.output_dir / "score.auto.musicxml"
                if batch_state.exists() and candidate_path.exists():
                    batch_store = AutoResolutionStore(batch_state)
                    persisted_batches = batch_store.load()
                    has_pending_commit = any(
                        batch.status == BatchStatus.COMMITTING
                        for batch in persisted_batches
                    )
                    if has_pending_commit:
                        recovered_batches = recover_pending_commits(candidate_path, batch_store)
                        if interrupted_upload_recognition and any(
                            batch.status == BatchStatus.AUTO_RESOLVED
                            for batch in recovered_batches
                        ):
                            record.status = JobStatus.AWAITING_REVIEW
                            record.stage = JobStatus.AWAITING_REVIEW.value
                            record.error = None
                        recovered_commits.append(
                            (
                                record,
                                recovered_batches,
                                candidate_path,
                            )
                        )
                    elif record.status == JobStatus.AWAITING_REVIEW and record.params is not None:
                        # Refresh the persisted report after batch-state migration. In
                        # particular, exhausted automatic candidates become accepted_original
                        # and must disappear from the upload queue after a restart.
                        self._refresh_auto_record(record, persisted_batches, candidate_path)
                if resumable_automatic_stage:
                    if _can_resume_automatic(record):
                        resumable_records.append(record)
                    else:
                        record.status = JobStatus.FAILED
                        record.stage = "failed"
                        record.error = (
                            "自动二次识别任务无法恢复：缺少原始 MusicXML、页面布局、"
                            "小节偏移或分析结果，请重新开始转换"
                        )
                        self._persist_record(record)
                if (
                    str(payload.get("status")) == JobStatus.RUNNING.value
                    and not resumable_automatic_stage
                    and record.status != JobStatus.AWAITING_REVIEW
                ):
                    record.status = JobStatus.FAILED
                    record.stage = "failed"
                    record.error = "服务重启中断了正在执行的任务，请重新开始转换或二次识别"
                    self._persist_record(record)
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                continue
        for record, batches, candidate_path in recovered_commits:
            if record.status == JobStatus.AWAITING_REVIEW and record.params is not None:
                try:
                    self._refresh_auto_record(record, batches, candidate_path)
                except (OSError, ValueError, TypeError, ET.ParseError):
                    record.status = JobStatus.FAILED
                    record.stage = "failed"
                    record.error = "候选结果已恢复，但审核报告刷新失败，请重新开始转换"
                    self._persist_record(record)
        for record in resumable_records:
            threading.Thread(
                target=self._resume_automatic,
                args=(record,),
                name=f"auto-resume-job-{record.job_id}",
                daemon=True,
            ).start()

    def _persist_record(self, record: JobRecord) -> None:
        state_path = record.workspace.root / "job-state.json"
        temporary_path = record.workspace.root / "job-state.json.tmp"
        payload = {
            "job_id": record.job_id,
            "status": record.status.value,
            "stage": record.stage,
            "error": record.error,
            "filename": record.filename,
            "input_kind": record.input_kind,
            "created_at": record.created_at,
            "params": record.params.to_dict() if record.params else None,
            "report": record.report,
            "analysis": record.analysis,
            "review_decisions": record.review_decisions,
            "progress": record.progress,
        }
        temporary_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary_path.replace(state_path)

    def save(self, record: JobRecord) -> None:
        with self._lock:
            self._persist_record(record)

    def get_auto_batch(self, record: JobRecord, batch_id: str) -> AutoResolutionBatch:
        if not batch_id:
            raise ReviewError("automatic-resolution batch id is required")
        batches = AutoResolutionStore(
            record.workspace.auto_resolution_dir / "batches.json"
        ).load()
        batch = next((item for item in batches if item.batch_id == batch_id), None)
        if batch is None:
            raise ReviewError("automatic-resolution batch does not exist")
        return batch

    def select_auto_candidate(
        self,
        record: JobRecord,
        batch_id: str,
        candidate_id: str,
    ) -> None:
        with self._worker_lock:
            self._select_auto_candidate_locked(record, batch_id, candidate_id)

    def _select_auto_candidate_locked(
        self,
        record: JobRecord,
        batch_id: str,
        candidate_id: str,
    ) -> None:
        if record.status != JobStatus.AWAITING_REVIEW:
            raise ReviewError("job is not awaiting automatic-resolution review")
        batch = self.get_auto_batch(record, batch_id)
        if batch.status != BatchStatus.NEEDS_CHOICE:
            raise ReviewError("batch does not require candidate selection")
        attempt = next(
            (
                item
                for item in batch.attempts
                if item.get("variant") == candidate_id
                and item.get("status") == "succeeded"
                and isinstance(item.get("validation"), Mapping)
                and item["validation"].get("accepted") is True
            ),
            None,
        )
        if attempt is None:
            raise ReviewError("candidate is not a valid choice for this batch")
        candidate_value = attempt.get("candidate_xml")
        if not isinstance(candidate_value, str):
            raise ReviewError("candidate MusicXML path is missing")
        params = record.params
        if params is None:
            raise ReviewError("job has no conversion parameters")
        # Persisted batches from older runs can be exhausted without a final
        # state transition. Treat three successful variants as accepting the
        # original score so restart recovery remains deterministic.
        for batch in batches:
            if batch.status == BatchStatus.NEEDS_UPLOAD and batch.attempts and all(
                attempt.get("status") == "succeeded" for attempt in batch.attempts
            ):
                batch.status = BatchStatus.ACCEPTED_ORIGINAL
                batch.attempts.append(
                    {"variant": None, "status": BatchStatus.ACCEPTED_ORIGINAL.value}
                )
        base_path = self._automatic_candidate_path(record)
        plan = coerce_structure_plan(params.structure_plan, params.time_signature)
        store = AutoResolutionStore(record.workspace.auto_resolution_dir / "batches.json")

        def persist_commit(journal: Mapping[str, object]) -> None:
            batch.status = BatchStatus.COMMITTING
            batch.selected_candidate = candidate_id
            batch.commit = dict(journal)
            store.save_batch(batch)

        result = apply_candidate_transactionally(
            TransactionContext(
                base_xml_path=base_path,
                validation_dir=record.workspace.auto_resolution_validation_dir,
                structure_plan=plan,
                page_measure_offsets=tuple(
                    item
                    for item in (record.report or {}).get("page_measure_offsets", [])
                    if isinstance(item, int) and not isinstance(item, bool)
                ),
                before_commit=persist_commit,
            ),
            batch,
            CandidateArtifact(
                candidate_id=candidate_id,
                xml_path=Path(candidate_value),
                candidate_global_start=batch.context_range[0],
            ),
        )
        attempt["transaction"] = {
            "committed": result.committed,
            "reason": result.reason,
            "new_high_risk_ids": list(result.new_high_risk_ids),
        }
        if not result.committed:
            AutoResolutionStore(
                record.workspace.auto_resolution_dir / "batches.json"
            ).save_batch(batch)
            raise ReviewError(f"candidate failed revalidation: {result.reason}")
        batch.status = BatchStatus.AUTO_RESOLVED
        batch.selected_candidate = candidate_id
        store.save_batch(batch)
        self._refresh_auto_record(record, store.load(), base_path)

    def retry_auto_batch(self, record: JobRecord, batch_id: str) -> None:
        if record.status != JobStatus.AWAITING_REVIEW:
            raise ReviewError("job is not awaiting automatic-resolution review")
        batch = self.get_auto_batch(record, batch_id)
        attempted = {
            str(item.get("variant"))
            for item in batch.attempts
            if item.get("variant") in {"standard", "contrast", "context"}
            and item.get("status") in {"succeeded", "failed"}
        }
        if attempted == {"standard", "contrast", "context"}:
            batch.status = BatchStatus.NEEDS_UPLOAD
            AutoResolutionStore(
                record.workspace.auto_resolution_dir / "batches.json"
            ).save_batch(batch)
            raise AutoResolutionConflict("all three automatic variants have already been tried")
        batch.status = BatchStatus.PENDING
        AutoResolutionStore(record.workspace.auto_resolution_dir / "batches.json").save_batch(batch)
        with self._lock:
            record.status = JobStatus.RUNNING
            record.stage = "automatic_reidentification"
            record.error = None
            self._persist_record(record)
        threading.Thread(
            target=self._resume_automatic,
            args=(record,),
            name=f"auto-retry-job-{record.job_id}",
            daemon=True,
        ).start()

    def submit_auto_upload(
        self,
        record: JobRecord,
        batch_id: str,
        image_path: Path,
    ) -> None:
        if record.status != JobStatus.AWAITING_REVIEW:
            raise ReviewError("job is not awaiting automatic-resolution review")
        batch = self.get_auto_batch(record, batch_id)
        if batch.status != BatchStatus.NEEDS_UPLOAD:
            raise ReviewError("batch upload is available only after automatic attempts fail")
        report = record.report or {}
        measure_count = report.get("num_measures")
        if not isinstance(measure_count, int):
            analysis = record.analysis or report.get("analysis", {})
            measure_count = analysis.get("measures_seen") if isinstance(analysis, Mapping) else None
        if not isinstance(measure_count, int):
            measure_count = batch.context_range[1]
        validate_region_request(
            image_path,
            batch.context_range[0],
            batch.context_range[1],
            measure_count,
        )
        with self._lock:
            record.status = JobStatus.RUNNING
            record.stage = "automatic_upload_recognition"
            record.error = None
            self._persist_record(record)
        threading.Thread(
            target=self._run_auto_upload,
            args=(record, batch, image_path),
            name=f"auto-upload-job-{record.job_id}-{batch.batch_id}",
            daemon=True,
        ).start()

    def _automatic_candidate_path(self, record: JobRecord) -> Path:
        candidate = record.workspace.output_dir / "score.auto.musicxml"
        if candidate.exists():
            return candidate
        report = record.report or {}
        raw_value = report.get("combined_musicxml_raw")
        if not isinstance(raw_value, str):
            raise ReviewError("job has no base MusicXML")
        source = record.workspace.root / raw_value
        if not source.exists():
            raise ReviewError("base MusicXML is missing")
        candidate.parent.mkdir(parents=True, exist_ok=True)
        candidate.write_bytes(source.read_bytes())
        return candidate

    def _refresh_auto_record(
        self,
        record: JobRecord,
        batches: list[AutoResolutionBatch],
        candidate_path: Path,
    ) -> None:
        params = record.params
        if params is None:
            raise ReviewError("job has no conversion parameters")
        plan = coerce_structure_plan(params.structure_plan, params.time_signature)
        offsets = (record.report or {}).get("page_measure_offsets", [])
        analysis = analyze_musicxml_tree(
            ET.parse(candidate_path).getroot(),
            plan,
            page_measure_offsets=offsets if isinstance(offsets, list) else [],
        ).to_dict()
        accepted_original = getattr(BatchStatus, "ACCEPTED_ORIGINAL", None)
        accepted_measures = {
            measure
            for batch in batches
            if accepted_original is not None and batch.status == accepted_original
            for measure in batch.target_measures
        }
        findings = analysis.get("findings", [])
        if isinstance(findings, list):
            for finding in findings:
                if (
                    isinstance(finding, dict)
                    and finding.get("kind") == "timing_measure_overflow"
                    and finding.get("measure_start") in accepted_measures
                ):
                    finding["status"] = getattr(accepted_original, "value", "accepted_original")
            analysis["requires_review"] = any(
                isinstance(finding, dict)
                and finding.get("severity") == "high"
                and finding.get("status", "pending") == "pending"
                for finding in findings
            )
        outcome = AutoResolutionOutcome(
            candidate_path=candidate_path,
            batches=tuple(batches),
            resolved_count=sum(item.status == BatchStatus.AUTO_RESOLVED for item in batches),
            needs_choice_count=sum(item.status == BatchStatus.NEEDS_CHOICE for item in batches),
            needs_upload_count=sum(
                item.status in {BatchStatus.NEEDS_UPLOAD, BatchStatus.FAILED}
                for item in batches
            ),
        )
        report = dict(record.report or {})
        report.update(
            {
                "status": JobStatus.AWAITING_REVIEW.value,
                "analysis": analysis,
                "combined_musicxml_candidate": candidate_path.relative_to(
                    record.workspace.root
                ).as_posix(),
                "auto_resolution": outcome.to_dict(record.workspace.root),
            }
        )
        with self._lock:
            record.report = report
            record.analysis = analysis
            record.status = JobStatus.AWAITING_REVIEW
            record.stage = JobStatus.AWAITING_REVIEW.value
            self._persist_record(record)
        write_report(record.workspace, report)

    def _run_auto_upload(
        self,
        record: JobRecord,
        batch: AutoResolutionBatch,
        image_path: Path,
    ) -> None:
        with self._worker_lock:
            candidate_id = f"user-upload-{uuid.uuid4().hex[:12]}"
            attempt: dict[str, object] = {
                "variant": candidate_id,
                "status": "failed",
                "uploaded_image": image_path.relative_to(record.workspace.root).as_posix(),
            }
            store = AutoResolutionStore(record.workspace.auto_resolution_dir / "batches.json")
            try:
                params = record.params
                report = record.report or {}
                if params is None:
                    raise ReviewError("job has no conversion parameters")
                candidate_dir = record.workspace.auto_resolution_candidate_dir / batch.batch_id
                candidate_dir.mkdir(parents=True, exist_ok=True)
                layout_path = candidate_dir / f"{candidate_id}.layout.json"
                result_path = run_homr_on_page(
                    image_path,
                    work_dir=(
                        record.workspace.auto_resolution_dir
                        / "homr_work"
                        / batch.batch_id
                        / candidate_id
                    ),
                    tempo_bpm=params.bpm,
                    use_gpu=params.use_gpu,
                    layout_output=layout_path,
                )
                candidate_path = candidate_dir / f"{candidate_id}.musicxml"
                candidate_path.write_bytes(result_path.read_bytes())
                base_path = self._automatic_candidate_path(record)
                layouts = report.get("page_layouts", [])
                offsets = report.get("page_measure_offsets", [])
                if not isinstance(layouts, list) or not all(
                    isinstance(item, str) for item in layouts
                ):
                    raise ReviewError("job has invalid page layout paths")
                if not isinstance(offsets, list) or not all(
                    isinstance(item, int) and not isinstance(item, bool) for item in offsets
                ):
                    raise ReviewError("job has invalid page offsets")
                plan = coerce_structure_plan(params.structure_plan, params.time_signature)
                validation = validate_batch_candidate_artifact(
                    workspace=record.workspace,
                    base_xml=base_path,
                    candidate_xml=candidate_path,
                    candidate_layout=layout_path,
                    batch=batch,
                    page_layouts=[record.workspace.root / item for item in layouts],
                    page_measure_offsets=offsets,
                    structure_plan=plan,
                    candidate_id=candidate_id,
                )
                attempt.update(
                    {
                        "status": "succeeded",
                        "candidate_xml": str(candidate_path),
                        "layout_json": str(layout_path),
                        "validation": {
                            "candidate_id": validation.candidate_id,
                            "accepted": validation.accepted,
                            "reasons": list(validation.reasons),
                            "fingerprint": validation.fingerprint,
                            "target_findings_before": validation.target_findings_before,
                            "target_findings_after": validation.target_findings_after,
                            "has_strong_single_candidate_evidence": (
                                validation.has_strong_single_candidate_evidence
                            ),
                        },
                    }
                )
                batch.attempts.append(attempt)
                store.save_batch(batch)
                if not validation.accepted:
                    raise ReviewError(
                        "uploaded candidate failed validation: "
                        + ", ".join(validation.reasons)
                    )
                def persist_commit(journal: Mapping[str, object]) -> None:
                    batch.status = BatchStatus.COMMITTING
                    batch.selected_candidate = candidate_id
                    batch.commit = dict(journal)
                    store.save_batch(batch)

                transaction = apply_candidate_transactionally(
                    TransactionContext(
                        base_xml_path=base_path,
                        validation_dir=record.workspace.auto_resolution_validation_dir,
                        structure_plan=plan,
                        page_measure_offsets=tuple(offsets),
                        before_commit=persist_commit,
                    ),
                    batch,
                    CandidateArtifact(
                        candidate_id=candidate_id,
                        xml_path=candidate_path,
                        candidate_global_start=batch.context_range[0],
                    ),
                )
                attempt["transaction"] = {
                    "committed": transaction.committed,
                    "reason": transaction.reason,
                    "new_high_risk_ids": list(transaction.new_high_risk_ids),
                }
                if not transaction.committed:
                    raise ReviewError(
                        f"uploaded candidate failed revalidation: {transaction.reason}"
                    )
                batch.status = BatchStatus.AUTO_RESOLVED
                batch.selected_candidate = candidate_id
                store.save_batch(batch)
                self._refresh_auto_record(record, store.load(), base_path)
            except Exception as exc:  # noqa: BLE001 - preserve final-upload fallback state
                if attempt not in batch.attempts:
                    attempt["error"] = f"{type(exc).__name__}: {exc}"
                    batch.attempts.append(attempt)
                else:
                    attempt["error"] = f"{type(exc).__name__}: {exc}"
                batch.status = BatchStatus.NEEDS_UPLOAD
                store.save_batch(batch)
                try:
                    self._refresh_auto_record(
                        record,
                        store.load(),
                        self._automatic_candidate_path(record),
                    )
                except Exception:
                    pass
                with self._lock:
                    record.error = f"{type(exc).__name__}: {exc}"
                    record.status = JobStatus.AWAITING_REVIEW
                    record.stage = JobStatus.AWAITING_REVIEW.value
                    self._persist_record(record)

    def create(self, filename: str, input_kind: str = "pdf") -> JobRecord:
        workspace = create_job_workspace(self.base_dir)
        record = JobRecord(
            job_id=workspace.root.name,
            workspace=workspace,
            status=JobStatus.PENDING,
            stage="uploaded",
            filename=filename,
            input_kind=input_kind,
            created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        with self._lock:
            self._jobs[record.job_id] = record
            self._persist_record(record)
        return record

    def get(self, job_id: str) -> JobRecord | None:
        with self._lock:
            return self._jobs.get(job_id)

    def mark_preview_ready(self, record: JobRecord) -> None:
        with self._lock:
            record.stage = "preview_ready"
            self._persist_record(record)

    def set_stage(self, record: JobRecord, stage: str) -> None:
        with self._lock:
            record.stage = stage
            if stage == "automatic_reidentification":
                report_path = record.workspace.output_dir / "report.json"
                if report_path.exists():
                    preparation = json.loads(report_path.read_text(encoding="utf-8"))
                    if isinstance(preparation, dict):
                        record.report = preparation
            self._persist_record(record)

    def set_progress(
        self,
        record: JobRecord,
        current: int,
        total: int,
        page: str,
        *,
        system: int | None = None,
        resolved: int | None = None,
        needs_review: int | None = None,
    ) -> None:
        with self._lock:
            progress: dict[str, object] = {
                "current": current,
                "total": total,
                "page": page,
            }
            if system is not None:
                progress["system"] = system
            if resolved is not None:
                progress["resolved"] = resolved
            if needs_review is not None:
                progress["needs_review"] = needs_review
            record.progress = progress
            self._persist_record(record)

    def start(self, record: JobRecord, params: ConvertParams, debug: bool = False) -> None:
        record.workspace.reset_auto_resolution()
        with self._lock:
            record.params = params
            record.error = None
            record.report = None
            record.analysis = None
            record.review_decisions = []
            record.progress = None
            record.status = JobStatus.RUNNING
            record.stage = "downloading_video_audio" if record.input_kind == "video_url" else "converting_audio" if record.input_kind == "audio" else "running_homr"
            self._persist_record(record)
        thread = threading.Thread(
            target=self._run,
            args=(record, params, debug),
            name=f"{record.input_kind}-job-{record.job_id}",
            daemon=True,
        )
        thread.start()

    def submit_review(
        self,
        record: JobRecord,
        decisions: object,
        debug: bool = False,
    ) -> None:
        with self._lock:
            if record.status != JobStatus.AWAITING_REVIEW:
                raise ReviewError("job is not awaiting review")
            analysis = record.analysis or {}
            params = record.params
            preparation = record.report
            if params is None or preparation is None:
                raise ReviewError("job has no prepared conversion")
            normalized = normalize_review_decisions(analysis, decisions)
            record.review_decisions = normalized
            record.error = None
            record.status = JobStatus.RUNNING
            record.stage = "finalizing_review"
            self._persist_record(record)

        thread = threading.Thread(
            target=self._finalize,
            args=(record, params, preparation, normalized),
            name=f"review-job-{record.job_id}",
            daemon=True,
        )
        thread.start()

    def _finalize(
        self,
        record: JobRecord,
        params: ConvertParams,
        preparation: dict[str, object],
        decisions: list[dict[str, object]],
    ) -> None:
        with self._worker_lock:
            try:
                report = finalize_conversion(
                    record.workspace,
                    params,
                    preparation,
                    review_decisions=decisions,
                    stage=lambda name: self.set_stage(record, name),
                )
                with self._lock:
                    record.report = report
                    record.analysis = report.get("analysis") if isinstance(report.get("analysis"), dict) else None
                    record.status = JobStatus.COMPLETED
                    record.stage = "completed"
                    self._persist_record(record)
            except Exception as exc:  # noqa: BLE001 - expose finalization errors to the client
                with self._lock:
                    record.error = f"{type(exc).__name__}: {exc}"
                    record.status = JobStatus.FAILED
                    record.stage = "failed"
                    self._persist_record(record)

    def submit_region(
        self,
        record: JobRecord,
        finding_id: str,
        image_path: Path,
        measure_start: int,
        measure_end: int,
        debug: bool = False,
    ) -> None:
        with self._lock:
            if record.status != JobStatus.AWAITING_REVIEW:
                raise ReviewError("job is not awaiting review")
            analysis = record.analysis or {}
            findings = analysis.get("findings", [])
            finding = next(
                (
                    item
                    for item in findings
                    if isinstance(item, Mapping) and item.get("id") == finding_id
                ),
                None,
            )
            if not isinstance(finding, Mapping) or finding.get("severity") != "high":
                raise ReviewError("finding is not a pending high-risk finding")
            previous_analysis = dict(analysis)
            params = record.params
            preparation = record.report
            if params is None or preparation is None:
                raise ReviewError("job has no prepared conversion")
            score_measure_count = preparation.get("num_measures")
            if not isinstance(score_measure_count, int):
                score_measure_count = analysis.get("measures_seen")
            if not isinstance(score_measure_count, int):
                raise ReviewError("prepared report has no score measure count")
            validate_region_request(image_path, measure_start, measure_end, score_measure_count)
            combined_raw_value = preparation.get("combined_musicxml_candidate")
            if not isinstance(combined_raw_value, str):
                combined_raw_value = preparation.get("combined_musicxml_raw")
            if not isinstance(combined_raw_value, str):
                raise ReviewError("prepared report has no combined MusicXML")
            base_path = record.workspace.root / combined_raw_value
            if not base_path.exists():
                raise ReviewError("combined MusicXML is missing")
            token = uuid.uuid4().hex
            raw_xml_path = record.workspace.region_raw_xml_dir / f"{token}.musicxml"
            merged_xml_path = record.workspace.region_merged_xml_dir / f"{token}.musicxml"
            record.error = None
            record.status = JobStatus.RUNNING
            record.stage = "reidentifying_region"
            self._persist_record(record)

        thread = threading.Thread(
            target=self._run_region,
            args=(
                record,
                params,
                preparation,
                base_path,
                image_path,
                raw_xml_path,
                merged_xml_path,
                finding_id,
                measure_start,
                measure_end,
                score_measure_count,
                previous_analysis,
                debug,
            ),
            name=f"region-job-{record.job_id}",
            daemon=True,
        )
        thread.start()

    def _run_region(
        self,
        record: JobRecord,
        params: ConvertParams,
        preparation: dict[str, object],
        base_path: Path,
        image_path: Path,
        raw_xml_path: Path,
        merged_xml_path: Path,
        finding_id: str,
        measure_start: int,
        measure_end: int,
        score_measure_count: int,
        previous_analysis: Mapping[str, object],
        debug: bool,
    ) -> None:
        with self._worker_lock:
            try:
                result = run_region_reidentification(
                    base_path,
                    image_path,
                    raw_xml_path,
                    merged_xml_path,
                    record.workspace.region_homr_work_dir / raw_xml_path.stem,
                    measure_start,
                    measure_end,
                    score_measure_count,
                    tempo_bpm=params.bpm,
                    use_gpu=params.use_gpu,
                    debug=debug,
                )
                plan = coerce_structure_plan(params.structure_plan, params.time_signature)
                analysis = analyze_musicxml_tree(
                    ET.parse(result["merged_xml"]).getroot(),
                    plan,
                    page_measure_offsets=preparation.get("page_measure_offsets") or [],
                ).to_dict()
                analysis_changes = summarize_review_changes(previous_analysis, analysis)
                updated_report = dict(preparation)
                updated_report.update(
                    {
                        "status": JobStatus.AWAITING_REVIEW.value,
                        "analysis": analysis,
                        "combined_musicxml_candidate": str(
                            result["merged_xml"].relative_to(record.workspace.root)
                        ),
                        "region_reidentification": {
                            "finding_id": finding_id,
                            "uploaded_image": str(image_path.relative_to(record.workspace.root)),
                            "raw_xml": str(raw_xml_path.relative_to(record.workspace.root)),
                            "merged_xml": str(merged_xml_path.relative_to(record.workspace.root)),
                            "analysis_changes": analysis_changes,
                        },
                    }
                )
                write_report(record.workspace, updated_report)
                with self._lock:
                    record.report = updated_report
                    record.analysis = analysis
                    record.review_decisions = []
                    record.status = JobStatus.AWAITING_REVIEW
                    record.stage = JobStatus.AWAITING_REVIEW.value
                    self._persist_record(record)
            except Exception as exc:  # noqa: BLE001 - retain review state for retry
                error = f"{type(exc).__name__}: {exc}"
                with self._lock:
                    analysis = dict(record.analysis or {})
                    findings = analysis.get("findings", [])
                    if isinstance(findings, list):
                        updated_findings: list[object] = []
                        for finding in findings:
                            if isinstance(finding, Mapping) and finding.get("id") == finding_id:
                                updated_finding = dict(finding)
                                updated_finding["status"] = "retry"
                                updated_findings.append(updated_finding)
                            else:
                                updated_findings.append(finding)
                        analysis["findings"] = updated_findings

                    updated_report = dict(record.report or preparation)
                    updated_report.update(
                        {
                            "status": JobStatus.AWAITING_REVIEW.value,
                            "analysis": analysis,
                            "region_error": error,
                        }
                    )
                    record.error = error
                    record.report = updated_report
                    record.analysis = analysis
                    record.status = JobStatus.AWAITING_REVIEW
                    record.stage = JobStatus.AWAITING_REVIEW.value
                    self._persist_record(record)
                write_report(record.workspace, updated_report)

    def _run(self, record: JobRecord, params: ConvertParams, debug: bool) -> None:
        with self._worker_lock:
            try:
                if record.input_kind in {"audio", "video_url"}:
                    report = run_audio_transcription(
                        record.workspace,
                        use_gpu=params.use_gpu,
                        transkun_model=params.transkun_model,
                        stage=lambda name: self.set_stage(record, name),
                    )
                else:
                    report = run_conversion(
                        record.workspace,
                        params,
                        stage=lambda name: self.set_stage(record, name),
                        progress=lambda current, total, page, **details: self.set_progress(
                            record, current, total, page, **details
                        ),
                        debug=debug,
                    )
                with self._lock:
                    record.report = report
                    report_status = report.get("status")
                    if report_status == JobStatus.AWAITING_REVIEW.value:
                        record.analysis = report.get("analysis") if isinstance(report.get("analysis"), dict) else None
                        record.status = JobStatus.AWAITING_REVIEW
                        record.stage = JobStatus.AWAITING_REVIEW.value
                    else:
                        record.status = JobStatus.COMPLETED
                        record.stage = "completed"
                    self._persist_record(record)
            except Exception as exc:  # noqa: BLE001 - 任何失败都要暴露给前端
                with self._lock:
                    record.error = f"{type(exc).__name__}: {exc}"
                    record.status = JobStatus.FAILED
                    record.stage = "failed"
                    self._persist_record(record)

    def _resume_automatic(self, record: JobRecord) -> None:
        with self._worker_lock:
            try:
                params = record.params
                preparation = record.report
                if params is None or preparation is None:
                    raise ReviewError("automatic-resolution job has no persisted preparation")
                updated = resume_automatic_resolution(
                    record.workspace,
                    params,
                    preparation,
                    progress=lambda current, total, page, **details: self.set_progress(
                        record,
                        current,
                        total,
                        page,
                        **details,
                    ),
                )
                if updated.get("status") == JobStatus.AWAITING_REVIEW.value:
                    with self._lock:
                        record.report = updated
                        record.analysis = (
                            updated.get("analysis")
                            if isinstance(updated.get("analysis"), dict)
                            else None
                        )
                        record.status = JobStatus.AWAITING_REVIEW
                        record.stage = JobStatus.AWAITING_REVIEW.value
                        self._persist_record(record)
                    return

                report = finalize_conversion(
                    record.workspace,
                    params,
                    updated,
                    stage=lambda name: self.set_stage(record, name),
                )
                with self._lock:
                    record.report = report
                    record.analysis = (
                        report.get("analysis")
                        if isinstance(report.get("analysis"), dict)
                        else None
                    )
                    record.status = JobStatus.COMPLETED
                    record.stage = "completed"
                    self._persist_record(record)
            except Exception as exc:  # noqa: BLE001 - persist recovery failures
                with self._lock:
                    record.error = f"{type(exc).__name__}: {exc}"
                    record.status = JobStatus.FAILED
                    record.stage = "failed"
                    self._persist_record(record)

    def reset(self, job_id: str) -> bool:
        with self._lock:
            record = self._jobs.pop(job_id, None)
        if record is None:
            return False
        record.workspace.cleanup()
        return True
