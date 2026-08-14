# Automatic Timing Reidentification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Automatically locate systems containing timing-overflow findings, re-recognize them from retained 600 DPI pages, adopt only independently validated candidates, and require user uploads only when automatic attempts cannot produce a safe result.

**Architecture:** Persist the complete raw-page-to-HOMR coordinate transform and a HOMR layout sidecar, then map high-risk findings to system-level batches. A focused auto-resolution engine creates image variants, validates normalized MusicXML candidates, and applies target-measure replacements transactionally before the existing review and export gates run.

**Tech Stack:** Python 3.10+, OpenCV, `xml.etree.ElementTree`, dataclasses, FastAPI, browser JavaScript, Node test runner, Python `unittest`, vendored HOMR/ONNX Runtime.

---

## File Structure

**New focused modules**

- `vendor/homr/homr/layout.py`: HOMR-side layout schema, system extraction, and JSON writer.
- `sheet2music/core/layout.py`: Sheet2Music layout schema, coordinate transforms, system mapping, grouping, and crop generation.
- `sheet2music/core/auto_resolution.py`: batch state, image variants, candidate normalization, validation, selection, and transactional application.
- `tests/test_layout.py`: page geometry, transform, mapping, grouping, and crop tests.
- `tests/test_auto_resolution.py`: candidate gates, selection, persistence, and rollback tests.
- `tests/auto-resolution-state.test.cjs`: frontend summary and submit-readiness tests.

**Existing integration points**

- `vendor/homr/homr/autocrop.py`: expose crop bounds without changing existing `autocrop()` callers.
- `vendor/homr/homr/main.py`: add `--layout-output` and emit the sidecar after recognition.
- `sheet2music/core/pages.py`: persist the 600 DPI page-to-HOMR-input crop geometry.
- `sheet2music/core/homr.py`: request and return HOMR layout sidecars.
- `sheet2music/core/workspace.py`: create layout and auto-resolution artifact directories.
- `sheet2music/core/convert.py`: launch automatic resolution after initial analysis.
- `sheet2music/core/reidentify.py`: reuse exact-range replacement for generated candidates.
- `sheet2music/web/jobs.py`: persist automatic progress and resume interrupted batches.
- `sheet2music/web/app.py`: serve system crops and accept candidate choices/retries.
- `sheet2music/web/static/review-state.js`: pure automatic-resolution UI state helpers.
- `sheet2music/web/static/app.js`, `index.html`, `style.css`: progress, audit, conflict, and final-upload UI.

All commit steps stage only the listed files because the working tree already contains unrelated, uncommitted work.

### Task 1: Persist Raw Page Crop Geometry

**Files:**
- Modify: `sheet2music/core/pages.py`
- Modify: `sheet2music/core/workspace.py`
- Modify: `tests/test_workspace.py`
- Modify: `tests/test_pages.py`

- [ ] **Step 1: Write failing tests for page geometry and workspace directories**

Add this test to `tests/test_pages.py`:

```python
def test_crop_page_writes_geometry_for_mapping_back_to_raw_page(self) -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        source = root / "raw.png"
        target = root / "page.png"
        geometry = root / "page.json"
        image = np.full((100, 80, 3), 255, dtype=np.uint8)
        cv2.imwrite(str(source), image)

        with mock.patch("sheet2music.core.pages.detect_music_vertical_bounds", return_value=(10, 90)):
            crop_page_vertically(source, target, geometry_path=geometry)

        self.assertEqual(
            json.loads(geometry.read_text(encoding="utf-8")),
            {
                "schema_version": 1,
                "raw_size": {"width": 80, "height": 100},
                "input_bounds_in_raw": [0, 10, 80, 90],
                "input_size": {"width": 80, "height": 80},
            },
        )

def test_existing_pages_rebuild_missing_geometry(self) -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        pages_dir = Path(temp_dir)
        raw_dir = pages_dir / "raw"
        raw_dir.mkdir()
        raw = np.full((100, 80, 3), 255, dtype=np.uint8)
        cropped = raw[10:90, :]
        cv2.imwrite(str(raw_dir / "page-1.png"), raw)
        cv2.imwrite(str(pages_dir / "page-1.png"), cropped)
        with mock.patch("sheet2music.core.pages.detect_music_vertical_bounds", return_value=(10, 90)):
            paths = export_numbered_pages(Path("unused.pdf"), pages_dir)
        self.assertEqual(paths, [pages_dir / "page-1.png"])
        self.assertTrue((pages_dir / "geometry" / "page-1.json").exists())
```

Extend `JobWorkspaceTest.test_create_layout`:

