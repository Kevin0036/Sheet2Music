"""Non-destructive Beat This grid cleanup and global metadata inference."""

from __future__ import annotations

import math
from typing import Sequence

import numpy as np

MIN_BEATS = 8
LOCAL_GAP_RADIUS = 5
DUPLICATE_GAP_FRACTION = 0.5
MISSING_BEAT_TOLERANCE = 0.20
MAX_INTERVAL_BEAT_COUNT = 8
MIN_SINGLE_INTERVAL_FRACTION = 0.60
MIN_METER_AGREEMENT = 0.60
MIN_BEATS_PER_BAR = 2
MAX_BEATS_PER_BAR = 12
DOWNBEAT_MATCH_FRACTION = 0.35


class BeatThisGridError(RuntimeError):
    """Beat This did not return a grid suitable for MIDI metadata."""


def _marks(values: Sequence[float], label: str) -> np.ndarray:
    marks = np.asarray(values, dtype=float).reshape(-1)
    if marks.size and (not np.all(np.isfinite(marks)) or np.any(marks < 0.0)):
        raise BeatThisGridError(f"{label} contain non-finite or negative times")
    if marks.size > 1 and np.any(np.diff(marks) < 0.0):
        raise BeatThisGridError(f"{label} are not ordered by time")
    return marks


def _local_spacing(times: np.ndarray, index: int) -> float:
    gaps = np.diff(times)
    lo = max(0, index - LOCAL_GAP_RADIUS)
    hi = min(gaps.size, index + LOCAL_GAP_RADIUS + 1)
    positive = gaps[lo:hi][gaps[lo:hi] > 0.0]
    if positive.size == 0:
        positive = gaps[gaps > 0.0]
    if positive.size == 0:
        raise BeatThisGridError("Beat grid has no positive interval")
    return float(np.median(positive))


def _remove_competing_marks(raw: np.ndarray) -> tuple[np.ndarray, int]:
    if raw.size < MIN_BEATS:
        raise BeatThisGridError(f"Beat This detected only {raw.size} beats; at least {MIN_BEATS} are required")
    kept: list[float] = []
    index = 0
    while index < raw.size:
        spacing = _local_spacing(raw, min(index, raw.size - 2))
        cluster_end = index + 1
        while cluster_end < raw.size and raw[cluster_end] - raw[cluster_end - 1] < DUPLICATE_GAP_FRACTION * spacing:
            cluster_end += 1
        cluster = raw[index:cluster_end]
        if cluster.size == 1 or not kept:
            selected = float(cluster[0])
        else:
            selected = float(min(cluster, key=lambda mark: abs(float(mark) - (kept[-1] + spacing))))
        kept.append(selected)
        index = cluster_end
    cleaned = np.asarray(kept, dtype=float)
    if cleaned.size < MIN_BEATS:
        raise BeatThisGridError(f"Only {cleaned.size} beats remain after duplicate cleanup")
    return cleaned, int(raw.size - cleaned.size)


def _count_positions(times: np.ndarray) -> tuple[np.ndarray, np.ndarray, int]:
    gaps = np.diff(times)
    if np.any(gaps <= 0.0):
        raise BeatThisGridError("Clean Beat This beat marks are not strictly increasing")
    interval_counts = np.ones(gaps.size, dtype=int)
    for index, gap in enumerate(gaps):
        spacing = _local_spacing(times, index)
        count = int(round(float(gap) / spacing))
        if 2 <= count <= MAX_INTERVAL_BEAT_COUNT and abs(gap / count - spacing) <= MISSING_BEAT_TOLERANCE * spacing:
            interval_counts[index] = count
    if float(np.mean(interval_counts == 1)) < MIN_SINGLE_INTERVAL_FRACTION:
        interval_counts.fill(1)
    positions = np.concatenate([np.array([0], dtype=int), np.cumsum(interval_counts)])
    return positions, interval_counts, int(np.sum(interval_counts - 1))


def _infer_time_signature(times: np.ndarray, positions: np.ndarray, downbeats: np.ndarray) -> tuple[list[int] | None, list[float]]:
    if downbeats.size < 3:
        return None, downbeats.tolist()
    matched: list[int] = []
    accepted: list[float] = []
    for downbeat in downbeats:
        nearest = int(np.argmin(np.abs(times - downbeat)))
        if abs(float(times[nearest] - downbeat)) > DOWNBEAT_MATCH_FRACTION * _local_spacing(times, min(nearest, times.size - 2)):
            continue
        position = int(positions[nearest])
        if not matched or position != matched[-1]:
            matched.append(position)
            accepted.append(float(downbeat))
    if len(matched) < 3:
        return None, accepted
    gaps = np.diff(np.asarray(matched))
    eligible = gaps[(gaps >= MIN_BEATS_PER_BAR) & (gaps <= MAX_BEATS_PER_BAR)]
    if eligible.size == 0:
        return None, accepted
    values, counts = np.unique(eligible, return_counts=True)
    winner = int(np.argmax(counts))
    if counts[winner] / gaps.size < MIN_METER_AGREEMENT:
        return None, accepted
    return [int(values[winner]), 4], accepted


def analyze_beat_this_grid(beats: Sequence[float], downbeats: Sequence[float]) -> dict[str, object]:
    """Clean Beat This marks and infer global BPM/meter without moving notes."""

    raw_beats = _marks(beats, "Beat This beat marks")
    raw_downbeats = _marks(downbeats, "Beat This downbeat marks")
    cleaned, removed_duplicates = _remove_competing_marks(raw_beats)
    positions, interval_counts, recovered_missing_beats = _count_positions(cleaned)
    slope, intercept = np.polyfit(positions.astype(float), cleaned, 1)
    if not math.isfinite(float(slope)) or slope <= 0.0:
        raise BeatThisGridError("Beat-position regression produced an invalid tempo")
    residuals = cleaned - (float(intercept) + float(slope) * positions)
    time_signature, accepted_downbeats = _infer_time_signature(cleaned, positions, raw_downbeats)
    return {
        "schema_version": 2,
        "raw_beats": raw_beats.tolist(),
        "raw_downbeats": raw_downbeats.tolist(),
        "beats": cleaned.tolist(),
        "downbeats": accepted_downbeats,
        "estimated_bpm": 60.0 / float(slope),
        "time_signature": time_signature,
        "cleanup": {
            "removed_duplicates": removed_duplicates,
            "recovered_missing_beats": recovered_missing_beats,
            "residual_rms_seconds": float(np.sqrt(np.mean(np.square(residuals)))),
            "interval_beat_counts": interval_counts.tolist(),
        },
    }
