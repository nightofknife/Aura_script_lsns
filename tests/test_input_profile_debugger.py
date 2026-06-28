from __future__ import annotations

import argparse
import unittest

from tools.input_profile_debugger import collect_profile_debug


class TestInputProfileDebugger(unittest.TestCase):
    def test_collects_and_resolves_default_pc_profile(self):
        payload = collect_profile_debug(
            argparse.Namespace(
                plan="aura_base",
                profile="default_pc",
                action=["confirm"],
                resolve_all=False,
                json=False,
            )
        )

        self.assertEqual(payload["active_profile"], "default_pc")
        self.assertGreaterEqual(payload["summary"]["action_count"], 10)
        self.assertEqual(payload["resolved"][0]["action_name"], "confirm")
        self.assertEqual(payload["resolved"][0]["key"], "enter")
        self.assertEqual(payload["errors"], [])


if __name__ == "__main__":
    unittest.main()
