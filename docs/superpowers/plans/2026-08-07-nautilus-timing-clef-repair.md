# Nautilus Timing and Clef Repair Implementation Plan

> **For agentic workers:** Work through the checkboxes in order. Use TDD for every behavior change and verify each checkpoint before starting the next task.

**Goal:** Turn the current fixed `4/4` repair pass into a structure-aware, reviewable workflow that can preserve real score changes, correct HOMR mistakes, and re-recognize a user-selected measure range when the repair suggestion is unreliable.

**Architecture:** Keep raw page MusicXML immutable. Add a validated `ScoreStructurePlan` for effective time signatures, clef ranges, and key signature. Split conversion into preparation/analysis and finalization so high-risk findings stop before MIDI/MP3 export. Add a region replacement service that merges a second HOMR result into the selected full-score measure range, then reruns analysis and requires approval again.

**Tech Stack:** Python 3.10+, `xml.etree.ElementTree`, `dataclasses`, `miditoolkit`, FastAPI, vanilla browser JavaScript, existing HOMR/MuseScore pipeline, `unittest`.

---

## Execution Rules

- Work in `C:\Users\Kevin\Downloads\Piano_Arranger\Sheet2Music` and preserve existing user changes, especially `sheets/` and the environment-related files.
- Do not change final note pitches or move notes across a measure boundary unless a reviewed region replacement explicitly supplies new MusicXML.
- A raw `<time>` or `<clef>` change is evidence to show to the user, not proof that HOMR is wrong.
- XML timing anomalies such as negative cursors or lane overflow are recognition diagnostics, not real musical changes.
- Every behavior change follows: write one failing test, run it, implement the smallest fix, run the focused tests, then run the full suite at the checkpoint.
- Do not export final MIDI/MP3 while a high-risk finding has an unresolved decision.

## File Map

- Create `sheet2music/core/structure.py`: validated score structure plan and measure-boundary calculations.
- Create `sheet2music/core/analysis.py`: structural finding model and preflight analysis.
- Create `sheet2music/core/reidentify.py`: region image validation, HOMR invocation, and XML range replacement.
- Modify `sheet2music/core/models.py`: optional structure plan and `AWAITING_REVIEW` job state.
- Modify `sheet2music/core/repair.py`: lane-aware MusicXML timing repair, local clef application, and structure-aware MIDI events.
- Modify `sheet2music/core/convert.py`: preparation/finalization split, page-to-full-score measure mapping, and reviewed plan propagation.
- Modify `sheet2music/core/workspace.py`: analysis, review upload, and region artifact paths.
- Modify `sheet2music/web/jobs.py`: review-aware job lifecycle and serialized finalization.
- Modify `sheet2music/web/app.py`: analysis, approval, and region re-identification endpoints.
- Modify `sheet2music/web/static/index.html`, `app.js`, and `style.css`: review panel, finding actions, range upload, and re-review state.
- Add or modify `tests/test_structure.py`, `tests/test_analysis.py`, `tests/test_reidentify.py`, `tests/test_repair.py`, `tests/test_convert_unit.py`, and `tests/test_api.py`.
- Update `docs/nautilus-timing-clef-repair-plan.md` and `README.md` after implementation behavior is stable.

## Task 1: Establish the Red Baseline

**Status:** completed

**Files:**
- Test: `tests/test_repair.py`
- Test: `tests/test_convert_unit.py`

- [x] Run the already-added intelligent repair tests before adding production code.

```powershell
python -m unittest tests.test_repair -v
```

Expected result: failure because `ScoreStructurePlan` and the new `structure_plan` arguments do not exist yet; existing legacy tests must still load so failures identify the missing behavior rather than an import typo.

- [x] Record the baseline full-suite result without changing unrelated files.

```powershell
python -m unittest discover -s tests -t .
```

Expected result: the pre-existing suite remains green apart from the new red tests, or any unrelated failure is documented before implementation continues.

## Task 2: Add the Structure Plan Contract

**Status:** completed

**Files:**
- Create: `sheet2music/core/structure.py`
- Modify: `sheet2music/core/models.py`
- Test: `tests/test_structure.py`
- Test: `tests/test_models.py`

