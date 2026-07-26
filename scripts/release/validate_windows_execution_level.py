from __future__ import annotations

import argparse
import json
from pathlib import Path
import xml.etree.ElementTree as ET


_MANIFEST_RESOURCE_TYPE = 24
_ASM_V3_NAMESPACE = "urn:schemas-microsoft-com:asm.v3"


def parse_execution_level(manifest: bytes | str) -> str:
    root = ET.fromstring(manifest)
    elements = root.findall(f".//{{{_ASM_V3_NAMESPACE}}}requestedExecutionLevel")
    if len(elements) != 1:
        raise ValueError(
            f"Expected exactly one requestedExecutionLevel element, found {len(elements)}."
        )
    level = str(elements[0].get("level") or "").strip()
    if not level:
        raise ValueError("requestedExecutionLevel is missing its level attribute.")
    return level


def read_execution_level(executable: Path) -> str:
    if not executable.is_file():
        raise FileNotFoundError(f"Executable not found: {executable}")

    try:
        from PyInstaller.utils.win32.winresource import get_resources
    except ImportError as exc:
        raise RuntimeError(
            "PyInstaller is required to inspect Windows executable resources."
        ) from exc

    resources = get_resources(str(executable), types=[_MANIFEST_RESOURCE_TYPE])
    manifests = [
        data
        for resources_by_name in resources.get(_MANIFEST_RESOURCE_TYPE, {}).values()
        for data in resources_by_name.values()
    ]
    if not manifests:
        raise ValueError(f"Executable has no Windows application manifest: {executable}")

    levels = {parse_execution_level(manifest) for manifest in manifests}
    if len(levels) != 1:
        raise ValueError(
            f"Executable contains conflicting execution levels {sorted(levels)}: {executable}"
        )
    return levels.pop()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate requestedExecutionLevel in Windows executable manifests."
    )
    parser.add_argument(
        "--expected-level",
        default="asInvoker",
        choices=("asInvoker", "highestAvailable", "requireAdministrator"),
    )
    parser.add_argument("executables", nargs="+", type=Path)
    args = parser.parse_args()

    results = []
    for executable in args.executables:
        resolved = executable.resolve()
        level = read_execution_level(resolved)
        if level != args.expected_level:
            raise ValueError(
                f"Expected execution level '{args.expected_level}', found '{level}': {resolved}"
            )
        results.append({"executable": str(resolved), "execution_level": level})

    print(json.dumps({"validated": results}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
