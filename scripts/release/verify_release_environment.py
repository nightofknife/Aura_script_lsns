from __future__ import annotations

import argparse
from importlib import metadata
from pathlib import Path

from scripts.release.release_contract import normalize_distribution_name, parse_hashed_lock


BOOTSTRAP_ALLOWLIST = {"pip", "setuptools", "wheel"}


def installed_packages() -> dict[str, str]:
    return {
        normalize_distribution_name(distribution.metadata["Name"]): distribution.version
        for distribution in metadata.distributions()
        if distribution.metadata.get("Name")
    }


def validate_environment(lock_path: Path) -> None:
    expected = parse_hashed_lock(lock_path)
    actual = installed_packages()
    missing = {name: version for name, version in expected.items() if actual.get(name) != version}
    extras = {
        name: version
        for name, version in actual.items()
        if name not in expected and name not in BOOTSTRAP_ALLOWLIST
    }
    if missing or extras:
        details = []
        if missing:
            details.append("missing/mismatched=" + ", ".join(f"{name}=={version}" for name, version in sorted(missing.items())))
        if extras:
            details.append("unexpected=" + ", ".join(f"{name}=={version}" for name, version in sorted(extras.items())))
        raise ValueError("Release environment does not exactly match its lock: " + "; ".join(details))


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify an isolated release environment against a hashed lock.")
    parser.add_argument("lock", type=Path)
    args = parser.parse_args()
    validate_environment(args.lock.resolve())
    print(f"Release environment matches {args.lock}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
