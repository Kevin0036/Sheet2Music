# HOMR Browser Tool Design

Date: 2026-07-31
Status: Proposed
Decision: Use `FastAPI + lightweight browser UI` as the first independent HOMR tool shape.

## Summary

Build a browser-based single-file conversion tool around the existing HOMR pipeline.
The tool accepts one PDF at a time, shows the first page preview, lets the user enter
constant BPM and choose output formats, then runs the recognition and repair pipeline
to export canonical symbolic outputs.

The first version is intentionally narrow:

- one PDF per conversion
- continuous same-page workflow for repeated use
- BPM supplied explicitly by the user
- optional exports: `MusicXML`, `MIDI`, `MP3`
- optional GPU request using HOMR `--gpu auto` with FP16 weights when CUDA is available
- designed to migrate into a standalone GitHub repository with minimal changes

## Context

The current project already contains a usable HOMR-based OMR pipeline:

- HOMR recognition
- conservative MusicXML timing repair
- MIDI time-signature normalization
- explicit BPM injection into MusicXML and MIDI
- conservative normalization of transient key-signature and clef changes
- MuseScore export

That pipeline is currently embedded in the training/data-prep repository and optimized
for bundle-based dataset generation. The new requirement is different: provide a
human-facing tool for ad hoc score conversion, while keeping the path compatible with
future dataset preparation and future extraction into an independent repository.

## Goals

1. Make the existing HOMR pipeline usable from a browser UI.
2. Let the user upload a PDF and immediately inspect the first page before conversion.
3. Require BPM as explicit user input instead of relying on OCR tempo recognition.
4. Allow the user to choose output formats per conversion.
5. Keep the page active after conversion so the next PDF can be processed immediately.
6. Structure the code so the HOMR tool can later move into its own repository with
   shallow dependencies on the current project.

## Non-Goals

1. Batch conversion in the first version.
2. Automatic BPM OCR or confidence scoring.
3. Editing recognized notes in the browser.
4. User accounts, job history, or remote storage.
5. Deep integration with model training or bundle manifests from the browser UI.

## User Story

As a local operator preparing piano-score assets, I want to upload a sheet PDF, inspect
its first page, enter the known BPM, choose the desired exports, and receive converted
outputs without leaving the page, so I can process many scores in sequence with low
friction.

## Product Shape

The tool is a local browser application backed by a Python web server.

### User-facing workflow

1. Open the tool in a browser.
2. Upload one PDF.
3. Wait for the first-page preview to render.
4. Inspect the visible score header and manually enter BPM.
5. Keep or adjust the default time signature value.
6. Select one or more output formats:
   - `MusicXML`
   - `MIDI`
   - `MP3`
7. Click `Convert`.
8. Watch progress through explicit pipeline stages.
9. Download the generated files.
10. Click `Next Score` or upload another PDF directly on the same page.

### Why single-file first

Single-file conversion gives the cleanest UX, matches the current manual workflow, and
avoids introducing queue management before the core tool shape is stable. It also makes
the later standalone repository easier to reason about: one job in, one set of outputs
out.

## UX Design

The page should feel like a compact workbench rather than a marketing site.

### Page layout

Use a two-column desktop layout with a stacked mobile fallback.

Left column:

- upload/drop zone
- first-page preview
- file metadata summary

Right column:

- BPM input
- time signature input, default `4/4`
- output-format checkboxes
- `Convert` button
- progress/status area
- results/download area
- `Next Score` reset action

### Primary states

1. Empty state
   - upload control visible
   - parameter controls disabled until a PDF is loaded

2. Preview-ready state
   - first page rendered
   - BPM and export controls enabled
   - convert action enabled once BPM is valid

3. Converting state
   - upload locked for the active job
   - progress stages shown in order
   - result actions hidden until completion

4. Success state
   - download actions shown for each generated asset
   - conversion summary shown
   - reset action prepares the tool for the next score

5. Failure state
   - clear error summary
   - path to retry with corrected inputs
   - previous preview remains visible for context

### Preview behavior

The first page preview exists to support manual tempo entry and quick visual sanity
checks. It should:

