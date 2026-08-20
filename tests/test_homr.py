import unittest
import os
from pathlib import Path
from unittest import mock

from sheet2music.core.homr import (
    HomrPageError,
    build_homr_command,
    gpu_runtime_environment,
    run_homr_on_page,
)


class HomrCommandTest(unittest.TestCase):
    def test_cpu_mode_disables_gpu(self) -> None:
        command = build_homr_command(Path("page.png"), use_gpu=False)
        self.assertEqual(command[command.index("--gpu") + 1], "no")

    def test_gpu_mode_requires_gpu_execution(self) -> None:
        command = build_homr_command(Path("page.png"), use_gpu=True)
        self.assertEqual(command[command.index("--gpu") + 1], "force")

    def test_gpu_environment_adds_virtualenv_nvidia_dll_directories(self) -> None:
        directories = [
            Path(r"C:\project\.venv\Lib\site-packages\nvidia\cu13\bin"),
            Path(r"C:\project\.venv\Lib\site-packages\nvidia\cudnn\bin"),
        ]
        with mock.patch("sheet2music.core.homr._windows_cuda_dll_directories", return_value=directories):
            env = gpu_runtime_environment({"PATH": r"C:\Windows\System32"})

        self.assertIn(
            os.path.normpath(r"C:\project\.venv\Lib\site-packages\nvidia\cu13\bin"),
            env["PATH"],
        )
        self.assertIn(
            os.path.normpath(r"C:\project\.venv\Lib\site-packages\nvidia\cudnn\bin"),
            env["PATH"],
        )

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

    def test_gpu_run_rejects_unavailable_cuda_instead_of_falling_back(self) -> None:
        with mock.patch("sheet2music.core.homr.probe_cuda_provider", return_value=(False, "missing DLL")):
            with self.assertRaisesRegex(HomrPageError, "HOMR 识别失败") as raised:
                run_homr_on_page(Path("page.png"), Path("work"), use_gpu=True)

        self.assertIn("missing DLL", raised.exception.stderr_tail)


if __name__ == "__main__":
    unittest.main()
