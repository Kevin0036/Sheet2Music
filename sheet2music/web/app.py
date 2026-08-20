"""FastAPI 应用：浏览器界面 + 转换 API。

端点对齐设计文档：/api/preview、/api/convert、/api/jobs/{id}、
/api/jobs/{id}/reset、/api/jobs/{id}/artifacts/{name}。
"""

from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..core import system
from ..core.models import ConvertParams, ValidationError
from ..core.pages import extract_first_page_preview
from ..core.reidentify import MAX_REGION_UPLOAD_BYTES
from ..core.settings import host, port, work_dir
from ..core.video_audio import validate_video_url
from .jobs import AutoResolutionConflict, JobStore, ReviewError

MAX_UPLOAD_BYTES = 50 * 1024 * 1024  # 50 MB


class ConvertRequest(BaseModel):
    job_id: str
    bpm: int | None = None
    time_signature: str = "4/4"
    outputs: list[str] = Field(default_factory=list)
    use_gpu: bool = False
    transkun_model: str = "v2"
    has_pickup_measure: bool = False


class ReviewRequest(BaseModel):
    decisions: list[dict[str, object]] = Field(default_factory=list)


class CandidateSelectionRequest(BaseModel):
    candidate_id: str


store = JobStore(work_dir())

app = FastAPI(title="Sheet2Music")


@app.post("/api/preview")
async def upload_pdf(file: UploadFile = File(...)) -> dict[str, object]:
    filename = file.filename or "score.pdf"
    if not filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="只支持 PDF 文件")

    record = store.create(filename)
    workspace = record.workspace
    try:
        with open(workspace.pdf_path, "wb") as target:
            written = 0
            while chunk := await file.read(1024 * 1024):
                written += len(chunk)
                if written > MAX_UPLOAD_BYTES:
                    raise HTTPException(status_code=413, detail="PDF 超过 50MB 限制")
                target.write(chunk)
        extract_first_page_preview(workspace.pdf_path, workspace.preview_dir)
    except HTTPException:
        store.reset(record.job_id)
        raise
    except Exception as exc:
        store.reset(record.job_id)
        raise HTTPException(status_code=400, detail=f"预览生成失败: {exc}") from exc

    store.mark_preview_ready(record)
    return {
        "job_id": record.job_id,
        "preview_url": f"/api/jobs/{record.job_id}/preview",
        "filename": filename,
    }


@app.post("/api/audio")
async def upload_audio(file: UploadFile = File(...)) -> dict[str, object]:
    filename = file.filename or "audio.mp3"
    if not filename.lower().endswith(".mp3"):
        raise HTTPException(status_code=400, detail="只支持 MP3 文件")
    record = store.create(filename, input_kind="audio")
    try:
        written = 0
        with record.workspace.audio_path.open("wb") as target:
            while chunk := await file.read(1024 * 1024):
                written += len(chunk)
                if written > MAX_UPLOAD_BYTES:
                    raise HTTPException(status_code=413, detail="MP3 超过 50MB 限制")
                target.write(chunk)
    except HTTPException:
        store.reset(record.job_id)
        raise
    except Exception as exc:
        store.reset(record.job_id)
        raise HTTPException(status_code=400, detail=f"音频上传失败: {exc}") from exc
    record.stage = "audio_uploaded"
    store.save(record)
    return {"job_id": record.job_id, "filename": filename, "input_kind": "audio"}


class VideoUrlRequest(BaseModel):
    url: str


@app.post("/api/video-url")
def upload_video_url(request: VideoUrlRequest) -> dict[str, object]:
    try:
        url = validate_video_url(request.url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    record = store.create(url, input_kind="video_url")
    record.workspace.source_url_path.write_text(url, encoding="utf-8")
    record.stage = "video_url_uploaded"
    store.save(record)
    return {"job_id": record.job_id, "filename": url, "input_kind": "video_url"}


@app.post("/api/convert")
def start_conversion(request: ConvertRequest) -> dict[str, object]:
    record = store.get(request.job_id)
    if record is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    try:
        if record.input_kind in {"audio", "video_url"}:
            params = ConvertParams.validate(120, "4/4", ["midi", "mp3"], request.use_gpu, transkun_model=request.transkun_model)
        else:
            params = ConvertParams.validate(
                request.bpm,
                request.time_signature,
                request.outputs,
                request.use_gpu,
                has_pickup_measure=request.has_pickup_measure,
            )
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    store.start(record, params)
    return {"job_id": record.job_id}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> dict[str, object]:
    record = store.get(job_id)
    if record is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return record.to_dict()


@app.get("/api/jobs/{job_id}/analysis")
def get_analysis(job_id: str) -> dict[str, object]:
    record = store.get(job_id)
    if record is None:
        raise HTTPException(status_code=404, detail="浠诲姟涓嶅瓨鍦?")
    return {
        "job_id": record.job_id,
        "status": record.status.value,
        "analysis": record.analysis,
        "review_decisions": record.review_decisions,
    }


@app.post("/api/jobs/{job_id}/review")
def submit_review(job_id: str, request: ReviewRequest) -> dict[str, object]:
    record = store.get(job_id)
    if record is None:
        raise HTTPException(status_code=404, detail="浠诲姟涓嶅瓨鍦?")
    try:
        store.submit_review(record, request.decisions)
    except ReviewError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return record.to_dict()


@app.post("/api/jobs/{job_id}/review/{finding_id}/region")
async def submit_region(
    job_id: str,
    finding_id: str,
    file: UploadFile = File(...),
    measure_start: int = Form(...),
    measure_end: int = Form(...),
) -> dict[str, object]:
    record = store.get(job_id)
    if record is None:
        raise HTTPException(status_code=404, detail="浠诲姟涓嶅瓨鍦?")
    suffix = Path(file.filename or "").suffix.lower()
    upload_path = record.workspace.region_upload_dir / f"{uuid.uuid4().hex}{suffix}"
    written = 0
    try:
        with upload_path.open("wb") as target:
            while chunk := await file.read(1024 * 1024):
                written += len(chunk)
                if written > MAX_REGION_UPLOAD_BYTES:
                    raise HTTPException(status_code=413, detail="区域图片超过 20MB 限制")
                target.write(chunk)
        store.submit_region(record, finding_id, upload_path, measure_start, measure_end)
    except HTTPException:
        raise
    except ReviewError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"区域二次识别失败: {exc}") from exc
    return record.to_dict()


