from __future__ import annotations

import unittest
from unittest.mock import patch

from packages.aura_core.runtime.privilege import ensure_admin_startup


class TestRuntimePrivilege(unittest.TestCase):
    def test_ensure_admin_startup_passes_for_admin_process(self):
        with (
            patch("packages.aura_core.runtime.privilege.os.name", "nt"),
            patch("packages.aura_core.runtime.privilege.is_running_as_admin", return_value=True),
        ):
            ensure_admin_startup("Aura Scheduler")

    def test_ensure_admin_startup_allows_non_admin_process(self):
        with (
            patch("packages.aura_core.runtime.privilege.os.name", "nt"),
            patch("packages.aura_core.runtime.privilege.is_running_as_admin", return_value=False),
        ):
            ensure_admin_startup("Aura Scheduler")
