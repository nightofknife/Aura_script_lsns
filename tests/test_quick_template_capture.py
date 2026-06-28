from __future__ import annotations

import shutil
import unittest
from pathlib import Path

from PIL import Image

from tools.quick_template_capture import (
    BoundWindow,
    LIBRARY_INDEX_FILE,
    apply_color_preprocess,
    persist_template_metadata,
)
from tools.plan_doctor import REPO_ROOT


class TestQuickTemplateCaptureMetadata(unittest.TestCase):
    def test_apply_color_preprocess_rgb_and_hsv_range(self):
        image = Image.new("RGB", (2, 1))
        image.putpixel((0, 0), (255, 0, 0))
        image.putpixel((1, 0), (0, 255, 0))

        rgb_processed = apply_color_preprocess(
            image,
            {
                "mode": "rgb_range",
                "rgb_lower": [200, 0, 0],
                "rgb_upper": [255, 60, 60],
            },
        )
        self.assertEqual(rgb_processed.getpixel((0, 0)), (255, 0, 0))
        self.assertEqual(rgb_processed.getpixel((1, 0)), (0, 0, 0))

        hsv_processed = apply_color_preprocess(
            image,
            {
                "mode": "hsv_range",
                "hsv_lower": [35, 100, 100],
                "hsv_upper": [85, 255, 255],
            },
        )
        self.assertEqual(hsv_processed.getpixel((0, 0)), (0, 0, 0))
        self.assertEqual(hsv_processed.getpixel((1, 0)), (0, 255, 0))

    def test_persist_template_metadata_updates_plan_config_and_library_index(self):
        plan_name = "_tmp_template_capture_case"
        target = REPO_ROOT / "plans" / plan_name
        if target.exists():
            shutil.rmtree(target)

        try:
            output_dir = target / "templates"
            output_dir.mkdir(parents=True)
            saved_file = output_dir / "sample_template.png"
            saved_file.write_bytes(b"png")

            result = persist_template_metadata(
                plan_name=plan_name,
                output_dir=output_dir,
                library_name="captured",
                saved_template_path=saved_file,
                bound_window=BoundWindow(
                    hwnd=100,
                    title="Demo Window",
                    process_name="demo.exe",
                    class_name="DemoClass",
                ),
                crop_rect=(10, 20, 30, 40),
                capture_size=(1920, 1080),
                preprocess={
                    "mode": "rgb_range",
                    "rgb_lower": [10, 20, 30],
                    "rgb_upper": [200, 210, 220],
                },
            )

            config_path = target / "config.yaml"
            index_path = output_dir / LIBRARY_INDEX_FILE

            self.assertEqual(Path(result["config_path"]), config_path)
            self.assertEqual(Path(result["index_path"]), index_path)
            self.assertTrue(config_path.is_file())
            self.assertTrue(index_path.is_file())

            config_text = config_path.read_text(encoding="utf-8")
            self.assertIn("templates:", config_text)
            self.assertIn("libraries:", config_text)
            self.assertIn("name: captured", config_text)
            self.assertIn("path: templates", config_text)

            index_text = index_path.read_text(encoding="utf-8")
            self.assertIn("library:", index_text)
            self.assertIn("name: sample_template", index_text)
            self.assertIn("file: sample_template.png", index_text)
            self.assertIn("hwnd: 100", index_text)
            self.assertIn("mode: rgb_range", index_text)
        finally:
            if target.exists():
                shutil.rmtree(target)


if __name__ == "__main__":
    unittest.main()
