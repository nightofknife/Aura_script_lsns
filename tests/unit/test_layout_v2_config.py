"""Layout-v2 preferences start fresh and never import earlier GUI settings."""

from pathlib import Path

import pytest
from PySide6.QtCore import QSettings

from packages.resonance_gui import config_repository
from packages.resonance_gui.config_repository import (
    GuiPreferences,
    PORTABLE_SETTINGS_FILENAME,
    ResonanceConfigRepository,
)



def test_new_namespace_ignores_old_ini_and_registry(tmp_path):
    old_ini = tmp_path / "gui-settings.ini"
    old_content = b"[game]\nexecutable_path=old.exe\n[runner]\ntimeout_sec=123\n"
    old_ini.write_bytes(old_content)
    repository = ResonanceConfigRepository(
        base_path=tmp_path
    )
    assert Path(repository.settings.fileName()) == tmp_path / PORTABLE_SETTINGS_FILENAME
    assert repository.value("game/executable_path", "") == ""
    assert repository.load_preferences() == GuiPreferences()
    assert repository.load_hotkeys() == {"start": "", "stop": ""}
    assert old_ini.read_bytes() == old_content


def test_default_construction_never_opens_registry(monkeypatch, tmp_path):
    opened = []
    real_settings = QSettings

    def portable_only(filename, format):
        opened.append((filename, format))
        assert Path(filename) == tmp_path / PORTABLE_SETTINGS_FILENAME
        assert format == real_settings.Format.IniFormat
        return real_settings(filename, format)

    portable_only.Format = real_settings.Format
    monkeypatch.setattr(config_repository, "QSettings", portable_only)
    config_repository.create_portable_settings(tmp_path)
    assert len(opened) == 1


def test_existing_defaults_are_preserved_in_clean_namespace(tmp_path):
    repository = ResonanceConfigRepository(base_path=tmp_path)
    trade = repository.load_trade_inputs()
    assert trade["fatigue_budget"] == 700
    assert trade["cargo_capacity"] == 750
    assert trade["arrival_timeout_seconds"] == 3600
    assert trade["book_budget"] == 0
    assert trade["trade_mode"] == "free"
    assert trade["auto_cape_island_investment"] is True
    assert trade["auto_rubbish_recycling"] is True
    assert trade["auto_passenger"] is False
    assert isinstance(trade["passenger_options"], dict)


def test_layout_v2_settings_survive_reopening_without_legacy_import(tmp_path):
    repository = ResonanceConfigRepository(base_path=tmp_path)
    repository.set_value("game/executable_path", "new.exe")
    repository.set_value("workflow/task_order", "startup,passenger,trade,battle,close")
    repository.save_preferences(GuiPreferences(timeout_sec=12.5, history_limit=23))
    repository.save_trade_inputs({"fatigue_budget": 321, "cargo_capacity": 456})
    repository.save_trade_preview_inputs({"fatigue_budget": 222})
    repository.save_hotkeys("Ctrl+F6", "Ctrl+F7")
    repository.sync_checked()

    reopened = ResonanceConfigRepository(
        base_path=tmp_path
    )
    assert reopened.value("game/executable_path") == "new.exe"
    assert reopened.value("workflow/task_order") == "startup,passenger,trade,battle,close"
    assert reopened.load_preferences() == GuiPreferences(timeout_sec=12.5, history_limit=23)
    assert reopened.load_trade_inputs()["fatigue_budget"] == 321
    assert reopened.load_trade_inputs()["cargo_capacity"] == 456
    assert reopened.load_trade_preview_inputs()["fatigue_budget"] == 222
    assert reopened.load_hotkeys() == {"start": "Ctrl+F6", "stop": "Ctrl+F7"}


def test_failed_new_settings_write_does_not_import_legacy(tmp_path):
    old_ini = tmp_path / "gui-settings.ini"
    old_content = b"[game]\nexecutable_path=old.exe\n"
    old_ini.write_bytes(old_content)
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("occupied", encoding="utf-8")
    repository = ResonanceConfigRepository(
        base_path=blocked
    )
    with pytest.raises(OSError):
        repository.save_hotkeys("Ctrl+F6", "Ctrl+F7")
    assert repository.value("game/executable_path", "") == ""
    assert old_ini.read_bytes() == old_content


def test_retired_passenger_settings_remain_inert_and_do_not_replace_freight_options(tmp_path):
    repository = ResonanceConfigRepository(base_path=tmp_path)
    legacy = '{"passenger_city_a_id":"11","trip_count":7}'
    repository.set_value("passenger/inputs_json", legacy)
    repository.save_trade_inputs({"auto_passenger": True, "passenger_options": {"seat_capacity": 64}})
    reopened = ResonanceConfigRepository(base_path=tmp_path)
    assert reopened.value("passenger/inputs_json") == legacy
    assert reopened.load_trade_inputs()["auto_passenger"] is True
    assert reopened.load_trade_inputs()["passenger_options"]["seat_capacity"] == 64
    assert not hasattr(reopened, "load_passenger_inputs")
