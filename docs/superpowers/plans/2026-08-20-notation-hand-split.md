# Piano Notation Hand Split Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create a notation-only two-hand MIDI derivative for audio/video PDF export without changing the downloadable Transkun MIDI.

**Architecture:** `sheet2music.core.notation_midi` reads MIDI event timing with `mido`, finds the weighted one-dimensional pitch split that minimizes within-hand pitch variance, and writes separate right/left piano tracks. The audio pipeline writes `score.notation.mid` only when PDF output is requested, passes that file to MuseScore, and keeps `score.mid` as the untouched playback artifact.

**Tech Stack:** Python 3.11, mido, unittest, MuseScore 4 CLI.

---

### Task 1: Define the non-destructive MIDI hand split

**Files:**
- Create: `sheet2music/core/notation_midi.py`
- Test: `tests/test_notation_midi.py`

- [x] **Step 1: Write the failing preservation and allocation tests**

```python
def test_split_preserves_every_note_and_routes_by_computed_boundary(self) -> None:
    result = split_midi_for_piano_notation(source, destination)
    self.assertEqual(result.split_note, 60)
    self.assertEqual(note_events(destination), note_events(source))
    self.assertEqual(track_notes(destination, "Piano Left Hand"), [48, 55, 60])
    self.assertEqual(track_notes(destination, "Piano Right Hand"), [64, 72])
```

- [x] **Step 2: Run the focused test and verify it fails because the module does not exist**

Run: `.venv\\Scripts\\python.exe -m unittest tests.test_notation_midi -v`

Expected: import failure for `sheet2music.core.notation_midi`.

- [x] **Step 3: Implement the minimal splitter**

```python
def split_midi_for_piano_notation(source: Path, destination: Path) -> HandSplitResult:
    boundary = optimal_pitch_boundary(note_events)
    write_conductor_track(source)
    write_hand_track("Piano Right Hand", notes_at_or_above=boundary + 1)
    write_hand_track("Piano Left Hand", notes_at_or_below=boundary)
    return HandSplitResult(boundary)
```

The implementation must preserve every note-on/note-off tick, channel, pitch, velocity, and program; it may only route those messages to one of two new tracks.

- [x] **Step 4: Run the focused test and verify it passes**

Run: `.venv\\Scripts\\python.exe -m unittest tests.test_notation_midi -v`

Expected: `OK`.

### Task 2: Route audio/video PDF export through the derivative

**Files:**
- Modify: `sheet2music/core/audio_transcription.py`
- Modify: `sheet2music/core/workspace.py`
- Modify: `tests/test_audio_transcription.py`

- [x] **Step 1: Change the existing PDF pipeline test to expect `score.notation.mid`**

```python
self.assertEqual(
    split_midi.call_args.args,
    (workspace.output_dir / "score.mid", workspace.output_dir / "score.notation.mid"),
)
export_pdf.assert_called_once_with(
    workspace.output_dir / "score.notation.mid", workspace.output_dir / "score.pdf"
)
```

- [x] **Step 2: Run the focused pipeline test and verify it fails under the old direct-export behavior**

Run: `.venv\\Scripts\\python.exe -m unittest tests.test_audio_transcription.AudioTranscriptionTest.test_audio_pipeline_can_render_piano_score_pdf_from_normalized_midi -v`

Expected: assertion failure because `export_pdf` receives `score.mid`.

- [x] **Step 3: Add derivative generation in the optional PDF path**

```python
notation_midi_path = workspace.output_dir / "score.notation.mid"
split_result = split_midi_for_piano_notation(midi_path, notation_midi_path)
export_pdf(notation_midi_path, workspace.output_dir / "score.pdf")
```

Add `notation_midi` and `notation_hand_split` report fields. Exclude `score.notation.mid` from user artifacts.

- [x] **Step 4: Run the focused pipeline test and verify it passes**

Run: `.venv\\Scripts\\python.exe -m unittest tests.test_audio_transcription.AudioTranscriptionTest.test_audio_pipeline_can_render_piano_score_pdf_from_normalized_midi -v`

Expected: `OK`.

### Task 3: Validate with the supplied Transkun MIDI and update milestone documentation

**Files:**
- Modify: `docs/transkun-v2-audio-transcription-design.md`
- Modify: `README.md`

- [x] **Step 1: Run the splitter against `audio/Yorushika - Usotsuki Arrangement_piano_transkun.mid`**

Run: `.venv\\Scripts\\python.exe -c "... split_midi_for_piano_notation(...) ..."`

Expected: the derived MIDI contains right/left piano tracks and the source note-event multiset equals the derived multiset.

- [x] **Step 2: Use `export_pdf()` to render the derived MIDI**

Run: `.venv\\Scripts\\python.exe -c "... export_pdf(...) ..."`

Expected: MuseScore emits a non-empty PDF.

- [x] **Step 3: Update the Step 1 milestone record**

Record the non-destructive split policy, that `score.mid` remains the download/playback source, and the supplied MIDI verification result.

- [x] **Step 4: Run regression verification**

Run: `.venv\\Scripts\\python.exe -m unittest tests.test_notation_midi tests.test_audio_transcription tests.test_export -v`

Expected: all tests pass.
