from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from packages.aura_core.packaging.core.package_manager import PackageManager


class TestPackageManagerGameLayout(unittest.TestCase):
    def test_discovers_game_yaml_under_games_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            packages_dir = root / "packages"
            plans_dir = root / "plans"
            games_dir = root / "games" / "demo_game"
            packages_dir.mkdir()
            plans_dir.mkdir()
            games_dir.mkdir(parents=True)

            (games_dir / "game.yaml").write_text(
                "\n".join(
                    [
                        "package:",
                        "  name: '@games/demo_game'",
                        "  version: '0.1.0'",
                        "  description: ''",
                        "  license: MIT",
                        "exports:",
                        "  services: []",
                        "  actions: []",
                        "  tasks: []",
                    ]
                ),
                encoding="utf-8",
            )

            manager = PackageManager(packages_dir=packages_dir, plans_dir=plans_dir)

            manifests = manager._discover_packages()

            self.assertIn("games/demo_game", manifests)
            self.assertEqual(manifests["games/demo_game"].path, games_dir)


if __name__ == "__main__":
    unittest.main()
