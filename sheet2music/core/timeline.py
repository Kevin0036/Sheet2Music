"""Read-only MusicXML timing analysis with exact beat normalization."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from fractions import Fraction


_NOTE_BEATS = {
    "maxima": Fraction(32),
    "long": Fraction(16),
    "breve": Fraction(8),
    "whole": Fraction(4),
    "half": Fraction(2),
    "quarter": Fraction(1),
    "eighth": Fraction(1, 2),
    "16th": Fraction(1, 4),
    "32nd": Fraction(1, 8),
    "64th": Fraction(1, 16),
    "128th": Fraction(1, 32),
    "256th": Fraction(1, 64),
    "512th": Fraction(1, 128),
    "1024th": Fraction(1, 256),
}


@dataclass(frozen=True)
class TimedNote:
    element: ET.Element
    onset_units: int
    duration_units: int
    end_units: int
    voice: str
    staff: int | None
    is_chord: bool
    is_grace: bool


@dataclass(frozen=True)
class TimingDiagnostic:
    code: str
    child_index: int
    message: str


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
    if divisions <= 0:
        raise ValueError("divisions must be positive")
    return Fraction(units, divisions)


def fraction_text(value: Fraction) -> str:
    if value.denominator == 1:
        return str(value.numerator)
    return f"{value.numerator}/{value.denominator}"


def expected_measure_units(divisions: int, beats: int, beat_type: int) -> int:
    if divisions <= 0 or beats <= 0 or beat_type <= 0:
        raise ValueError("divisions and time-signature values must be positive")
    value = Fraction(divisions * beats * 4, beat_type)
    if value.denominator != 1:
        raise ValueError(
            f"time signature {beats}/{beat_type} is incompatible with divisions={divisions}"
        )
    return value.numerator


def notated_duration_units(note: ET.Element, divisions: int) -> int | None:
    if divisions <= 0:
        return None
    duration = _NOTE_BEATS.get(note.findtext("type", ""))
    if duration is None:
        return None

    dot_count = len(note.findall("dot"))
    dot_multiplier = sum(
        (Fraction(1, 2**index) for index in range(dot_count + 1)),
        Fraction(0),
    )

    modification = note.find("time-modification")
    tuplet_multiplier = Fraction(1)
    if modification is not None:
        actual = _positive_int(modification.findtext("actual-notes"))
        normal = _positive_int(modification.findtext("normal-notes"))
        if actual is None or normal is None:
            return None
        tuplet_multiplier = Fraction(normal, actual)

    units = duration * dot_multiplier * tuplet_multiplier * divisions
    return units.numerator if units.denominator == 1 else None


def analyze_measure(
    measure: ET.Element,
    divisions: int,
    beats: int,
    beat_type: int,
) -> MeasureTimeline:
    expected_units = expected_measure_units(divisions, beats, beat_type)
    cursor = 0
    maximum_note_end = 0
    previous_non_chord_onset = 0
    events: list[TimedNote] = []
    diagnostics: list[TimingDiagnostic] = []

    for child_index, child in enumerate(measure):
        if child.tag == "note":
            is_chord = child.find("chord") is not None
            is_grace = child.find("grace") is not None
            duration = 0 if is_grace else _duration(child, child_index, diagnostics)
            onset = previous_non_chord_onset if is_chord else cursor
            if not is_chord:
                previous_non_chord_onset = onset
            end = onset + duration
            staff = _positive_int(child.findtext("staff"))
            events.append(
                TimedNote(
                    element=child,
                    onset_units=onset,
                    duration_units=duration,
                    end_units=end,
                    voice=child.findtext("voice", "1"),
                    staff=staff,
                    is_chord=is_chord,
                    is_grace=is_grace,
                )
            )
            maximum_note_end = max(maximum_note_end, end)
            if not is_chord and not is_grace:
                cursor = end
        elif child.tag in {"backup", "forward"}:
            duration = _duration(child, child_index, diagnostics)
            cursor += -duration if child.tag == "backup" else duration
            if cursor < 0:
                diagnostics.append(
                    TimingDiagnostic(
                        code="negative_cursor",
                        child_index=child_index,
                        message=f"{child.tag} moves the MusicXML cursor below zero",
                    )
                )

    return MeasureTimeline(
        divisions=divisions,
        expected_units=expected_units,
        final_cursor_units=cursor,
        maximum_note_end_units=maximum_note_end,
        events=tuple(events),
        diagnostics=tuple(diagnostics),
    )


def _duration(
    element: ET.Element,
    child_index: int,
    diagnostics: list[TimingDiagnostic],
) -> int:
    raw = element.findtext("duration")
    try:
        duration = int(raw) if raw is not None else 0
    except ValueError:
        duration = 0
    if raw is None or duration < 0 or (raw is not None and not raw.lstrip("-").isdigit()):
        diagnostics.append(
            TimingDiagnostic(
                code="invalid_duration",
                child_index=child_index,
                message=f"{element.tag} has an invalid duration",
            )
        )
        return 0
    return duration


def _positive_int(value: str | None) -> int | None:
    try:
        parsed = int(value) if value is not None else 0
    except ValueError:
        return None
    return parsed if parsed > 0 else None