- [x] Write tests for `ScoreStructurePlan.from_dict()` covering default `4/4`, measure 25 `2/4` and measure 26 onward `4/4`, staff 2 G-clef measures 14-16 and F-clef from 17, `key_signature.fifths = -5`, and malformed ranges/signatures/staff values.

- [x] Implement these exact public operations:

```python
class ScoreStructurePlan:
    @classmethod
    def from_dict(cls, value: Mapping[str, object], fallback_time_signature: str = "4/4") -> "ScoreStructurePlan": ...
    def time_signature_for(self, measure_number: int) -> tuple[int, int]: ...
    def clef_for(self, staff: int, measure_number: int) -> tuple[str, int] | None: ...
    def to_dict(self) -> dict[str, object]: ...
```

- [x] Add `structure_plan: dict[str, object] | None = None` to `ConvertParams`, validate it through `ScoreStructurePlan.from_dict`, and keep existing callers that only pass `bpm`, `time_signature`, `outputs`, and `use_gpu` unchanged.

- [x] Run the focused tests.

```powershell
python -m unittest tests.test_structure tests.test_models -v
```

## Task 3: Implement Lane-Aware MusicXML Repair

**Status:** completed

**Files:**
- Modify: `sheet2music/core/repair.py`
- Test: `tests/test_repair.py`

- [x] Add failing cases for independent `staff + voice` lanes with legal `forward`, chord notes, generated trailing `forward`, and a lane that exceeds the target boundary.

- [x] Implement `rebuild_measure_timing()` so it groups notes by `(staff, voice)`, assigns `forward` gaps to the next lane note when possible, calculates local occupancy, refuses to rebuild when a lane exceeds the target length, rebuilds only inter-lane `backup` and necessary `forward` elements, leaves original notes untouched on refusal, and validates the rebuilt measure.

- [x] Make `fix_musicxml_tree()` accept:

```python
def fix_musicxml_tree(
    root: ET.Element,
    target_time_signature: str,
    target_tempo_bpm: int | None = None,
    structure_plan: ScoreStructurePlan | Mapping[str, object] | None = None,
    measure_offset: int = 0,
    normalize_transient: bool = True,
) -> FixReport: ...
```

- [x] Apply a time declaration only at measure 1 or at an effective plan change; use full-score measure numbers after applying `measure_offset`.

- [x] In plan mode, apply explicit clef overrides by staff and range, skip the old part-wide first-clef baseline rule, and keep legacy transient normalization only for the no-plan compatibility path.

- [x] Add report fields for `time_signature_changes`, clef override ranges, structural warnings, and whether a measure was refused because of overflow.

- [x] Run:

```powershell
python -m unittest tests.test_repair -v
```

## Task 4: Make MIDI Follow the Same Boundaries

**Status:** completed

**Files:**
- Modify: `sheet2music/core/repair.py`
- Test: `tests/test_repair.py`

- [x] Write a failing MIDI test with raw `9/8` and `7/16` events and a plan containing `4/4 -> 2/4 -> 4/4`.

- [x] Implement `fix_midi_file()` with these additional keyword arguments:

```python
def fix_midi_file(
    input_path: Path,
    output_path: Path,
    target_time_signature: str,
    target_tempo_bpm: int | None = None,
    structure_plan: ScoreStructurePlan | Mapping[str, object] | None = None,
    measure_count: int | None = None,
) -> MidiFixReport: ...
```

- [x] Compute measure starts from `ticks_per_beat`, the effective signature for each measure, and `measure_count`; replace all raw time-signature events with only effective changes at actual measure starts.

- [x] Preserve note start/end ticks and tempo behavior. Report generated events, source event count, and unresolved boundary warnings.

- [x] Run:

```powershell
python -m unittest tests.test_repair -v
```

## Task 5: Integrate Full-Score Structure Into Conversion

**Status:** completed; raw XML analysis was corrected during Task 11 verification.

**Files:**
- Modify: `sheet2music/core/convert.py`
- Modify: `sheet2music/core/workspace.py`
- Test: `tests/test_convert_unit.py`

- [x] Add a unit test with two page XML files where page-local measure 1 maps to full-score measure 25; assert that page repair receives the correct `measure_offset` and combined repair receives offset 0.

- [x] Keep raw page XML immutable and compute page offsets from the cumulative maximum measure count before applying structure changes.

