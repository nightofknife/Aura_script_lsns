from __future__ import annotations

import json
import os
from pathlib import Path
import runpy
import sys
import zipfile
from unittest.mock import patch

import pytest

from scripts.release.assemble_release_plans import collect_plan_files, stage_plan_tree, validate_selected_files
from scripts.release.detect_release_scope import classify_paths
from scripts.release.prune_release_payload import (
    classify_nvidia_file,
    classify_runtime_file,
    path_is_file,
    prune_files,
    reset_release_runtime_data,
)
from scripts.release.pyinstaller_filters import excluded_data_globs, should_collect_submodule
from scripts.release.validate_ocr_bundle import validate_bundle
from scripts.release.release_contract import load_contract, parse_hashed_lock
from scripts.release.validate_release import (
    _resolve_powershell_executable,
    validate_archive,
    validate_archive_matches_tree,
)
from scripts.release.validate_release_set import _equivalent_generated_metadata
from scripts.release.validate_windows_execution_level import parse_execution_level


@pytest.mark.parametrize(
    ("paths", "expected"),
    [
        (["plans/resonance/src/action.py", "tests/test_action.py"], "full"),
        (["packages/aura_core/runtime.py"], "full"),
        (["updater/aura_updater.py"], "full"),
        (["plans/resonance/task.yaml", "packaging/pyinstaller/aura.spec"], "full"),
        (["scripts/package_release.ps1"], "full"),
        (["README.md", "tests/test_docs.py"], "none"),
        (["plans/old/manifest.yaml", "plans/new/manifest.yaml"], "full"),
    ],
)
def test_release_scope_classification(paths, expected):
    assert classify_paths(paths)["scope"] == expected


def test_full_release_plan_tree_is_complete_and_filtered(tmp_path):
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
    destination = tmp_path / "release-plans"
    stage_plan_tree(files, destination)
    names = {path.relative_to(destination).as_posix() for path in destination.rglob("*") if path.is_file()}
    assert "demo/manifest.yaml" in names
    assert "demo/src/action.py" in names
    assert "demo/data/meta/catalog.json" in names
    assert "demo/data/cache/latest.json" not in names
    assert "demo/src/ignored.pyc" not in names
    assert "demo/src/credentials.json" not in names
    assert "demo/src/.env.local" not in names


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


def test_windows_manifest_uses_as_invoker():
    manifest = b"""<?xml version="1.0" encoding="UTF-8"?>
<assembly xmlns="urn:schemas-microsoft-com:asm.v1" manifestVersion="1.0">
  <trustInfo xmlns="urn:schemas-microsoft-com:asm.v3">
    <security>
      <requestedPrivileges>
        <requestedExecutionLevel level="asInvoker" uiAccess="false" />
      </requestedPrivileges>
    </security>
  </trustInfo>
</assembly>
"""

    assert parse_execution_level(manifest) == "asInvoker"


def test_release_builder_does_not_force_administrator_startup():
    repo_root = Path(__file__).resolve().parents[1]
    build_script = (repo_root / "scripts" / "build_release.ps1").read_text(encoding="utf-8")

    assert "--uac-admin" not in build_script
    assert "validate_windows_execution_level.py" in build_script


def test_release_builder_embeds_and_validates_gui_icons():
    repo_root = Path(__file__).resolve().parents[1]
    build_script = (repo_root / "scripts" / "build_release.ps1").read_text(encoding="utf-8")
    pyinstaller_spec = (repo_root / "packaging" / "pyinstaller" / "aura.spec").read_text(
        encoding="utf-8"
    )
    contract = load_contract(repo_root / "packaging" / "release-contract.json")

    icon_name = "aura_resonance_chibi_icon-optimized.ico"
    assert f"packaging\\\\assets\\\\{icon_name}" in build_script
    assert "--icon $IconPath" in build_script
    assert "--require-icon" in build_script
    assert icon_name in pyinstaller_spec
    assert "icon=str(APP_ICON)" in pyinstaller_spec
    assert (
        f"runtime/_internal/packaging/assets/{icon_name}"
        in contract["full_release"]["required_paths"]
    )


