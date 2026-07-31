"""pages.py 单元测试（无需 pdftoppm）。"""

import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np

from sheet2music.core.pages import detect_music_vertical_bounds, numbered_page_paths


class NumberedPagePathsTest(unittest.TestCase):
    def test_matches_zero_padded_and_single_digit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "page-01.png").touch()
            (directory / "page-2.png").touch()
            (directory / "page-10.png").touch()
            (directory / "page-1-cut.png").touch()  # 实验衍生图应忽略
            (directory / "other.png").touch()
            names = [path.name for path in numbered_page_paths(directory)]
        self.assertEqual(names, ["page-01.png", "page-2.png", "page-10.png"])


class MusicCropTest(unittest.TestCase):
    def test_detects_staff_region_and_keeps_vertical_margin(self) -> None:
        image = np.full((500, 800, 3), 255, dtype=np.uint8)
        # Header/footer ink should not define the music region.
        cv2.putText(image, "TITLE", (280, 50), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 0, 0), 2)
        for staff_top in (180, 300):
            for line in range(5):
                y = staff_top + line * 10
                cv2.line(image, (80, y), (720, y), (0, 0, 0), 1)
        cv2.putText(image, "FOOTER", (280, 470), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 0, 0), 2)

        top, bottom = detect_music_vertical_bounds(image)

        self.assertGreaterEqual(top, 100)
        self.assertLess(bottom, 450)
        self.assertLess(top, 180)
        self.assertGreater(bottom, 340)

    def test_returns_full_page_when_staff_lines_are_not_detected(self) -> None:
        image = np.full((100, 160, 3), 255, dtype=np.uint8)

        self.assertEqual(detect_music_vertical_bounds(image), (0, 100))


if __name__ == "__main__":
    unittest.main()
