from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings

from packages.resonance_gui.config_repository import (
    ResonanceConfigRepository,
    create_portable_settings,
)


def _file_settings(path) -> QSettings:
    return QSettings(str(path), QSettings.Format.IniFormat)


def test_repository_persists_settings_in_portable_root(tmp_path):
    repository = ResonanceConfigRepository(base_path=tmp_path, legacy_settings=_file_settings(tmp_path / "empty.ini"))

    repository.set_value("workbench/last_task_id", "demo")

    settings_path = tmp_path / "gui-settings.ini"
    assert settings_path.is_file()
    assert _file_settings(settings_path).value("workbench/last_task_id") == "demo"


def test_portable_settings_migrate_and_clear_legacy_values(tmp_path):
    legacy = _file_settings(tmp_path / "legacy.ini")
    legacy.setValue("runner/timeout_sec", 42.5)
    legacy.setValue("trade/inputs_json", '{"fatigue_budget": 123}')
    legacy.sync()

    portable = create_portable_settings(tmp_path, legacy_settings=legacy)

    assert float(portable.value("runner/timeout_sec")) == 42.5
    assert "123" in str(portable.value("trade/inputs_json"))
    assert legacy.allKeys() == []
    assert (tmp_path / "gui-settings.ini").is_file()


def test_existing_portable_settings_are_not_overwritten_by_legacy_values(tmp_path):
    portable_path = tmp_path / "gui-settings.ini"
    existing = _file_settings(portable_path)
    existing.setValue("runner/timeout_sec", 10)
    existing.sync()
    legacy = _file_settings(tmp_path / "legacy.ini")
    legacy.setValue("runner/timeout_sec", 99)
    legacy.sync()

    portable = create_portable_settings(tmp_path, legacy_settings=legacy)

    assert float(portable.value("runner/timeout_sec")) == 10
    assert legacy.value("runner/timeout_sec") == 99
