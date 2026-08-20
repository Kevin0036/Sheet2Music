# Measure Timing and Clef Review Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace ambiguous timing review with beat-normalized, fixed-boundary analysis, deterministic duration repair, precise clef events, and an export gate that prevents unresolved overflow.

**Architecture:** Add a read-only MusicXML timeline module as the single source of truth for cursor movement, note ends, beat normalization, and notation-derived durations. Analysis consumes that model to create capability-aware findings; repair applies only deterministic duration corrections and finalization independently validates the resulting score before MuseScore runs.

**Tech Stack:** Python 3.10+, `xml.etree.ElementTree`, `fractions.Fraction`, FastAPI, browser JavaScript, `unittest`.

---

### Task 1: MusicXML Timeline and Beat Normalization

**Files:**
- Create: `sheet2music/core/timeline.py`
- Create: `tests/test_timeline.py`

- [ ] **Step 1: Write failing tests for unit normalization and document-order cursor semantics**

```python
class TimelineTest(unittest.TestCase):
    def test_different_divisions_normalize_to_four_beats(self):
        self.assertEqual(units_to_beats(16, 4), Fraction(4, 1))
        self.assertEqual(units_to_beats(96, 24), Fraction(4, 1))

    def test_chord_and_cross_staff_voice_do_not_create_extra_time(self):
        timeline = analyze_measure(ET.fromstring(XML), divisions=4, beats=4, beat_type=4)
        self.assertEqual(timeline.maximum_note_end_units, 16)
        self.assertEqual(timeline.final_cursor_units, 16)
        self.assertFalse(timeline.has_overflow)

    def test_backup_below_zero_is_invalid(self):
        timeline = analyze_measure(ET.fromstring(NEGATIVE_BACKUP_XML), 4, 4, 4)
        self.assertEqual(timeline.diagnostics[0].code, "negative_cursor")
```

- [ ] **Step 2: Run tests and verify RED**

Run: `.venv\Scripts\python.exe -m unittest tests.test_timeline -v`

Expected: import failure because `sheet2music.core.timeline` does not exist.

- [ ] **Step 3: Implement immutable timeline values and parser**

```python
@dataclass(frozen=True)
class MeasureTimeline:
    divisions: int
    expected_units: int
    final_cursor_units: int
    maximum_note_end_units: int
    events: tuple[TimedNote, ...]
    diagnostics: tuple[TimingDiagnostic, ...]

    @property
    def has_overflow(self) -> bool:
        return self.maximum_note_end_units > self.expected_units

def units_to_beats(units: int, divisions: int) -> Fraction:
    return Fraction(units, divisions)
```

Parse `note`, `backup`, and `forward` in XML document order; chords share the previous non-chord onset, grace notes consume zero units, and `staff` never creates a separate timeline.

- [ ] **Step 4: Run timeline tests and the existing repair suite**

Run: `.venv\Scripts\python.exe -m unittest tests.test_timeline tests.test_repair -v`

Expected: PASS.

### Task 2: Notation-Derived Duration Evidence

**Files:**
- Modify: `sheet2music/core/timeline.py`
- Modify: `tests/test_timeline.py`

- [ ] **Step 1: Write failing tests for dots and tuplets**

```python
def test_notated_duration_handles_dots_and_triplets(self):
    self.assertEqual(notated_duration_units(dotted_eighth, 24), 18)
    self.assertEqual(notated_duration_units(double_dotted_quarter, 16), 28)
    self.assertEqual(notated_duration_units(triplet_eighth, 24), 8)

def test_notated_duration_rejects_non_integral_or_missing_evidence(self):
    self.assertIsNone(notated_duration_units(missing_type, 24))
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `.venv\Scripts\python.exe -m unittest tests.test_timeline.TimelineTest.test_notated_duration_handles_dots_and_triplets -v`

Expected: FAIL because notation duration calculation is absent.

- [ ] **Step 3: Implement exact rational notation calculation**

```python
NOTE_BEATS = {"whole": Fraction(4), "half": Fraction(2), "quarter": Fraction(1), "eighth": Fraction(1, 2)}

def notated_duration_units(note: ET.Element, divisions: int) -> int | None:
    value = NOTE_BEATS.get(note.findtext("type", ""))
    if value is None:
        return None
    multiplier = sum((Fraction(1, 2**index) for index in range(note_count_dots(note) + 1)), Fraction())
    ratio = time_modification_ratio(note)
    units = value * multiplier * ratio * divisions
    return units.numerator if units.denominator == 1 else None