```python
self.assertTrue(workspace.layout_dir.is_dir())
self.assertTrue(workspace.page_geometry_dir.is_dir())
self.assertTrue(workspace.auto_resolution_crop_dir.is_dir())
self.assertTrue(workspace.auto_resolution_candidate_dir.is_dir())
self.assertTrue(workspace.auto_resolution_validation_dir.is_dir())
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `.venv\Scripts\python.exe -m unittest tests.test_pages tests.test_workspace -v`

Expected: FAIL because `geometry_path` and the new workspace paths do not exist.

- [ ] **Step 3: Implement geometry persistence and workspace paths**

Add to `sheet2music/core/pages.py`:

```python
def write_page_geometry(
    geometry_path: Path,
    raw_width: int,
    raw_height: int,
    top: int,
    bottom: int,
) -> None:
    payload = {
        "schema_version": 1,
        "raw_size": {"width": raw_width, "height": raw_height},
        "input_bounds_in_raw": [0, top, raw_width, bottom],
        "input_size": {"width": raw_width, "height": bottom - top},
    }
    geometry_path.parent.mkdir(parents=True, exist_ok=True)
    geometry_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
```

Change `crop_page_vertically` to accept `geometry_path: Path | None = None` and call `write_page_geometry` after `cv2.imwrite`. In `export_numbered_pages`, create `pages/geometry` and pass `geometry_path=geometry_dir / f"{raw_page.stem}.json"`. Before returning cached numbered pages, call `ensure_page_geometry()` for every page; it recalculates bounds from the retained raw page and writes metadata only when the calculated crop dimensions equal the cached HOMR input dimensions.

Add these paths in `JobWorkspace.__init__` and `create()`:

```python
self.layout_dir = root / "layout"
self.page_geometry_dir = self.pages_dir / "geometry"
self.auto_resolution_dir = root / "auto_resolution"
self.auto_resolution_crop_dir = self.auto_resolution_dir / "crops"
self.auto_resolution_candidate_dir = self.auto_resolution_dir / "candidates"
self.auto_resolution_validation_dir = self.auto_resolution_dir / "validation"
```

- [ ] **Step 4: Run focused tests and verify GREEN**

Run: `.venv\Scripts\python.exe -m unittest tests.test_pages tests.test_workspace -v`

Expected: PASS.

- [ ] **Step 5: Commit page geometry support**

```powershell
git add sheet2music/core/pages.py sheet2music/core/workspace.py tests/test_pages.py tests/test_workspace.py
git commit -m "feat: persist page crop geometry"
```

### Task 2: Emit HOMR Layout Sidecars With Transform Metadata

**Files:**
- Create: `vendor/homr/homr/layout.py`
- Create: `vendor/homr/tests/test_layout.py`
- Modify: `vendor/homr/homr/autocrop.py`
- Modify: `vendor/homr/homr/main.py`

- [ ] **Step 1: Write failing HOMR tests for crop transforms and ordered systems**

Create `vendor/homr/tests/test_layout.py` with this helper and these tests:

```python
def fake_multistaff(
    y: int,
    barline_x: tuple[int, ...],
    note_x: tuple[int, ...],
) -> SimpleNamespace:
    def staff(offset: int) -> SimpleNamespace:
        return SimpleNamespace(
            min_x=10,
            max_x=190,
            min_y=y + offset,
            max_y=y + offset + 40,
            average_unit_size=10,
            get_bar_lines=lambda: [
                SimpleNamespace(center=(x, y + offset + 20)) for x in barline_x
            ],
            get_notes=lambda: [
                SimpleNamespace(center=(x, y + offset + 20)) for x in note_x
            ],
        )

    return SimpleNamespace(staffs=[staff(0), staff(80)])


class LayoutTest(unittest.TestCase):
    def test_autocrop_with_bounds_returns_identity_for_full_page(self) -> None:
        image = np.full((100, 80, 3), 255, dtype=np.uint8)
        cropped, bounds = autocrop_with_bounds(image)
        self.assertEqual(cropped.shape, image.shape)
        self.assertEqual(bounds, (0, 0, 80, 100))

    def test_build_layout_orders_systems_and_deduplicates_barlines(self) -> None:
        lower = fake_multistaff(y=400, barline_x=(20, 99, 101, 180), note_x=(45, 130))
        upper = fake_multistaff(y=100, barline_x=(20, 100, 180), note_x=(40, 140))
        layout = build_page_layout(
            [lower, upper],
            source_size=(2400, 3600),
            autocrop_bounds=(100, 200, 2300, 3400),
            recognition_size=(1920, 2793),
        )
        self.assertEqual([item.system_index for item in layout.systems], [0, 1])
        self.assertEqual(layout.systems[0].barline_x, (20, 100, 180))
        self.assertEqual(layout.transform.autocrop_bounds, (100, 200, 2300, 3400))
