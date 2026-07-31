"""system.py 自检与权重下载逻辑的单元测试（不访问网络）。"""

import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from sheet2music.core import system


class SystemStatusTest(unittest.TestCase):
    def test_required_model_names_and_locations(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "homr"
            segmentation = root / "homr" / "segmentation"
            transformer = root / "homr" / "transformer"
            segmentation.mkdir(parents=True)
            transformer.mkdir(parents=True)
            (segmentation / "config.py").write_text('model_name = "segnet_308-test"\n', encoding="utf-8")
            (transformer / "configs.py").write_text(
                'class FilePaths:\n    def __init__(self) -> None:\n        model_name = "pytorch_model_426-test"\n',
                encoding="utf-8",
            )

            files = system.model_files(root)

        self.assertEqual(len(files), 3)
        names = [path.name for path in files]
        self.assertTrue(any(name.startswith("segnet_308-test") for name in names))
        self.assertTrue(any(name.startswith("encoder_pytorch_model_426-test") for name in names))
        self.assertTrue(any(name.startswith("decoder_pytorch_model_426-test") for name in names))
        # segnet 在 segmentation/，两个 transformer 模型在 transformer/
        self.assertEqual(len([p for p in files if "segmentation" in p.parts]), 1)
        self.assertEqual(len([p for p in files if "transformer" in p.parts]), 2)

    def test_status_shape(self) -> None:
        status = system.system_status()
        for key in ("homr_root", "weights", "gpu", "python_deps", "binaries", "all_ok"):
            self.assertIn(key, status)
        self.assertIn("ok", status["weights"])
        self.assertIn("missing", status["weights"])
        self.assertIsInstance(status["python_deps"], list)
        self.assertIsInstance(status["binaries"], list)
        self.assertIn("providers", status["gpu"])

    def test_model_files_include_fp16_variants_when_requested(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "homr"
            segmentation = root / "homr" / "segmentation"
            transformer = root / "homr" / "transformer"
            segmentation.mkdir(parents=True)
            transformer.mkdir(parents=True)
            (segmentation / "config.py").write_text('model_name = "segnet-test"\n', encoding="utf-8")
            (transformer / "configs.py").write_text(
                'model_name = "pytorch_model-test"\n', encoding="utf-8"
            )

            fp32 = system.model_files(root)
            all_models = system.model_files(root, include_fp16=True)

        self.assertEqual(len(fp32), 3)
        self.assertEqual(len(all_models), 6)
        self.assertTrue(all(path.name.endswith("_fp16.onnx") for path in all_models[3:]))

    def test_gpu_request_falls_back_to_fp32_when_no_provider_exists(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "homr"
            segmentation = root / "homr" / "segmentation"
            transformer = root / "homr" / "transformer"
            segmentation.mkdir(parents=True)
            transformer.mkdir(parents=True)
            (segmentation / "config.py").write_text('model_name = "segnet-test"\n', encoding="utf-8")
            (transformer / "configs.py").write_text('model_name = "pytorch_model-test"\n', encoding="utf-8")

            with mock.patch.object(system, "homr_root", return_value=root):
                with mock.patch.object(system, "available_gpu_providers", return_value=[]):
                    missing = system.missing_model_files(use_gpu=True)

        self.assertEqual(len(missing), 3)
        self.assertTrue(all(not path.name.endswith("_fp16.onnx") for path in missing))

    def test_download_guard_rejects_while_running(self) -> None:
        with system._DOWNLOAD_LOCK:
            previous = dict(system.WEIGHT_DOWNLOAD_STATE)
            system.WEIGHT_DOWNLOAD_STATE["running"] = True
        try:
            self.assertFalse(system.start_weight_download())
        finally:
            with system._DOWNLOAD_LOCK:
                system.WEIGHT_DOWNLOAD_STATE.clear()
                system.WEIGHT_DOWNLOAD_STATE.update(previous)


class WeightExtractTest(unittest.TestCase):
    def test_unzip_extracts_nested_onnx_to_target_and_cleans_zip(self) -> None:
        filename = "encoder_pytorch_model_426-test.onnx"
        with tempfile.TemporaryDirectory() as tmp:
            target_dir = Path(tmp) / "transformer"
            target_dir.mkdir()
            zip_name = filename.rsplit(".", 1)[0] + ".zip"
            zip_path = target_dir / zip_name
            with zipfile.ZipFile(zip_path, "w") as archive:
                archive.writestr(f"nested/{filename}", b"FAKE-ONNX")

            with mock.patch.object(system, "_download_file") as fake_download:
                fake_download.return_value = zip_path.stat().st_size
                system._download_and_unzip(filename, target_dir / filename)

            self.assertEqual((target_dir / filename).read_bytes(), b"FAKE-ONNX")
            self.assertFalse(zip_path.exists())


if __name__ == "__main__":
    unittest.main()