```

- [ ] **Step 4: Run all timeline tests**

Run: `.venv\Scripts\python.exe -m unittest tests.test_timeline -v`

Expected: PASS.

### Task 3: Replace `timing_structure` with Specific Findings

**Files:**
- Modify: `sheet2music/core/analysis.py`
- Modify: `tests/test_analysis.py`

- [ ] **Step 1: Write failing analysis tests**

```python
def test_underfilled_voice_is_not_high_risk(self):
    report = analyze_musicxml_tree(score_with_short_voice(), default_plan())
    self.assertFalse(report.requires_review)

def test_overflow_reports_beats_and_available_actions(self):
    finding = analyze_musicxml_tree(score_with_overflow(), default_plan()).high_risk_findings[0]
    self.assertEqual(finding.kind, "timing_measure_overflow")
    self.assertEqual(finding.observed["occupied_beats"], "19/4")
    self.assertEqual(finding.observed["expected_beats"], "4")
    self.assertEqual(finding.available_actions, ["reidentify"])
```

- [ ] **Step 2: Run focused tests and verify RED**

Run: `.venv\Scripts\python.exe -m unittest tests.test_analysis -v`

Expected: FAIL because analysis still emits `timing_structure` and raw ticks.

- [ ] **Step 3: Consume `MeasureTimeline` and serialize rational beats**

Add `available_actions` to `ReviewFinding`. Emit `timing_cursor_invalid`, `timing_notation_mismatch`, and `timing_measure_overflow`; keep underfill as non-blocking diagnostics only when useful. Include `divisions`, unit values, normalized beat strings, affected staff numbers, and a Chinese-ready structured reason payload.

- [ ] **Step 4: Run analysis and API regression tests**

Run: `.venv\Scripts\python.exe -m unittest tests.test_analysis tests.test_api -v`

Expected: PASS after updating legacy fixture IDs to the specific finding kinds.

### Task 4: Deterministic Automatic Duration Repair

**Files:**
- Modify: `sheet2music/core/repair.py`
- Modify: `sheet2music/core/analysis.py`
- Modify: `sheet2music/core/convert.py`
- Modify: `tests/test_repair.py`
- Modify: `tests/test_analysis.py`
- Modify: `tests/test_convert.py`

- [ ] **Step 1: Write a failing workflow test**

```python
def test_automatic_timing_repair_changes_only_provably_wrong_durations(self):
    before_pitches = pitches(root)
    result = apply_deterministic_timing_repair(root, finding)
    self.assertTrue(result.applied)
    self.assertEqual(pitches(root), before_pitches)
    self.assertFalse(analyze_measure(repaired_measure, 24, 4, 4).has_overflow)
```

- [ ] **Step 2: Verify RED**

Run: `.venv\Scripts\python.exe -m unittest tests.test_repair -v`

Expected: FAIL because deterministic timing repair does not exist.

- [ ] **Step 3: Implement evidence-bound correction**

Only replace `<duration>` when every changed note has one exact notation-derived integral value and the reanalyzed candidate has no invalid cursor or overflow. Work on a deep copy and replace the live measure only after validation succeeds. Expose action `correct` only when this dry run succeeds.

- [ ] **Step 4: Wire approval to execute the repair**

In `_reviewed_structure_plan` and finalization, route timing `correct` decisions to the deterministic repair pass. Reject `preserve` for blocking timing findings and reject `correct` when the finding lacks a repair candidate.

- [ ] **Step 5: Run repair, conversion, and API tests**

Run: `.venv\Scripts\python.exe -m unittest tests.test_repair tests.test_analysis tests.test_convert tests.test_api -v`

Expected: PASS.

### Task 5: Precise Clef Events

**Files:**
- Modify: `sheet2music/core/structure.py`
- Modify: `sheet2music/core/analysis.py`
- Modify: `sheet2music/core/repair.py`
- Modify: `tests/test_analysis.py`
- Modify: `tests/test_repair.py`

- [ ] **Step 1: Write failing tests for start and mid-measure clefs**

```python
def test_clef_events_keep_measure_offset_and_staff(self):
    findings = analyze_musicxml_tree(score_with_two_clef_events(), default_plan()).findings
    self.assertEqual(findings[0].kind, "clef_change_at_measure_start")
    self.assertEqual(findings[0].offset_units, 0)
    self.assertEqual(findings[1].kind, "clef_change_mid_measure")
    self.assertEqual(findings[1].offset_beats, "2")
    self.assertEqual(findings[1].staff, 2)