def test_release_builder_packages_portable_updater():
    repo_root = Path(__file__).resolve().parents[1]
    build_script = (repo_root / "scripts" / "build_release.ps1").read_text(encoding="utf-8")
    validator = (repo_root / "scripts" / "release" / "validate_release.py").read_text(encoding="utf-8")
    contract = load_contract(repo_root / "packaging" / "release-contract.json")

    assert "updater\\\\aura_updater.py" in build_script
    assert "[char]0x66F4" in build_script
    assert "[char]0x65B0" in build_script
    assert "--name $ExecutableName" in build_script
    assert "Join-Path $ReleaseRoot $UpdaterExecutableName" in build_script
    assert 'release_root / "更新.exe"' in validator
    assert "更新.exe" in contract["full_release"]["required_paths"]
    assert "更新.exe" in contract["comparison"]["allowed_cpu_gpu_differences"]
    assert {"gui-settings.ini", "updates", ".update-work"}.issubset(
        contract["full_release"]["forbidden_paths"]
    )


def test_release_self_check_does_not_mask_runtime_base_path_discovery():
    repo_root = Path(__file__).resolve().parents[1]
    validator = (repo_root / "scripts" / "release" / "validate_release.py").read_text(encoding="utf-8")

    assert 'env.pop("AURA_BASE_PATH", None)' in validator
    assert 'env["AURA_BASE_PATH"]' not in validator


def test_release_smoke_falls_back_to_windows_powershell():
    with patch(
        "scripts.release.validate_release.shutil.which",
        side_effect=lambda name: None if name == "pwsh" else r"C:\Windows\powershell.exe",
    ):
        assert _resolve_powershell_executable() == r"C:\Windows\powershell.exe"


def test_frozen_runtime_hook_infers_release_root_from_runtime_executable(tmp_path):
    repo_root = Path(__file__).resolve().parents[1]
    hook_path = repo_root / "packaging" / "pyinstaller" / "rthook_aura_external_plans.py"
    release_root = tmp_path / "AuraResonance"
    runtime_dir = release_root / "runtime"
    runtime_dir.mkdir(parents=True)
    (release_root / "plans").mkdir()
    runtime_exe = runtime_dir / "AuraResonanceRuntime.exe"
    runtime_exe.touch()

    with (
        patch.dict(os.environ, {}, clear=True),
        patch.object(sys, "executable", str(runtime_exe)),
        patch.object(sys, "frozen", True, create=True),
        patch.object(sys, "path", list(sys.path)),
    ):
        hook_globals = runpy.run_path(str(hook_path))
        resolved = hook_globals["_resolve_external_base_path"]()

    assert resolved == release_root.resolve()


def test_frozen_runtime_hook_prefers_explicit_base_path(tmp_path):
    repo_root = Path(__file__).resolve().parents[1]
    hook_path = repo_root / "packaging" / "pyinstaller" / "rthook_aura_external_plans.py"
    configured_root = tmp_path / "configured"
    configured_root.mkdir()

    with (
        patch.dict(os.environ, {"AURA_BASE_PATH": str(configured_root)}, clear=True),
        patch.object(sys, "path", list(sys.path)),
    ):
        hook_globals = runpy.run_path(str(hook_path))
        resolved = hook_globals["_resolve_external_base_path"]()

    assert resolved == configured_root.resolve()


@pytest.mark.parametrize(
    ("package_name", "module_name", "expected"),
    [
        ("numpy", "numpy._core.tests.test_numeric", False),
        ("numpy", "numpy.f2py", False),
        ("numpy", "numpy._core.numeric", True),
        ("onnxruntime", "onnxruntime.backend", False),
        ("onnxruntime", "onnxruntime.tools.convert_onnx_models_to_ort", False),
        ("onnxruntime", "onnxruntime.capi._pybind_state", True),
        ("av", "av.datasets", False),
        ("av", "av.codec", True),
        ("screeninfo", "screeninfo.__main__", False),
        ("dotenv", "dotenv.main", True),
    ],
)
def test_pyinstaller_submodule_filter(package_name, module_name, expected):
    assert should_collect_submodule(package_name, module_name) is expected