```

The test helper constructs real `Staff`, `StaffPoint`, `BarLine`, and `Note` instances so production extraction is exercised rather than mocked dictionaries.

- [ ] **Step 2: Run HOMR tests and verify RED**

Run: `.venv\Scripts\python.exe -m unittest vendor.homr.tests.test_layout -v`

Expected: FAIL because `homr.layout` and `autocrop_with_bounds` do not exist.

- [ ] **Step 3: Implement the layout schema and crop transform**

In `vendor/homr/homr/autocrop.py`, move the existing logic into:

```python
def autocrop_with_bounds(img: NDArray) -> tuple[NDArray, tuple[int, int, int, int]]:
    bounds = _detect_crop_bounds(img)
    if bounds is None:
        return img, (0, 0, img.shape[1], img.shape[0])
    x, y, width, height = bounds
    return img[y : y + height, x : x + width], (x, y, x + width, y + height)


def autocrop(img: NDArray) -> NDArray:
    return autocrop_with_bounds(img)[0]
```

Create immutable values in `vendor/homr/homr/layout.py`:

```python
@dataclass(frozen=True)
class HomrTransform:
    source_size: tuple[int, int]
    autocrop_bounds: tuple[int, int, int, int]
    recognition_size: tuple[int, int]


@dataclass(frozen=True)
class HomrSystemLayout:
    system_index: int
    bbox: tuple[int, int, int, int]
    staff_bboxes: tuple[tuple[int, int, int, int], ...]
    barline_x: tuple[int, ...]
    notehead_x: tuple[int, ...]
    local_measure_start: int | None
    local_measure_end: int | None
    measure_notehead_counts: tuple[int, ...]
    mapping_confidence: str


@dataclass(frozen=True)
class HomrPageLayout:
    schema_version: int
    transform: HomrTransform
    systems: tuple[HomrSystemLayout, ...]

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
```

`build_page_layout()` sorts `MultiStaff` values by `min(staff.min_y)`, derives each system box from all member staves, merges barline x positions within one average staff-space tolerance, and records assigned notehead x positions. `musicxml_system_ranges()` reads `<print new-system="yes">` boundaries from the generated XML and assigns exact local measure ranges. A system receives `mapping_confidence="high"` only when the number of XML ranges equals the number of detected systems and its deduplicated geometric intervals equal its XML measure count; otherwise its local range is retained for diagnostics but confidence is `ambiguous`.

- [ ] **Step 4: Add `--layout-output` to HOMR and emit after successful XML generation**

Extend `ProcessingConfig` with `layout_output: str | None`, add the CLI option, and write:

```python
if config.layout_output:
    layout = build_page_layout(
        multi_staffs,
        xml,
        source_size=(source_width, source_height),
        autocrop_bounds=autocrop_bounds,
        recognition_size=(image.shape[1], image.shape[0]),
    )
    layout.write(Path(config.layout_output))
```

Change `load_and_preprocess_predictions` and `detect_staffs_in_image` to return the source dimensions and autocrop bounds alongside their existing values. Keep the default CLI behavior unchanged when `--layout-output` is absent.

- [ ] **Step 5: Run HOMR regression tests and commit**

Run: `.venv\Scripts\python.exe -m unittest vendor.homr.tests.test_layout vendor.homr.tests.test_staff_parsing vendor.homr.tests.test_music_xml_generator -v`

Expected: PASS.

```powershell
git add vendor/homr/homr/autocrop.py vendor/homr/homr/layout.py vendor/homr/homr/main.py vendor/homr/tests/test_layout.py
git commit -m "feat: emit HOMR page layout sidecars"
```

### Task 3: Collect Layout Sidecars in Sheet2Music

**Files:**
- Modify: `sheet2music/core/homr.py`
- Modify: `sheet2music/core/convert.py`
- Modify: `tests/test_homr.py`
- Modify: `tests/test_convert_unit.py`

- [ ] **Step 1: Write failing command and conversion tests**

Extend `tests/test_homr.py`:

```python
def test_layout_output_is_forwarded_to_homr(self) -> None:
    command = build_homr_command(
        Path("page.png"),
        use_gpu=True,
        layout_output=Path("page.layout.json"),
    )
    self.assertIn("--layout-output", command)
    self.assertEqual(command[-3:], ["--layout-output", "page.layout.json", "page.png"])
```

Add a `prepare_conversion` test whose mocked `run_homr_on_page` writes both XML and layout JSON, then assert:

```python
self.assertEqual(preparation["page_layouts"], ["layout/page-1.json"])
self.assertTrue((workspace.layout_dir / "page-1.json").exists())
```

- [ ] **Step 2: Run focused tests and verify RED**

Run: `.venv\Scripts\python.exe -m unittest tests.test_homr tests.test_convert_unit -v`

Expected: FAIL because the HOMR wrapper does not accept a layout output.

- [ ] **Step 3: Extend the HOMR wrapper with a structured result**

Add:

```python
@dataclass(frozen=True)
class HomrPageResult:
    musicxml: Path
    layout: Path | None
