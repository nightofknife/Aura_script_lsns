from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional

from importlib import metadata
from packaging.requirements import Requirement
from packaging.specifiers import InvalidSpecifier

from packages.aura_core.config.loader import get_config_value
from packages.aura_core.observability.logging.core_logger import logger


@dataclass
class DependencyCheckResult:
    ok: bool
    missing: List[str]


class DependencyManager:
    """Plan dependency checker and installer (requirements.txt)."""

    def __init__(self, base_path: Path):
        self.base_path = base_path

    def ensure_plan_dependencies(self, plan_path: Path) -> DependencyCheckResult:
        req_file = plan_path / self._requirements_file_name()
        if not req_file.is_file():
            return DependencyCheckResult(ok=True, missing=[])

        requirements = self._read_requirements(req_file)
        missing = self._find_missing(requirements)
        if not missing:
            return DependencyCheckResult(ok=True, missing=[])

        logger.warning(
            "Plan dependencies missing for '%s': %s",
            plan_path.name,
            ", ".join(missing),
        )

        if self._auto_install_enabled():
            installed = self._install_requirements(req_file)
            if installed:
                missing = self._find_missing(requirements)
                if not missing:
                    return DependencyCheckResult(ok=True, missing=[])

        return DependencyCheckResult(ok=False, missing=missing)

    def _requirements_file_name(self) -> str:
        return str(get_config_value("dependencies.requirements_file", "requirements.txt"))

    def _auto_install_enabled(self) -> bool:
        return bool(get_config_value("dependencies.auto_install", True))

    def _pip_args(self) -> List[str]:
        args = get_config_value("dependencies.pip.args", [])
        return args if isinstance(args, list) else []

    def _pip_timeout(self) -> int:
        return int(get_config_value("dependencies.pip.timeout_sec", 120))

    def _install_requirements(self, req_file: Path) -> bool:
        cmd = [
            sys.executable,
            "-m",
            "pip",
            "install",
            "-r",
            str(req_file),
            *self._pip_args(),
        ]
        try:
            logger.info("Installing plan requirements: %s", " ".join(cmd))
            result = subprocess.run(cmd, check=False, timeout=self._pip_timeout())
            if result.returncode == 0:
                return True
            logger.error("pip install failed with code %s for %s", result.returncode, req_file)
            return False
        except subprocess.TimeoutExpired:
            logger.error("pip install timed out for %s", req_file)
            return False
        except Exception as exc:
            logger.error("pip install failed for %s: %s", req_file, exc)
            return False

    def _read_requirements(self, req_file: Path) -> List[Requirement]:
        raw = self._read_requirements_lines(req_file)
        requirements: List[Requirement] = []
        for line in raw:
            try:
                requirements.append(Requirement(line))
            except (InvalidSpecifier, ValueError):
                logger.warning("Skipping invalid requirement in %s: %s", req_file.name, line)
        return requirements

    def _read_requirements_lines(self, req_file: Path, seen: Optional[set] = None) -> List[str]:
        seen = seen or set()
        req_file = req_file.resolve()
        if req_file in seen:
            return []
        seen.add(req_file)

        lines: List[str] = []
        for raw in req_file.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue

            if line.startswith("-r") or line.startswith("--requirement"):
                parts = line.split(maxsplit=1)
                if len(parts) == 2:
                    nested = (req_file.parent / parts[1]).resolve()
                    if nested.is_file():
                        lines.extend(self._read_requirements_lines(nested, seen))
                    else:
                        logger.warning("Requirement file not found: %s", nested)
                continue

            if line.startswith("-"):
                continue

            lines.append(line)

        return lines

    def _find_missing(self, requirements: Iterable[Requirement]) -> List[str]:
        missing: List[str] = []
        for req in requirements:
            if req.marker and not req.marker.evaluate():
                continue
            try:
                version = metadata.version(req.name)
            except metadata.PackageNotFoundError:
                missing.append(str(req))
                continue
            if req.specifier and not req.specifier.contains(version, prereleases=True):
                missing.append(f"{req.name}{req.specifier}")
        return missing
