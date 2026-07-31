"""内存任务存储 + 后台执行。

任务串行执行（本地单任务，避免 CPU 密集的 HOMR 互相争抢）。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..core.convert import run_conversion
from ..core.models import ConvertParams, JobStatus
from ..core.workspace import JobWorkspace, create_job_workspace


@dataclass
class JobRecord:
    job_id: str
    workspace: JobWorkspace
    status: JobStatus = JobStatus.PENDING
    stage: str = "uploaded"
    error: str | None = None
    params: ConvertParams | None = None
    report: dict[str, object] | None = None
    progress: dict[str, object] | None = None
    filename: str = ""
    created_at: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "job_id": self.job_id,
            "status": self.status.value,
            "stage": self.stage,
            "error": self.error,
            "filename": self.filename,
            "created_at": self.created_at,
            "params": self.params.to_dict() if self.params else None,
            "report": self.report,
            "progress": self.progress,
            "artifacts": [a.to_dict() for a in self.workspace.artifacts()],
        }


class JobStore:
    def __init__(self, base_dir: Path) -> None:
        self.base_dir = base_dir
        self._jobs: dict[str, JobRecord] = {}
        self._lock = threading.Lock()
        self._worker_lock = threading.Lock()  # 串行化转换任务

    def create(self, filename: str) -> JobRecord:
        workspace = create_job_workspace(self.base_dir)
        record = JobRecord(
            job_id=workspace.root.name,
            workspace=workspace,
            status=JobStatus.PENDING,
            stage="uploaded",
            filename=filename,
            created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        with self._lock:
            self._jobs[record.job_id] = record
        return record

    def get(self, job_id: str) -> JobRecord | None:
        with self._lock:
            return self._jobs.get(job_id)

    def mark_preview_ready(self, record: JobRecord) -> None:
        with self._lock:
            record.stage = "preview_ready"

    def set_stage(self, record: JobRecord, stage: str) -> None:
        with self._lock:
            record.stage = stage

    def set_progress(self, record: JobRecord, current: int, total: int, page: str) -> None:
        with self._lock:
            record.progress = {"current": current, "total": total, "page": page}

    def start(self, record: JobRecord, params: ConvertParams, debug: bool = False) -> None:
        with self._lock:
            record.params = params
            record.error = None
            record.report = None
            record.progress = None
            record.status = JobStatus.RUNNING
            record.stage = "running_homr"
        thread = threading.Thread(
            target=self._run,
            args=(record, params, debug),
            name=f"homr-job-{record.job_id}",
            daemon=True,
        )
        thread.start()

    def _run(self, record: JobRecord, params: ConvertParams, debug: bool) -> None:
        with self._worker_lock:
            try:
                report = run_conversion(
                    record.workspace,
                    params,
                    stage=lambda name: self.set_stage(record, name),
                    progress=lambda current, total, page: self.set_progress(
                        record, current, total, page
                    ),
                    debug=debug,
                )
                with self._lock:
                    record.report = report
                    record.status = JobStatus.COMPLETED
                    record.stage = "completed"
            except Exception as exc:  # noqa: BLE001 - 任何失败都要暴露给前端
                with self._lock:
                    record.error = f"{type(exc).__name__}: {exc}"
                    record.status = JobStatus.FAILED
                    record.stage = "failed"

    def reset(self, job_id: str) -> bool:
        with self._lock:
            record = self._jobs.pop(job_id, None)
        if record is None:
            return False
        record.workspace.cleanup()
        return True