```

Change `build_homr_command` to accept `layout_output: Path | None`, append the option before the image, and change `run_homr_on_page` to return `HomrPageResult`. It must raise `HomrPageError` when layout was requested but not produced.

- [ ] **Step 4: Copy sidecars during page recognition**

In `_recognize_pages`, request `work_dir / f"{page_image.stem}.layout.json"`, copy it to `workspace.layout_dir / f"{page_image.stem}.json"`, and return layout paths with page XML paths. Add `page_layouts` to the preparation report as workspace-relative paths.

Update existing mocks to return `HomrPageResult` rather than a bare path.

- [ ] **Step 5: Run tests and commit**

Run: `.venv\Scripts\python.exe -m unittest tests.test_homr tests.test_convert_unit -v`

Expected: PASS.

```powershell
git add sheet2music/core/homr.py sheet2music/core/convert.py tests/test_homr.py tests/test_convert_unit.py
git commit -m "feat: collect HOMR layout metadata"
```

### Task 4: Map Findings to System Batches and Crop 600 DPI Images

**Files:**
- Create: `sheet2music/core/layout.py`
- Create: `tests/test_layout.py`

- [ ] **Step 1: Write failing mapping, ambiguity, grouping, and crop tests**

Create tests covering these public functions:

```python
def test_maps_global_measures_and_groups_same_system(self) -> None:
    page = load_page_layout(layout_path, geometry_path, page_number=6, measure_offset=64)
    batches = group_overflow_findings(findings_for(66, 67, 70), [page])
    self.assertEqual(
        [(item.page_number, item.system_index, item.target_measures) for item in batches],
        [(6, 0, (66, 67)), (6, 1, (70,))],
    )

def test_ambiguous_barline_count_disables_automatic_crop(self) -> None:
    page = load_page_layout(ambiguous_layout_path, geometry_path, 2, 12)
    self.assertEqual(page.systems[0].mapping_confidence, "ambiguous")

def test_crop_maps_recognition_coordinates_back_to_raw_page(self) -> None:
    crop = crop_system_from_raw_page(page, page.systems[0], raw_page, output_path)
    self.assertEqual(crop.raw_bbox, (250, 420, 4600, 1620))
    self.assertEqual(cv2.imread(str(output_path)).shape[:2], (1200, 4350))
