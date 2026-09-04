from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import pytest

from packages.aura_core.utils.updater import FrameworkUpdater
from scripts.release.prune_release_payload import reset_release_runtime_data


def test_framework_update_preserves_user_data(tmp_path: Path) -> None:
    install = tmp_path / "install"
    user_info = install / "user-data" / "user-info.json"
    user_info.parent.mkdir(parents=True)
    user_info.write_text('{"daily":{"used":3}}', encoding="utf-8")
    before_hash = hashlib.sha256(user_info.read_bytes()).hexdigest()
    (install / "obsolete.txt").write_text("old", encoding="utf-8")

    update_zip = tmp_path / "update.zip"
    with zipfile.ZipFile(update_zip, "w") as archive:
        archive.writestr("replacement.txt", "new")

    result = FrameworkUpdater(install).apply(str(update_zip), backup=False)

    assert result["status"] == "success"
    assert hashlib.sha256(user_info.read_bytes()).hexdigest() == before_hash
    assert (install / "replacement.txt").read_text(encoding="utf-8") == "new"
    assert not (install / "obsolete.txt").exists()


def test_release_runtime_cleanup_removes_user_data(tmp_path: Path) -> None:
    release = tmp_path / "release"
    user_info = release / "user-data" / "user-info.json"
    user_info.parent.mkdir(parents=True)
    user_info.write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="generated runtime files"):
        reset_release_runtime_data(release, check_only=True)

    report = reset_release_runtime_data(release)
    assert report["removed_files"] == 1
    assert not (release / "user-data").exists()


def test_release_contract_forbids_user_data() -> None:
    contract = json.loads(Path("packaging/release-contract.json").read_text(encoding="utf-8"))
    assert "user-data" in contract["full_release"]["forbidden_paths"]
