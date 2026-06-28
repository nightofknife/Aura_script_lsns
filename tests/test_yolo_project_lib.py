from __future__ import annotations

import shutil
import unittest
from pathlib import Path

from tools.yolo_project_lib import (
    complete_labelimg_session,
    create_labelimg_session,
    create_project,
    export_training_dataset,
    import_images,
    load_project_config,
    load_samples,
    project_root,
    summarize_samples,
)
from tools.plan_doctor import REPO_ROOT


class TestYoloProjectLib(unittest.TestCase):
    def setUp(self):
        self.plan_name = "_tmp_yolo_project_case"
        self.plan_dir = REPO_ROOT / "plans" / self.plan_name
        if self.plan_dir.exists():
            shutil.rmtree(self.plan_dir)
        self.plan_dir.mkdir(parents=True)
        (self.plan_dir / "__init__.py").write_text("", encoding="utf-8")

    def tearDown(self):
        if self.plan_dir.exists():
            shutil.rmtree(self.plan_dir)

    def test_create_project_and_import_images_deduplicate_by_content(self):
        root = create_project(plan_name=self.plan_name, project_name="demo", class_names=["enemy", "ally"])
        self.assertTrue((root / "project.yaml").is_file())
        self.assertTrue((root / "classes.txt").is_file())

        image_a = self.plan_dir / "image_a.png"
        image_b = self.plan_dir / "image_b.png"
        image_a.write_bytes(b"same-bytes")
        image_b.write_bytes(b"same-bytes")

        result = import_images(root, [image_a, image_b])
        self.assertEqual(result["imported_count"], 1)
        self.assertEqual(len(load_samples(root)), 1)
        summary = summarize_samples(load_samples(root))
        self.assertEqual(summary["unlabeled"], 1)

    def test_manual_session_completion_promotes_labeled_samples(self):
        root = create_project(plan_name=self.plan_name, project_name="demo", class_names=["enemy"])
        image_path = self.plan_dir / "sample.png"
        image_path.write_bytes(b"sample-image")
        import_images(root, [image_path])

        session = create_labelimg_session(root, session_type="manual", batch_size=10)
        session_dir = root / session.session_dir
        sample = load_samples(root)[0]
        (session_dir / f"{sample.sample_id}.txt").write_text("0 0.5 0.5 0.2 0.2\n", encoding="utf-8")

        result = complete_labelimg_session(root, session_type="manual")
        self.assertEqual(result["approved_count"], 1)

        samples = load_samples(root)
        self.assertEqual(samples[0].status, "approved")
        self.assertIsNotNone(samples[0].approved_label_relpath)
        approved_path = root / str(samples[0].approved_label_relpath)
        self.assertTrue(approved_path.is_file())

    def test_export_training_dataset_creates_dataset_yaml_and_split_dirs(self):
        root = create_project(plan_name=self.plan_name, project_name="demo", class_names=["enemy"])
        image_path = self.plan_dir / "sample.png"
        image_path.write_bytes(b"sample-image")
        import_images(root, [image_path])

        session = create_labelimg_session(root, session_type="manual", batch_size=10)
        session_dir = root / session.session_dir
        sample = load_samples(root)[0]
        (session_dir / f"{sample.sample_id}.txt").write_text("0 0.5 0.5 0.2 0.2\n", encoding="utf-8")
        complete_labelimg_session(root, session_type="manual")

        export_info = export_training_dataset(root)
        export_dir = Path(export_info["export_dir"])

        self.assertTrue((export_dir / "dataset.yaml").is_file())
        self.assertTrue((export_dir / "images" / "train").is_dir())
        self.assertTrue((export_dir / "images" / "val").is_dir())
        self.assertTrue((export_dir / "labels" / "train").is_dir())
        self.assertTrue((export_dir / "labels" / "val").is_dir())
        train_images = list((export_dir / "images" / "train").glob("*"))
        train_labels = list((export_dir / "labels" / "train").glob("*.txt"))
        self.assertTrue(train_images)
        self.assertTrue(train_labels)
        self.assertEqual(train_images[0].stem, train_labels[0].stem)

        project = load_project_config(root)
        self.assertEqual(project["latest_export"]["export_id"], export_info["export_id"])


if __name__ == "__main__":
    unittest.main()
