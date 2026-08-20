"""每任务工作目录管理：目录布局、产物收集、清理与 zip 打包。"""

from __future__ import annotations

import json
import shutil
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ArtifactInfo:
    name: str
    path: Path
    kind: str  # musicxml | midi | mp3 | zip | report
    size: int

    def to_dict(self) -> dict[str, object]:
        return {"name": self.name, "kind": self.kind, "size": self.size}


class JobWorkspace:
    """单个转换任务的工作目录。

    布局（对齐设计文档的 File and Artifact Model）：
      input/score.pdf
      preview/page-1.png
      pages/raw/page-1.png ...     high-resolution source pages
      pages/page-1.png ...        cropped HOMR input pages
      homr_raw/page-1.musicxml ...
      homr_fixed/page-1.musicxml ...
      homr_work/<page>/...         HOMR 子进程工作目录
      output/score.musicxml score.mid score.mp3 score.zip report.json
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.input_dir = root / "input"
        self.preview_dir = root / "preview"
        self.audio_dir = root / "audio"
        self.pages_dir = root / "pages"
        self.page_geometry_dir = self.pages_dir / "geometry"
        self.layout_dir = root / "layout"
        self.raw_page_xml_dir = root / "homr_raw"
        self.fixed_page_xml_dir = root / "homr_fixed"
        self.homr_work_dir = root / "homr_work"
        self.region_dir = root / "regions"
        self.region_upload_dir = self.region_dir / "uploads"
        self.region_raw_xml_dir = self.region_dir / "raw"
        self.region_merged_xml_dir = self.region_dir / "merged"
        self.region_homr_work_dir = self.region_dir / "homr_work"
        self.auto_resolution_dir = root / "auto_resolution"
        self.auto_resolution_crop_dir = self.auto_resolution_dir / "crops"
        self.auto_resolution_candidate_dir = self.auto_resolution_dir / "candidates"
        self.auto_resolution_validation_dir = self.auto_resolution_dir / "validation"
        self.auto_resolution_upload_dir = self.auto_resolution_dir / "uploads"
        self.output_dir = root / "output"

    def create(self) -> "JobWorkspace":
        for directory in (
            self.input_dir,
            self.preview_dir,
            self.audio_dir,
            self.pages_dir,
            self.page_geometry_dir,
            self.layout_dir,
            self.raw_page_xml_dir,
            self.fixed_page_xml_dir,
            self.homr_work_dir,
            self.region_upload_dir,
            self.region_raw_xml_dir,
            self.region_merged_xml_dir,
            self.region_homr_work_dir,
            self.auto_resolution_crop_dir,
            self.auto_resolution_candidate_dir,
            self.auto_resolution_validation_dir,
            self.auto_resolution_upload_dir,
            self.output_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        return self

    def cleanup(self) -> None:
        if self.root.exists():
            shutil.rmtree(self.root, ignore_errors=True)

    def reset_auto_resolution(self) -> None:
        if self.auto_resolution_dir.exists():
            shutil.rmtree(self.auto_resolution_dir)
        for directory in (
            self.auto_resolution_crop_dir,
            self.auto_resolution_candidate_dir,
            self.auto_resolution_validation_dir,
            self.auto_resolution_upload_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        automatic_candidate = self.output_dir / "score.auto.musicxml"
        if automatic_candidate.exists():
            automatic_candidate.unlink()

    @property
    def pdf_path(self) -> Path:
        return self.input_dir / "score.pdf"

    @property
    def audio_path(self) -> Path:
        return self.input_dir / "score.mp3"

    @property
    def source_url_path(self) -> Path:
        return self.input_dir / "source.url"

    @property
    def audio_wav_path(self) -> Path:
        return self.audio_dir / "score.wav"

    @property
    def beats_path(self) -> Path:
        return self.audio_dir / "beats.json"

    def artifacts(self) -> list[ArtifactInfo]:
        """可下载产物：规范输出（score.musicxml / score.mid / score.mp3 / score.zip）
        与 report.json。`score.raw.*` 等中间文件只留档、不当作下载产物。
        """
        artifacts: list[ArtifactInfo] = []
        if not self.output_dir.exists():
            return artifacts
        for path in sorted(self.output_dir.iterdir()):
            if not path.is_file():
                continue
            if path.name.startswith("score.raw."):
                continue
            artifacts.append(self._artifact(path))
        return artifacts

    def _artifact(self, path: Path) -> ArtifactInfo:
        suffix = path.suffix.lower()
        kind = {
            ".musicxml": "musicxml",
            ".mid": "midi",
            ".mp3": "mp3",
            ".zip": "zip",
            ".json": "report",
        }.get(suffix, suffix.lstrip("."))
        return ArtifactInfo(name=path.name, path=path, kind=kind, size=path.stat().st_size)


def create_job_workspace(base_dir: Path) -> JobWorkspace:
    job_id = uuid.uuid4().hex[:12]
    workspace = JobWorkspace(base_dir / job_id)
    workspace.create()
    return workspace


def write_report(workspace: JobWorkspace, report: dict[str, object]) -> Path:
    report_path = workspace.output_dir / "report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report_path


def make_zip_bundle(workspace: JobWorkspace) -> Path:
    """把当前所有产物（含 report.json）打包成一个 zip，供一键下载。"""
    bundle_path = workspace.output_dir / "score.zip"
    with zipfile.ZipFile(bundle_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for artifact in workspace.artifacts():
            if artifact.name == "score.zip":
                continue
            archive.write(artifact.path, arcname=artifact.name)
    return bundle_path
