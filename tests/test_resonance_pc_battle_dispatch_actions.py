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
    resonance_pc_prepare_battle_formation,
    resonance_pc_select_action_summary_stage,
    resonance_pc_validate_battle_jobs,
)


class TestResonanceBattleDispatchActions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        task_file = Path("plans/resonance_pc/tasks/auto_battle_dispatch_pc.yaml")
        cls.task_data = yaml.safe_load(task_file.read_text(encoding="utf-8"))
        combat_task_file = Path("plans/resonance_pc/tasks/auto_battle_combat_pc.yaml")
        cls.combat_task_data = yaml.safe_load(combat_task_file.read_text(encoding="utf-8"))

    def test_ocr_normalization_ignores_action_summary_decorative_punctuation(self):
        expected = battle_dispatch_pc_actions._normalize_text("特供·救世")
        self.assertEqual(battle_dispatch_pc_actions._normalize_text("“特供?救世"), expected)
        self.assertEqual(battle_dispatch_pc_actions._normalize_text("特供•救世"), expected)

    def test_global_supply_ocr_labels_use_only_distinctive_suffixes(self):
        self.assertEqual(battle_dispatch_pc_actions._ACTION_SUMMARY_STAGE_OCR_TEXT["savior"], "救世")
        self.assertEqual(battle_dispatch_pc_actions._ACTION_SUMMARY_STAGE_OCR_TEXT["standard"], "制式")
        self.assertEqual(battle_dispatch_pc_actions._ACTION_SUMMARY_STAGE_OCR_TEXT["elegant"], "雅致")

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

    @patch("plans.resonance_pc.src.actions.battle_dispatch_pc_actions.time.sleep")
    def test_prepare_battle_formation_keeps_current_when_not_requested(self, sleep_mock):
        app = Mock()

        out = resonance_pc_prepare_battle_formation(
            formation_index=None,
            settle_sec=0.5,
            app=app,
        )

        app.click.assert_not_called()
        sleep_mock.assert_called_once_with(0.5)
        self.assertEqual(
            out,
            {
                "ok": True,
                "formation_index": None,
                "selection_changed": False,
                "click_point": None,
                "settle_sec": 0.5,
            },
        )

    @patch("plans.resonance_pc.src.actions.battle_dispatch_pc_actions.time.sleep")
    def test_prepare_battle_formation_clicks_requested_slot(self, sleep_mock):
        expected_points = {
            1: (310, 40),
            2: (490, 40),
            3: (660, 40),
            4: (840, 40),
        }

        for formation_index, (x, y) in expected_points.items():
            with self.subTest(formation_index=formation_index):
                app = Mock()
                sleep_mock.reset_mock()

                out = resonance_pc_prepare_battle_formation(
                    formation_index=formation_index,
                    settle_sec=0.5,
                    app=app,
                )

                app.click.assert_called_once_with(x=x, y=y)
                sleep_mock.assert_called_once_with(0.5)
                self.assertTrue(out["selection_changed"])
                self.assertEqual(out["formation_index"], formation_index)
                self.assertEqual(out["click_point"], [x, y])

    def test_prepare_battle_formation_rejects_invalid_slot(self):
        app = Mock()

        with self.assertRaises(ResonancePcBattleDispatchError) as cm:
            resonance_pc_prepare_battle_formation(formation_index=5, app=app)

        self.assertEqual(cm.exception.code, "invalid_formation_index")
        app.click.assert_not_called()

    def test_combat_task_uses_single_formation_action(self):
        steps = self.combat_task_data["auto_battle_combat_pc"]["steps"]

        self.assertNotIn("click_formation_1", steps)
        self.assertNotIn("click_formation_2", steps)
        self.assertNotIn("click_formation_3", steps)
        self.assertNotIn("click_formation_4", steps)
        self.assertNotIn("wait_after_formation", steps)
        self.assertEqual(
            steps["prepare_formation"],
            {
                "action": "resonance_pc.prepare_battle_formation",
                "params": {
                    "formation_index": "{{ inputs.formation_index | default(none) }}",
                    "settle_sec": 0.5,
                },
                "depends_on": "log_combat_job",
            },
        )
        self.assertEqual(steps["click_start_entry"]["depends_on"], "prepare_formation")

    def test_combat_inputs_are_forwarded_from_job_tasks(self):
        cases = [
            ("auto_battle_ct_tie_an_batch_pc", "run_tie_an_jobs"),
            ("auto_battle_ct_regional_ops_batch_pc", "run_regional_ops_jobs"),
            ("auto_battle_gp_action_summary_run_group_pc", "run_group_jobs"),
        ]
        for task_name, step_name in cases:
            with self.subTest(task_name=task_name):
                inputs = self.task_data[task_name]["steps"][step_name]["params"]["inputs"]
                self.assertIn("formation_index", inputs)
                self.assertIn("capture_count", inputs)

        combat_call_cases = [
            ("auto_battle_ct_tie_an_run_one_pc", "run_combat_after_go_combat_with_difficulty"),
            ("auto_battle_ct_tie_an_run_one_pc", "run_combat_after_go_combat_without_difficulty"),
            ("auto_battle_ct_regional_ops_run_one_pc", "run_combat_after_go_combat"),
            ("auto_battle_gp_action_summary_run_difficulty_pc", "run_combat_after_start_battle"),
        ]
        for task_name, step_name in combat_call_cases:
            with self.subTest(task_name=task_name, step_name=step_name):
                inputs = self.task_data[task_name]["steps"][step_name]["params"]["inputs"]
                self.assertIn("formation_index", inputs)
                self.assertIn("capture_count", inputs)

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
        enter_button = [
            {
                "text": "进入挑战",
                "normalized": battle_dispatch_pc_actions._normalize_text("进入挑战"),
                "center": (948, 606),
                "confidence": 0.99,
            }
        ]
        transition = [
            {
                "text": "开始作战",
                "normalized": battle_dispatch_pc_actions._normalize_text("开始作战"),
                "center": (965, 502),
                "confidence": 0.98,
            }
        ]
        app = Mock()
        ocr = Mock()

        with (
            patch.object(
                battle_dispatch_pc_actions,
                "_recognize_text_items",
                side_effect=[first_page, second_page, enter_button, transition],
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
        app.click.assert_called_once_with(x=948, y=606)
        self.assertTrue(out["found"])
        self.assertEqual(out["stage_name"], "武器材质分析")
        self.assertEqual(out["button_region"], [812, 563, 258, 95])
        self.assertTrue(out["transition_confirmed"])
        self.assertEqual(out["transition_text"], "开始作战")
        messages = [str(call.args[0]) for call in log_info.call_args_list]
        self.assertTrue(any("[BattleOCR][ActionSummaryStage]" in message for message in messages))
        self.assertTrue(any("[BattleDrag][ActionSummaryStage]" in message for message in messages))

    @patch("plans.resonance_pc.src.actions.battle_dispatch_pc_actions.time.sleep", return_value=None)
    def test_action_summary_selector_reports_unavailable_stage(self, _sleep):
        title = [
            {
                "text": "特供·救世",
                "normalized": battle_dispatch_pc_actions._normalize_text("特供·救世"),
                "center": (577, 422),
                "confidence": 0.87,
            }
        ]
        unavailable = [
            {
                "text": "周一、日开放",
                "normalized": battle_dispatch_pc_actions._normalize_text("周一、日开放"),
                "center": (590, 606),
                "confidence": 0.98,
            }
        ]
        with patch.object(
            battle_dispatch_pc_actions,
            "_recognize_text_items",
            side_effect=[title, unavailable],
        ):
            with self.assertRaises(ResonancePcBattleDispatchError) as cm:
                resonance_pc_select_action_summary_stage(
                    route_id="gp.action_summary.global_supply.savior",
                    app=Mock(),
                    ocr=Mock(),
                )

        self.assertEqual(cm.exception.code, "action_summary_stage_unavailable")

    @patch("plans.resonance_pc.src.actions.battle_dispatch_pc_actions.time.sleep", return_value=None)
    def test_action_summary_selector_retries_when_button_remains_visible(self, _sleep):
        title = [
            {
                "text": "特供·救世",
                "normalized": battle_dispatch_pc_actions._normalize_text("特供·救世"),
                "center": (577, 422),
                "confidence": 0.87,
            }
        ]
        enter_button = [
            {
                "text": "进入挑战",
                "normalized": battle_dispatch_pc_actions._normalize_text("进入挑战"),
                "center": (594, 606),
                "confidence": 0.99,
            }
        ]
        app = Mock()
        with patch.object(
            battle_dispatch_pc_actions,
            "_recognize_text_items",
            side_effect=[title, enter_button, [], enter_button, [], enter_button],
        ):
            with self.assertRaises(ResonancePcBattleDispatchError) as cm:
                resonance_pc_select_action_summary_stage(
                    route_id="gp.action_summary.global_supply.savior",
                    button_click_attempts=2,
                    transition_timeout_sec=0,
                    app=app,
                    ocr=Mock(),
                )

        self.assertEqual(cm.exception.code, "action_summary_enter_transition_failed")
        self.assertEqual(app.click.call_count, 2)

    def test_action_summary_task_uses_swapped_drag_params(self):
        params = self.task_data["auto_battle_gp_action_summary_run_group_pc"]["steps"]["select_stage_and_enter"]["params"]
        self.assertEqual(params["drag_forward"], [1100, 400, 700, 400])
        self.assertEqual(params["drag_backward"], [700, 400, 1100, 400])
        self.assertEqual(params["drag_hold_before_release_sec"], 0.5)
        self.assertNotIn("click_offset_x", params)
        self.assertNotIn("click_offset_y", params)
        self.assertEqual(params["enter_button_text"], "进入挑战")
        self.assertEqual(params["button_region_left_offset"], -128)
        self.assertEqual(params["button_region_top_offset"], 143)
        self.assertEqual(params["transition_text"], "开始作战")
        self.assertEqual(params["transition_region"], [790, 460, 430, 100])
        self.assertEqual(params["transition_timeout_sec"], 4.0)

        steps = self.task_data["auto_battle_gp_action_summary_run_group_pc"]["steps"]
        self.assertNotIn("wait_after_enter_challenge", steps)
        self.assertEqual(steps["run_group_jobs"]["depends_on"], "select_stage_and_enter")

    def test_action_summary_difficulty_requires_start_battle_business_success(self):
        steps = self.task_data["auto_battle_gp_action_summary_run_difficulty_pc"]["steps"]
        wait_step = steps["wait_after_difficulty"]
        self.assertEqual(wait_step["action"], "plans/aura_base/wait_for_text")
        self.assertEqual(wait_step["params"]["text_to_find"], "开始作战")
        self.assertEqual(wait_step["params"]["region"], [790, 460, 430, 100])
        self.assertEqual(wait_step["params"]["timeout"], 4.0)
        self.assertEqual(steps["click_start_battle"]["params"]["region"], [790, 460, 430, 100])
        assertion = steps["assert_start_battle_clicked"]
        self.assertEqual(assertion["action"], "plans/aura_base/assert_condition")
        self.assertIn("nodes.click_start_battle.output", assertion["params"]["condition"])
        self.assertEqual(steps["run_combat_after_start_battle"]["depends_on"], "assert_start_battle_clicked")

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
