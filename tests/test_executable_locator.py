from __future__ import annotations

from packages.aura_game.executable_locator import validate_executable_path


def test_validate_executable_path_requires_existing_exact_executable_name(tmp_path):
    executable = tmp_path / "雷索纳斯.exe"
    executable.touch()
    wrong_name = tmp_path / "launcher.exe"
    wrong_name.touch()

    assert validate_executable_path(
        f'"{executable}"', executable_name="雷索纳斯.exe"
    ) == executable.resolve()
    assert validate_executable_path(wrong_name, executable_name="雷索纳斯.exe") is None
    assert validate_executable_path(tmp_path / "missing.exe", executable_name="雷索纳斯.exe") is None
    assert validate_executable_path("", executable_name="雷索纳斯.exe") is None
