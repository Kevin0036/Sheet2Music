"""Conservative mapping between internal measure ordinals and printed numbers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Iterable, Mapping

import numpy as np


@dataclass(frozen=True)
class MeasureNumberAnchor:
    """A trusted row-start observation from the printed score."""

    system_index: int
    measure_ordinal: int
    display_measure_number: int


@dataclass(frozen=True)
class MeasureNumberMapping:
    """An immutable, constant-offset mapping for one score region."""

    ordinal_start: int
    ordinal_end: int
    offset: int
    confidence: str

    def display_for_ordinal(self, measure_ordinal: int) -> int | None:
        if not self.ordinal_start <= measure_ordinal <= self.ordinal_end:
            return None
        return measure_ordinal + self.offset

    def ordinal_for_display(self, display_measure_number: int) -> int | None:
        ordinal = display_measure_number - self.offset
        if not self.ordinal_start <= ordinal <= self.ordinal_end:
            return None
        return ordinal


_PLAIN_INTEGER = re.compile(r"^[0-9]+$")


def parse_ocr_measure_number(text: str | None) -> int | None:
    """Return a number only when OCR produced exactly one plain integer."""
    if text is None:
        return None
    value = text.strip()
    if not _PLAIN_INTEGER.fullmatch(value):
        return None
    number = int(value)
    return number if number > 0 else None


def extract_single_ocr_number(
    result: object,
    *,
    minimum_score: float = 0.80,
) -> int | None:
    """Extract one conservative number from a RapidOCR-like result object."""
    texts = getattr(result, "txts", None)
    scores = getattr(result, "scores", None)
    if texts is None or scores is None:
        if isinstance(result, Mapping):
            texts = result.get("txts")
            scores = result.get("scores")
    if not isinstance(texts, (list, tuple)) or not isinstance(scores, (list, tuple)):
        return None
    candidates = [
        parsed
        for text, score in zip(texts, scores, strict=False)
        if isinstance(score, (int, float))
        and score >= minimum_score
        and (parsed := parse_ocr_measure_number(str(text))) is not None
    ]
    return candidates[0] if len(candidates) == 1 else None


def recognize_row_anchor(
    image: np.ndarray,
    *,
    system_index: int,
    measure_ordinal: int,
    bbox: tuple[int, int, int, int],
    reader: Callable[[np.ndarray], object],
) -> MeasureNumberAnchor | None:
    """OCR a caller-selected row header and return only a unique number."""
    left, top, right, bottom = bbox
    height, width = image.shape[:2]
    left, top = max(0, left), max(0, top)
    right, bottom = min(width, right), min(height, bottom)
    if right <= left or bottom <= top:
        return None
    number = extract_single_ocr_number(reader(image[top:bottom, left:right]))
    if number is None:
        return None
    return MeasureNumberAnchor(system_index, measure_ordinal, number)


def build_number_mapping(
    anchors: Iterable[MeasureNumberAnchor],
    *,
    ordinal_start: int,
    ordinal_end: int,
) -> MeasureNumberMapping:
    """Build a mapping only when all row anchors agree on one offset.

    A missing or conflicting anchor is deliberately an error. Callers should
    keep using internal ordinals and require review instead of guessing.
    """
    if ordinal_start <= 0 or ordinal_end < ordinal_start:
        raise ValueError("ordinal range must be positive and ordered")
    values = tuple(anchors)
    if not values:
        raise ValueError("at least one printed-number anchor is required")

    offsets: set[int] = set()
    for anchor in values:
        if not 0 <= anchor.system_index:
            raise ValueError("system_index must be non-negative")
        if not ordinal_start <= anchor.measure_ordinal <= ordinal_end:
            raise ValueError("anchor ordinal is outside the requested range")
        if anchor.display_measure_number <= 0:
            raise ValueError("display measure number must be positive")
        offsets.add(anchor.display_measure_number - anchor.measure_ordinal)

    if len(offsets) != 1:
        raise ValueError("printed-number anchors disagree")
    return MeasureNumberMapping(
        ordinal_start=ordinal_start,
        ordinal_end=ordinal_end,
        offset=offsets.pop(),
        confidence="high",
    )
