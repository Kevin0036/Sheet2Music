# FluidSynth Rendering Parity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace MuseScore-based audio rendering with the `music-to-midi` compatible FluidSynth 2.5.6 + MuseScore General SoundFont pipeline for PDF, MP3, and video jobs, while exposing one reusable renderer for later GUI playback/editing.

**Architecture:** Keep MuseScore responsible for MusicXML-to-MIDI export only. Add a focused `fluidsynth_renderer` module that accepts any validated MIDI path and produces canonical 44.1 kHz PCM16 stereo WAV, then reuse the existing ffmpeg MP3 encoder. The renderer validates runtime/SoundFont identity, preserves MIDI controllers such as CC64, checks output duration, and returns a structured render result suitable for future preview/edit jobs.

**Tech Stack:** Python 3.11, `mido`, FluidSynth 2.5.6 CLI, MuseScore General SoundFont, ffmpeg, `unittest`, existing Sheet2Music settings/system status.

---

### Task 1: Define runtime and asset contracts

**Files:**
- Modify: `sheet2music/core/settings.py`
- Modify: `sheet2music/core/system.py`
- Test: `tests/test_settings.py`
- Test: `tests/test_system.py`

- [ ] **Step 1: Add failing tests for FluidSynth and SoundFont resolution.** Assert explicit environment overrides win, missing runtime produces a clear error, and valid identity metadata is returned without importing GUI code.
- [ ] **Step 2: Run `python -m unittest tests.test_settings tests.test_system -v`; verify new tests fail because no FluidSynth contract exists.**
- [ ] **Step 3: Add `fluidsynth_binary()`, `fluidsynth_version()`, `soundfont_path()`, and identity constants. Support `SHEET2MUSIC_FLUIDSYNTH`, `SHEET2MUSIC_SOUNDFONT`, and the documented cache paths; validate exact version, file size, and SHA-256.
- [ ] **Step 4: Extend `system_status()` with `fluidsynth` and `soundfont` records and include them in audio readiness, without making PDF-only environments falsely report HOMR failure.**
- [ ] **Step 5: Run the focused tests and commit `feat: add verified FluidSynth runtime contract`.**

### Task 2: Implement reusable MIDI renderer

**Files:**
- Create: `sheet2music/core/fluidsynth_renderer.py`
- Test: `tests/test_fluidsynth_renderer.py`

- [ ] **Step 1: Write failing tests for command construction, PCM16/stereo/44.1 kHz validation, duration coverage, and subprocess failure reporting.**
- [ ] **Step 2: Run the focused test file and confirm failure.**
- [ ] **Step 3: Implement `RenderResult`, `build_fluidsynth_command()`, `render_midi_to_wav()`, and `render_midi_to_mp3()`. Use a temporary `.part.wav`, `fluidsynth -ni -F <wav> -r 44100 <sf2> <midi>`, isolated PATH/DLL environment, `wave` validation, and ffmpeg MP3 encoding. Keep MIDI unmodified so CC64 and other controllers reach FluidSynth.
- [ ] **Step 4: Add deterministic tail coverage: compute final MIDI event seconds with mido, require rendered WAV to cover the last note plus a 30 ms fade margin, and reject truncated output.**
- [x] **Step 5: Run focused tests and commit `feat: add reusable FluidSynth MIDI renderer`.**

### Task 3: Replace all production render calls

**Files:**
- Modify: `sheet2music/core/export.py`
- Modify: `sheet2music/core/audio_transcription.py`
- Test: `tests/test_audio_transcription.py`
- Test: `tests/test_convert.py`

- [ ] **Step 1: Add mocks proving PDF finalization and audio transcription both call the same renderer with `score.mid`.**
- [ ] **Step 2: Run the focused tests and confirm the current MuseScore renderer is still called.**
- [x] **Step 3: Route `render_mp3()` through `fluidsynth_renderer.render_midi_to_mp3()` and keep `render_wav()` only for explicit legacy/debug use. Ensure PDF first exports MIDI with MuseScore, then sends that MIDI to FluidSynth.
- [ ] **Step 4: Ensure audio tasks render the metadata-normalized `score.mid`, preserve `score.raw.mid`, and report renderer/runtime identity.**
- [ ] **Step 5: Run all audio/PDF tests and commit `feat: use FluidSynth for all MIDI audio rendering`.**

### Task 4: Prepare later GUI playback/edit reuse

**Files:**
- Modify: `sheet2music/core/fluidsynth_renderer.py`
- Modify: `docs/transkun-v2-audio-transcription-design.md`
- Modify: `README.md`

- [ ] **Step 1: Add a renderer API contract documenting that GUI preview/edit jobs can call `render_midi_to_wav()` directly without going through conversion jobs.**
- [ ] **Step 2: Record the unified PDF/audio/video rendering milestone, required external assets, and fallback policy in the design document.**
- [ ] **Step 3: Update README with installation/download commands and expected asset paths.**
- [ ] **Step 4: Run complete tests, static checks, `pip check`, and environment status; commit `docs: document unified FluidSynth rendering`.**

### Task 5: Real asset validation

**Files:**
- No source changes unless validation reveals a defect.

- [ ] **Step 1: Prepare or verify FluidSynth 2.5.6 and the official MuseScore General SoundFont using the documented commands.**
- [ ] **Step 2: Render one PDF MIDI and one Transkun V2 MIDI to WAV and MP3; inspect WAV metadata and confirm output files are playable.**
- [ ] **Step 3: Record actual asset paths and validation results in the milestone documentation.**
