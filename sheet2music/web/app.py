"""FastAPI 应用：浏览器界面 + 转换 API。

端点对齐设计文档：/api/preview、/api/convert、/api/jobs/{id}、
/api/jobs/{id}/reset、/api/jobs/{id}/artifacts/{name}。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..core import system
from ..core.models import ConvertParams, ValidationError
from ..core.pages import extract_first_page_preview
from ..core.settings import host, port, work_dir
from .jobs import JobStore

MAX_UPLOAD_BYTES = 50 * 1024 * 1024  # 50 MB


class ConvertRequest(BaseModel):
    job_id: str
    bpm: int
    time_signature: str = "4/4"
    outputs: list[str] = Field(default_factory=list)
    use_gpu: bool = False


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


@app.post("/api/convert")
def start_conversion(request: ConvertRequest) -> dict[str, object]:
    record = store.get(request.job_id)
    if record is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    try:
        params = ConvertParams.validate(
            request.bpm,
            request.time_signature,
            request.outputs,
            request.use_gpu,
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


@app.get("/api/jobs/{job_id}/preview")
def get_preview(job_id: str) -> FileResponse:
    record = store.get(job_id)
    if record is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    preview = record.workspace.preview_dir / "page-1.png"
    if not preview.exists():
        raise HTTPException(status_code=404, detail="预览不存在")
    return FileResponse(preview, media_type="image/png")


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