def test_pyinstaller_data_filter_excludes_dependency_tests():
    assert "**/tests/**" in excluded_data_globs("numpy")
    assert "**/*.pyi" in excluded_data_globs("cv2")


def test_runtime_payload_pruning_is_precise(tmp_path):
    runtime = tmp_path / "runtime"
    removable = [
        runtime / "_internal" / "numpy" / "_core" / "tests" / "test_numeric.py",
        runtime / "_internal" / "PySide6" / "translations" / "qtbase_de.qm",
        runtime / "_internal" / "PySide6" / "Qt6Qml.dll",
        runtime
        / "_internal"
        / "PySide6"
        / "plugins"
        / "platforminputcontexts"
        / "qtvirtualkeyboardplugin.dll",
        runtime / "_internal" / "PySide6" / "plugins" / "imageformats" / "qpdf.dll",
        runtime / "_internal" / "PIL" / "_avif.cp312-win_amd64.pyd",
        runtime / "_internal" / "opencv_videoio_ffmpeg4130_64.dll",
        runtime / "_internal" / "cv2" / "typing" / "__init__.pyi",
        runtime / "_internal" / "numpy" / "_core" / "include" / "numpy" / "arrayobject.h",
        runtime / "_internal" / "numpy" / "_core" / "_multiarray_tests.cp312-win_amd64.pyd",
        runtime / "_internal" / "numpy" / "random" / "lib" / "npyrandom.lib",
        runtime / "_internal" / "onnxruntime" / "tools" / "mobile_helpers" / "ops.md",
        runtime / "_internal" / "av-17.0.0.dist-info" / "licenses" / "__pycache__" / "AUTHORS.pyc",
    ]
    retained = [
        runtime / "_internal" / "PySide6" / "Qt6Widgets.dll",
        runtime / "_internal" / "PySide6" / "plugins" / "platforms" / "qwindows.dll",
        runtime / "_internal" / "av.libs" / "avcodec-62.dll",
        runtime / "_internal" / "onnxruntime" / "capi" / "onnxruntime.dll",
    ]
    for path in removable + retained:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"runtime")

    with pytest.raises(ValueError, match="excluded files"):
        prune_files(runtime, classify_runtime_file, check_only=True)

    report = prune_files(runtime, classify_runtime_file)

    assert report["removed_files"] == len(removable)
    assert all(not path.exists() for path in removable)
    assert all(path.is_file() for path in retained)
    assert prune_files(runtime, classify_runtime_file, check_only=True)["removed_files"] == 0


def test_nvidia_payload_pruning_keeps_runtime_dlls(tmp_path):
    nvidia = tmp_path / "nvidia"
    header = nvidia / "cu13" / "include" / "cuda.h"
    import_library = nvidia / "cu13" / "lib" / "x64" / "cublas.lib"
    runtime_dll = nvidia / "cu13" / "bin" / "x86_64" / "cublas64_13.dll"
    for path in (header, import_library, runtime_dll):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"nvidia")

    report = prune_files(nvidia, classify_nvidia_file)

    assert report["removed_files"] == 2
    assert not header.exists()
    assert not import_library.exists()
    assert runtime_dll.is_file()


def test_release_runtime_data_is_cleared_after_self_check(tmp_path):
    release = tmp_path / "release"
    session_log = release / "logs" / "aura_session.log"
    run_store = release / "logs" / "runs" / "run_store.sqlite3"
    for path in (session_log, run_store):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"generated")

    report = reset_release_runtime_data(release)

    assert report["removed_files"] == 2
    assert (release / "logs").is_dir()
    assert not any((release / "logs").rglob("*"))


