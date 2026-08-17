"""Validated score-level structure plans used by repair and review flows."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from .models import parse_time_signature


class StructurePlanError(ValueError):
    """Raised when a score structure plan cannot be applied safely."""


@dataclass(frozen=True)
class TimeSignatureChange:
    from_measure: int
    to_measure: int | None
    signature: str
    beats: int
    beat_type: int


@dataclass(frozen=True)
class ClefOverride:
    staff: int
    from_measure: int
    to_measure: int | None
    sign: str
    line: int


@dataclass(frozen=True)
class ScoreStructurePlan:
    default_time_signature: str = "4/4"
    time_signature_changes: tuple[TimeSignatureChange, ...] = ()
    clef_overrides: tuple[ClefOverride, ...] = ()
    key_signature_fifths: int | None = None

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, object],
        fallback_time_signature: str = "4/4",
    ) -> "ScoreStructurePlan":
        if not isinstance(value, Mapping):
            raise StructurePlanError("structure_plan must be an object")

        default_value = value.get("default_time_signature", fallback_time_signature)
        if not isinstance(default_value, str):
            raise StructurePlanError("default_time_signature must be a string")
        default_beats, default_beat_type = _parse_signature(default_value)
        default_signature = f"{default_beats}/{default_beat_type}"

        time_signature_changes = tuple(
            sorted(
                (_parse_time_signature_change(item) for item in _sequence(value, "time_signature_changes")),
                key=lambda item: item.from_measure,
            )
        )
        _validate_non_overlapping(
            time_signature_changes,
            lambda item: (0, item.from_measure, item.to_measure),
            "time signature changes",
        )

        clef_overrides = tuple(
            sorted(
                (_parse_clef_override(item) for item in _sequence(value, "clef_overrides")),
                key=lambda item: (item.staff, item.from_measure),
            )
        )
        _validate_non_overlapping(
            clef_overrides,
            lambda item: (item.staff, item.from_measure, item.to_measure),
            "clef overrides",
        )

        key_signature_fifths = _parse_key_signature(value.get("key_signature"))
        return cls(
            default_time_signature=default_signature,
            time_signature_changes=time_signature_changes,
            clef_overrides=clef_overrides,
            key_signature_fifths=key_signature_fifths,
        )

    def time_signature_for(self, measure_number: int) -> tuple[int, int]:
        _validate_measure_number(measure_number)
        for change in self.time_signature_changes:
            if _contains(change.from_measure, change.to_measure, measure_number):
                return change.beats, change.beat_type
        return parse_time_signature(self.default_time_signature)

    def clef_for(self, staff: int, measure_number: int) -> tuple[str, int] | None:
        _positive_int(staff, "staff")
        _validate_measure_number(measure_number)
        for override in self.clef_overrides:
            if override.staff == staff and _contains(
                override.from_measure, override.to_measure, measure_number
            ):
                return override.sign, override.line
        return None

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "default_time_signature": self.default_time_signature,
            "time_signature_changes": [
                _time_signature_change_to_dict(change) for change in self.time_signature_changes
            ],
            "clef_overrides": [_clef_override_to_dict(override) for override in self.clef_overrides],
        }
        if self.key_signature_fifths is not None:
            result["key_signature"] = {"fifths": self.key_signature_fifths}
        return result


def coerce_structure_plan(
    value: ScoreStructurePlan | Mapping[str, object] | None,
    fallback_time_signature: str = "4/4",
) -> ScoreStructurePlan:
    if value is None:
        beats, beat_type = _parse_signature(fallback_time_signature)
        return ScoreStructurePlan(default_time_signature=f"{beats}/{beat_type}")
    if isinstance(value, ScoreStructurePlan):
        return value
    return ScoreStructurePlan.from_dict(value, fallback_time_signature=fallback_time_signature)


def _parse_signature(value: str) -> tuple[int, int]:
    try:
        beats, beat_type = parse_time_signature(value)
    except (TypeError, ValueError) as exc:
        raise StructurePlanError(f"invalid time signature: {value!r}") from exc
    return beats, beat_type


def _parse_time_signature_change(value: object) -> TimeSignatureChange:
    mapping = _as_mapping(value, "time signature change")
    from_measure = _measure_from_mapping(mapping)
    to_measure = _measure_to_mapping(mapping)
    signature_value = mapping.get("signature")
    if not isinstance(signature_value, str):
        raise StructurePlanError("time signature change signature must be a string")
    beats, beat_type = _parse_signature(signature_value)
    return TimeSignatureChange(
        from_measure=from_measure,
        to_measure=to_measure,
        signature=f"{beats}/{beat_type}",
        beats=beats,
        beat_type=beat_type,
    )


def _parse_clef_override(value: object) -> ClefOverride:
    mapping = _as_mapping(value, "clef override")
    staff = _positive_mapping_int(mapping, "staff")
    from_measure = _measure_from_mapping(mapping)
    to_measure = _measure_to_mapping(mapping)
    sign_value = mapping.get("sign")
    if not isinstance(sign_value, str) or sign_value.upper() not in {"G", "F", "C"}:
        raise StructurePlanError("clef override sign must be G, F, or C")
    line = _positive_mapping_int(mapping, "line")
    return ClefOverride(
        staff=staff,
        from_measure=from_measure,
        to_measure=to_measure,
        sign=sign_value.upper(),
        line=line,
    )


def _parse_key_signature(value: object) -> int | None:
    if value is None:
        return None
    mapping = _as_mapping(value, "key signature")
    fifths = mapping.get("fifths")
    if isinstance(fifths, bool) or not isinstance(fifths, int) or not -7 <= fifths <= 7:
        raise StructurePlanError("key signature fifths must be an integer from -7 to 7")
    return fifths


def _sequence(value: Mapping[str, object], key: str) -> list[object]:
    raw = value.get(key, [])
    if not isinstance(raw, (list, tuple)):
        raise StructurePlanError(f"{key} must be a list")
    return list(raw)


def _as_mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise StructurePlanError(f"{label} must be an object")
    return value


def _measure_from_mapping(value: Mapping[str, object]) -> int:
    return _positive_mapping_int(value, "from_measure")


def _measure_to_mapping(value: Mapping[str, object]) -> int | None:
    raw = value.get("to_measure")
    if raw is None:
        return None
    to_measure = _positive_int(raw, "to_measure")
    from_measure = _positive_mapping_int(value, "from_measure")
    if to_measure < from_measure:
        raise StructurePlanError("to_measure must not be before from_measure")
    return to_measure


def _positive_mapping_int(value: Mapping[str, object], key: str) -> int:
    if key not in value:
        raise StructurePlanError(f"{key} is required")
    return _positive_int(value[key], key)


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise StructurePlanError(f"{label} must be a positive integer")
    return value


def _validate_measure_number(value: int) -> None:
    _positive_int(value, "measure_number")


def _contains(from_measure: int, to_measure: int | None, measure_number: int) -> bool:
    return from_measure <= measure_number and (to_measure is None or measure_number <= to_measure)


def _validate_non_overlapping(items: tuple[object, ...], key, label: str) -> None:
    previous_by_group: dict[object, tuple[int, int | None]] = {}
    for item in items:
        group, from_measure, to_measure = key(item)
        previous = previous_by_group.get(group)
        if previous is not None:
            _, previous_to = previous
            if previous_to is None or from_measure <= previous_to:
                raise StructurePlanError(f"overlapping {label}")
        previous_by_group[group] = (from_measure, to_measure)


def _time_signature_change_to_dict(change: TimeSignatureChange) -> dict[str, object]:
    result: dict[str, object] = {
        "from_measure": change.from_measure,
        "signature": change.signature,
    }
    if change.to_measure is not None:
        result["to_measure"] = change.to_measure
    return result


def _clef_override_to_dict(override: ClefOverride) -> dict[str, object]:
    result: dict[str, object] = {
        "staff": override.staff,
        "from_measure": override.from_measure,
        "sign": override.sign,
        "line": override.line,
    }
    if override.to_measure is not None:
        result["to_measure"] = override.to_measure
    return result
