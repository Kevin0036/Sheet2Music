import unittest

from sheet2music.core.structure import ScoreStructurePlan


class ScoreStructurePlanTest(unittest.TestCase):
    def test_resolves_time_signatures_clefs_and_key_signature(self) -> None:
        plan = ScoreStructurePlan.from_dict(
            {
                "default_time_signature": "4/4",
                "time_signature_changes": [
                    {"from_measure": 25, "to_measure": 25, "signature": "2/4"},
                    {"from_measure": 26, "signature": "4/4"},
                ],
                "clef_overrides": [
                    {"staff": 2, "from_measure": 14, "to_measure": 16, "sign": "G", "line": 2},
                    {"staff": 2, "from_measure": 17, "sign": "F", "line": 4},
                ],
                "key_signature": {"fifths": -5},
            }
        )

        self.assertEqual(plan.time_signature_for(24), (4, 4))
        self.assertEqual(plan.time_signature_for(25), (2, 4))
        self.assertEqual(plan.time_signature_for(26), (4, 4))
        self.assertEqual(plan.clef_for(2, 13), None)
        self.assertEqual(plan.clef_for(2, 15), ("G", 2))
        self.assertEqual(plan.clef_for(2, 17), ("F", 4))
        self.assertEqual(plan.key_signature_fifths, -5)

    def test_round_trips_plan_dict_without_open_range_sentinel(self) -> None:
        source = {
            "default_time_signature": "3/4",
            "time_signature_changes": [
                {"from_measure": 5, "to_measure": 6, "signature": "6/8"},
            ],
            "clef_overrides": [
                {"staff": 1, "from_measure": 3, "to_measure": 4, "sign": "C", "line": 3},
            ],
            "key_signature": {"fifths": 2},
        }

        plan = ScoreStructurePlan.from_dict(source)

        self.assertEqual(plan.to_dict(), source)

    def test_rejects_overlapping_ranges_and_invalid_values(self) -> None:
        invalid_plans = [
            {
                "time_signature_changes": [
                    {"from_measure": 2, "signature": "4/4"},
                    {"from_measure": 3, "signature": "3/4"},
                ]
            },
            {
                "clef_overrides": [
                    {"staff": 2, "from_measure": 4, "to_measure": 8, "sign": "G", "line": 2},
                    {"staff": 2, "from_measure": 8, "sign": "F", "line": 4},
                ]
            },
            {"default_time_signature": "0/4"},
            {"clef_overrides": [{"staff": 0, "from_measure": 1, "sign": "G", "line": 2}]},
            {"key_signature": {"fifths": 8}},
        ]

        for value in invalid_plans:
            with self.assertRaises(ValueError):
                ScoreStructurePlan.from_dict(value)


if __name__ == "__main__":
    unittest.main()
