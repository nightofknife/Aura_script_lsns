from __future__ import annotations

import json
from pathlib import Path
import zipfile

import pytest

from scripts.release.build_plan_package import collect_plan_files, create_archive, validate_selected_files
from scripts.release.detect_release_scope import classify_paths
from scripts.release.validate_ocr_bundle import validate_bundle


@pytest.mark.parametrize(
    ("paths", "expected"),
    [
        (["plans/resonance/src/action.py", "tests/test_action.py"], "plan"),
        (["packages/aura_core/runtime.py"], "full"),
        (["plans/resonance/task.yaml", "packaging/pyinstaller/aura.spec"], "full"),
        (["README.md", "tests/test_docs.py"], "none"),
        (["plans/old/manifest.yaml", "plans/new/manifest.yaml"], "plan"),
    ],
)
def test_release_scope_classification(paths, expected):
    assert classify_paths(paths)["scope"] == expected


def test_plan_archive_contains_full_filtered_tree(tmp_path):
    repo = tmp_path / "repo"
    plan = repo / "plans" / "demo"
    (plan / "src").mkdir(parents=True)
    (plan / "data" / "meta").mkdir(parents=True)
    (plan / "data" / "cache").mkdir(parents=True)
    (repo / "plans" / "__init__.py").write_text("", encoding="utf-8")
    (plan / "manifest.yaml").write_text("package: {}\n", encoding="utf-8")
    (plan / "src" / "action.py").write_text("VALUE = 1\n", encoding="utf-8")
    (plan / "data" / "meta" / "catalog.json").write_text("{}", encoding="utf-8")
    (plan / "data" / "cache" / "latest.json").write_text("{}", encoding="utf-8")
    (plan / "src" / "ignored.pyc").write_bytes(b"cache")
    (plan / "src" / "credentials.json").write_text("{}", encoding="utf-8")
    (plan / "src" / ".env.local").write_text("TOKEN=secret", encoding="utf-8")

    files = collect_plan_files(repo / "plans")
    validate_selected_files(files)
    archive_path = tmp_path / "plans.zip"
    create_archive(files, archive_path)

    with zipfile.ZipFile(archive_path) as archive:
        names = set(archive.namelist())
    assert "plans/demo/manifest.yaml" in names
    assert "plans/demo/src/action.py" in names
    assert "plans/demo/data/meta/catalog.json" in names
    assert "plans/demo/data/cache/latest.json" not in names
    assert "plans/demo/src/ignored.pyc" not in names
    assert "plans/demo/src/credentials.json" not in names
    assert "plans/demo/src/.env.local" not in names


def test_ocr_bundle_uses_optional_model_flags(tmp_path):
    bundle = tmp_path / "ppocrv5_server"
    bundle.mkdir()
    metadata = {
        "models": {
            "det": "det.onnx",
            "rec": "rec.onnx",
            "textline_orientation": "textline_orientation.onnx",
            "doc_orientation": None,
        },
        "pipeline": {"use_textline_orientation": True, "use_doc_orientation": False},
    }
    (bundle / "ocr.meta.json").write_text(json.dumps(metadata), encoding="utf-8")
    for name in ("det.onnx", "rec.onnx", "textline_orientation.onnx"):
        (bundle / name).write_bytes(b"onnx")

    required = validate_bundle(bundle)

    assert {path.name for path in required} == {
        "ocr.meta.json",
        "det.onnx",
        "rec.onnx",
        "textline_orientation.onnx",
    }


def test_ocr_bundle_requires_enabled_doc_orientation(tmp_path):
    bundle = tmp_path / "ppocrv5_server"
    bundle.mkdir()
    metadata = {
        "models": {"det": "det.onnx", "rec": "rec.onnx", "doc_orientation": None},
        "pipeline": {"use_doc_orientation": True},
    }
    (bundle / "ocr.meta.json").write_text(json.dumps(metadata), encoding="utf-8")
    (bundle / "det.onnx").write_bytes(b"onnx")
    (bundle / "rec.onnx").write_bytes(b"onnx")

    with pytest.raises(ValueError, match="doc_orientation"):
        validate_bundle(bundle)
