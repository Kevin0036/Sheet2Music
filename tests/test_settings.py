import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sheet2music.core import settings, system


class PdftoppmBinaryTest(unittest.TestCase):
    def test_prefers_executable_over_path_shim(self) -> None:
        resolved = {
            "pdftoppm.exe": r"C:\real\pdftoppm.exe",
            "pdftoppm": r"C:\shim\pdftoppm.cmd",
        }
        with mock.patch.object(settings, "_windows_poppler_binary", return_value=None):
            with mock.patch.object(settings.shutil, "which", side_effect=resolved.get):
                self.assertEqual(settings.pdftoppm_binary(), resolved["pdftoppm.exe"])

    def test_prefers_winget_poppler_over_path_shim(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            local_app_data = Path(temp_dir)
            binary = (
                local_app_data
                / "Microsoft"
                / "WinGet"
                / "Packages"
                / "oschwartz10612.Poppler_test"
                / "poppler-25.07.0"
                / "Library"
                / "bin"
                / "pdftoppm.exe"
            )
            binary.parent.mkdir(parents=True)
            binary.touch()

            with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(local_app_data)}):
                with mock.patch.object(
                    settings.shutil,
                    "which",
                    return_value=r"C:\\broken\\pdftoppm.cmd",
                ):
                    self.assertEqual(settings.pdftoppm_binary(), str(binary))

    def test_system_status_reports_the_resolved_pdftoppm_binary(self) -> None:
        with mock.patch.object(system, "pdftoppm_binary", create=True, return_value="real-poppler"):
            with mock.patch.object(system, "find_tool", return_value="path-tool"):
                status = system.system_status()

        pdftoppm = next(item for item in status["binaries"] if item["name"] == "pdftoppm")
        self.assertEqual(pdftoppm["path"], "real-poppler")

    def test_system_status_reports_the_resolved_ffmpeg_binary(self) -> None:
        with mock.patch.object(system, "pdftoppm_binary", return_value="real-poppler"):
            with mock.patch.object(system, "ffmpeg_binary", return_value="real-ffmpeg"):
                with mock.patch.object(system, "find_tool", return_value="path-tool"):
                    status = system.system_status()

        ffmpeg = next(item for item in status["binaries"] if item["name"] == "ffmpeg")
        self.assertEqual(ffmpeg["path"], "real-ffmpeg")


class FfmpegBinaryTest(unittest.TestCase):
    def test_explicit_ffmpeg_environment_override_wins(self) -> None:
        with mock.patch.dict(os.environ, {"SHEET2MUSIC_FFMPEG": r"C:\tools\ffmpeg.exe"}):
            self.assertEqual(settings.ffmpeg_binary(), r"C:\tools\ffmpeg.exe")

    def test_prefers_winget_ffmpeg(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            local_app_data = Path(temp_dir)
            binary = (
                local_app_data
                / "Microsoft"
                / "WinGet"
                / "Packages"
                / "Gyan.FFmpeg_test"
                / "ffmpeg-9.0-full_build"
                / "bin"
                / "ffmpeg.exe"
            )
            binary.parent.mkdir(parents=True)
            binary.touch()

            with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(local_app_data)}):
                with mock.patch.object(settings.shutil, "which", return_value=None):
                    self.assertEqual(settings.ffmpeg_binary(), str(binary))


class TranskunModelSettingsTest(unittest.TestCase):
    def test_default_v2_uses_reference_packaged_model(self) -> None:
        weight, conf = settings.transkun_model_files("v2")

        self.assertEqual(weight, settings.transkun_root() / "transkun" / "pretrained" / "2.0.pt")
        self.assertEqual(conf, settings.transkun_root() / "transkun" / "pretrained" / "2.0.conf")

    def test_model_identity_check_rejects_wrong_weight_contents(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            weight = Path(temp_dir) / "weight.pt"
            weight.write_bytes(b"not-the-reference-model")
            with self.assertRaisesRegex(RuntimeError, "SHA-256"):
                settings.validate_file_identity(
                    weight,
                    expected_size=weight.stat().st_size,
                    expected_sha256="0" * 64,
                    label="Transkun V2 weight",
                )

    def test_falls_back_to_home_appdata_without_localappdata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir)
            binary = (
                home
                / "AppData"
                / "Local"
                / "Microsoft"
                / "WinGet"
                / "Packages"
                / "Gyan.FFmpeg_test"
                / "ffmpeg-9.0-full_build"
                / "bin"
                / "ffmpeg.exe"
            )
            binary.parent.mkdir(parents=True)
            binary.touch()

            with mock.patch.dict(os.environ, {}, clear=True):
                with mock.patch.object(settings.Path, "home", return_value=home):
                    with mock.patch.object(settings.shutil, "which", return_value=None):
                        self.assertEqual(settings.ffmpeg_binary(), str(binary))


if __name__ == "__main__":
    unittest.main()