- [x] Pass the same validated plan to page repair, combined repair, and MIDI repair; pass the combined full-score measure count to `fix_midi_file()`.

- [x] Extend the report with `structure_plan`, page measure offsets, analysis status, and final structure/MIDI event reports.

- [x] Keep the existing no-plan call path defaulting to `4/4` and automatic finalization.

- [x] Run:

```powershell
python -m unittest tests.test_convert_unit tests.test_convert -v
```

## Task 6: Add Preflight Analysis and Review Findings

**Status:** completed

**Files:**
- Create: `sheet2music/core/analysis.py`
- Test: `tests/test_analysis.py`

- [x] Write tests for a finding model containing `id`, `kind`, `severity`, `measure_start`, `measure_end`, `page_numbers`, `observed`, `suggestion`, `reason`, and `status`.

- [x] Implement:

```python
def analyze_musicxml_tree(
    root: ET.Element,
    structure_plan: ScoreStructurePlan,
    page_measure_offsets: list[int] | None = None,
) -> AnalysisReport: ...
```

- [x] Use deterministic rules: every effective time-signature change not already confirmed is high-risk; conflicting signatures for the same full-score measure are high-risk; explicit clef changes that alter staff interpretation are high-risk; unresolved lane overflow, negative cursor, overlap, or boundary warnings are high-risk recognition diagnostics; unchanged duplicate metadata and safe in-boundary timing are informational only.

- [x] Never discard an isolated change. The Nautilus measure 25 `2/4` must produce a `25..25` finding with suggestion `preserve`, not disappear because it is rare.

- [x] Map findings to source page numbers and source measure ranges using page offsets; keep raw and candidate XML paths in the report.

- [x] Run:

```powershell
python -m unittest tests.test_analysis -v
```

## Task 7: Split Preparation From Finalization

**Status:** completed

**Files:**
- Modify: `sheet2music/core/convert.py`
- Modify: `sheet2music/core/models.py`
- Test: `tests/test_convert_unit.py`

- [x] Add `JobStatus.AWAITING_REVIEW` and tests proving that a high-risk preparation returns without creating final `score.mid` or `score.mp3`.

- [x] Extract these conversion operations:

```python
def prepare_conversion(...) -> dict[str, object]: ...
def finalize_conversion(..., review_decisions: list[dict[str, object]]) -> dict[str, object]: ...
```

- [x] `prepare_conversion()` stops after raw page XML, combined raw XML, and the analysis report. It may create candidate fixed XML for display, but it must not export final MIDI/MP3.

- [x] `finalize_conversion()` converts decisions into a new structure plan: `preserve` adds the observed change, `correct` adds the suggested correction, and `reidentify` requires a completed region replacement artifact.

- [x] Keep `run_conversion()` as a compatibility wrapper: automatically finalize when there are no high-risk findings; otherwise return a review-pending report.

- [x] Run:

```powershell
python -m unittest tests.test_convert_unit -v
```

## Task 8: Add Region Re-Identification and XML Replacement

**Status:** completed

**Files:**
- Create: `sheet2music/core/reidentify.py`
- Modify: `sheet2music/core/workspace.py`
- Test: `tests/test_reidentify.py`

- [x] Write tests for valid PNG/JPEG/WEBP upload, positive inclusive measure range, rejection of reversed/out-of-score ranges, and rejection of oversized files.

- [x] Implement these exact operations:

```python
def validate_region_request(
    image_path: Path,
    measure_start: int,
    measure_end: int,
    score_measure_count: int,
) -> None: ...

def replace_musicxml_measure_range(
    base_root: ET.Element,
    replacement_root: ET.Element,
    measure_start: int,
    measure_end: int,
) -> None: ...
```

- [x] Replace measures by `part` id and full-score number; preserve measures outside the range, part ids, score metadata, structure state, and global measure numbering.

- [x] Run HOMR on the uploaded crop through the existing `run_homr_on_page()` entry point in a separate workspace directory. Keep the uploaded image, replacement raw XML, and merged XML as artifacts.

- [x] After replacement, rerun analysis and set the finding back to `awaiting_review`; do not finalize automatically.

- [x] Run:

```powershell
python -m unittest tests.test_reidentify -v
```