- render automatically after PDF upload
- show the full first page scaled to fit
- support click-to-open full-size preview if convenient
- not require the user to download an intermediate image

### Parameter rules

- `BPM` is required and must be a positive integer
- `Time signature` defaults to `4/4`
- `Use GPU` is opt-in; checked means HOMR `--gpu auto`, which selects CUDA/FP16 when available
  and otherwise keeps HOMR's CPU path
- at least one export format must be selected
- parameter values apply to the current conversion only

### Results behavior

Results should be shown as individual downloadable files, not hidden inside logs.

Preferred outputs:

- repaired `MusicXML`
- normalized `MIDI`
- rendered `MP3` when requested

Optional convenience output:

- `.zip` bundle containing all selected artifacts plus a small JSON report

## System Design

Use a thin web layer over a reusable conversion core.

### High-level architecture

1. `web-ui`
   - browser page
   - upload, preview, status, download interactions

2. `web-api`
   - FastAPI endpoints
   - request validation
   - job orchestration

3. `conversion-core`
   - preview extraction
   - HOMR invocation
   - MusicXML repair
   - MIDI repair
   - MuseScore export
   - optional MP3 rendering

4. `job-workspace`
   - per-job temporary directories
   - artifact collection
   - cleanup policy

### Recommended module boundaries

Within the current repository, create a self-contained tool package with boundaries like:

- `sheet2music/core/preview.py`
- `sheet2music/core/convert.py`
- `sheet2music/core/export.py`
- `sheet2music/core/workspace.py`
- `sheet2music/core/models.py`
- `sheet2music/web/app.py`
- `sheet2music/web/static/...`
- `sheet2music/web/templates/...` or a small SPA build output

The important rule is that UI code should depend on `core`, while `core` should not
depend on dataset bundles, training configs, or PiCoGen-specific logic.

## Pipeline Design

Each conversion job should run this ordered pipeline:

1. Save uploaded PDF into a job workspace.
2. Extract the first page preview as PNG.
3. Render numbered page images from the PDF at 600 DPI.
4. Crop only vertical margins outside detected staff systems, while preserving
   the high-resolution originals under `pages/raw/`.
5. Run HOMR on the cropped page images.
6. Preserve raw page MusicXML for audit.
7. Apply conservative MusicXML repair with:
   - constant time signature
   - constant BPM
   - transient key-signature normalization only when the original baseline returns
   - transient clef normalization per staff only when the original baseline returns
8. Combine page MusicXML into one canonical score.
9. Run final MusicXML repair on the combined score.
10. Export raw MIDI with MuseScore.
11. Normalize MIDI metadata:
    - one constant time signature
    - one constant tempo at tick `0`
12. If requested, render audio and transcode to MP3.
13. Publish selected outputs to the results area.

## File and Artifact Model

For a single conversion job, the tool should keep a local workspace like:

```text
tmp/jobs/<job_id>/
  input/
    score.pdf
  preview/
    page-1.png
  pages/
    page-1.png
    page-2.png
    ...
  homr_raw/
    page-1.musicxml
    ...
  homr_fixed/
    page-1.musicxml
    ...
  output/
    score.musicxml
    score.mid
    score.mp3
    report.json
```

Only the selected outputs need to be exposed in the UI. Intermediate files stay on disk
for the active session and can be cleaned on reset or by a retention policy.

## API Design

The first version can stay simple and synchronous from the user perspective, even if the
server uses background job handling internally.

### Proposed endpoints

- `POST /api/preview`
  - input: PDF file
  - output: preview job id and preview image URL

- `POST /api/convert`
  - input:
    - uploaded PDF reference or file
    - `bpm`
    - `time_signature`
    - `outputs[]`
  - output: conversion job id

- `GET /api/jobs/{job_id}`
  - output:
    - current status
    - current stage
    - error details if failed
    - artifact list when done

- `POST /api/jobs/{job_id}/reset`
  - clears server-side state for that job

- `GET /api/jobs/{job_id}/artifacts/{name}`
  - downloads one artifact

### Job stages

Expose readable stage names:

- `uploaded`
- `preview_ready`
- `running_homr`
- `repairing_musicxml`
- `exporting_midi`
- `rendering_mp3`
- `completed`
- `failed`

