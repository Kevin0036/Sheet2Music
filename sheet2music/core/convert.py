"""Conversion preparation, review, and final export orchestration."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Callable

from .analysis import analyze_musicxml_tree
from .auto_resolution import resolve_timing_overflows
from .combine import combine_page_musicxml
from .export import export_midi, render_mp3
from .homr import HomrPageError, run_homr_on_page
from .models import ConvertParams
from .pages import export_numbered_pages
from .repair import (
    apply_deterministic_timing_decisions,
    apply_reviewed_clef_decisions,
    find_measure_divisions,
    fix_midi_file,
    fix_musicxml_file,
)
from .structure import ScoreStructurePlan, coerce_structure_plan
from .system import missing_model_files
from .timeline import analyze_measure, fraction_text, units_to_beats
from .workspace import JobWorkspace, make_zip_bundle, write_report

StageCallback = Callable[[str], None]
ProgressCallback = Callable[[int, int, str], None]


class ConversionError(RuntimeError):
    """Raised when a conversion cannot complete safely."""


def validate_musicxml_boundaries(
    root: ET.Element,
    structure_plan: ScoreStructurePlan | dict[str, object] | None,
    allowed_overflow_measures: set[int] | None = None,
) -> None:
    """Reject invalid cursors and notes extending beyond ordinary measure bounds."""
    plan = coerce_structure_plan(structure_plan)
    failures: list[str] = []
    for part in root.findall("part"):
        part_id = part.get("id", "?")
        divisions = 1
        for ordinal, measure in enumerate(part.findall("measure"), start=1):
            divisions = find_measure_divisions(measure, divisions)
            if not any(child.tag in {"note", "backup", "forward"} for child in measure):
                continue
            beats, beat_type = plan.time_signature_for(ordinal)
            timeline = analyze_measure(measure, divisions, beats, beat_type)
            label = measure.get("number") or str(ordinal)
            if timeline.diagnostics:
                if allowed_overflow_measures and ordinal in allowed_overflow_measures:
                    continue
                failures.append(f"{part_id} 第 {label} 小节时间游标无效")
            elif timeline.has_overflow:
                if allowed_overflow_measures and ordinal in allowed_overflow_measures:
                    continue
                occupied = fraction_text(
                    units_to_beats(timeline.maximum_note_end_units, divisions)
                )
                expected = fraction_text(units_to_beats(timeline.expected_units, divisions))
                failures.append(
                    f"{part_id} 第 {label} 小节占用 {occupied} 拍，容量 {expected} 拍"
                )
    if failures:
        raise ConversionError("仍有未解决的小节时值问题：" + "；".join(failures))


def _accepted_original_measures(preparation: dict[str, object]) -> set[int]:
    auto_resolution = preparation.get("auto_resolution")
    batches = auto_resolution.get("batches", []) if isinstance(auto_resolution, dict) else []
    accepted: set[int] = set()
    for batch in batches:
        if not isinstance(batch, dict) or batch.get("status") != "accepted_original":
            continue
        targets = batch.get("target_measures", [])
        if isinstance(targets, list):
            accepted.update(item for item in targets if isinstance(item, int))
    return accepted


def _mark_accepted_original_findings(
    analysis: object,
    accepted_measures: set[int],
) -> None:
    findings = getattr(analysis, "findings", [])
    for finding in findings:
        if (
            finding.kind in {"timing_measure_overflow", "timing_cursor_invalid"}
            and finding.measure_start in accepted_measures
        ):
            finding.status = "accepted_original"


def _musicxml_measure_count(path: Path) -> int:
    root = ET.parse(path).getroot()
    return max((len(part.findall("measure")) for part in root.findall("part")), default=0)


def _relative(workspace: JobWorkspace, path: Path) -> str:
    return path.relative_to(workspace.root).as_posix()


def _stage_callback(stage: StageCallback | None) -> Callable[[str], None]:
    def emit(name: str) -> None:
        if stage is not None:
            stage(name)

    return emit


def _validate_workspace(workspace: JobWorkspace, params: ConvertParams) -> None:
    if not workspace.pdf_path.exists():
        raise ConversionError(f"缺少输入 PDF: {workspace.pdf_path}")

    missing = missing_model_files(use_gpu=params.use_gpu)
    if missing:
        names = "、".join(path.name for path in missing)
        weight_kind = "GPU/FP16" if params.use_gpu else "CPU/FP32"
        raise ConversionError(
            f"{weight_kind} 模型权重缺失：{names}。请先在页面顶部「环境检查」中点击「下载模型权重」，完成后重试。"
        )


def _recognize_pages(
    workspace: JobWorkspace,
    params: ConvertParams,
    emit_stage: Callable[[str], None],
    progress: ProgressCallback | None,
    debug: bool,
    max_pages: int | None,
) -> tuple[list[Path], list[Path], list[Path], list[dict[str, str]]]:
    emit_stage("running_homr")
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
    page_layouts: list[Path] = []
    skipped_pages: list[dict[str, str]] = []
    for index, page_image in enumerate(page_images, start=1):
        if progress is not None:
            progress(index, len(page_images), page_image.name)
        layout_path = workspace.layout_dir / f"{page_image.stem}.json"
        try:
            page_xml = run_homr_on_page(
                page_image,
                work_dir=workspace.homr_work_dir / page_image.stem,
                debug=debug,
                tempo_bpm=params.bpm,
                use_gpu=params.use_gpu,
                layout_output=layout_path,
            )
        except HomrPageError as exc:
            skipped_pages.append({"page": exc.page, "error": exc.stderr_tail})
            continue
        raw_xml = workspace.raw_page_xml_dir / f"{page_image.stem}.musicxml"
        raw_xml.write_bytes(page_xml.read_bytes())
        raw_page_xmls.append(raw_xml)
        page_layouts.append(layout_path)

    if not raw_page_xmls:
        detail = f" 最后一页错误：{skipped_pages[-1]['error']}" if skipped_pages else ""
        raise ConversionError(f"所有页面都无法识别（可能不是乐谱，或缺少模型权重）。{detail}")
    return page_images, raw_page_xmls, page_layouts, skipped_pages


def _repair_pages_and_combine(
    workspace: JobWorkspace,
    params: ConvertParams,
    raw_page_xmls: list[Path],
) -> tuple[list[int], list[Path], list[dict[str, object]], Path, dict[str, int]]:
    page_measure_offsets: list[int] = []
    measure_offset = 0
    for raw_xml in raw_page_xmls:
        page_measure_offsets.append(measure_offset)
        measure_offset += _musicxml_measure_count(raw_xml)

    fixed_page_xmls: list[Path] = []
    page_fix_reports: list[dict[str, object]] = []
    for raw_xml, page_measure_offset in zip(raw_page_xmls, page_measure_offsets, strict=True):
        fixed_xml = workspace.fixed_page_xml_dir / raw_xml.name
        report = fix_musicxml_file(
            raw_xml,
            fixed_xml,
            params.time_signature,
            params.bpm,
            structure_plan=params.structure_plan,
            measure_offset=page_measure_offset,
            normalize_transient=params.structure_plan is None,
        )
        fixed_page_xmls.append(fixed_xml)
        page_fix_reports.append(report.to_dict())

    combined_raw = workspace.output_dir / "score.raw.musicxml"
    # Analyze the immutable HOMR output before candidate repair can normalize
    # an unconfirmed score-level change such as an isolated time signature.
    score_stats = combine_page_musicxml(raw_page_xmls, combined_raw)
    return page_measure_offsets, fixed_page_xmls, page_fix_reports, combined_raw, score_stats


def prepare_conversion(
    workspace: JobWorkspace,
    params: ConvertParams,
    stage: StageCallback | None = None,
    progress: ProgressCallback | None = None,
    debug: bool = False,
    max_pages: int | None = None,
) -> dict[str, object]:
    """Run HOMR and preflight analysis without exporting final audio artifacts."""

    emit_stage = _stage_callback(stage)
    _validate_workspace(workspace, params)
    page_images, raw_page_xmls, page_layouts, skipped_pages = _recognize_pages(
        workspace,
        params,
        emit_stage,
        progress,
        debug,
        max_pages,
    )

    emit_stage("repairing_musicxml")
    (
        page_measure_offsets,
        fixed_page_xmls,
        page_fix_reports,
        combined_raw,
        score_stats,
    ) = _repair_pages_and_combine(workspace, params, raw_page_xmls)
    plan = coerce_structure_plan(params.structure_plan, params.time_signature)
    analysis = analyze_musicxml_tree(
        ET.parse(combined_raw).getroot(),
        plan,
        page_measure_offsets=page_measure_offsets,
    )
    has_timing_overflow = any(
        finding.kind in {"timing_measure_overflow", "timing_cursor_invalid"}
        and finding.severity == "high"
        for finding in analysis.findings
    )
    status = (
        "automatic_reidentification"
        if has_timing_overflow
        else "awaiting_review"
        if analysis.requires_review
        else "prepared"
    )
    preparation: dict[str, object] = {
        "status": status,
        "tool": "sheet2music",
        "pipeline": "homr_musicxml_repair_musescore_export",
        "params": params.to_dict(),
        "page_count": len(page_images),
        "page_count_recognized": len(raw_page_xmls),
        "skipped_pages": skipped_pages,
        "page_musicxml_raw": [path.name for path in raw_page_xmls],
        "page_layouts": [_relative(workspace, path) for path in page_layouts],
        "page_musicxml_fixed": [path.name for path in fixed_page_xmls],
        "page_measure_offsets": page_measure_offsets,
        "structure_plan": params.structure_plan,
        "page_fix_reports": page_fix_reports,
        "combined_musicxml_raw": _relative(workspace, combined_raw),
        "analysis": analysis.to_dict(),
        **score_stats,
    }
    if has_timing_overflow:
        # Persist enough information for job recovery before any system-level
        # inference starts. Batch attempts are persisted separately after each run.
        write_report(workspace, preparation)
        emit_stage("automatic_reidentification")
        outcome = resolve_timing_overflows(
            workspace=workspace,
            base_xml=combined_raw,
            analysis=analysis.to_dict(),
            page_layouts=page_layouts,
            page_measure_offsets=page_measure_offsets,
            structure_plan=plan,
            tempo_bpm=params.bpm,
            use_gpu=params.use_gpu,
            progress=progress,
            debug=debug,
            has_pickup_measure=params.has_pickup_measure,
        )
        analysis_source = outcome.candidate_path or combined_raw
        analysis = analyze_musicxml_tree(
            ET.parse(analysis_source).getroot(),
            plan,
            page_measure_offsets=page_measure_offsets,
        )
        _mark_accepted_original_findings(
            analysis,
            {
                measure
                for batch in outcome.batches
                if batch.status.value == "accepted_original"
                for measure in batch.target_measures
            },
        )
        preparation["auto_resolution"] = outcome.to_dict(workspace.root)
        preparation["analysis"] = analysis.to_dict()
        if outcome.candidate_path is not None:
            preparation["combined_musicxml_candidate"] = _relative(
                workspace,
                outcome.candidate_path,
            )
        preparation["status"] = "awaiting_review" if analysis.requires_review else "prepared"
    write_report(workspace, preparation)
    return preparation


def resume_automatic_resolution(
    workspace: JobWorkspace,
    params: ConvertParams,
    preparation: dict[str, object],
    progress: ProgressCallback | None = None,
    debug: bool = False,
) -> dict[str, object]:
    """Resume system-level recognition from an already persisted preparation."""
    raw_value = preparation.get("combined_musicxml_raw")
    layouts_value = preparation.get("page_layouts")
    offsets_value = preparation.get("page_measure_offsets")
    analysis_value = preparation.get("analysis")
    if not isinstance(raw_value, str):
        raise ConversionError("automatic-resolution report has no base MusicXML")
    if not isinstance(layouts_value, list) or not all(
        isinstance(item, str) for item in layouts_value
    ):
        raise ConversionError("automatic-resolution report has invalid page layouts")
    if not isinstance(offsets_value, list) or not all(
        isinstance(item, int) and not isinstance(item, bool) for item in offsets_value
    ):
        raise ConversionError("automatic-resolution report has invalid page offsets")
    if not isinstance(analysis_value, dict):
        raise ConversionError("automatic-resolution report has no analysis")

    plan = coerce_structure_plan(params.structure_plan, params.time_signature)
    raw_path = workspace.root / raw_value
    current_candidate = workspace.output_dir / "score.auto.musicxml"
    current_source = current_candidate if current_candidate.exists() else raw_path
    current_analysis = analyze_musicxml_tree(
        ET.parse(current_source).getroot(),
        plan,
        page_measure_offsets=offsets_value,
    )
    outcome = resolve_timing_overflows(
        workspace=workspace,
        base_xml=raw_path,
        analysis=current_analysis.to_dict(),
        page_layouts=[workspace.root / item for item in layouts_value],
        page_measure_offsets=offsets_value,
        structure_plan=plan,
        tempo_bpm=params.bpm,
        use_gpu=params.use_gpu,
        progress=progress,
        debug=debug,
        has_pickup_measure=params.has_pickup_measure,
    )
    analysis_source = outcome.candidate_path or raw_path
    analysis = analyze_musicxml_tree(
        ET.parse(analysis_source).getroot(),
        plan,
        page_measure_offsets=offsets_value,
    )
    _mark_accepted_original_findings(
        analysis,
        {
            measure
            for batch in outcome.batches
            if batch.status.value == "accepted_original"
            for measure in batch.target_measures
        },
    )
    updated = dict(preparation)
    updated["auto_resolution"] = outcome.to_dict(workspace.root)
    updated["analysis"] = analysis.to_dict()
    updated["status"] = "awaiting_review" if analysis.requires_review else "prepared"
    if outcome.candidate_path is not None:
        updated["combined_musicxml_candidate"] = _relative(workspace, outcome.candidate_path)
    write_report(workspace, updated)
    return updated


def _reviewed_structure_plan(
    params: ConvertParams,
    review_decisions: list[dict[str, object]],
) -> dict[str, object] | None:
    if not review_decisions:
        return params.structure_plan

    plan = coerce_structure_plan(params.structure_plan, params.time_signature).to_dict()
    time_changes = list(plan.get("time_signature_changes", []))
    clef_overrides = list(plan.get("clef_overrides", []))
    for decision in review_decisions:
        action = decision.get("action")
        if action not in {"preserve", "correct", "reidentify", "ignore"}:
            raise ConversionError(f"invalid review action: {action!r}")
        if action == "reidentify":
            raise ConversionError("review decision still requires region re-identification")
        if action == "ignore":
            continue

        kind = str(decision.get("kind", ""))
        measure_start = int(decision.get("measure_start", 0))
        measure_end = int(decision.get("measure_end", measure_start))
        observed = decision.get("observed")
        suggestion = decision.get("suggestion")
        selected = observed if action == "preserve" else suggestion
        if not isinstance(selected, dict):
            continue

        if kind.startswith("timing_"):
            if action != "correct":
                raise ConversionError("时值越界不能保留，必须自动压缩或二次识别")
            continue
        if kind in {"time_signature_change", "missing_time_signature"}:
            signature = selected.get("signature")
            if isinstance(signature, str):
                time_changes.append(
                    {
                        "from_measure": measure_start,
                        "to_measure": measure_end,
                        "signature": signature,
                    }
                )
        elif kind.startswith("clef_"):
            # Exact clef events are applied directly to the reviewed XML.
            continue
        elif kind == "missing_clef":
            sign = selected.get("sign")
            line = selected.get("line")
            staff = decision.get("staff", selected.get("staff"))
            if isinstance(sign, str) and isinstance(line, int) and isinstance(staff, int):
                clef_overrides.append(
                    {
                        "staff": staff,
                        "from_measure": measure_start,
                        "to_measure": measure_end,
                        "sign": sign,
                        "line": line,
                    }
                )

    plan["time_signature_changes"] = time_changes
    plan["clef_overrides"] = clef_overrides
    return ScoreStructurePlan.from_dict(plan).to_dict()


def finalize_conversion(
    workspace: JobWorkspace,
    params: ConvertParams,
    preparation: dict[str, object],
    review_decisions: list[dict[str, object]] | None = None,
    stage: StageCallback | None = None,
) -> dict[str, object]:
    """Finalize a prepared conversion after any required review decisions."""

    emit_stage = _stage_callback(stage)
    effective_structure_plan = _reviewed_structure_plan(params, review_decisions or [])
    candidate_value = preparation.get("combined_musicxml_candidate")
    if isinstance(candidate_value, str):
        combined_raw = workspace.root / candidate_value
        if not combined_raw.exists():
            raise ConversionError(f"candidate MusicXML is missing: {combined_raw}")
    else:
        combined_raw = workspace.output_dir / "score.raw.musicxml"
    reviewed_source = combined_raw
    if review_decisions:
        reviewed_tree = ET.parse(combined_raw)
        applied_clef_repairs = apply_reviewed_clef_decisions(
            reviewed_tree.getroot(),
            review_decisions,
        )
        applied_timing_repairs = apply_deterministic_timing_decisions(
            reviewed_tree.getroot(),
            effective_structure_plan,
            review_decisions,
        )
        if applied_timing_repairs or applied_clef_repairs:
            reviewed_source = workspace.output_dir / "score.reviewed.musicxml"
            reviewed_tree.write(reviewed_source, encoding="unicode", xml_declaration=True)
    combined_xml = workspace.output_dir / "score.musicxml"
    emit_stage("repairing_musicxml")
    combined_fix_report = fix_musicxml_file(
        reviewed_source,
        combined_xml,
        params.time_signature,
        params.bpm,
        structure_plan=effective_structure_plan,
        measure_offset=0,
        normalize_transient=effective_structure_plan is None,
    )
    validate_musicxml_boundaries(
        ET.parse(combined_xml).getroot(),
        effective_structure_plan,
        allowed_overflow_measures=_accepted_original_measures(preparation),
    )

    score_stats = {
        "num_parts": int(preparation.get("num_parts", 0)),
        "num_measures": int(preparation.get("num_measures", 0)),
    }
    emit_stage("exporting_midi")
    raw_midi = workspace.output_dir / "score.raw.mid"
    export_midi(combined_xml, raw_midi)
    midi_path = workspace.output_dir / "score.mid"
    midi_fix_report = fix_midi_file(
        raw_midi,
        midi_path,
        params.time_signature,
        params.bpm,
        structure_plan=effective_structure_plan,
        measure_count=score_stats["num_measures"],
    )

    if "mp3" in params.outputs:
        emit_stage("rendering_mp3")
        render_mp3(midi_path, workspace.output_dir / "score.mp3")

    if "musicxml" not in params.outputs:
        combined_xml.unlink(missing_ok=True)
    if "midi" not in params.outputs:
        midi_path.unlink(missing_ok=True)

    report_params = params.to_dict()
    report_params["structure_plan"] = effective_structure_plan
    report = {
        **preparation,
        "status": "completed",
        "params": report_params,
        "structure_plan": effective_structure_plan,
        "combined_musicxml": _relative(workspace, combined_xml),
        "combined_fix_report": combined_fix_report.to_dict(),
        "midi_raw": _relative(workspace, raw_midi),
        "midi": _relative(workspace, midi_path),
        "midi_fix_report": midi_fix_report.to_dict(),
        "review_decisions": review_decisions or [],
        **score_stats,
    }
    write_report(workspace, report)
    if "zip" in params.outputs:
        make_zip_bundle(workspace)
    emit_stage("completed")
    return report


def run_conversion(
    workspace: JobWorkspace,
    params: ConvertParams,
    stage: StageCallback | None = None,
    progress: ProgressCallback | None = None,
    debug: bool = False,
    max_pages: int | None = None,
    review_decisions: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    """Run preparation and finalize automatically only when review is unnecessary."""

    preparation = prepare_conversion(
        workspace,
        params,
        stage=stage,
        progress=progress,
        debug=debug,
        max_pages=max_pages,
    )
    if preparation["status"] == "awaiting_review" and review_decisions is None:
        return preparation
    return finalize_conversion(
        workspace,
        params,
        preparation,
        review_decisions=review_decisions,
        stage=stage,
    )
