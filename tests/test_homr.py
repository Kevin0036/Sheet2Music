import unittest
from pathlib import Path

from sheet2music.core.homr import build_homr_command


class HomrCommandTest(unittest.TestCase):
    def test_cpu_mode_disables_gpu(self) -> None:
        command = build_homr_command(Path("page.png"), use_gpu=False)
        self.assertEqual(command[command.index("--gpu") + 1], "no")

    def test_gpu_mode_requests_auto_detection(self) -> None:
        command = build_homr_command(Path("page.png"), use_gpu=True)
        self.assertEqual(command[command.index("--gpu") + 1], "auto")

    def test_layout_output_is_forwarded_to_homr(self) -> None:
        command = build_homr_command(
            Path("page.png"),
            use_gpu=True,
            layout_output=Path("page.layout.json"),
        )

        self.assertIn("--layout-output", command)
        self.assertEqual(
            command[-3:],
            ["--layout-output", "page.layout.json", "page.png"],
        )


if __name__ == "__main__":
    unittest.main()