## Task 9: Add Review-Aware Job and API State

**Status:** completed

**Files:**
- Modify: `sheet2music/web/jobs.py`
- Modify: `sheet2music/web/app.py`
- Modify: `sheet2music/core/workspace.py`
- Test: `tests/test_api.py`

- [x] Add job fields for `analysis`, `review_decisions`, and review-pending state; expose them through `JobRecord.to_dict()`.

- [x] Add endpoints:

```text
GET  /api/jobs/{job_id}/analysis
POST /api/jobs/{job_id}/review
POST /api/jobs/{job_id}/review/{finding_id}/region
```

- [x] `POST /review` accepts only `preserve`, `correct`, and `reidentify`, validates that every high-risk finding has a decision, then queues finalization.

- [x] `POST /region` accepts multipart `file`, `measure_start`, and `measure_end`; validates the image and range, queues region HOMR, merges the replacement, and returns updated analysis.

- [x] Keep workers serialized so full-page HOMR and region HOMR cannot run concurrently. A failed region attempt keeps prior raw/candidate artifacts and marks the finding for retry.

- [x] Add API tests for review-pending state, absent final artifacts before approval, successful preserve/correct finalization, invalid region ranges, non-image uploads, and successful region upload returning a new pending analysis.

- [x] Run:

```powershell
python -m unittest tests.test_api -v
```

## Task 10: Build the Browser Review UI

**Status:** implemented; browser approval interaction, region upload controls, and re-review state were verified against the running app.

**Files:**
- Modify: `sheet2music/web/static/index.html`
- Modify: `sheet2music/web/static/app.js`
- Modify: `sheet2music/web/static/style.css`

- [x] Add a hidden review panel that appears at `awaiting_review`, showing each finding's page, measure range, observed value, suggestion, reason, and source preview.

- [x] Give each finding three mutually exclusive actions: `保留`, `更正`, and `二次识别`.

- [x] When `二次识别` is selected, show a file input and two numeric fields for inclusive full-score measure start/end, defaulting both to the finding range while allowing a wider range.

- [x] Disable final approval until every high-risk finding has a decision and every selected region upload has completed successfully.

- [x] After region replacement, refresh the analysis list and require a new decision for the returned finding.

- [x] Preserve the existing automatic no-finding flow, GPU controls, output selection, reset action, and download links.

- [x] Verify with the running app at `http://127.0.0.1:8610`: upload a PDF, trigger review-pending state, approve a finding, upload a crop, and confirm the UI returns to review before final export.

## Task 11: End-to-End Nautilus Verification

**Status:** completed with a documented HOMR recognition limitation: the raw GPU run still misses the source score's measure-25 `2/4`, while the structure-plan and review workflows handle that missing declaration explicitly.

**Files:**
- Modify: `docs/nautilus-timing-clef-repair-plan.md`
- Modify: `README.md`
- Test: `tests/test_convert.py`

- [x] Add the Nautilus structure plan as an integration fixture without changing the default `4/4` fixture.

- [x] Run the full test suite:

```powershell
python -m unittest discover -s tests -t .
```

Expected result: all tests pass, with only documented external-tool skips.

- [x] Run the real GPU conversion using `sheets/ヨルシカ - ノーチラス.pdf`; verify the 7-page/80-measure GPU run, raw/candidate artifact separation, 34 review findings, post-approval MIDI output, and region `25..36` re-identification. The raw HOMR output misses the source score's measure-25 `2/4`; a structure-plan replay reports that omission as `missing_time_signature`, and the repair/finalization tests verify `2/4 -> 4/4`, local clef overrides, and calculated MIDI starts.

- [x] Exercise the third review path with a crop covering measure 25, replace that range, inspect merged XML, and confirm replacement triggers a second approval rather than silently finalizing.

- [x] Update the source plan status from design-only to implemented only after end-to-end evidence is recorded. Document remaining HOMR recognition errors separately from repair results.

## Checkpoint Order

1. Tasks 1-4: pure structure, MusicXML, and MIDI repair; no browser behavior.
2. Tasks 5-7: full-score conversion and review-pending state.
3. Tasks 8-9: region replacement and API workflow.
4. Task 10: browser interaction.
5. Task 11: full regression and real Nautilus verification.