```

- [ ] **Step 2: Run tests and verify RED**

Run: `.venv\Scripts\python.exe -m unittest tests.test_layout -v`

Expected: import failure for `sheet2music.core.layout`.

- [ ] **Step 3: Implement immutable layout and batch mapping values**

Create:

```python
@dataclass(frozen=True)
class CoordinateTransform:
    raw_size: tuple[int, int]
    input_bounds_in_raw: tuple[int, int, int, int]
    homr_autocrop_bounds: tuple[int, int, int, int]
    recognition_size: tuple[int, int]

    def recognition_bbox_to_raw(self, bbox: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
        raw_width, raw_height = self.raw_size
        input_left, input_top, _, _ = self.input_bounds_in_raw
        crop_left, crop_top, crop_right, crop_bottom = self.homr_autocrop_bounds
        recognition_width, recognition_height = self.recognition_size
        scale_x = (crop_right - crop_left) / recognition_width
        scale_y = (crop_bottom - crop_top) / recognition_height
        left, top, right, bottom = bbox
        mapped = (
            math.floor(input_left + crop_left + left * scale_x),
            math.floor(input_top + crop_top + top * scale_y),
            math.ceil(input_left + crop_left + right * scale_x),
            math.ceil(input_top + crop_top + bottom * scale_y),
        )
        return (
            max(0, min(raw_width, mapped[0])),
            max(0, min(raw_height, mapped[1])),
            max(0, min(raw_width, mapped[2])),
            max(0, min(raw_height, mapped[3])),
        )


@dataclass(frozen=True)
class ScoreSystem:
    page_number: int
    system_index: int
    bbox: tuple[int, int, int, int]
    global_measure_start: int
    global_measure_end: int
    mapping_confidence: str


@dataclass(frozen=True)
class SystemBatchSpec:
    page_number: int
    system_index: int
    target_measures: tuple[int, ...]
    context_range: tuple[int, int]
```

`load_page_layout()` validates the sidecar schema, adds the page measure offset to each confirmed local range, and preserves HOMR's `mapping_confidence`. It never reconstructs missing ranges by distributing page measures proportionally.

- [ ] **Step 4: Implement system crop generation**

`crop_system_from_raw_page()` maps the system box back to raw coordinates, expands by staff-space-derived padding, clamps to the raw image, writes the crop, and returns a `SystemCrop` containing source path, output path, and raw bounding box.

- [ ] **Step 5: Run tests and commit**

Run: `.venv\Scripts\python.exe -m unittest tests.test_layout -v`

Expected: PASS.

```powershell
git add sheet2music/core/layout.py tests/test_layout.py
git commit -m "feat: map timing findings to score systems"
```

### Task 5: Build Candidate Validation and Selection

**Files:**
- Create: `sheet2music/core/auto_resolution.py`
- Create: `tests/test_auto_resolution.py`
- Modify: `sheet2music/core/reidentify.py`

- [ ] **Step 1: Write failing tests for normalization and hard gates**

Create tests for canonical content, overflow rejection, structure changes, and independent visual evidence:

```python
def test_normalization_ignores_layout_metadata_but_keeps_music(self) -> None:
    self.assertEqual(candidate_fingerprint(xml_a), candidate_fingerprint(xml_b))

def test_candidate_rejects_overflow_and_unconfirmed_clef_change(self) -> None:
    result = validate_candidate(candidate_xml, batch, evidence, plan)
    self.assertFalse(result.accepted)
    self.assertEqual(set(result.reasons), {"measure_overflow", "structure_changed"})

def test_underfilled_voice_is_not_rejected(self) -> None:
    result = validate_candidate(underfilled_xml, batch, evidence, plan)
    self.assertTrue(result.accepted)

def test_sparse_targets_replace_only_requested_measures(self) -> None:
    merged = replace_selected_musicxml_measures(
        base_xml,
        system_candidate_xml,
        candidate_global_start=70,
        target_measure_numbers=(70, 72),
    )
    self.assertEqual(measure_digest(merged, 71), measure_digest(base_xml, 71))
    self.assertNotEqual(measure_digest(merged, 70), measure_digest(base_xml, 70))
    self.assertNotEqual(measure_digest(merged, 72), measure_digest(base_xml, 72))
```

- [ ] **Step 2: Run focused tests and verify RED**

Run: `.venv\Scripts\python.exe -m unittest tests.test_auto_resolution -v`

Expected: import failure for `sheet2music.core.auto_resolution`.

- [ ] **Step 3: Implement candidate values, normalization, and hard gates**

Define:

```python
class BatchStatus(str, Enum):
    PENDING = "pending"
    LOCATING = "locating"
    RECOGNIZING = "recognizing"
    VALIDATING = "validating"
    AUTO_RESOLVED = "auto_resolved"
    NEEDS_CHOICE = "needs_choice"
    NEEDS_UPLOAD = "needs_upload"
    FAILED = "failed"


@dataclass(frozen=True)
class CandidateValidation:
    accepted: bool
    reasons: tuple[str, ...]
    fingerprint: str
    target_findings_before: int
    target_findings_after: int


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
```

`candidate_fingerprint()` deep-copies target measures, removes layout-only elements and attributes, normalizes numeric text, and hashes deterministic XML bytes. `validate_candidate()` invokes the existing timeline analyzer, compares confirmed structure attributes, validates exact measure count/alignment, and checks notehead count tolerance.

Add `replace_selected_musicxml_measures()` to `reidentify.py`. It maps candidate ordinal 1 to `candidate_global_start`, deep-copies only the requested global measure numbers, restores global numbering and inherited divisions, and leaves every non-target measure byte-for-byte equivalent after XML normalization.

- [ ] **Step 4: Implement transparent selection rules**

Add:

```python
def choose_candidate(validations: Sequence[CandidateValidation]) -> CandidateChoice:
    accepted = [item for item in validations if item.accepted]
    groups = group_by_fingerprint(accepted)
    consensus = [items for items in groups.values() if len(items) >= 2]
    if len(consensus) == 1:
        return CandidateChoice.auto(consensus[0][0], "two_variants_agree")
    if len(groups) > 1:
        return CandidateChoice.needs_choice(tuple(item[0] for item in groups.values()))
    if len(accepted) == 1 and accepted[0].has_strong_single_candidate_evidence:
        return CandidateChoice.auto(accepted[0], "only_valid_candidate_with_visual_evidence")
    return CandidateChoice.needs_upload()
```

Use explicit fields for `has_strong_single_candidate_evidence`; do not derive it from an opaque weighted score.

- [ ] **Step 5: Run tests and commit**

Run: `.venv\Scripts\python.exe -m unittest tests.test_auto_resolution tests.test_timeline tests.test_reidentify -v`

Expected: PASS.

```powershell
git add sheet2music/core/auto_resolution.py sheet2music/core/reidentify.py tests/test_auto_resolution.py
git commit -m "feat: validate automatic recognition candidates"
```

### Task 6: Generate Variants and Apply Replacements Transactionally

**Files:**
- Modify: `sheet2music/core/auto_resolution.py`
- Modify: `tests/test_auto_resolution.py`
- Modify: `sheet2music/core/workspace.py`

- [ ] **Step 1: Write failing tests for deterministic variants, retry limits, and rollback**

Add:

```python
def test_builds_three_distinct_deterministic_variants(self) -> None:
    variants = build_image_variants(source_crop, output_dir)
    self.assertEqual([item.name for item in variants], ["standard", "contrast", "context"])
    self.assertEqual(len({item.digest for item in variants}), 3)

def test_does_not_rerun_completed_variant(self) -> None:
    batch.attempts.append(successful_attempt("standard"))
    runner.resolve_batch(batch, context)
    self.assertNotIn("standard", runner.called_variants)

def test_transaction_rolls_back_when_global_findings_increase(self) -> None:
    result = apply_candidate_transactionally(context, batch, candidate)
    self.assertFalse(result.committed)
    self.assertEqual(result.reason, "new_high_risk_findings")
    self.assertEqual(base_xml.read_bytes(), original_bytes)

def test_batch_store_round_trips_completed_attempts_atomically(self) -> None:
    store = AutoResolutionStore(state_path)
    batch.attempts.append(successful_attempt("standard"))
    store.save([batch])
    restored = store.load()
    self.assertEqual(restored[0].attempts[0]["variant"], "standard")
    self.assertFalse(state_path.with_suffix(".json.tmp").exists())
```

- [ ] **Step 2: Run tests and verify RED**

Run: `.venv\Scripts\python.exe -m unittest tests.test_auto_resolution -v`

Expected: FAIL because variant generation and transactional application are absent.

- [ ] **Step 3: Implement three bounded image variants and candidate execution**

`build_image_variants()` writes exactly three variants:

```python
ImageVariant("standard", padded_crop)
ImageVariant("contrast", apply_conservative_clahe(padded_crop))
ImageVariant("context", resize_with_wider_context(context_crop))
```

`AutoResolutionRunner.resolve_batch()` skips attempts already persisted by variant name, calls `run_homr_on_page()` with the job GPU preference, stores raw MusicXML under `auto_resolution/candidates/<batch-id>/`, and records failures without aborting other variants.

Implement `AutoResolutionStore` around `auto_resolution/batches.json`. It serializes enum values and tuple fields to JSON, validates `schema_version`, writes through `batches.json.tmp`, and atomically replaces the state file after each completed attempt and status transition. When GPU was requested but no CUDA provider is available, the runner records `gpu_unavailable` and does not silently launch all three variants on CPU.

- [ ] **Step 4: Implement transactional replacement and validation report**

`apply_candidate_transactionally()` must:

1. Copy the current base tree in memory.
2. Replace only `batch.target_measures` using `replace_selected_musicxml_measures`.
3. Re-run `analyze_musicxml_tree` for the entire score.
4. Compare high-risk finding IDs outside the target range.
5. Run `validate_musicxml_boundaries`.
6. Write a temporary candidate, atomically replace the official candidate only on success, and write `validation/<batch-id>.json` in both cases.

- [ ] **Step 5: Run tests and commit**

Run: `.venv\Scripts\python.exe -m unittest tests.test_auto_resolution tests.test_analysis tests.test_export -v`

Expected: PASS.

```powershell
git add sheet2music/core/auto_resolution.py sheet2music/core/workspace.py tests/test_auto_resolution.py
git commit -m "feat: resolve timing batches transactionally"
```

### Task 7: Integrate Automatic Resolution Into Conversion and Job Recovery

**Files:**
- Modify: `sheet2music/core/convert.py`
- Modify: `sheet2music/web/jobs.py`
- Modify: `tests/test_convert_unit.py`
- Modify: `tests/test_api.py`

- [ ] **Step 1: Write failing conversion and recovery tests**

Add scenarios:

```python
def test_prepare_conversion_runs_auto_resolution_before_manual_review(self) -> None:
    preparation = prepare_conversion(workspace, params)
    self.assertEqual(preparation["auto_resolution"]["resolved_count"], 1)
    self.assertNotIn(resolved_finding_id, high_risk_ids(preparation["analysis"]))

def test_only_unresolved_batches_remain_in_manual_review(self) -> None:
    self.assertEqual(preparation["auto_resolution"]["needs_choice_count"], 1)
    self.assertEqual(preparation["auto_resolution"]["needs_upload_count"], 1)

def test_restore_keeps_completed_attempts_and_resumes_pending_batch(self) -> None:
    restored = JobStore(base_dir).get(record.job_id)
    self.assertEqual(restored.stage, "automatic_reidentification")
    self.assertEqual(restored.report["auto_resolution"]["batches"][0]["status"], "auto_resolved")
```

- [ ] **Step 2: Run focused tests and verify RED**

Run: `.venv\Scripts\python.exe -m unittest tests.test_convert_unit tests.test_api -v`

Expected: FAIL because conversion does not invoke automatic resolution.

- [ ] **Step 3: Add the automatic stage to preparation**

After initial analysis, when timing-overflow findings exist:

```python
emit_stage("automatic_reidentification")
auto_report = resolve_timing_overflows(
    workspace=workspace,
    base_xml=combined_raw,
    analysis=analysis.to_dict(),
    page_layouts=layout_paths,
    page_measure_offsets=page_measure_offsets,
    structure_plan=plan,
    tempo_bpm=params.bpm,
    use_gpu=params.use_gpu,
    progress=progress,
)
combined_raw = auto_report.candidate_path or combined_raw
analysis = analyze_musicxml_tree(ET.parse(combined_raw).getroot(), plan, page_measure_offsets)
```

Store `auto_resolution`, `combined_musicxml_candidate`, and the refreshed analysis in the preparation report. Only unresolved findings continue to manual review.

- [ ] **Step 4: Persist and resume auto-resolution state**

Update `JobRecord.to_dict`, `_persist_record`, and `_restore_jobs` so a job interrupted during `automatic_reidentification` returns to `RUNNING` and resumes unfinished variants instead of being marked failed. Other running stages retain current failure-on-restart behavior.

The worker must update progress as `{current, total, page, system, resolved, needs_review}`.

- [ ] **Step 5: Run tests and commit**

Run: `.venv\Scripts\python.exe -m unittest tests.test_convert_unit tests.test_api -v`

Expected: PASS.

```powershell
git add sheet2music/core/convert.py sheet2music/web/jobs.py tests/test_convert_unit.py tests/test_api.py
git commit -m "feat: run automatic timing resolution in jobs"
```

### Task 8: Add Candidate Choice, Retry, and Crop APIs

**Files:**
- Modify: `sheet2music/web/app.py`
- Modify: `sheet2music/web/jobs.py`
- Modify: `tests/test_api.py`

- [ ] **Step 1: Write failing API tests**

Add tests for:

```python
def test_serves_only_crop_inside_requested_job(self) -> None:
    response = self.client.get(f"/api/jobs/{job_id}/auto-resolution/{batch_id}/crop")
    self.assertEqual(response.status_code, 200)
    self.assertEqual(response.headers["content-type"], "image/png")

def test_select_candidate_revalidates_before_committing(self) -> None:
    response = self.client.post(
        f"/api/jobs/{job_id}/auto-resolution/{batch_id}/select",
        json={"candidate_id": candidate_id},
    )
    self.assertEqual(response.status_code, 200)
    self.assertEqual(response.json()["report"]["auto_resolution"]["needs_choice_count"], 0)

def test_retry_rejects_fourth_automatic_attempt(self) -> None:
    response = self.client.post(f"/api/jobs/{job_id}/auto-resolution/{batch_id}/retry")
    self.assertEqual(response.status_code, 409)

def test_batch_upload_is_the_final_fallback_and_covers_the_batch_range(self) -> None:
    response = self.client.post(
        f"/api/jobs/{job_id}/auto-resolution/{batch_id}/upload",
        files={"file": ("system.png", PNG_BYTES, "image/png")},
    )
    self.assertEqual(response.status_code, 200)
    self.assertEqual(captured_range, batch.context_range)
```

- [ ] **Step 2: Run API tests and verify RED**

Run: `.venv\Scripts\python.exe -m unittest tests.test_api -v`

Expected: endpoint tests return 404.

- [ ] **Step 3: Implement safe batch lookup and crop serving**

Add `JobStore.get_auto_batch(record, batch_id)` that reads only the persisted batch list for that job. Serve the stored crop with `FileResponse`; never accept a filesystem path from the request.

- [ ] **Step 4: Implement candidate selection and bounded retry**

Candidate selection runs the same transactional validator as automatic selection. Retry is allowed only while an untried configured variant exists; otherwise return HTTP 409 and leave the batch in `needs_upload`. Add a batch upload endpoint that accepts one image only for `needs_upload`, uses the persisted context range instead of a user-supplied range, and runs the same candidate and transaction gates. Refresh `analysis` and aggregate counts after every successful choice or upload.

- [ ] **Step 5: Run tests and commit**

Run: `.venv\Scripts\python.exe -m unittest tests.test_api -v`

Expected: PASS.

```powershell
git add sheet2music/web/app.py sheet2music/web/jobs.py tests/test_api.py
git commit -m "feat: expose automatic resolution review APIs"
```

### Task 9: Replace Per-Finding Upload UI With Batch Review

**Files:**
- Create: `tests/auto-resolution-state.test.cjs`
- Modify: `sheet2music/web/static/review-state.js`
- Modify: `sheet2music/web/static/app.js`
- Modify: `sheet2music/web/static/index.html`
- Modify: `sheet2music/web/static/style.css`

- [ ] **Step 1: Write failing frontend state tests**

Create:

```javascript
test("summarizes automatic batches in Chinese", () => {
  assert.deepEqual(autoResolutionSummary(fixture), {
    total: 9,
    resolved: 6,
    needsChoice: 2,
    needsUpload: 1,
    text: "已检查 9 个谱表区域：自动解决 6 个，需要选择 2 个，仍需补充图片 1 个。",
  });
});

test("blocks submission only for unresolved batches and manual findings", () => {
  assert.equal(autoReviewReady(allResolved, []), true);
  assert.equal(autoReviewReady(withChoice, []), false);
  assert.equal(autoReviewReady(allResolved, [null]), false);
});

test("does not expose upload for a batch that still has automatic candidates", () => {
  assert.equal(batchActions({ status: "needs_choice" }).includes("upload"), false);
  assert.equal(batchActions({ status: "needs_upload" }).includes("upload"), true);
});
```

- [ ] **Step 2: Run Node tests and verify RED**

Run: `node --test tests/review-state.test.cjs tests/auto-resolution-state.test.cjs`

Expected: FAIL because the automatic-resolution helpers are absent.

- [ ] **Step 3: Implement pure UI state helpers**

Export `autoResolutionSummary`, `autoReviewReady`, `batchActions`, and `candidateSummaryText` from `review-state.js`. Keep all visible text in Chinese and format beat fractions as decimal values where exact.

- [ ] **Step 4: Render progress, audit, conflicts, and final upload**

Update the review panel to include:

- a stable summary band for total/resolved/conflict/upload counts;
- a collapsed `<details>` audit list for `auto_resolved` batches;
- conflict rows with page, measures, hand region, stored crop, and candidate summaries;
- select and retry controls for `needs_choice`;
- the existing upload controls only for `needs_upload`, prefilled with the batch range.

Add `automatic_reidentification` to stage labels. Poll while this stage is running and preserve fixed panel dimensions so progress text does not shift the layout. Bump static cache versions in `index.html`.

- [ ] **Step 5: Run frontend tests and commit**

Run: `node --test tests/review-state.test.cjs tests/job-session.test.cjs tests/auto-resolution-state.test.cjs`

Expected: PASS.

```powershell
git add tests/auto-resolution-state.test.cjs sheet2music/web/static/review-state.js sheet2music/web/static/app.js sheet2music/web/static/index.html sheet2music/web/static/style.css
git commit -m "feat: add automatic timing review workflow"
```

### Task 10: Full Regression and Nautilus Acceptance

**Files:**
- Modify: `docs/nautilus-timing-clef-repair-plan.md`

- [ ] **Step 1: Run all automated tests**

Run:

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests -v
node --test tests/review-state.test.cjs tests/job-session.test.cjs tests/auto-resolution-state.test.cjs
.venv\Scripts\python.exe -m unittest vendor.homr.tests.test_layout vendor.homr.tests.test_staff_parsing vendor.homr.tests.test_music_xml_generator -v
git diff --check
```

Expected: all tests pass and `git diff --check` prints no errors.

- [ ] **Step 2: Rebuild layout metadata for the retained Nautilus task**

Use the existing raw pages under `%TEMP%\sheet2music\jobs\8f7ec48483fd\pages\raw` and the configured GPU provider. Trigger layout regeneration for pages without sidecars, then run automatic resolution for the 27 timing-overflow findings.

Expected: the job records grouped system batches and completes no more than three variants per system without asking for individual uploads during automatic attempts.

- [ ] **Step 3: Verify the non-regression invariants**

Record from `report.json`:

```text
before_high_risk_count
after_high_risk_count
resolved_count
needs_choice_count
needs_upload_count
new_outside_target_count
```

Expected: `new_outside_target_count == 0` and `after_high_risk_count <= before_high_risk_count`. Inspect batches covering measures 72, 75, and 76 and confirm every accepted candidate has a validation artifact.

- [ ] **Step 4: Verify the browser workflow**

At desktop and mobile widths, confirm:

1. Automatic progress remains visible while inference runs.
2. Resolved batches are collapsed but inspectable.
3. Conflicts show the correct page crop and Chinese candidate summaries.
4. Upload is absent until a batch reaches `needs_upload`.
5. The submit button enables when all unresolved batches and manual clef findings have decisions.
6. Refreshing the page restores the same counts and decisions.

- [ ] **Step 5: Export and inspect timing-sensitive output**

After all blocking batches are resolved, export MusicXML and MIDI, then MP3 when ffmpeg is available. Verify the fixed boundary validator passes and compare the passages around measures 72, 75, and 76 for extra measure-length pauses.

- [ ] **Step 6: Update the Nautilus plan and commit acceptance results**

Add the measured batch counts, unresolved regions, provider used, test commands, and listening result to `docs/nautilus-timing-clef-repair-plan.md`.

```powershell
git add docs/nautilus-timing-clef-repair-plan.md
git commit -m "docs: record automatic timing acceptance results"
```
