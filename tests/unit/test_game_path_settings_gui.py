"""Game-path notifications use fake executable files and never launch them."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from packages.resonance_gui.config_repository import ResonanceConfigRepository
from packages.resonance_gui.widgets import settings_hub_page
from packages.resonance_gui.widgets.settings_hub_page import SettingsHubPage


@pytest.fixture
def page(tmp_path):
    app = QApplication.instance() or QApplication(["game-path-setting-test"])
    widget = SettingsHubPage(ResonanceConfigRepository(base_path=tmp_path))
    yield widget
    widget.close()
    widget.deleteLater()
    app.processEvents()


@pytest.fixture
def fake_executable(tmp_path):
    path = tmp_path / "fake-client" / "雷索纳斯.exe"
    path.parent.mkdir()
    path.write_bytes(b"fake executable; never run")
    return path.resolve()


@pytest.mark.parametrize("save_method", ["browse", "detect_entered", "detect_registry"])
def test_successful_path_save_notifies_after_persistence(page, fake_executable, monkeypatch, save_method):
    saved = []
    page.gamePathSaved.connect(
        lambda path: saved.append((path, page._settings.value("game/executable_path")))
    )
    if save_method == "browse":
        monkeypatch.setattr(
            settings_hub_page.QFileDialog, "getOpenFileName",
            lambda *_: (str(fake_executable), "程序 (*.exe)"),
        )
        page._browse_executable()
    elif save_method == "detect_entered":
        page.executable_path.setText(f'"{fake_executable}"')
        page._detect_executable()
    else:
        monkeypatch.setattr(
            settings_hub_page, "find_registry_executables", lambda **_: (fake_executable,)
        )
        page._detect_executable()
    assert saved == [(str(fake_executable), str(fake_executable))]
    assert page.executable_path.text() == str(fake_executable)
    assert page.detect_result.property("status") == "success"


def test_editing_path_does_not_notify_before_validation(page, fake_executable):
    saved = []
    page.gamePathSaved.connect(saved.append)
    page.executable_path.setText(str(fake_executable))
    assert saved == []
    assert page._settings.value("game/executable_path", "") == ""


def test_invalid_path_does_not_notify_or_replace_saved_path(page, fake_executable):
    page._settings.set_value("game/executable_path", str(fake_executable))
    saved = []
    page.gamePathSaved.connect(saved.append)
    assert page._save_game_path(str(fake_executable.parent / "missing.exe")) is False
    assert saved == []
    assert page._settings.value("game/executable_path") == str(fake_executable)
    assert page.detect_result.property("status") == "warning"


def test_failed_path_flush_does_not_emit_success(page, fake_executable, monkeypatch):
    saved = []
    page.gamePathSaved.connect(saved.append)

    def failed_flush():
        raise OSError("injected flush failure")

    monkeypatch.setattr(page._settings, "sync_checked", failed_flush)
    assert page._save_game_path(str(fake_executable)) is False
    assert saved == []
    assert "保存失败" in page.detect_result.text()


def test_saving_empty_path_notifies_the_saved_clear(page, fake_executable):
    page._settings.set_value("game/executable_path", str(fake_executable))
    saved, settings_saved = [], []
    page.gamePathSaved.connect(
        lambda path: saved.append((path, page._settings.value("game/executable_path")))
    )
    page.settingsSaved.connect(lambda: settings_saved.append(True))
    page.executable_path.clear()
    page.save_values()
    assert saved == [("", "")]
    assert settings_saved == [True]
    assert page.save_result.text() == "设置已保存"
