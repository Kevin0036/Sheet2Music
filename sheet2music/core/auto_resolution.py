"""Validation and explainable selection for automatic timing candidates."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import shutil
import xml.etree.ElementTree as ET
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path

import cv2

from .analysis import analyze_musicxml_tree
from .homr import run_homr_on_page
from .layout import (
    PageLayout,
    ScoreSystem,
    annotate_page_layout_with_printed_numbers,
    build_measure_number_reader,
    crop_system_from_raw_page,
    group_overflow_findings,
    load_page_layout,
)
from .reidentify import replace_selected_musicxml_measures
from .structure import ScoreStructurePlan, coerce_structure_plan
from .system import available_gpu_providers
from .workspace import JobWorkspace


_LAYOUT_ELEMENTS = {
    "appearance",
    "credit",
    "defaults",
    "measure-layout",
    "page-layout",
    "print",
    "staff-layout",
    "system-layout",
}
_LAYOUT_ATTRIBUTES = {
    "color",
    "default-x",
    "default-y",
    "font-family",
    "font-size",
    "font-style",
    "font-weight",
    "halign",
    "height",
    "justify",
    "placement",
    "print-object",
    "relative-x",
    "relative-y",
    "valign",
    "width",
}
_NUMBER_TEXT = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)$")
_STRUCTURE_FINDING_KINDS = {
    "clef_change_at_measure_start",
    "clef_change_mid_measure",
    "clef_mismatch",
    "conflicting_time_signature",
    "missing_time_signature",
    "time_signature_change",
}


class BatchStatus(str, Enum):
    PENDING = "pending"
    LOCATING = "locating"
    RECOGNIZING = "recognizing"
    VALIDATING = "validating"
    COMMITTING = "committing"
    AUTO_RESOLVED = "auto_resolved"
    ACCEPTED_ORIGINAL = "accepted_original"
    NEEDS_CHOICE = "needs_choice"
    NEEDS_UPLOAD = "needs_upload"
    ACCEPTED_ORIGINAL = "accepted_original"
    FAILED = "failed"


@dataclass(frozen=True)
class CandidateEvidence:
    variant: str
    mapping_confidence: str
    source_notehead_counts: tuple[int, ...]
    candidate_notehead_counts: tuple[int, ...]
    context_anchors_aligned: bool
    base_structure_aligned: bool = True
    notehead_tolerance: int = 1

    @property
    def noteheads_within_tolerance(self) -> bool:
        return (
            self.notehead_tolerance >= 0
            and len(self.source_notehead_counts) == len(self.candidate_notehead_counts)
            and all(
                abs(source - candidate) <= self.notehead_tolerance
                for source, candidate in zip(
                    self.source_notehead_counts,
                    self.candidate_notehead_counts,
                    strict=True,
                )
            )
        )

    @property
    def has_strong_visual_evidence(self) -> bool:
        return (
            self.mapping_confidence == "high"
            and self.context_anchors_aligned
            and self.base_structure_aligned
            and self.noteheads_within_tolerance
        )


@dataclass(frozen=True)
class CandidateValidation:
    candidate_id: str
    accepted: bool
    reasons: tuple[str, ...]
    fingerprint: str
    target_findings_before: int
    target_findings_after: int
    has_strong_single_candidate_evidence: bool = False


@dataclass
class AutoResolutionBatch:
    batch_id: str
    page_number: int
    system_index: int
    target_measures: tuple[int, ...]
    context_range: tuple[int, int]
    status: BatchStatus = BatchStatus.PENDING
    attempts: list[dict[str, object]] = field(default_factory=list)
    selected_candidate: str | None = None
    commit: dict[str, object] | None = None


@dataclass(frozen=True)
class CandidateChoice:
    status: BatchStatus
    selected_candidate: str | None = None
    candidate_ids: tuple[str, ...] = ()
    reason: str | None = None


@dataclass(frozen=True)
class ImageVariant:
    name: str
    path: Path
    digest: str


@dataclass(frozen=True)
class BatchRunContext:
    source_crop: Path
    crop_dir: Path
    candidate_dir: Path
    work_dir: Path
    use_gpu: bool
    gpu_available: bool
    context_crop: Path | None = None
    tempo_bpm: int | None = None
    debug: bool = False


@dataclass(frozen=True)
class CandidateArtifact:
    candidate_id: str
    xml_path: Path
    candidate_global_start: int


@dataclass(frozen=True)
class TransactionContext:
    base_xml_path: Path
    validation_dir: Path
    structure_plan: ScoreStructurePlan
    page_measure_offsets: tuple[int, ...] = ()
    before_commit: Callable[[Mapping[str, object]], None] | None = None


@dataclass(frozen=True)
class TransactionResult:
    committed: bool
    reason: str
    before_high_risk_count: int
    after_high_risk_count: int
    target_findings_before: int
    target_findings_after: int
    new_high_risk_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class AutoResolutionOutcome:
    candidate_path: Path | None
    batches: tuple[AutoResolutionBatch, ...]
    resolved_count: int
    needs_choice_count: int
    needs_upload_count: int

    def to_dict(self, workspace_root: Path) -> dict[str, object]:
        return {
            "resolved_count": self.resolved_count,
            "needs_choice_count": self.needs_choice_count,
            "needs_upload_count": self.needs_upload_count,
            "accepted_original_count": sum(
                batch.status == BatchStatus.ACCEPTED_ORIGINAL for batch in self.batches
            ),
            "candidate_path": (
                _relative_path(self.candidate_path, workspace_root)
                if self.candidate_path is not None
                else None
            ),
            "batches": [
                _batch_to_report_dict(batch, workspace_root) for batch in self.batches
            ],
        }


class AutoResolutionStore:
    """Atomic JSON persistence for resumable automatic batches."""

    SCHEMA_VERSION = 1

    def __init__(self, state_path: Path) -> None:
        self.state_path = state_path

    def save(self, batches: Sequence[AutoResolutionBatch]) -> None:
        payload = {
            "schema_version": self.SCHEMA_VERSION,
            "batches": [_batch_to_dict(batch) for batch in batches],
        }
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self.state_path)

    def load(self) -> list[AutoResolutionBatch]:
        if not self.state_path.exists():
            return []
        payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        if not isinstance(payload, Mapping) or payload.get("schema_version") != self.SCHEMA_VERSION:
            raise ValueError("unsupported automatic-resolution state schema")
        raw_batches = payload.get("batches")
        if not isinstance(raw_batches, list):
            raise ValueError("automatic-resolution batches must be a list")
        batches = [_batch_from_dict(item) for item in raw_batches]
        changed = False
        for batch in batches:
            if (
                batch.status in {BatchStatus.NEEDS_UPLOAD, BatchStatus.FAILED}
                and automatic_attempts_exhausted(batch)
            ):
                batch.status = BatchStatus.ACCEPTED_ORIGINAL
                if not any(item.get("status") == "accepted_original" for item in batch.attempts):
                    batch.attempts.append(
                        {
                            "variant": None,
                            "status": "accepted_original",
                            "reason": "自动候选均未通过安全检查，保留原始识别结果",
                        }
                    )
                changed = True
        if changed:
            self.save(batches)
        return batches

    def save_batch(self, batch: AutoResolutionBatch) -> None:
        batches = self.load()
        for index, existing in enumerate(batches):
            if existing.batch_id == batch.batch_id:
                batches[index] = batch
                break
        else:
            batches.append(batch)
        self.save(batches)


def reconcile_batches(
    specs: Sequence[object],
    persisted: Sequence[AutoResolutionBatch],
) -> list[AutoResolutionBatch]:
    """Reuse state only when the persisted batch still matches its current spec."""
    existing = {batch.batch_id: batch for batch in persisted}
    reconciled: list[AutoResolutionBatch] = []
    for spec in specs:
        batch_id = str(getattr(spec, "batch_id"))
        values = (
            int(getattr(spec, "page_number")),
            int(getattr(spec, "system_index")),
            tuple(getattr(spec, "target_measures")),
            tuple(getattr(spec, "context_range")),
        )
        previous = existing.get(batch_id)
        if previous is not None and (
            previous.page_number,
            previous.system_index,
            previous.target_measures,
            previous.context_range,
        ) == values:
            reconciled.append(previous)
            continue
        reconciled.append(
            AutoResolutionBatch(
                batch_id=batch_id,
                page_number=values[0],
                system_index=values[1],
                target_measures=values[2],
                context_range=(values[3][0], values[3][1]),
            )
        )
    current_ids = {batch.batch_id for batch in reconciled}
    reconciled.extend(
        batch
        for batch in persisted
        if batch.batch_id not in current_ids
        and batch.status in {
            BatchStatus.AUTO_RESOLVED,
            BatchStatus.ACCEPTED_ORIGINAL,
            BatchStatus.COMMITTING,
        }
    )
    return reconciled


def recover_pending_commits(
    base_xml_path: Path,
    store: AutoResolutionStore,
) -> list[AutoResolutionBatch]:
    """Finish durable XML commits without overwriting an unknown official version."""
    batches = store.load()
    changed = False
    for batch in batches:
        if batch.status != BatchStatus.COMMITTING:
            continue
        journal = batch.commit or {}
        base_digest = journal.get("base_digest")
        result_digest = journal.get("result_digest")
        prepared_value = journal.get("prepared_xml")
        candidate_id = journal.get("candidate_id")
        if not all(
            isinstance(item, str) and item
            for item in (base_digest, result_digest, prepared_value, candidate_id)
        ) or not base_xml_path.exists():
            batch.status = BatchStatus.FAILED
            changed = True
            continue

        current_digest = _file_digest(base_xml_path)
        prepared = Path(str(prepared_value))
        if current_digest == base_digest and prepared.exists() and _file_digest(prepared) == result_digest:
            temporary = base_xml_path.with_suffix(base_xml_path.suffix + ".tmp")
            shutil.copy2(prepared, temporary)
            temporary.replace(base_xml_path)
            current_digest = _file_digest(base_xml_path)
        if current_digest == result_digest:
            batch.status = BatchStatus.AUTO_RESOLVED
            batch.selected_candidate = str(candidate_id)
        else:
            batch.status = BatchStatus.FAILED
        changed = True
    if changed:
        store.save(batches)
    return batches


class AutoResolutionRunner:
    """Run each deterministic image variant at most once per batch."""

    def __init__(
        self,
        store: AutoResolutionStore,
        homr_runner: Callable[..., Path] = run_homr_on_page,
    ) -> None:
        self.store = store
        self.homr_runner = homr_runner

    def resolve_batch(
        self,
        batch: AutoResolutionBatch,
        context: BatchRunContext,
    ) -> AutoResolutionBatch:
        if context.use_gpu and not context.gpu_available:
            if not any(item.get("status") == "gpu_unavailable" for item in batch.attempts):
                batch.attempts.append({"variant": None, "status": "gpu_unavailable"})
            batch.status = BatchStatus.NEEDS_UPLOAD
            self.store.save_batch(batch)
            return batch

        batch.status = BatchStatus.RECOGNIZING
        self.store.save_batch(batch)
        variants = build_image_variants(
            context.source_crop,
            context.crop_dir / batch.batch_id,
            context_crop=context.context_crop,
        )
        completed = {
            str(item.get("variant"))
            for item in batch.attempts
            if item.get("variant") is not None
            and item.get("status") in {"succeeded", "failed"}
        }
        candidate_dir = context.candidate_dir / batch.batch_id
        candidate_dir.mkdir(parents=True, exist_ok=True)
        for variant in variants:
            if variant.name in completed:
                continue
            layout_path = candidate_dir / f"{variant.name}.layout.json"
            candidate_path = candidate_dir / f"{variant.name}.musicxml"
            try:
                result = self.homr_runner(
                    variant.path,
                    work_dir=context.work_dir / batch.batch_id / variant.name,
                    debug=context.debug,
                    tempo_bpm=context.tempo_bpm,
                    use_gpu=context.use_gpu,
                    layout_output=layout_path,
                )
                shutil.copy2(result, candidate_path)
                attempt: dict[str, object] = {
                    "variant": variant.name,
                    "status": "succeeded",
                    "image_digest": variant.digest,
                    "candidate_xml": str(candidate_path),
                    "layout_json": str(layout_path),
                }
            except Exception as exc:  # noqa: BLE001 - retain the failure and try the next variant
                attempt = {
                    "variant": variant.name,
                    "status": "failed",
                    "image_digest": variant.digest,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            batch.attempts.append(attempt)
            self.store.save_batch(batch)

        batch.status = (
            BatchStatus.VALIDATING
            if any(item.get("status") == "succeeded" for item in batch.attempts)
            else BatchStatus.NEEDS_UPLOAD
        )
        self.store.save_batch(batch)
        return batch


def build_image_variants(
    source_crop: Path,
    output_dir: Path,
    context_crop: Path | None = None,
) -> tuple[ImageVariant, ...]:
    """Write exactly three deterministic system-recognition inputs."""
    image = cv2.imread(str(source_crop), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError(f"cannot read system crop: {source_crop}")
    context_source = cv2.imread(str(context_crop), cv2.IMREAD_COLOR) if context_crop else image
    if context_source is None:
        raise ValueError(f"cannot read context crop: {context_crop}")

    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    contrast_gray = cv2.createCLAHE(clipLimit=1.5, tileGridSize=(8, 8)).apply(gray)
    contrast = cv2.cvtColor(contrast_gray, cv2.COLOR_GRAY2BGR)
    border_y = max(4, round(context_source.shape[0] * 0.06))
    border_x = max(4, round(context_source.shape[1] * 0.04))
    wider_context = cv2.copyMakeBorder(
        context_source,
        border_y,
        border_y,
        border_x,
        border_x,
        cv2.BORDER_CONSTANT,
        value=(255, 255, 255),
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    results: list[ImageVariant] = []
    for name, value in (
        ("standard", image),
        ("contrast", contrast),
        ("context", wider_context),
    ):
        path = output_dir / f"{name}.png"
        if not cv2.imwrite(str(path), value, [cv2.IMWRITE_PNG_COMPRESSION, 3]):
            raise OSError(f"failed to write image variant: {path}")
        results.append(
            ImageVariant(
                name=name,
                path=path,
                digest=hashlib.sha256(path.read_bytes()).hexdigest(),
            )
        )
    return tuple(results)


def apply_candidate_transactionally(
    context: TransactionContext,
    batch: AutoResolutionBatch,
    candidate: CandidateArtifact,
) -> TransactionResult:
    """Commit a sparse replacement only when whole-score risk does not rise."""
    plan = coerce_structure_plan(context.structure_plan)
    base_digest = _file_digest(context.base_xml_path)
    base_root = ET.parse(context.base_xml_path).getroot()
    candidate_root = ET.parse(candidate.xml_path).getroot()
    baseline = analyze_musicxml_tree(
        base_root,
        plan,
        page_measure_offsets=list(context.page_measure_offsets),
    )
    merged_root = copy.deepcopy(base_root)
    replace_selected_musicxml_measures(
        merged_root,
        candidate_root,
        candidate_global_start=candidate.candidate_global_start,
        target_measure_numbers=batch.target_measures,
    )
    current = analyze_musicxml_tree(
        merged_root,
        plan,
        page_measure_offsets=list(context.page_measure_offsets),
    )

    before_high = {item.id: item for item in baseline.high_risk_findings}
    after_high = {item.id: item for item in current.high_risk_findings}
    new_high_ids = tuple(sorted(after_high.keys() - before_high.keys()))
    before_target = _target_overflow_count(baseline.high_risk_findings, batch.target_measures)
    after_target = _target_overflow_count(current.high_risk_findings, batch.target_measures)

    boundary_error: str | None = None
    try:
        from .convert import validate_musicxml_boundaries

        validate_musicxml_boundaries(merged_root, plan)
    except Exception as exc:  # Existing unresolved batches may still fail this whole-score check.
        boundary_error = f"{type(exc).__name__}: {exc}"

    if new_high_ids:
        reason = "new_high_risk_findings"
    elif after_target >= before_target:
        reason = "target_findings_not_reduced"
    elif boundary_error is not None:
        reason = "boundary_validation_failed"
    else:
        reason = "committed"

    committed = reason == "committed"
    validation = {
        "schema_version": 1,
        "batch_id": batch.batch_id,
        "candidate_id": candidate.candidate_id,
        "committed": committed,
        "reason": reason,
        "before_high_risk_count": len(before_high),
        "after_high_risk_count": len(after_high),
        "target_findings_before": before_target,
        "target_findings_after": after_target,
        "new_high_risk_ids": list(new_high_ids),
        "boundary_error": boundary_error,
    }
    context.validation_dir.mkdir(parents=True, exist_ok=True)
    validation_path = context.validation_dir / f"{batch.batch_id}.json"
    validation_tmp = validation_path.with_suffix(validation_path.suffix + ".tmp")
    validation_tmp.write_text(
        json.dumps(validation, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    validation_tmp.replace(validation_path)

    if committed:
        prepared = context.validation_dir / f"{batch.batch_id}.prepared.musicxml"
        prepared_tmp = prepared.with_suffix(prepared.suffix + ".tmp")
        ET.ElementTree(merged_root).write(
            prepared_tmp,
            encoding="utf-8",
            xml_declaration=True,
        )
        prepared_tmp.replace(prepared)
        result_digest = _file_digest(prepared)
        if context.before_commit is not None:
            context.before_commit(
                {
                    "base_digest": base_digest,
                    "result_digest": result_digest,
                    "prepared_xml": str(prepared),
                    "candidate_id": candidate.candidate_id,
                }
            )
        xml_tmp = context.base_xml_path.with_suffix(context.base_xml_path.suffix + ".tmp")
        shutil.copy2(prepared, xml_tmp)
        xml_tmp.replace(context.base_xml_path)

    return TransactionResult(
        committed=committed,
        reason=reason,
        before_high_risk_count=len(before_high),
        after_high_risk_count=len(after_high),
        target_findings_before=before_target,
        target_findings_after=after_target,
        new_high_risk_ids=new_high_ids,
    )


def _file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def resolve_timing_overflows(
    *,
    workspace: JobWorkspace,
    base_xml: Path,
    analysis: Mapping[str, object],
    page_layouts: Sequence[Path],
    page_measure_offsets: Sequence[int],
    structure_plan: ScoreStructurePlan,
    tempo_bpm: int | None,
    use_gpu: bool,
    progress: Callable[..., None] | None = None,
    debug: bool = False,
    enable_measure_number_ocr: bool = True,
    has_pickup_measure: bool = False,
) -> AutoResolutionOutcome:
    """Resolve grouped timing overflows using retained page images and sidecars."""
    findings = analysis.get("findings", [])
    if not isinstance(findings, list):
        findings = []
    layouts = _load_score_layouts(workspace, page_layouts, page_measure_offsets)
    if enable_measure_number_ocr:
        try:
            reader = build_measure_number_reader()
            layouts = tuple(
                annotate_page_layout_with_printed_numbers(
                    page,
                    workspace.pages_dir / "raw" / f"page-{page.page_number}.png",
                    reader,
                    has_pickup_measure=has_pickup_measure,
                )
                for page in layouts
            )
        except Exception:  # noqa: BLE001 - OCR is advisory; ordinal safety remains intact
            pass
    specs = group_overflow_findings(
        [item for item in findings if isinstance(item, Mapping)],
        layouts,
    )
    store = AutoResolutionStore(workspace.auto_resolution_dir / "batches.json")
    batches = reconcile_batches(specs, store.load())
    if not batches:
        return AutoResolutionOutcome(None, (), 0, 0, 0)
    store.save(batches)

    official_candidate = workspace.output_dir / "score.auto.musicxml"
    if not official_candidate.exists():
        shutil.copy2(base_xml, official_candidate)
    batches = recover_pending_commits(official_candidate, store)
    runner = AutoResolutionRunner(store)
    page_systems = {
        (system.page_number, system.system_index): (page, system)
        for page in layouts
        for system in page.systems
    }
    spec_confidence = {
        (spec.page_number, spec.system_index): spec.mapping_confidence for spec in specs
    }

    for index, batch in enumerate(batches, start=1):
        if batch.status in {
            BatchStatus.AUTO_RESOLVED,
            BatchStatus.NEEDS_CHOICE,
            BatchStatus.NEEDS_UPLOAD,
            BatchStatus.FAILED,
            BatchStatus.ACCEPTED_ORIGINAL,
        }:
            _emit_batch_progress(progress, index, len(batches), batch, batches)
            continue
        page_system = page_systems.get((batch.page_number, batch.system_index))
        if page_system is None:
            batch.status = BatchStatus.NEEDS_UPLOAD
            batch.attempts.append(
                {"variant": None, "status": "failed", "error": "layout_system_missing"}
            )
            _accept_original_after_automatic_failure(
                batch, "自动定位缺少谱表系统，保留原始识别结果"
            )
            store.save_batch(batch)
            _emit_batch_progress(progress, index, len(batches), batch, batches)
            continue

        page, system = page_system
        mapping_is_ambiguous = spec_confidence.get(
            (batch.page_number, batch.system_index)
        ) != "high"
        batch.status = BatchStatus.LOCATING
        store.save_batch(batch)
        raw_page = workspace.pages_dir / "raw" / f"page-{batch.page_number}.png"
        source_crop = workspace.auto_resolution_crop_dir / batch.batch_id / "source.png"
        context_crop = workspace.auto_resolution_crop_dir / batch.batch_id / "source-context.png"
        try:
            crop_system_from_raw_page(page, system, raw_page, source_crop, padding_spaces=4)
            crop_system_from_raw_page(page, system, raw_page, context_crop, padding_spaces=8)
        except Exception as exc:  # noqa: BLE001 - preserve the batch for final upload
            batch.status = BatchStatus.NEEDS_UPLOAD
            batch.attempts.append(
                {
                    "variant": None,
                    "status": "failed",
                    "error": f"crop_failed: {type(exc).__name__}: {exc}",
                }
            )
            _accept_original_after_automatic_failure(
                batch, "自动裁切失败，保留原始识别结果"
            )
            store.save_batch(batch)
            _emit_batch_progress(progress, index, len(batches), batch, batches)
            continue

        if mapping_is_ambiguous:
            batch.status = BatchStatus.NEEDS_UPLOAD
            batch.attempts.append(
                {"variant": None, "status": "failed", "error": "layout_mapping_ambiguous"}
            )
            _accept_original_after_automatic_failure(
                batch, "自动定位结果存在歧义，保留原始识别结果"
            )
            store.save_batch(batch)
            _emit_batch_progress(progress, index, len(batches), batch, batches)
            continue

        runner.resolve_batch(
            batch,
            BatchRunContext(
                source_crop=source_crop,
                context_crop=context_crop,
                crop_dir=workspace.auto_resolution_crop_dir,
                candidate_dir=workspace.auto_resolution_candidate_dir,
                work_dir=workspace.auto_resolution_dir / "homr_work",
                use_gpu=use_gpu,
                gpu_available=bool(available_gpu_providers()),
                tempo_bpm=tempo_bpm,
                debug=debug,
            ),
        )
        if batch.status == BatchStatus.NEEDS_UPLOAD:
            if automatic_attempts_exhausted(batch):
                batch.status = BatchStatus.ACCEPTED_ORIGINAL
                batch.attempts.append(
                    {
                        "variant": None,
                        "status": "accepted_original",
                        "reason": "自动候选均未能生成可用结果，保留原始识别结果",
                    }
                )
                store.save_batch(batch)
            _emit_batch_progress(progress, index, len(batches), batch, batches)
            continue

        validations: list[CandidateValidation] = []
        base_root = ET.parse(official_candidate).getroot()
        for attempt in batch.attempts:
            if attempt.get("status") != "succeeded":
                continue
            candidate_path = Path(str(attempt.get("candidate_xml", "")))
            layout_path = Path(str(attempt.get("layout_json", "")))
            try:
                mapping_confidence, candidate_counts = _candidate_visual_layout(
                    layout_path,
                    expected_measure_count=batch.context_range[1] - batch.context_range[0] + 1,
                )
                candidate_root = ET.parse(candidate_path).getroot()
                validation = validate_candidate(
                    candidate_root,
                    batch,
                    CandidateEvidence(
                        variant=str(attempt["variant"]),
                        mapping_confidence=mapping_confidence,
                        source_notehead_counts=system.notehead_counts,
                        candidate_notehead_counts=candidate_counts,
                        context_anchors_aligned=_context_anchors_match(
                            base_root,
                            candidate_root,
                            batch,
                        ),
                        base_structure_aligned=_structure_matches_base(
                            base_root,
                            candidate_root,
                            batch,
                        ),
                    ),
                    structure_plan,
                )
            except Exception as exc:  # noqa: BLE001 - malformed candidate is a rejected attempt
                validation = CandidateValidation(
                    candidate_id=str(attempt.get("variant", "unknown")),
                    accepted=False,
                    reasons=(f"candidate_invalid:{type(exc).__name__}",),
                    fingerprint="",
                    target_findings_before=len(batch.target_measures),
                    target_findings_after=len(batch.target_measures),
                )
            attempt["validation"] = _validation_to_dict(validation)
            validations.append(validation)
            store.save_batch(batch)

        choice = choose_candidate(validations)
        if choice.status == BatchStatus.AUTO_RESOLVED and choice.selected_candidate is not None:
            selected_attempt = next(
                item
                for item in batch.attempts
                if item.get("variant") == choice.selected_candidate
                and item.get("status") == "succeeded"
            )

            def persist_commit(journal: Mapping[str, object]) -> None:
                batch.status = BatchStatus.COMMITTING
                batch.selected_candidate = choice.selected_candidate
                batch.commit = dict(journal)
                store.save_batch(batch)

            transaction = apply_candidate_transactionally(
                TransactionContext(
                    base_xml_path=official_candidate,
                    validation_dir=workspace.auto_resolution_validation_dir,
                    structure_plan=structure_plan,
                    page_measure_offsets=tuple(page_measure_offsets),
                    before_commit=persist_commit,
                ),
                batch,
                CandidateArtifact(
                    candidate_id=choice.selected_candidate,
                    xml_path=Path(str(selected_attempt["candidate_xml"])),
                    candidate_global_start=batch.context_range[0],
                ),
            )
            selected_attempt["transaction"] = _transaction_to_dict(transaction)
            if transaction.committed:
                batch.status = BatchStatus.AUTO_RESOLVED
                batch.selected_candidate = choice.selected_candidate
            else:
                batch.status = (
                    BatchStatus.ACCEPTED_ORIGINAL
                    if automatic_attempts_exhausted(batch)
                    else BatchStatus.NEEDS_UPLOAD
                )
                if batch.status == BatchStatus.ACCEPTED_ORIGINAL:
                    batch.attempts.append(
                        {
                            "variant": None,
                            "status": "accepted_original",
                            "reason": "候选未通过整谱边界验证，保留原始识别结果",
                        }
                    )
        elif choice.status == BatchStatus.NEEDS_UPLOAD and automatic_attempts_exhausted(batch):
            batch.status = BatchStatus.ACCEPTED_ORIGINAL
            batch.attempts.append(
                {
                    "variant": None,
                    "status": "accepted_original",
                    "reason": "自动候选均未通过安全检查，保留原始识别结果",
                }
            )
        else:
            batch.status = choice.status
        store.save_batch(batch)
        _emit_batch_progress(progress, index, len(batches), batch, batches)

    resolved_count = sum(batch.status == BatchStatus.AUTO_RESOLVED for batch in batches)
    needs_choice_count = sum(batch.status == BatchStatus.NEEDS_CHOICE for batch in batches)
    needs_upload_count = sum(
        batch.status in {BatchStatus.NEEDS_UPLOAD, BatchStatus.FAILED} for batch in batches
    )
    return AutoResolutionOutcome(
        candidate_path=official_candidate if resolved_count else None,
        batches=tuple(batches),
        resolved_count=resolved_count,
        needs_choice_count=needs_choice_count,
        needs_upload_count=needs_upload_count,
    )


def validate_batch_candidate_artifact(
    *,
    workspace: JobWorkspace,
    base_xml: Path,
    candidate_xml: Path,
    candidate_layout: Path,
    batch: AutoResolutionBatch,
    page_layouts: Sequence[Path],
    page_measure_offsets: Sequence[int],
    structure_plan: ScoreStructurePlan,
    candidate_id: str,
) -> CandidateValidation:
    """Validate a stored candidate using only job-owned layout evidence."""
    layouts = _load_score_layouts(workspace, page_layouts, page_measure_offsets)
    source_system = next(
        (
            system
            for page in layouts
            for system in page.systems
            if system.page_number == batch.page_number
            and system.system_index == batch.system_index
        ),
        None,
    )
    if source_system is None:
        raise ValueError("batch system is missing from persisted page layout")
    expected_count = batch.context_range[1] - batch.context_range[0] + 1
    mapping_confidence, candidate_counts = _candidate_visual_layout(
        candidate_layout,
        expected_measure_count=expected_count,
    )
    base_root = ET.parse(base_xml).getroot()
    candidate_root = ET.parse(candidate_xml).getroot()
    return validate_candidate(
        candidate_root,
        batch,
        CandidateEvidence(
            variant=candidate_id,
            mapping_confidence=mapping_confidence,
            source_notehead_counts=source_system.notehead_counts,
            candidate_notehead_counts=candidate_counts,
            context_anchors_aligned=_context_anchors_match(
                base_root,
                candidate_root,
                batch,
            ),
            base_structure_aligned=_structure_matches_base(
                base_root,
                candidate_root,
                batch,
            ),
        ),
        structure_plan,
    )


def candidate_fingerprint(
    root: ET.Element,
    measure_numbers: Sequence[int] | None = None,
) -> str:
    """Hash musical content while ignoring engraving-only differences."""
    selected = set(measure_numbers) if measure_numbers is not None else None
    parts: list[object] = []
    for part in sorted(root.findall("part"), key=lambda item: item.get("id", "")):
        measures = []
        for ordinal, measure in enumerate(part.findall("measure"), start=1):
            if selected is not None and ordinal not in selected:
                continue
            measures.append(_canonical_element(measure))
        parts.append((part.get("id", ""), measures))
    payload = json.dumps(parts, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def validate_candidate(
    candidate_root: ET.Element,
    batch: AutoResolutionBatch,
    evidence: CandidateEvidence,
    structure_plan: ScoreStructurePlan,
) -> CandidateValidation:
    """Apply independent structural, timing, and visual hard gates."""
    plan = coerce_structure_plan(structure_plan)
    context_start, context_end = batch.context_range
    expected_measure_count = context_end - context_start + 1
    local_targets = tuple(
        number - context_start + 1 for number in batch.target_measures
    )
    reasons: list[str] = []

    parts = candidate_root.findall("part")
    measure_lists = [part.findall("measure") for part in parts]
    if not parts or any(len(measures) != expected_measure_count for measures in measure_lists):
        reasons.append("measure_count_mismatch")
    if not _measures_are_locally_aligned(measure_lists, expected_measure_count):
        reasons.append("measure_alignment_invalid")

    report = analyze_musicxml_tree(
        candidate_root,
        _localize_structure_plan(plan, context_start, expected_measure_count),
    )
    target_timing_findings = [
        finding
        for finding in report.findings
        if finding.kind in {"timing_measure_overflow", "timing_cursor_invalid"}
        and finding.measure_start in local_targets
    ]
    if any(
        finding.kind in {"timing_measure_overflow", "timing_cursor_invalid"}
        for finding in report.findings
    ):
        reasons.append("measure_overflow")
    if any(finding.kind == "timing_cursor_invalid" for finding in report.findings):
        reasons.append("timing_cursor_invalid")
    if any(finding.kind in _STRUCTURE_FINDING_KINDS for finding in report.findings) or _key_changed(
        candidate_root, plan
    ):
        reasons.append("structure_changed")
    if evidence.mapping_confidence != "high":
        reasons.append("mapping_not_high_confidence")
    if not evidence.context_anchors_aligned:
        reasons.append("context_anchor_mismatch")
    if not evidence.base_structure_aligned:
        reasons.append("structure_changed")
    if not evidence.noteheads_within_tolerance:
        reasons.append("visual_notehead_mismatch")

    fingerprint_targets = tuple(
        number for number in local_targets if 1 <= number <= expected_measure_count
    )
    return CandidateValidation(
        candidate_id=evidence.variant,
        accepted=not reasons,
        reasons=tuple(dict.fromkeys(reasons)),
        fingerprint=candidate_fingerprint(candidate_root, fingerprint_targets),
        target_findings_before=len(batch.target_measures),
        target_findings_after=len(target_timing_findings),
        has_strong_single_candidate_evidence=(
            not reasons and evidence.has_strong_visual_evidence
        ),
    )


def choose_candidate(validations: Sequence[CandidateValidation]) -> CandidateChoice:
    """Choose a candidate only through explicit consensus or strong evidence."""
    accepted = [item for item in validations if item.accepted]
    groups: dict[str, list[CandidateValidation]] = {}
    for item in accepted:
        groups.setdefault(item.fingerprint, []).append(item)

    consensus = [items for items in groups.values() if len(items) >= 2]
    if len(consensus) == 1:
        selected = consensus[0][0]
        return CandidateChoice(
            status=BatchStatus.AUTO_RESOLVED,
            selected_candidate=selected.candidate_id,
            candidate_ids=tuple(item.candidate_id for item in consensus[0]),
            reason="two_variants_agree",
        )
    if len(groups) > 1:
        representatives = tuple(items[0].candidate_id for items in groups.values())
        return CandidateChoice(
            status=BatchStatus.NEEDS_CHOICE,
            candidate_ids=representatives,
            reason="valid_candidates_conflict",
        )
    if len(accepted) == 1 and accepted[0].has_strong_single_candidate_evidence:
        return CandidateChoice(
            status=BatchStatus.AUTO_RESOLVED,
            selected_candidate=accepted[0].candidate_id,
            candidate_ids=(accepted[0].candidate_id,),
            reason="only_valid_candidate_with_visual_evidence",
        )
    return CandidateChoice(
        status=BatchStatus.NEEDS_UPLOAD,
        candidate_ids=tuple(item.candidate_id for item in accepted),
        reason="no_reliable_automatic_candidate",
    )


def automatic_attempts_exhausted(batch: AutoResolutionBatch) -> bool:
    """Return true when no further useful automatic image attempt remains."""
    if any(
        item.get("status") == "failed"
        and item.get("error") in {"layout_mapping_ambiguous", "crop_failed"}
        for item in batch.attempts
    ):
        return True
    completed = {
        str(item.get("variant"))
        for item in batch.attempts
        if item.get("variant") in {"standard", "contrast", "context"}
        and item.get("status") in {"succeeded", "failed"}
    }
    return completed == {"standard", "contrast", "context"}


def _accept_original_after_automatic_failure(batch: AutoResolutionBatch, reason: str) -> None:
    batch.status = BatchStatus.ACCEPTED_ORIGINAL
    batch.attempts.append(
        {"variant": None, "status": "accepted_original", "reason": reason}
    )


def _canonical_element(element: ET.Element) -> object:
    children = [
        _canonical_element(child)
        for child in element
        if _local_name(child.tag) not in _LAYOUT_ELEMENTS
    ]
    attributes = tuple(
        sorted(
            (name, _normalize_text(value))
            for name, value in element.attrib.items()
            if _local_name(name) not in _LAYOUT_ATTRIBUTES
        )
    )
    return (
        _local_name(element.tag),
        attributes,
        _normalize_text(element.text or ""),
        children,
    )


def _normalize_text(value: str) -> str:
    stripped = value.strip()
    if not stripped or not _NUMBER_TEXT.fullmatch(stripped):
        return stripped
    try:
        normalized = Decimal(stripped).normalize()
    except InvalidOperation:
        return stripped
    return format(normalized, "f")


def _local_name(name: str) -> str:
    return name.rsplit("}", 1)[-1]


def _measures_are_locally_aligned(
    measure_lists: list[list[ET.Element]],
    expected_measure_count: int,
) -> bool:
    if not measure_lists:
        return False
    expected = [str(number) for number in range(1, expected_measure_count + 1)]
    return all([measure.get("number") for measure in measures] == expected for measures in measure_lists)


def _localize_structure_plan(
    plan: ScoreStructurePlan,
    global_start: int,
    measure_count: int,
) -> ScoreStructurePlan:
    signatures = [
        plan.time_signature_for(global_start + offset)
        for offset in range(measure_count)
    ]
    changes: list[dict[str, object]] = []
    run_start = 0
    for index in range(1, len(signatures) + 1):
        if index < len(signatures) and signatures[index] == signatures[run_start]:
            continue
        if run_start > 0:
            beats, beat_type = signatures[run_start]
            change: dict[str, object] = {
                "from_measure": run_start + 1,
                "signature": f"{beats}/{beat_type}",
            }
            if index < len(signatures):
                change["to_measure"] = index
            changes.append(change)
        run_start = index

    clefs: list[dict[str, object]] = []
    global_end = global_start + measure_count - 1
    for override in plan.clef_overrides:
        override_end = override.to_measure if override.to_measure is not None else global_end
        overlap_start = max(global_start, override.from_measure)
        overlap_end = min(global_end, override_end)
        if overlap_start > overlap_end:
            continue
        item: dict[str, object] = {
            "staff": override.staff,
            "from_measure": overlap_start - global_start + 1,
            "sign": override.sign,
            "line": override.line,
        }
        if overlap_end < global_end:
            item["to_measure"] = overlap_end - global_start + 1
        clefs.append(item)

    beats, beat_type = signatures[0] if signatures else plan.time_signature_for(global_start)
    value: dict[str, object] = {
        "default_time_signature": f"{beats}/{beat_type}",
        "time_signature_changes": changes,
        "clef_overrides": clefs,
    }
    if plan.key_signature_fifths is not None:
        value["key_signature"] = {"fifths": plan.key_signature_fifths}
    return ScoreStructurePlan.from_dict(value)


def _key_changed(root: ET.Element, plan: ScoreStructurePlan) -> bool:
    for part in root.findall("part"):
        previous: int | None = None
        for measure in part.findall("measure"):
            for key in (
                key
                for attributes in measure.findall("attributes")
                for key in attributes.findall("key")
            ):
                try:
                    current = int(key.findtext("fifths", ""))
                except ValueError:
                    return True
                if plan.key_signature_fifths is not None and current != plan.key_signature_fifths:
                    return True
                if previous is not None and current != previous:
                    return True
                previous = current
    return False


def _batch_to_dict(batch: AutoResolutionBatch) -> dict[str, object]:
    return {
        "batch_id": batch.batch_id,
        "page_number": batch.page_number,
        "system_index": batch.system_index,
        "target_measures": list(batch.target_measures),
        "context_range": list(batch.context_range),
        "status": batch.status.value,
        "attempts": batch.attempts,
        "selected_candidate": batch.selected_candidate,
        "commit": batch.commit,
    }


def _batch_from_dict(value: object) -> AutoResolutionBatch:
    if not isinstance(value, Mapping):
        raise ValueError("automatic-resolution batch must be an object")
    target_measures = _positive_int_tuple(value.get("target_measures"), "target_measures")
    context_range = _positive_int_tuple(value.get("context_range"), "context_range")
    if len(context_range) != 2 or context_range[0] > context_range[1]:
        raise ValueError("context_range must be an ordered pair")
    attempts = value.get("attempts", [])
    if not isinstance(attempts, list) or any(not isinstance(item, Mapping) for item in attempts):
        raise ValueError("batch attempts must be a list of objects")
    batch_id = value.get("batch_id")
    if not isinstance(batch_id, str) or not batch_id:
        raise ValueError("batch_id must be a non-empty string")
    selected = value.get("selected_candidate")
    if selected is not None and not isinstance(selected, str):
        raise ValueError("selected_candidate must be a string or null")
    commit = value.get("commit")
    if commit is not None and not isinstance(commit, Mapping):
        raise ValueError("commit must be an object or null")
    return AutoResolutionBatch(
        batch_id=batch_id,
        page_number=_positive_int_value(value.get("page_number"), "page_number"),
        system_index=_nonnegative_int_value(value.get("system_index"), "system_index"),
        target_measures=target_measures,
        context_range=(context_range[0], context_range[1]),
        status=BatchStatus(str(value.get("status", BatchStatus.PENDING.value))),
        attempts=[dict(item) for item in attempts],
        selected_candidate=selected,
        commit=dict(commit) if commit is not None else None,
    )


def _positive_int_tuple(value: object, label: str) -> tuple[int, ...]:
    if not isinstance(value, (list, tuple)) or not value:
        raise ValueError(f"{label} must be a non-empty list")
    return tuple(_positive_int_value(item, label) for item in value)


def _positive_int_value(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must contain positive integers")
    return value


def _nonnegative_int_value(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def _target_overflow_count(
    findings: Sequence[object],
    target_measures: Sequence[int],
) -> int:
    targets = set(target_measures)
    return sum(
        1
        for finding in findings
        if getattr(finding, "kind", None) in {
            "timing_measure_overflow",
            "timing_cursor_invalid",
        }
        and getattr(finding, "measure_start", None) in targets
    )


def _load_score_layouts(
    workspace: JobWorkspace,
    layout_paths: Sequence[Path],
    page_measure_offsets: Sequence[int],
) -> tuple[PageLayout, ...]:
    if len(layout_paths) != len(page_measure_offsets):
        raise ValueError("page layouts and measure offsets are not aligned")
    pages: list[PageLayout] = []
    for layout_path, measure_offset in zip(layout_paths, page_measure_offsets, strict=True):
        match = re.search(r"(\d+)$", layout_path.stem)
        if match is None:
            raise ValueError(f"cannot determine page number from layout: {layout_path}")
        page_number = int(match.group(1))
        geometry_path = workspace.page_geometry_dir / f"page-{page_number}.json"
        pages.append(
            load_page_layout(
                layout_path,
                geometry_path,
                page_number=page_number,
                measure_offset=measure_offset,
            )
        )
    return tuple(pages)


def _candidate_visual_layout(
    layout_path: Path,
    expected_measure_count: int,
) -> tuple[str, tuple[int, ...]]:
    payload = json.loads(layout_path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("candidate layout must be an object")
    systems = payload.get("systems")
    if not isinstance(systems, list) or len(systems) != 1 or not isinstance(systems[0], Mapping):
        raise ValueError("candidate layout must contain exactly one system")
    system = systems[0]
    counts = system.get("measure_notehead_counts")
    if (
        not isinstance(counts, list)
        or len(counts) != expected_measure_count
        or any(isinstance(item, bool) or not isinstance(item, int) or item < 0 for item in counts)
    ):
        raise ValueError("candidate notehead counts do not align with the system")
    return str(system.get("mapping_confidence", "ambiguous")), tuple(counts)


def _context_anchors_match(
    base_root: ET.Element,
    candidate_root: ET.Element,
    batch: AutoResolutionBatch,
) -> bool:
    context_start, context_end = batch.context_range
    global_anchors = tuple(
        number
        for number in range(context_start, context_end + 1)
        if number not in set(batch.target_measures)
    )
    if not global_anchors:
        return False
    local_anchors = tuple(number - context_start + 1 for number in global_anchors)
    return candidate_fingerprint(base_root, global_anchors) == candidate_fingerprint(
        candidate_root,
        local_anchors,
    )


def _structure_matches_base(
    base_root: ET.Element,
    candidate_root: ET.Element,
    batch: AutoResolutionBatch,
) -> bool:
    base_parts = {part.get("id", ""): part for part in base_root.findall("part")}
    candidate_parts = {
        part.get("id", ""): part for part in candidate_root.findall("part")
    }
    if base_parts.keys() != candidate_parts.keys():
        return False

    context_start, context_end = batch.context_range
    context_count = context_end - context_start + 1
    for part_id, base_part in base_parts.items():
        base_measures = base_part.findall("measure")
        candidate_measures = candidate_parts[part_id].findall("measure")
        if context_end > len(base_measures) or len(candidate_measures) != context_count:
            return False

        initial_state = _structure_state_before(base_measures, context_start - 1)
        base_trace = _structure_trace(
            base_measures[context_start - 1 : context_end],
            initial_state,
        )
        candidate_trace = _structure_trace(candidate_measures, initial_state)
        if base_trace != candidate_trace:
            return False
    return True


def _structure_state_before(
    measures: Sequence[ET.Element],
    count: int,
) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    state: tuple[dict[str, object], dict[str, object], dict[str, object]] = ({}, {}, {})
    for measure in measures[:count]:
        _, state = _measure_structure_trace(measure, state)
    return state


def _structure_trace(
    measures: Sequence[ET.Element],
    initial_state: tuple[dict[str, object], dict[str, object], dict[str, object]],
) -> tuple[object, ...]:
    state = (dict(initial_state[0]), dict(initial_state[1]), dict(initial_state[2]))
    result: list[object] = []
    for measure in measures:
        events, state = _measure_structure_trace(measure, state)
        result.append(
            (
                events,
                tuple(sorted(state[0].items())),
                tuple(sorted(state[1].items())),
                tuple(sorted(state[2].items())),
            )
        )
    return tuple(result)


def _measure_structure_trace(
    measure: ET.Element,
    initial_state: tuple[dict[str, object], dict[str, object], dict[str, object]],
) -> tuple[
    tuple[object, ...],
    tuple[dict[str, object], dict[str, object], dict[str, object]],
]:
    keys = dict(initial_state[0])
    clefs = dict(initial_state[1])
    times = dict(initial_state[2])
    events: list[object] = []
    cursor = 0
    for child in measure:
        if _local_name(child.tag) == "attributes":
            for key in child.findall("key"):
                number = key.get("number", "all")
                value = _canonical_element(key)
                if keys.get(number) != value:
                    keys[number] = value
                    events.append((cursor, "key", number, value))
            for clef in child.findall("clef"):
                staff = clef.get("number", "1")
                value = _canonical_element(clef)
                if clefs.get(staff) != value:
                    clefs[staff] = value
                    events.append((cursor, "clef", staff, value))
            for time in child.findall("time"):
                staff = time.get("number", "all")
                value = _canonical_element(time)
                if times.get(staff) != value:
                    times[staff] = value
                    events.append((cursor, "time", staff, value))
        elif _local_name(child.tag) == "note":
            if child.find("chord") is None:
                cursor += _xml_duration(child)
        elif _local_name(child.tag) == "backup":
            cursor -= _xml_duration(child)
        elif _local_name(child.tag) == "forward":
            cursor += _xml_duration(child)
    return tuple(events), (keys, clefs, times)


def _xml_duration(element: ET.Element) -> int:
    try:
        return int(element.findtext("duration", "0"))
    except ValueError:
        return 0


def _validation_to_dict(validation: CandidateValidation) -> dict[str, object]:
    return {
        "candidate_id": validation.candidate_id,
        "accepted": validation.accepted,
        "reasons": list(validation.reasons),
        "fingerprint": validation.fingerprint,
        "target_findings_before": validation.target_findings_before,
        "target_findings_after": validation.target_findings_after,
        "has_strong_single_candidate_evidence": (
            validation.has_strong_single_candidate_evidence
        ),
    }


def _transaction_to_dict(result: TransactionResult) -> dict[str, object]:
    return {
        "committed": result.committed,
        "reason": result.reason,
        "before_high_risk_count": result.before_high_risk_count,
        "after_high_risk_count": result.after_high_risk_count,
        "target_findings_before": result.target_findings_before,
        "target_findings_after": result.target_findings_after,
        "new_high_risk_ids": list(result.new_high_risk_ids),
    }


def _emit_batch_progress(
    progress: Callable[..., None] | None,
    current: int,
    total: int,
    batch: AutoResolutionBatch,
    batches: Sequence[AutoResolutionBatch],
) -> None:
    if progress is None:
        return
    resolved = sum(item.status == BatchStatus.AUTO_RESOLVED for item in batches)
    needs_review = sum(
        item.status in {BatchStatus.NEEDS_CHOICE, BatchStatus.NEEDS_UPLOAD, BatchStatus.FAILED}
        for item in batches
    )
    progress(
        current,
        total,
        f"page-{batch.page_number}",
        system=batch.system_index,
        resolved=resolved,
        needs_review=needs_review,
    )


def _batch_to_report_dict(
    batch: AutoResolutionBatch,
    workspace_root: Path,
) -> dict[str, object]:
    result = _batch_to_dict(batch)
    attempts: list[dict[str, object]] = []
    for raw_attempt in batch.attempts:
        attempt = dict(raw_attempt)
        for key in ("candidate_xml", "layout_json"):
            value = attempt.get(key)
            if isinstance(value, str):
                attempt[key] = _relative_path(Path(value), workspace_root)
        attempts.append(attempt)
    result["attempts"] = attempts
    reasons: list[str] = []
    for attempt in batch.attempts:
        validation = attempt.get("validation")
        if not isinstance(validation, Mapping):
            continue
        values = validation.get("reasons", [])
        if not isinstance(values, list):
            continue
        for reason in values:
            if isinstance(reason, str) and reason not in reasons:
                reasons.append(reason)
    result["failure_reasons"] = reasons
    return result


def _relative_path(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)
