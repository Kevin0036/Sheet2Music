"""数据模型与输入校验：BPM / 拍号 / 导出格式。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from enum import Enum

# 合法导出格式（对应设计文档的 MusicXML / MIDI / MP3 与可选 zip 打包）。
VALID_OUTPUT_FORMATS = ("musicxml", "midi", "mp3", "zip")


class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    AWAITING_REVIEW = "awaiting_review"
    COMPLETED = "completed"
    FAILED = "failed"


class ValidationError(ValueError):
    """用户输入的转换参数不合法时抛出。"""


def parse_time_signature(value: str) -> tuple[int, int]:
    try:
        beats_text, beat_type_text = value.split("/", maxsplit=1)
        beats = int(beats_text)
        beat_type = int(beat_type_text)
    except (ValueError, IndexError) as exc:
        raise ValidationError(f"Invalid time signature: {value!r}") from exc
    if beats <= 0 or beat_type <= 0:
        raise ValidationError(f"Invalid time signature: {value}")
    return beats, beat_type


def validate_bpm(value: object) -> int:
    if value is None:
        raise ValidationError("BPM 为必填项")
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValidationError(f"BPM 必须是正整数，收到: {value!r}")
    return value


def validate_outputs(outputs: object) -> list[str]:
    if not isinstance(outputs, (list, tuple)):
        raise ValidationError("outputs 必须是数组")
    if not outputs:
        raise ValidationError("至少选择一种输出格式")
    cleaned: list[str] = []
    for fmt in outputs:
        if fmt not in VALID_OUTPUT_FORMATS:
            raise ValidationError(f"未知的输出格式: {fmt}")
        if fmt not in cleaned:
            cleaned.append(fmt)
    return cleaned


@dataclass
class ConvertParams:
    bpm: int
    time_signature: str = "4/4"
    outputs: list[str] = field(default_factory=list)
    use_gpu: bool = False
    transkun_model: str = "v2"
    has_pickup_measure: bool = False
    structure_plan: dict[str, object] | None = None
    generate_pdf: bool = False

    @classmethod
    def validate(
        cls,
        bpm: object,
        time_signature: object,
        outputs: object,
        use_gpu: object = False,
        structure_plan: object = None,
        has_pickup_measure: object = False,
        transkun_model: object = "v2",
        generate_pdf: object = False,
    ) -> "ConvertParams":
        validated_bpm = validate_bpm(bpm)
        parse_time_signature(str(time_signature))
        validated_outputs = validate_outputs(outputs)
        if not isinstance(use_gpu, bool):
            raise ValidationError(f"use_gpu 必须是布尔值，收到: {use_gpu!r}")
        if not isinstance(has_pickup_measure, bool):
            raise ValidationError("has_pickup_measure 必须是布尔值")
        if not isinstance(generate_pdf, bool):
            raise ValidationError("generate_pdf 必须是布尔值")
        if transkun_model not in {"v2", "v2_aug"}:
            raise ValidationError(f"未知的 Transkun 模型: {transkun_model!r}")
        validated_structure_plan: dict[str, object] | None = None
        if structure_plan is not None:
            if not isinstance(structure_plan, Mapping):
                raise ValidationError("structure_plan must be an object")
            from .structure import ScoreStructurePlan

            try:
                ScoreStructurePlan.from_dict(
                    structure_plan,
                    fallback_time_signature=str(time_signature),
                )
            except ValueError as exc:
                raise ValidationError(f"invalid structure_plan: {exc}") from exc
            validated_structure_plan = dict(structure_plan)

        return cls(
            bpm=validated_bpm,
            time_signature=str(time_signature),
            outputs=validated_outputs,
            use_gpu=use_gpu,
            transkun_model=str(transkun_model),
            has_pickup_measure=has_pickup_measure,
            structure_plan=validated_structure_plan,
            generate_pdf=generate_pdf,
        )

    def to_dict(self) -> dict[str, object]:
        return asdict(self)
