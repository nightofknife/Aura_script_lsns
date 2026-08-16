"""Read-only helpers for validating and locating installed Windows programs."""

from __future__ import annotations

import os
from pathlib import Path


def validate_executable_path(value: str | Path | None, *, executable_name: str) -> Path | None:
    text = str(value or "").strip().strip('"')
    if not text:
        return None
    candidate = Path(os.path.expandvars(os.path.expanduser(text)))
    if not candidate.is_file() or candidate.name.casefold() != executable_name.casefold():
        return None
    return candidate.resolve()


def find_registry_executables(
    *,
    display_name_fragment: str,
    executable_name: str,
) -> tuple[Path, ...]:
    if os.name != "nt":
        return ()
    import winreg

    roots = (
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
    )
    matches: dict[str, Path] = {}
    for hive, key_path in roots:
        try:
            with winreg.OpenKey(hive, key_path) as root:
                for index in range(winreg.QueryInfoKey(root)[0]):
                    try:
                        with winreg.OpenKey(root, winreg.EnumKey(root, index)) as entry:
                            display_name = str(winreg.QueryValueEx(entry, "DisplayName")[0] or "")
                            if display_name_fragment.casefold() not in display_name.casefold():
                                continue
                            raw_candidates: list[Path] = []
                            try:
                                display_icon = str(winreg.QueryValueEx(entry, "DisplayIcon")[0] or "")
                            except OSError:
                                display_icon = ""
                            try:
                                install_location = str(
                                    winreg.QueryValueEx(entry, "InstallLocation")[0] or ""
                                )
                            except OSError:
                                install_location = ""
                            if display_icon:
                                raw_candidates.append(
                                    Path(display_icon.strip().strip('"').split(",", 1)[0])
                                )
                            if install_location:
                                raw_candidates.append(Path(install_location.strip().strip('"')) / executable_name)
                            for candidate in raw_candidates:
                                validated = validate_executable_path(
                                    candidate, executable_name=executable_name
                                )
                                if validated is not None:
                                    matches[str(validated).casefold()] = validated
                    except OSError:
                        continue
        except OSError:
            continue
    return tuple(matches.values())


__all__ = ["find_registry_executables", "validate_executable_path"]