def test_overlay_requirements_only_install_nvidia_runtime_packages():
    repo_root = Path(__file__).resolve().parents[1]
    requirements = (
        repo_root / "requirements" / "release-nvidia-overlay.txt"
    ).read_text(encoding="utf-8")

    assert "-r release-gpu.txt" not in requirements
    assert "nvidia-cuda-runtime" in requirements
    assert "onnxruntime-gpu" not in requirements


def test_local_release_entrypoint_uses_isolated_profile_environments():
    repo_root = Path(__file__).resolve().parents[1]
    script = (repo_root / "scripts" / "package_release.ps1").read_text(encoding="utf-8")

    assert '.venv-release-$SelectedProfile' in script
    assert 'requirements_lock' in script
    assert '--require-hashes' in script
    assert 'build_release.ps1' in script
    assert '-IncludeGui' in script
    assert 'scripts.release.validate_release' in script


def test_release_contract_defines_one_locked_rule_set():
    repo_root = Path(__file__).resolve().parents[1]
    contract = load_contract(repo_root / "packaging" / "release-contract.json")

    assert contract["python"]["major_minor"] == "3.12"
    assert contract["platform"] == "win-x64"
    for profile in ("cpu", "gpu", "overlay"):
        lock_path = repo_root / contract["profiles"][profile]["requirements_lock"]
        assert lock_path.is_file()
        assert parse_hashed_lock(lock_path)


def test_release_archives_reject_path_traversal(tmp_path):
    contract = load_contract()
    archive = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("AuraResonance-test-win-x64-cpu/../escape.txt", "bad")

    with pytest.raises(ValueError, match="Unsafe archive entry"):
        validate_archive(archive, expected_root="AuraResonance-test-win-x64-cpu", contract=contract)


def test_release_archive_must_match_validated_tree(tmp_path):
    root = tmp_path / "AuraResonance-test-win-x64-cpu"
    root.mkdir()
    (root / "payload.txt").write_text("expected", encoding="utf-8")
    archive = tmp_path / "release.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.write(root / "payload.txt", f"{root.name}/payload.txt")

    validate_archive_matches_tree(archive, root)
    (root / "payload.txt").write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="do not exactly match"):
        validate_archive_matches_tree(archive, root)


