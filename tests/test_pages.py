"""pages.py 单元测试（无需 pdftoppm）。"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import cv2
import numpy as np

from sheet2music.core.pages import (
    crop_page_vertically,
    detect_music_vertical_bounds,
    export_numbered_pages,
    numbered_page_paths,
)


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

    def test_crop_page_writes_geometry_for_mapping_back_to_raw_page(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "raw.png"
            target = root / "page.png"
            geometry = root / "page.json"
            image = np.full((100, 80, 3), 255, dtype=np.uint8)
            cv2.imwrite(str(source), image)

            with mock.patch(
                "sheet2music.core.pages.detect_music_vertical_bounds",
                return_value=(10, 90),
            ):
                crop_page_vertically(source, target, geometry_path=geometry)

            self.assertEqual(
                json.loads(geometry.read_text(encoding="utf-8")),
                {
                    "schema_version": 1,
                    "raw_size": {"width": 80, "height": 100},
                    "input_bounds_in_raw": [0, 10, 80, 90],
                    "input_size": {"width": 80, "height": 80},
                },
            )

    def test_existing_pages_rebuild_missing_geometry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            pages_dir = Path(temp_dir)
            raw_dir = pages_dir / "raw"
            raw_dir.mkdir()
            raw = np.full((100, 80, 3), 255, dtype=np.uint8)
            cv2.imwrite(str(raw_dir / "page-1.png"), raw)
            cv2.imwrite(str(pages_dir / "page-1.png"), raw[10:90, :])

            with mock.patch(
                "sheet2music.core.pages.detect_music_vertical_bounds",
                return_value=(10, 90),
            ):
                paths = export_numbered_pages(Path("unused.pdf"), pages_dir)

            self.assertEqual(paths, [pages_dir / "page-1.png"])
            self.assertTrue((pages_dir / "geometry" / "page-1.json").exists())


if __name__ == "__main__":
    unittest.main()
