import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sheet2music.core import export


class MuseScoreEnvironmentTest(unittest.TestCase):
    def test_uses_windows_qt_platform_plugin_on_windows(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with mock.patch.object(export.os, "name", "nt"):
                with mock.patch.object(export.tempfile, "gettempdir", return_value=temp_dir):
                    environment = export._musescore_env()

        self.assertEqual(environment["QT_QPA_PLATFORM"], "windows")
        self.assertEqual(
            environment["USERPROFILE"],
            str(Path(temp_dir) / "sheet2music" / "musescore"),
        )

    def test_exports_midi_with_musescore4_cli_options(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            input_score = Path(temp_dir) / "score.musicxml"
            output_midi = Path(temp_dir) / "score.mid"
            input_score.touch()
            captured: list[list[str]] = []

            def fake_run(args: list[str]) -> None:
                captured.append(args)
                output_midi.touch()

            with mock.patch.object(export, "_run_musescore", side_effect=fake_run):
                export.export_midi(input_score, output_midi)

        self.assertEqual(captured, [["-f", "-o", str(output_midi), str(input_score)]])


if __name__ == "__main__":
    unittest.main()