## Frontend Design Notes

The browser UI should be intentionally plain and efficient.

### Controls

- drag-and-drop upload area plus file picker
- numeric BPM input
- compact segmented or text input for time signature
- checkbox group for output formats
- primary `Convert` action
- secondary `Reset` or `Next Score` action

### Visual hierarchy

- preview should dominate the left side after upload
- parameters and progress should stay visible without scrolling on common laptop sizes
- result buttons should be immediately scannable

### Accessibility

- keyboard-operable upload and convert flow
- visible labels for all inputs
- progress text not dependent on color only

## Error Handling

The tool must fail in a way that helps the operator recover.

### Expected error classes

1. Invalid input
   - missing PDF
   - invalid BPM
   - no output selected

2. Preview failure
   - PDF cannot be rasterized

3. HOMR failure
   - recognition crash
   - missing output file

4. Export failure
   - MuseScore not installed or fails
   - MP3 rendering/transcoding failure

5. Unsupported score quality
   - recognition output too broken for repair/export

### Recovery behavior

- keep the uploaded file context visible
- show the failed stage clearly
- show a concise operator-readable error summary
- allow retry after parameter changes when meaningful

## Testing Strategy

Testing should scale from core logic outward.

### Core tests

- BPM validation
- export-format validation
- workspace path management
- repair pipeline with explicit tempo injection
- transient key/clef repair keeps persistent changes and does not alter pitch nodes
- MIDI normalization preserves note timing while replacing metadata

### Integration tests

- preview extraction from sample PDF
- end-to-end conversion on a known short fixture
- selective export behavior:
  - `MusicXML` only
  - `MIDI` only
  - `MusicXML + MIDI + MP3`

### UI tests

- upload enables preview
- valid BPM enables conversion
- status updates appear in order
- completed job exposes download actions
- reset returns page to ready-for-next-file state

## Data Preparation Implications

The browser tool should not be the only path to dataset prep, but it should produce
artifacts compatible with the new dataset organization.

### Asset-level direction

Future dataset preparation should treat MIDI as a first-class asset:

- `assets/raw/sheets/...pdf`
- `assets/midi/...mid`

This keeps reusable converted symbolic outputs in `assets`, while `dataset/bundles/...`
holds training-oriented bundle organization and metadata.

### Boundary rule

The browser tool may optionally offer a later feature to copy outputs into `assets`, but
that is not required for the first version. First version success is defined by clean
local conversion and download.

## Migration Path To A Standalone Repository

This design should be implemented as if extraction is expected.

### Keep in current repo for now

- shared path resolution for existing local toolchain
- existing repair scripts and HOMR wrappers
- existing fixture PDFs and tests where useful

### Avoid baking in

- dataset manifest assumptions
- bundle-specific file naming
- training config dependencies
- PiCoGen tokenization steps in the UI flow

### Expected future standalone repository shape

```text
Sheet2Music/
  sheet2music/
    core/
    web/
  tests/
  docs/
  pyproject.toml
  README.md
```

The current project can then either vendor it or invoke it as an external tool.

## Operational Assumptions

1. The tool runs locally, not as a public internet service.
2. HOMR, MuseScore, PDF rasterization tools, and audio tools are available on the host.
3. Conversion jobs are small enough that one-at-a-time local execution is acceptable.
4. The initial user is an internal operator comfortable entering BPM manually.

## Success Criteria

The first version is successful when all of the following are true:

1. A user can open a local browser page and upload one PDF.
2. The page shows the first-page preview before conversion.
3. The user can enter BPM and keep the default `4/4` or override it.
4. The tool can export selected outputs among `MusicXML`, `MIDI`, and `MP3`.
5. The produced `MusicXML` and `MIDI` contain the user-supplied constant BPM.
6. After one conversion completes, the page can be reused immediately for the next file.
7. The implementation boundary is clean enough that the tool can later move to its own
   repository without a large rewrite.

## Recommended Next Step

After this design is approved, write an implementation plan in two tracks:

1. extract the current HOMR repair/export logic into a reusable `core` package
2. build the FastAPI UI shell around that package
