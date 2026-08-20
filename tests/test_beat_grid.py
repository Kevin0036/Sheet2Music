import unittest


class BeatGridTest(unittest.TestCase):
    def test_analyzes_grid_with_duplicate_mark_missing_beat_and_four_four_meter(self) -> None:
        from sheet2music.core.beat_grid import analyze_beat_this_grid

        result = analyze_beat_this_grid(
            [0.0, 0.5, 1.0, 1.03, 1.5, 2.0, 3.0, 3.5, 4.0, 4.5, 5.0, 5.5],
            [0.0, 2.0, 4.0],
        )

        self.assertEqual(result["beats"], [0.0, 0.5, 1.0, 1.5, 2.0, 3.0, 3.5, 4.0, 4.5, 5.0, 5.5])
        self.assertEqual(result["cleanup"]["removed_duplicates"], 1)
        self.assertEqual(result["cleanup"]["recovered_missing_beats"], 1)
        self.assertAlmostEqual(result["estimated_bpm"], 120.0)
        self.assertEqual(result["time_signature"], [4, 4])

    def test_rejects_grid_with_fewer_than_eight_beats(self) -> None:
        from sheet2music.core.beat_grid import BeatThisGridError, analyze_beat_this_grid

        with self.assertRaisesRegex(BeatThisGridError, "at least 8"):
            analyze_beat_this_grid([0.0, 0.5, 1.0], [0.0])

    def test_exposes_variable_tempo_map_for_expressive_grid(self) -> None:
        from sheet2music.core.beat_grid import analyze_beat_this_grid

        result = analyze_beat_this_grid(
            [0.0, 0.5, 1.0, 1.8, 2.6, 3.1, 3.6, 4.4, 5.2, 5.7],
            [],
        )

        self.assertGreaterEqual(len(result["tempo_map"]), 2)
        self.assertEqual(result["tempo_map"][0][0], 0.0)