```

- [ ] **Step 2: Verify RED**

Run: `.venv\Scripts\python.exe -m unittest tests.test_analysis -v`

Expected: FAIL because all attributes are currently treated as measure-start events.

- [ ] **Step 3: Add `ClefEvent` and document-order attribute offsets**

Track the MusicXML cursor while walking each measure. Serialize previous/observed clefs, staff, hand region, offset units, divisions, and normalized offset beats. Applying a mid-measure decision updates the matching attributes node without changing any timing child.

- [ ] **Step 4: Run analysis and repair tests**

Run: `.venv\Scripts\python.exe -m unittest tests.test_analysis tests.test_repair -v`

Expected: PASS.

### Task 6: Capability-Aware Chinese Review UI

**Files:**
- Modify: `sheet2music/web/static/app.js`
- Modify: `sheet2music/web/static/review-state.js`
- Modify: `tests/review-state.test.cjs`

- [ ] **Step 1: Write failing state tests**

```javascript
assert.deepEqual(actionsForFinding({available_actions: ["reidentify"]}), ["reidentify"]);
assert.equal(formatBeatLocation({measure_start: 15, offset_beats: "3/2"}), "第 15 小节，第 1.5 拍后");
```

- [ ] **Step 2: Verify RED**

Run: `node --test tests/review-state.test.cjs`

Expected: FAIL because finding-specific action selection is absent.

- [ ] **Step 3: Render only allowed actions and readable Chinese timing**

Display current occupied beats, capacity, overflow, affected hand/staff, measure and intra-measure beat. Do not expose raw `cursor`, `expected_ticks`, internal kind names, or code symbols.

- [ ] **Step 4: Run Node tests and syntax check**

Run: `node --test tests/review-state.test.cjs`

Run: `node --check sheet2music/web/static/app.js`

Expected: PASS.

### Task 7: Final Export Boundary Gate

**Files:**
- Modify: `sheet2music/core/convert.py`
- Modify: `tests/test_convert.py`

- [ ] **Step 1: Write a failing export-gate test**

```python
def test_unresolved_overflow_blocks_musescore_export(self):
    with self.assertRaisesRegex(ConversionError, "第 72 小节"):
        finalize_prepared_conversion(workspace, params, review_decisions=[])
    self.assertFalse((workspace.output_dir / "score.mid").exists())
```

- [ ] **Step 2: Verify RED**

Run: `.venv\Scripts\python.exe -m unittest tests.test_convert -v`

Expected: FAIL because unsupported overflow currently continues to MuseScore.

- [ ] **Step 3: Validate the complete fixed MusicXML immediately before export**

Run timeline analysis over every normal measure. Reject negative cursors, invalid movement, overflow, unapplied automatic repairs, and unresolved re-identification decisions. Return measure-specific Chinese errors and create no MIDI/MP3 on failure.

- [ ] **Step 4: Run conversion tests**

Run: `.venv\Scripts\python.exe -m unittest tests.test_convert tests.test_api -v`

Expected: PASS.

### Task 8: Persist Review Jobs and Run Nautilus Acceptance

**Files:**
- Modify: `sheet2music/web/jobs.py`
- Modify: `tests/test_api.py`
- Modify: `README.md`

- [ ] **Step 1: Write a failing reload test**

```python
def test_job_store_restores_awaiting_review_job(self):
    first = JobStore(root)
    record = first.create(...)
    first.mark_awaiting_review(record, analysis)
    restored = JobStore(root).get(record.job_id)
    self.assertEqual(restored.stage, "awaiting_review")
    self.assertEqual(restored.analysis, analysis)
```

- [ ] **Step 2: Verify RED**

Run: `.venv\Scripts\python.exe -m unittest tests.test_api -v`

Expected: FAIL because job records are memory-only.

- [ ] **Step 3: Atomically persist job metadata**

Write one JSON state file per job after every state transition using temporary-file replacement. Restore valid records at startup; retain uploaded region paths and review decisions without serializing locks or threads.

- [ ] **Step 4: Run the full automated suite**

Run: `.venv\Scripts\python.exe -m unittest discover -s tests -t .`

Run: `node --test tests/review-state.test.cjs`

Run: `git -c safe.directory='C:/Users/Kevin/Downloads/Piano_Arranger' diff --check -- Sheet2Music`

Expected: all unit tests pass; external-tool integration tests may be skipped only when their documented fixture is absent.

- [ ] **Step 5: Restart only after persistence is verified and run Nautilus acceptance**

Use the real Nautilus source to verify measure 25 is 2/4, measure 26 returns to 4/4, clef changes at measures 14-17 show exact positions, and measures 72/75/76 cannot export while overflowing. Complete recognition, review, one region re-identification, export, and audio comparison; record the measured finding counts and generated artifact paths in the test report.