@app.get("/api/jobs/{job_id}/preview")
def get_preview(job_id: str) -> FileResponse:
    record = store.get(job_id)
    if record is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    preview = record.workspace.preview_dir / "page-1.png"
    if not preview.exists():
        raise HTTPException(status_code=404, detail="预览不存在")
    return FileResponse(preview, media_type="image/png")


@app.get("/api/jobs/{job_id}/pages/{page_number}")
def get_page_preview(job_id: str, page_number: int) -> FileResponse:
    record = store.get(job_id)
    if record is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if page_number <= 0:
        raise HTTPException(status_code=400, detail="页码必须为正整数")
    page = record.workspace.pages_dir / f"page-{page_number}.png"
    if not page.exists():
        raise HTTPException(status_code=404, detail="页面图像不存在")
    return FileResponse(page, media_type="image/png")


@app.get("/api/jobs/{job_id}/auto-resolution/{batch_id}/crop")
def get_auto_resolution_crop(job_id: str, batch_id: str) -> FileResponse:
    record = store.get(job_id)
    if record is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    try:
        batch = store.get_auto_batch(record, batch_id)
    except ReviewError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    crop = record.workspace.auto_resolution_crop_dir / batch.batch_id / "source.png"
    if not crop.exists() or not crop.is_file():
        raise HTTPException(status_code=404, detail="自动裁图不存在")
    return FileResponse(crop, media_type="image/png")


@app.post("/api/jobs/{job_id}/auto-resolution/{batch_id}/select")
def select_auto_resolution_candidate(
    job_id: str,
    batch_id: str,
    request: CandidateSelectionRequest,
) -> dict[str, object]:
    record = store.get(job_id)
    if record is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    try:
        store.select_auto_candidate(record, batch_id, request.candidate_id)
    except ReviewError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return record.to_dict()


@app.post("/api/jobs/{job_id}/auto-resolution/{batch_id}/retry")
def retry_auto_resolution_batch(job_id: str, batch_id: str) -> dict[str, object]:
    record = store.get(job_id)
    if record is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    try:
        store.retry_auto_batch(record, batch_id)
    except AutoResolutionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ReviewError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return record.to_dict()


@app.post("/api/jobs/{job_id}/auto-resolution/{batch_id}/upload")
async def upload_auto_resolution_batch(
    job_id: str,
    batch_id: str,
    file: UploadFile = File(...),
) -> dict[str, object]:
    record = store.get(job_id)
    if record is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    suffix = Path(file.filename or "").suffix.lower()
    upload_path = record.workspace.auto_resolution_upload_dir / f"{uuid.uuid4().hex}{suffix}"
    written = 0
    try:
        with upload_path.open("wb") as target:
            while chunk := await file.read(1024 * 1024):
                written += len(chunk)
                if written > MAX_REGION_UPLOAD_BYTES:
                    raise HTTPException(status_code=413, detail="系统图片超过 20MB 限制")
                target.write(chunk)
        store.submit_auto_upload(record, batch_id, upload_path)
    except HTTPException:
        raise
    except ReviewError as exc:
        upload_path.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        upload_path.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail=f"系统二次识别失败: {exc}") from exc
    return record.to_dict()


@app.get("/api/jobs/{job_id}/artifacts/{name}")
def get_artifact(job_id: str, name: str) -> FileResponse:
    if "/" in name or "\\" in name or name in (".", ".."):
        raise HTTPException(status_code=400, detail="非法文件名")
    record = store.get(job_id)
    if record is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    artifact_path = record.workspace.output_dir / name
    if not artifact_path.exists() or not artifact_path.is_file():
        raise HTTPException(status_code=404, detail="产物不存在")
    return FileResponse(artifact_path, filename=name)


@app.post("/api/jobs/{job_id}/reset")
def reset_job(job_id: str) -> dict[str, object]:
    if not store.reset(job_id):
        raise HTTPException(status_code=404, detail="任务不存在")
    return {"ok": True}


@app.get("/api/system/status")
def get_system_status() -> dict[str, object]:
    return system.system_status()


@app.post("/api/system/weights/download")
def start_weights_download() -> dict[str, object]:
    started = system.start_weight_download()
    return {"started": started, "state": system.weight_download_state()}


@app.get("/api/system/weights/download")
def get_weights_download_state() -> dict[str, object]:
    return system.weight_download_state()


# 静态前端挂在 API 路由之后，避免吞掉 API 路径。
STATIC_DIR = Path(__file__).resolve().parent / "static"
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")


def main() -> None:
    import uvicorn

    uvicorn.run(app, host=host(), port=port())


if __name__ == "__main__":
    main()
