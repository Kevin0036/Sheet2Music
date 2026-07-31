"""完整转换流水线编排。

镜像 `run_homr_trial.py` 的 process_bundle，但只依赖纯 job workspace，
不碰数据集 bundle / 清单 / 训练配置。阶段名与设计文档一致。
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from .combine import combine_page_musicxml
from .export import export_midi, render_mp3
from .homr import HomrPageError, run_homr_on_page
from .models import ConvertParams
from .pages import export_numbered_pages
from .repair import fix_midi_file, fix_musicxml_file
from .system import missing_model_files
from .workspace import JobWorkspace, make_zip_bundle, write_report

#: 转换阶段的回调签名（stage_callback("running_homr") 等）。
StageCallback = Callable[[str], None]

#: 逐页进度回调：ProgressCallback(current, total, page_name)，current 从 1 开始。
ProgressCallback = Callable[[int, int, str], None]


class ConversionError(RuntimeError):
    """转换无法完成时抛出（缺少输入、HOMR 失败、导出失败等）。"""


def run_conversion(
    workspace: JobWorkspace,
    params: ConvertParams,
    stage: StageCallback | None = None,
    progress: ProgressCallback | None = None,
    debug: bool = False,
    max_pages: int | None = None,
) -> dict[str, object]:
    """对一个 PDF 运行规范流水线，返回转换报告（并写入 report.json）。

    参数:
        workspace: 该任务的工作目录（必须已包含 input/score.pdf）。
        params:    转换参数（BPM、拍号、导出格式）。
        stage:     可选阶段回调，用于前端展示进度。
        progress:  可选逐页进度回调 (current, total, page_name)。
        debug:     传给 HOMR 的 --debug。
        max_pages: 只转换前 N 页（测试 / 快速试跑用）。
    """
    def _stage(name: str) -> None:
        if stage is not None:
            stage(name)

    if not workspace.pdf_path.exists():
        raise ConversionError(f"缺少输入 PDF: {workspace.pdf_path}")

    missing = missing_model_files(use_gpu=params.use_gpu)
    if missing:
        names = "、".join(path.name for path in missing)
        weight_kind = "GPU/FP16" if params.use_gpu else "CPU/FP32"
        raise ConversionError(
            f"{weight_kind} 模型权重缺失：{names}。请先在页面顶部「环境检查」中点击「下载模型权重」，完成后重试。"
        )

    _stage("running_homr")
    page_images = export_numbered_pages(
        workspace.pdf_path,
        workspace.pages_dir,
        dpi=600,
        crop_vertical=True,
    )
    if max_pages is not None:
        page_images = page_images[:max_pages]
    if not page_images:
        raise ConversionError("没有可用的页面图")

    raw_page_xmls: list[Path] = []
    skipped_pages: list[dict[str, str]] = []
    for index, page_image in enumerate(page_images, start=1):
        if progress is not None:
            progress(index, len(page_images), page_image.name)
        try:
            page_xml = run_homr_on_page(
                page_image,
                work_dir=workspace.homr_work_dir / page_image.stem,
                debug=debug,
                tempo_bpm=params.bpm,
                use_gpu=params.use_gpu,
            )
        except HomrPageError as exc:
            # HOMR 对个别页（如歌词/文本页、空页）可能崩溃：跳过并记录，不中断整份乐谱。
            skipped_pages.append({"page": exc.page, "error": exc.stderr_tail})
            continue
        raw_xml = workspace.raw_page_xml_dir / f"{page_image.stem}.musicxml"
        raw_xml.write_bytes(page_xml.read_bytes())
        raw_page_xmls.append(raw_xml)

    if not raw_page_xmls:
        detail = f" 最后一页错误：{skipped_pages[-1]['error']}" if skipped_pages else ""
        raise ConversionError(f"所有页面都无法识别（可能不是乐谱，或缺少模型权重）。{detail}")

    _stage("repairing_musicxml")
    fixed_page_xmls: list[Path] = []
    page_fix_reports: list[dict[str, object]] = []
    for raw_xml in raw_page_xmls:
        fixed_xml = workspace.fixed_page_xml_dir / raw_xml.name
        report = fix_musicxml_file(raw_xml, fixed_xml, params.time_signature, params.bpm)
        fixed_page_xmls.append(fixed_xml)
        page_fix_reports.append(report.to_dict())

    combined_raw = workspace.output_dir / "score.raw.musicxml"
    score_stats = combine_page_musicxml(fixed_page_xmls, combined_raw)

    combined_xml = workspace.output_dir / "score.musicxml"
    combined_fix_report = fix_musicxml_file(combined_raw, combined_xml, params.time_signature, params.bpm)

    _stage("exporting_midi")
    raw_midi = workspace.output_dir / "score.raw.mid"
    export_midi(combined_xml, raw_midi)
    midi_path = workspace.output_dir / "score.mid"
    midi_fix_report = fix_midi_file(raw_midi, midi_path, params.time_signature, params.bpm)

    if "mp3" in params.outputs:
        _stage("rendering_mp3")
        render_mp3(combined_xml, workspace.output_dir / "score.mp3")

    # 按用户勾选裁剪规范输出（raw 中间文件保留留档）。
    if "musicxml" not in params.outputs:
        (workspace.output_dir / "score.musicxml").unlink(missing_ok=True)
    if "midi" not in params.outputs:
        (workspace.output_dir / "score.mid").unlink(missing_ok=True)

    report = {
        "tool": "sheet2music",
        "pipeline": "homr_musicxml_repair_musescore_export",
        "params": params.to_dict(),
        "page_count": len(page_images),
        "page_count_recognized": len(raw_page_xmls),
        "skipped_pages": skipped_pages,
        "page_musicxml_raw": [p.name for p in raw_page_xmls],
        "page_musicxml_fixed": [p.name for p in fixed_page_xmls],
        "page_fix_reports": page_fix_reports,
        "combined_musicxml_raw": str(combined_raw.relative_to(workspace.root)),
        "combined_musicxml": str(combined_xml.relative_to(workspace.root)),
        "combined_fix_report": combined_fix_report.to_dict(),
        "midi_raw": str(raw_midi.relative_to(workspace.root)),
        "midi": str(midi_path.relative_to(workspace.root)),
        "midi_fix_report": midi_fix_report.to_dict(),
        **score_stats,
    }
    write_report(workspace, report)

    if "zip" in params.outputs:
        make_zip_bundle(workspace)

    _stage("completed")
    return report
