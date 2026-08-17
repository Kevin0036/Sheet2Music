import unittest

from sheet2music.core.models import (
    ConvertParams,
    JobStatus,
    ValidationError,
    parse_time_signature,
    validate_bpm,
)


class ModelsTest(unittest.TestCase):
    def test_validate_bpm_accepts_positive_int(self) -> None:
        self.assertEqual(validate_bpm(120), 120)

    def test_validate_bpm_rejects_bad_values(self) -> None:
        for bad in (None, 0, -5, True, "120"):
            with self.assertRaises(ValidationError):
                validate_bpm(bad)

    def test_parse_time_signature_ok(self) -> None:
        self.assertEqual(parse_time_signature("4/4"), (4, 4))
        self.assertEqual(parse_time_signature("3/4"), (3, 4))
        self.assertEqual(parse_time_signature("6/8"), (6, 8))

    def test_parse_time_signature_rejects_bad_values(self) -> None:
        for bad in ("4", "0/4", "4/0", "-1/4", "abc", "", "4/4/4"):
            with self.assertRaises((ValidationError, ValueError)):
                parse_time_signature(bad)

    def test_validate_outputs(self) -> None:
        self.assertEqual(ConvertParams.validate(120, "4/4", ["midi"]).outputs, ["midi"])

    def test_validate_gpu_preference(self) -> None:
        self.assertFalse(ConvertParams.validate(120, "4/4", ["midi"]).use_gpu)
        self.assertTrue(ConvertParams.validate(120, "4/4", ["midi"], use_gpu=True).use_gpu)

    def test_validate_outputs_rejects_empty_and_unknown(self) -> None:
        with self.assertRaises(ValidationError):
            ConvertParams.validate(120, "4/4", [])
        with self.assertRaises(ValidationError):
            ConvertParams.validate(120, "4/4", ["wav"])
        with self.assertRaises(ValidationError):
            ConvertParams.validate(120, "4/4", "not-a-list")

    def test_validate_rejects_bad_bpm_and_signature(self) -> None:
        with self.assertRaises(ValidationError):
            ConvertParams.validate(0, "4/4", ["midi"])
        with self.assertRaises(ValidationError):
            ConvertParams.validate(120, "7/0", ["midi"])

    def test_to_dict(self) -> None:
        params = ConvertParams.validate(90, "3/4", ["musicxml", "mp3"])
        self.assertEqual(params.to_dict()["bpm"], 90)
        self.assertEqual(params.to_dict()["time_signature"], "3/4")

    def test_validate_accepts_structure_plan(self) -> None:
        params = ConvertParams.validate(
            80,
            "4/4",
            ["midi"],
            structure_plan={
                "time_signature_changes": [
                    {"from_measure": 25, "to_measure": 25, "signature": "2/4"},
                ]
            },
        )

        self.assertEqual(params.structure_plan["time_signature_changes"][0]["signature"], "2/4")

    def test_validate_rejects_non_mapping_structure_plan(self) -> None:
        with self.assertRaises(ValidationError):
            ConvertParams.validate(80, "4/4", ["midi"], structure_plan=["4/4"])

    def test_job_status_includes_awaiting_review(self) -> None:
        self.assertEqual(JobStatus.AWAITING_REVIEW.value, "awaiting_review")


if __name__ == "__main__":
    unittest.main()