def test_workflow_has_no_plan_only_release_path():
    repo_root = Path(__file__).resolve().parents[1]
    workflow = (repo_root / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    reusable = (repo_root / ".github" / "workflows" / "_build-windows.yml").read_text(encoding="utf-8")

    assert "plan-*" not in workflow
    assert "build-plan:" not in workflow
    assert "release-plans" not in workflow
    assert "scripts\\package_release.ps1" in reusable
    assert "scripts\\build_release.ps1" not in reusable
    assert "onnxruntime_providers_cuda.dll" not in reusable


def test_workflow_only_publishes_commits_reachable_from_main():
    repo_root = Path(__file__).resolve().parents[1]
    workflow = (repo_root / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")

    assert "publish_allowed: ${{ steps.release_source.outputs.publish_allowed }}" in workflow
    assert "git fetch --no-tags origin main:refs/remotes/origin/main" in workflow
    assert "git merge-base --is-ancestor $env:GITHUB_SHA refs/remotes/origin/main" in workflow
    assert "needs.scope.outputs.publish_allowed == 'true'" in workflow


def test_cpu_and_gpu_locks_only_swap_onnxruntime_distribution():
    repo_root = Path(__file__).resolve().parents[1]
    cpu = parse_hashed_lock(repo_root / "requirements" / "release-cpu.lock.txt")
    gpu = parse_hashed_lock(repo_root / "requirements" / "release-gpu.lock.txt")

    assert set(cpu) - set(gpu) == {"onnxruntime"}
    assert set(gpu) - set(cpu) == {"onnxruntime-gpu"}
    cpu.pop("onnxruntime")
    gpu.pop("onnxruntime-gpu")
    assert cpu == gpu


def test_mumu_asset_lock_uses_immutable_urls_and_hashes():
    repo_root = Path(__file__).resolve().parents[1]
    payload = json.loads((repo_root / "packaging" / "assets" / "mumu-runtime.lock.json").read_text(encoding="utf-8"))

    assert payload["schema_version"] == 1
    assert len(payload["assets"]) == 5
    for asset in payload["assets"]:
        assert "/master/" not in asset["url"]
        assert len(asset["sha256"]) == 64
        assert asset["size"] > 0


def test_generated_record_metadata_ignores_only_environment_launchers(tmp_path):
    cpu = tmp_path / "cpu"
    gpu = tmp_path / "gpu"
    relative = "runtime/_internal/demo-1.0.dist-info/RECORD"
    for root, launcher_hash in ((cpu, "cpu"), (gpu, "gpu")):
        path = root / relative
        path.parent.mkdir(parents=True)
        path.write_text(
            f"../../Scripts/demo.exe,sha256={launcher_hash},10\npackage/module.py,sha256=shared,20\n",
            encoding="utf-8",
        )

    assert _equivalent_generated_metadata(relative, cpu, gpu)
    (gpu / relative).write_text("package/module.py,sha256=changed,20\n", encoding="utf-8")
    assert not _equivalent_generated_metadata(relative, cpu, gpu)


@pytest.mark.skipif(os.name != "nt", reason="Windows long-path behavior")
def test_payload_pruner_handles_files_beyond_legacy_max_path(tmp_path):
    root = tmp_path / "nvidia"
    relative = Path("cu13") / "include" / ("nested" * 35) / "header.h"
    extended = Path("\\\\?\\" + str((root / relative).resolve()))
    extended.parent.mkdir(parents=True)
    extended.write_bytes(b"header")

    assert len(str((root / relative).resolve())) > 260
    assert path_is_file(root / relative)
    report = prune_files(root, classify_nvidia_file)
    assert report["removed_files"] == 1
    assert not path_is_file(root / relative)


def test_development_requirements_include_gui_and_pinned_pytest():
    repo_root = Path(__file__).resolve().parents[1]
    requirements = (repo_root / "requirements" / "dev.txt").read_text(encoding="utf-8")

    assert "-r runtime.lock" in requirements
    assert "PySide6==6.11.1" in requirements
    assert "pytest==9.0.3" in requirements


def test_locked_runtime_setup_does_not_upgrade_then_downgrade_tooling():
    repo_root = Path(__file__).resolve().parents[1]
    script = (repo_root / "scripts" / "setup_python_runtime.ps1").read_text(encoding="utf-8")
    lock_branch = script.index('if ($UseLock -and (Test-Path $LockFile))')
    fallback_branch = script.index("} else {", lock_branch)
    upgrade_command = script.index('"--upgrade", "pip", "wheel"', lock_branch)

    assert upgrade_command > fallback_branch


def test_generated_plan_cache_is_ignored_and_not_packaged():
    repo_root = Path(__file__).resolve().parents[1]
    gitignore = (repo_root / ".gitignore").read_text(encoding="utf-8")

    assert "plans/*/data/cache/" in gitignore


def test_development_vision_runtime_matches_release_version():
    repo_root = Path(__file__).resolve().parents[1]
    cpu = (repo_root / "requirements" / "optional-vision-onnx-cpu.txt").read_text(encoding="utf-8")
    gpu = (repo_root / "requirements" / "optional-vision-onnx-cuda.txt").read_text(encoding="utf-8")

    assert "onnxruntime==1.27.0" in cpu
    assert "onnxruntime-gpu==1.27.0" in gpu


def test_legacy_gui_spec_is_removed():
    repo_root = Path(__file__).resolve().parents[1]

    assert not (repo_root / "packaging" / "pyinstaller" / "resonance_gui.spec").exists()


def test_pytest_collection_is_limited_to_canonical_tests_directory():
    repo_root = Path(__file__).resolve().parents[1]
    config = (repo_root / "pytest.ini").read_text(encoding="utf-8")

    assert "testpaths = tests" in config
