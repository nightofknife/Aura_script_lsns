from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import yaml

from plans.resonance_pc.src.actions import battle_dispatch_pc_actions
from plans.resonance_pc.src.actions.battle_dispatch_pc_actions import (
    ResonancePcBattleDispatchError,
    resonance_pc_group_consecutive_jobs_by_route,
    resonance_pc_group_gp_jobs,
    resonance_pc_select_action_summary_stage,
    resonance_pc_validate_battle_jobs,
)


class TestResonanceBattleDispatchActions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        task_file = Path("plans/resonance_pc/tasks/auto_battle_dispatch_pc.yaml")
        cls.task_data = yaml.safe_load(task_file.read_text(encoding="utf-8"))

    def test_tie_an_expel_missing_stage_fails(self):
        jobs = [
            {
                "route_id": "ct.tie_an.shoggolith_city.expel",
                "difficulty": 3,
            }
        ]

        with self.assertRaises(ResonancePcBattleDispatchError) as cm:
            resonance_pc_validate_battle_jobs(jobs)

        self.assertEqual(cm.exception.code, "invalid_tie_an_expel")

    def test_regional_missing_threat_level_fails(self):
        jobs = [
            {
                "route_id": "ct.regional_ops_center.wilderness_station",
                "difficulty": 2,
            }
        ]

        with self.assertRaises(ResonancePcBattleDispatchError) as cm:
            resonance_pc_validate_battle_jobs(jobs)

        self.assertEqual(cm.exception.code, "invalid_regional_ops")

    def test_gp_action_summary_missing_difficulty_fails(self):
        jobs = [
            {
                "route_id": "gp.action_summary.blade_encirclement.special_order",
            }
        ]

        with self.assertRaises(ResonancePcBattleDispatchError) as cm:
            resonance_pc_validate_battle_jobs(jobs)

        self.assertEqual(cm.exception.code, "invalid_gp_action_summary")

    def test_tie_an_bounty_drops_incompatible_difficulty(self):
        jobs = [
            {
                "route_id": "ct.tie_an.shoggolith_city.bounty",
                "difficulty": 2,
            }
        ]

        out = resonance_pc_validate_battle_jobs(jobs)

        self.assertTrue(out["ok"])
        self.assertIsNone(out["normalized_jobs"][0]["difficulty"])

    def test_gp_structural_drops_incompatible_difficulty(self):
        jobs = [
            {
                "route_id": "gp.structural_exploration.echo_buoy",
                "difficulty": 2,
            }
        ]

        out = resonance_pc_validate_battle_jobs(jobs)

        self.assertTrue(out["ok"])
        self.assertIsNone(out["normalized_jobs"][0]["difficulty"])

    def test_gp_structural_drops_out_of_range_incompatible_difficulty(self):
        jobs = [
            {
                "route_id": "gp.structural_exploration.echo_buoy",
                "difficulty": 7,
            }
        ]

        out = resonance_pc_validate_battle_jobs(jobs)

        self.assertTrue(out["ok"])
        self.assertIsNone(out["normalized_jobs"][0]["difficulty"])

    def test_unknown_route_id_fails(self):
        jobs = [
            {
                "route_id": "ct.tie_an.unknown_city.expel",
                "difficulty": 1,
                "stage": 1,
            }
        ]

        with self.assertRaises(ResonancePcBattleDispatchError) as cm:
            resonance_pc_validate_battle_jobs(jobs)

        self.assertEqual(cm.exception.code, "unknown_route_id")

    def test_valid_mixed_jobs_are_normalized(self):
        jobs = [
            {
                "route_id": "ct.tie_an.shoggolith_city.expel",
                "difficulty": 4,
                "stage": 2,
            },
            {
                "route_id": "ct.tie_an.shoggolith_city.bounty",
            },
            {
                "route_id": "ct.regional_ops_center.wilderness_station",
                "difficulty": 5,
                "threat_level": 11,
            },
            {
                "route_id": "gp.action_summary.global_supply.savior",
                "difficulty": 2,
            },
        ]

        out = resonance_pc_validate_battle_jobs(jobs)
        self.assertTrue(out["ok"])
        self.assertEqual(out["job_count"], 4)

        n0 = out["normalized_jobs"][0]
        self.assertEqual(n0["ct_subcategory"], "tie_an")
        self.assertEqual(n0["mission_type"], "expel")
        self.assertEqual(n0["stage"], 2)
        self.assertEqual(n0["difficulty"], 4)

        n1 = out["normalized_jobs"][1]
        self.assertEqual(n1["mission_type"], "bounty")
        self.assertIsNone(n1["stage"])
        self.assertIsNone(n1["threat_level"])

        n2 = out["normalized_jobs"][2]
        self.assertEqual(n2["ct_subcategory"], "regional_ops_center")
        self.assertEqual(n2["threat_level"], 11)
        self.assertEqual(n2["difficulty"], 5)

        n3 = out["normalized_jobs"][3]
        self.assertEqual(n3["main_category"], "gp")
        self.assertEqual(n3["gp_subcategory"], "action_summary")
        self.assertEqual(n3["gp_group_key"], "global_supply")
        self.assertEqual(n3["gp_stage_name"], "特供·救世")

    def test_run_count_field_is_rejected(self):
        jobs = [
            {
                "route_id": "ct.tie_an.shoggolith_city.expel",
                "difficulty": 2,
                "stage": 1,
                "run_count": 2,
            }
        ]

        with self.assertRaises(ResonancePcBattleDispatchError) as cm:
            resonance_pc_validate_battle_jobs(jobs)

        self.assertEqual(cm.exception.code, "invalid_job_field")

    def test_group_gp_jobs_preserves_first_seen_order(self):
        jobs = [
            {"route_id": "gp.structural_exploration.echo_buoy"},
            {"route_id": "gp.action_summary.global_supply.savior"},
            {"route_id": "gp.structural_exploration.birch_buoy"},
        ]

        out = resonance_pc_group_gp_jobs(jobs)
        self.assertEqual(out["category_order"], ["structural_exploration", "action_summary"])
        self.assertEqual(len(out["structural_exploration_jobs"]), 2)
        self.assertEqual(len(out["action_summary_jobs"]), 1)

    def test_group_consecutive_jobs_by_route(self):
        jobs = [
            {"route_id": "gp.action_summary.global_supply.savior", "difficulty": 1},
            {"route_id": "gp.action_summary.global_supply.savior", "difficulty": 2},
            {"route_id": "gp.action_summary.global_supply.standard", "difficulty": 1},
        ]

        out = resonance_pc_group_consecutive_jobs_by_route(jobs)
        self.assertEqual(out["group_count"], 2)
        self.assertEqual(out["groups"][0]["route_id"], "gp.action_summary.global_supply.savior")
        self.assertEqual(out["groups"][0]["job_count"], 2)
        self.assertEqual(out["groups"][1]["route_id"], "gp.action_summary.global_supply.standard")

    def test_battle_ocr_helper_logs_recognized_items(self):
        app = Mock()
        app.capture.return_value = SimpleNamespace(success=True, image=object())
        ocr = Mock()
        ocr.recognize_all.return_value = SimpleNamespace(
            results=[
                SimpleNamespace(
                    text="特供救世",
                    center_point=(120, 80),
                    rect=(100, 60, 80, 30),
                    confidence=0.93,
                )
            ]
        )

        with patch.object(battle_dispatch_pc_actions.logger, "debug") as log_debug:
            items = battle_dispatch_pc_actions._recognize_text_items(app, ocr, (10, 20, 300, 200))

        self.assertEqual(items[0]["text"], "特供救世")
        self.assertEqual(items[0]["center"], (130, 100))
        self.assertTrue(
            any("[BattleOCR]" in str(call.args[0]) and call.args[2] == 1 for call in log_debug.call_args_list)
        )

    @patch("plans.resonance_pc.src.actions.battle_dispatch_pc_actions.time.sleep", return_value=None)
    def test_action_summary_selector_uses_left_drag_for_later_stage(self, _sleep):
        first_page = [
            {
                "text": "特殊订单",
                "normalized": battle_dispatch_pc_actions._normalize_text("特殊订单"),
                "center": (520, 420),
                "confidence": 0.95,
            },
            {
                "text": "利刃行动",
                "normalized": battle_dispatch_pc_actions._normalize_text("利刃行动"),
                "center": (760, 420),
                "confidence": 0.95,
            },
            {
                "text": "挑灯看剑",
                "normalized": battle_dispatch_pc_actions._normalize_text("挑灯看剑"),
                "center": (1000, 420),
                "confidence": 0.95,
            },
        ]
        second_page = [
            {
                "text": "武器材质分析",
                "normalized": battle_dispatch_pc_actions._normalize_text("武器材质分析"),
                "center": (940, 420),
                "confidence": 0.95,
            }
        ]
        app = Mock()
        ocr = Mock()

        with (
            patch.object(
                battle_dispatch_pc_actions,
                "_recognize_text_items",
                side_effect=[first_page, second_page],
            ),
            patch.object(battle_dispatch_pc_actions.logger, "info") as log_info,
        ):
            out = resonance_pc_select_action_summary_stage(
                route_id="gp.action_summary.blade_encirclement.weapon_material_analysis",
                drag_forward=[1100, 400, 700, 400],
                drag_backward=[700, 400, 1100, 400],
                app=app,
                ocr=ocr,
            )

        app.drag.assert_called_once_with(
            start_x=1100,
            start_y=400,
            end_x=700,
            end_y=400,
            duration=0.5,
            hold_before_release_sec=0.5,
        )
        app.click.assert_called_once_with(x=940, y=600)
        self.assertTrue(out["found"])
        self.assertEqual(out["stage_name"], "武器材质分析")
        messages = [str(call.args[0]) for call in log_info.call_args_list]
        self.assertTrue(any("[BattleOCR][ActionSummaryStage]" in message for message in messages))
        self.assertTrue(any("[BattleDrag][ActionSummaryStage]" in message for message in messages))

    def test_action_summary_task_uses_swapped_drag_params(self):
        params = self.task_data["auto_battle_gp_action_summary_run_group_pc"]["steps"]["select_stage_and_enter"]["params"]
        self.assertEqual(params["drag_forward"], [1100, 400, 700, 400])
        self.assertEqual(params["drag_backward"], [700, 400, 1100, 400])
        self.assertEqual(params["drag_hold_before_release_sec"], 0.5)

    def test_all_pc_battle_task_drags_hold_before_release(self):
        expected_custom_actions = {
            "resonance_pc.select_ordered_city": 2,
            "resonance_pc.select_threat_level_numeric": 1,
            "resonance_pc.select_action_summary_stage": 1,
        }
        observed_custom_actions = {name: 0 for name in expected_custom_actions}
        direct_drag_count = 0

        for task in self.task_data.values():
            for step in task.get("steps", {}).values():
                action = step.get("action")
                if action in expected_custom_actions:
                    observed_custom_actions[action] += 1
                    self.assertEqual(step.get("params", {}).get("drag_hold_before_release_sec"), 0.5)
                if action == "plans/aura_base/drag":
                    direct_drag_count += 1
                    self.assertEqual(step.get("params", {}).get("hold_before_release_sec"), 0.5)

        self.assertEqual(observed_custom_actions, expected_custom_actions)
        self.assertEqual(direct_drag_count, 1)

    def test_structural_target_roi_is_expanded(self):
        params = self.task_data["auto_battle_gp_structural_run_one_pc"]["steps"]["reconcile_structural_selection"]["params"]
        self.assertEqual(params["region"], [70, 360, 220, 270])

    def test_gp_to_ct_menu_recovery_steps_exist(self):
        steps = self.task_data["auto_battle_dispatch_pc"]["steps"]

        recover = steps["recover_ct_menu_after_first_gp"]
        self.assertEqual(recover["action"], "plans/aura_base/drag")
        self.assertEqual(recover["params"]["start_x"], 117)
        self.assertEqual(recover["params"]["start_y"], 334)
        self.assertEqual(recover["params"]["end_x"], 117)
        self.assertEqual(recover["params"]["end_y"], 447)
        self.assertEqual(recover["params"]["duration"], 0.4)
        self.assertEqual(recover["params"]["hold_before_release_sec"], 0.5)
        self.assertEqual(
            recover["depends_on"],
            {
                "all": [
                    {"run_first_ct_batch": "success|failed|skipped"},
                    {"run_first_gp_batch": "success|failed|skipped"},
                ]
            },
        )

        wait_step = steps["wait_after_ct_menu_recovery"]
        self.assertEqual(wait_step["depends_on"], "recover_ct_menu_after_first_gp")

        switch_second_ct = steps["switch_second_ct"]
        self.assertEqual(switch_second_ct["depends_on"], "wait_after_ct_menu_recovery")


if __name__ == "__main__":
    unittest.main()
