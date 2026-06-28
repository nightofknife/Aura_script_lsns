from __future__ import annotations

import unittest
from unittest.mock import patch

from packages.aura_core.runtime.privilege import AdminPrivilegeRequiredError, ensure_admin_startup


class TestRuntimePrivilege(unittest.TestCase):
    def test_ensure_admin_startup_passes_for_admin_process(self):
        with (
            patch("packages.aura_core.runtime.privilege.os.name", "nt"),
            patch("packages.aura_core.runtime.privilege.is_running_as_admin", return_value=True),
        ):
            ensure_admin_startup("Aura Scheduler")

    def test_ensure_admin_startup_raises_for_non_admin_process(self):
        with (
            patch("packages.aura_core.runtime.privilege.os.name", "nt"),
            patch("packages.aura_core.runtime.privilege.is_running_as_admin", return_value=False),
        ):
            with self.assertRaises(AdminPrivilegeRequiredError) as cm:
                ensure_admin_startup("Aura Scheduler")

        self.assertIn("administrator privileges", str(cm.exception))
        self.assertEqual(cm.exception.context, "Aura Scheduler")
