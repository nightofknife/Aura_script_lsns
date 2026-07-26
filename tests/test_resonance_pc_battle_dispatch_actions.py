from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import cv2
import numpy as np
import yaml

from packages.aura_core.context.execution import ExecutionContext
from packages.aura_core.engine.execution_engine import ExecutionEngine, StepState
from packages.aura_core.engine.node_executor import NodeExecutor
from plans.resonance_pc.src.actions import battle_dispatch_pc_actions
from plans.resonance_pc.src.actions.battle_dispatch_pc_actions import (
    ResonancePcBattleDispatchError,
    resonance_pc_detect_and_cancel_insufficient_stamina,
    resonance_pc_group_consecutive_jobs_by_route,
    resonance_pc_group_gp_jobs,
    resonance_pc_prepare_battle_formation,
    resonance_pc_select_action_summary_stage,
    resonance_pc_select_threat_level_numeric,
    resonance_pc_try_select_action_summary_stage,
    resonance_pc_validate_battle_jobs,
    resonance_pc_wait_and_click_back_button,
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

    def test_threat_level_preprocessing_removes_blue_and_red_card_backgrounds(self):
        blue_card = np.full((70, 220, 3), (150, 70, 20), dtype=np.uint8)
        red_card = np.full((70, 220, 3), (25, 25, 150), dtype=np.uint8)
        for image in (blue_card, red_card):
            cv2.putText(
                image,
                "101",
                (20, 55),
                cv2.FONT_HERSHEY_SIMPLEX,
                1.8,
                (230, 230, 230),
                4,
                cv2.LINE_AA,
            )

        blue_processed = battle_dispatch_pc_actions._preprocess_threat_level_image(blue_card)
        red_processed = battle_dispatch_pc_actions._preprocess_threat_level_image(red_card)

        self.assertEqual(blue_processed.shape, (70, 220, 3))
        self.assertEqual(int(blue_processed[0, 0, 0]), 0)
        self.assertEqual(int(red_processed[0, 0, 0]), 0)
        blue_foreground = int(np.count_nonzero(blue_processed[:, :, 0] > 192))
        red_foreground = int(np.count_nonzero(red_processed[:, :, 0] > 192))
        self.assertGreater(blue_foreground, 100)
        self.assertLessEqual(abs(blue_foreground - red_foreground), 10)

    def test_threat_level_selector_ocr_uses_preprocessed_image_and_restores_coordinates(self):
        image = np.full((70, 710, 3), (150, 70, 20), dtype=np.uint8)
        cv2.putText(
            image,
            "1",
            (125, 58),
            cv2.FONT_HERSHEY_SIMPLEX,
            2.0,
            (230, 230, 230),
            5,
            cv2.LINE_AA,
        )
        app = Mock()
        app.capture.return_value = SimpleNamespace(success=True, image=image)
        ocr = Mock()
        ocr.recognize_all.return_value = SimpleNamespace(
            results=[
                SimpleNamespace(
                    text="1",
                    center_point=(150, 30),
                    rect=(135, 10, 30, 40),
                    confidence=0.99,
                )
            ]
        )

        out = resonance_pc_select_threat_level_numeric(
            threat_level=1,
            region=[540, 200, 710, 70],
            max_attempts=1,
            after_drag_sec=0.0,
            app=app,
            ocr=ocr,
        )

        self.assertTrue(out["found"])
        self.assertEqual(out["click_x"], 690)
        self.assertEqual(out["click_y"], 230)
        app.click.assert_called_once_with(x=690, y=230)
        processed = ocr.recognize_all.call_args.args[0]
        self.assertEqual(processed.shape, (70, 710, 3))
        self.assertEqual(int(processed[0, 0, 0]), 0)

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

    def test_action_summary_stage_cleanup_runs_after_enter_attempt(self):
        steps = self.task_data["auto_battle_gp_action_summary_run_group_pc"]["steps"]
        self.assertEqual(
            steps["select_stage_and_enter"]["action"],
            "resonance_pc.try_select_action_summary_stage",
        )
        self.assertEqual(
            steps["run_group_jobs"]["when"],
            "{{ nodes.select_stage_and_enter.output.success }}",
        )
        self.assertEqual(
            steps["click_back_to_action_summary"]["depends_on"],
            {
                "all": [
                    {"select_stage_and_enter": "success"},
                    {"run_group_jobs": "success|failed|skipped"},
                ]
            },
        )
        self.assertEqual(
            steps["wait_after_back_to_action_summary"]["depends_on"],
            "click_back_to_action_summary",
        )
        self.assertEqual(
            steps["assert_stage_entry_succeeded"]["depends_on"],
            "wait_after_back_to_action_summary",
        )
        self.assertEqual(
            steps["click_back_to_action_summary"]["action"],
            "resonance_pc.wait_and_click_back_button",
        )

    def test_try_action_summary_stage_returns_expected_failure_as_data(self):
        error = ResonancePcBattleDispatchError(
            code="action_summary_stage_unavailable",
            message="stage is closed",
        )
        with patch.object(
            battle_dispatch_pc_actions,
            "resonance_pc_select_action_summary_stage",
            side_effect=error,
        ):
            out = resonance_pc_try_select_action_summary_stage(
                route_id="gp.action_summary.global_supply.standard",
                app=Mock(),
                ocr=Mock(),
            )

        self.assertFalse(out["success"])
        self.assertEqual(out["error_code"], "action_summary_stage_unavailable")
        self.assertIn("stage is closed", out["error_message"])

    def test_back_button_template_is_matched_before_click(self):
        image = np.zeros((70, 160, 3), dtype=np.uint8)
        app = Mock()
        app.capture.return_value = SimpleNamespace(success=True, image=image)
        vision = Mock()
        vision.find_template.return_value = SimpleNamespace(
            found=True,
            center_point=(80, 31),
            rect=(14, 8, 133, 47),
            confidence=0.99,
        )

        out = resonance_pc_wait_and_click_back_button(
            region=[10, 5, 160, 70],
            template="templates/battle_back_button.png",
            threshold=0.9,
            timeout_sec=0.1,
            interval_sec=0.01,
            stable_scans=2,
            move_duration_sec=0.0,
            after_click_sec=0.0,
            app=app,
            vision=vision,
        )

        self.assertTrue(out["found"])
        self.assertTrue(out["clicked"])
        self.assertEqual(out["scans"], 2)
        self.assertEqual(out["click"], [90, 36])
        app.click.assert_called_once_with(x=90, y=36)
        self.assertTrue(vision.find_template.call_args.kwargs["template_image"].endswith("battle_back_button.png"))
        self.assertEqual(vision.find_template.call_args.kwargs["threshold"], 0.9)

    def test_battle_back_button_template_asset_is_real_screenshot_crop(self):
        template_path = Path("plans/resonance_pc/templates/battle_back_button.png")
        template = cv2.imread(str(template_path), cv2.IMREAD_GRAYSCALE)
        self.assertIsNotNone(template)
        self.assertEqual(template.shape, (47, 133))
        self.assertGreater(float(template.std()), 20.0)

    def test_battle_main_terminal_marker_is_real_screenshot_crop(self):
        template_path = Path("plans/resonance_pc/templates/battle_main_terminal_marker.png")
        template = cv2.imread(str(template_path), cv2.IMREAD_GRAYSCALE)
        self.assertIsNotNone(template)
        self.assertEqual(template.shape, (60, 160))
        self.assertGreater(float(template.std()), 20.0)

    def test_stamina_dialog_templates_are_real_screenshot_crops(self):
        expected_shapes = {
            "battle_insufficient_stamina_dialog.png": (52, 455),
            "battle_insufficient_stamina_cancel.png": (66, 135),
        }
        for filename, expected_shape in expected_shapes.items():
            with self.subTest(filename=filename):
                template_path = Path("plans/resonance_pc/templates") / filename
                template = cv2.imread(str(template_path), cv2.IMREAD_GRAYSCALE)
                self.assertIsNotNone(template)
                self.assertEqual(template.shape, expected_shape)
                self.assertGreater(float(template.std()), 20.0)

    def test_stamina_dialog_is_cancelled_by_templates(self):
        app = Mock()
        app.capture.return_value = SimpleNamespace(
            success=True,
            image=np.zeros((120, 500, 3), dtype=np.uint8),
        )
        dialog_found = SimpleNamespace(
            found=True,
            center_point=(250, 60),
            rect=(23, 34, 455, 52),
            confidence=0.98,
        )
        cancel_found = SimpleNamespace(
            found=True,
            center_point=(95, 55),
            rect=(28, 22, 135, 66),
            confidence=0.97,
        )
        missing = SimpleNamespace(
            found=False,
            center_point=None,
            rect=None,
            confidence=0.0,
        )
        vision = Mock()
        vision.find_template.side_effect = [
            dialog_found,
            cancel_found,
            missing,
            missing,
        ]

        out = resonance_pc_detect_and_cancel_insufficient_stamina(
            timeout_sec=2.0,
            interval_sec=0.01,
            stable_scans=1,
            dismiss_timeout_sec=0.2,
            dismiss_stable_scans=2,
            move_duration_sec=0.0,
            after_click_sec=0.0,
            app=app,
            vision=vision,
        )

        self.assertEqual(out["outcome"], "insufficient_stamina")
        self.assertTrue(out["insufficient_stamina"])
        self.assertTrue(out["cancel_clicked"])
        self.assertTrue(out["dialog_closed"])
        app.click.assert_called_once_with(x=335, y=505)
        template_paths = [
            Path(call.kwargs["template_image"]).name
            for call in vision.find_template.call_args_list
        ]
        self.assertEqual(
            template_paths,
            [
                "battle_insufficient_stamina_dialog.png",
                "battle_insufficient_stamina_cancel.png",
                "battle_insufficient_stamina_dialog.png",
                "battle_insufficient_stamina_dialog.png",
            ],
        )

    def test_stamina_dialog_absence_is_normal_business_outcome(self):
        app = Mock()
        app.capture.return_value = SimpleNamespace(
            success=True,
            image=np.zeros((120, 500, 3), dtype=np.uint8),
        )
        vision = Mock()
        vision.find_template.return_value = SimpleNamespace(
            found=False,
            center_point=None,
            rect=None,
            confidence=0.0,
        )

        out = resonance_pc_detect_and_cancel_insufficient_stamina(
            timeout_sec=0.0,
            interval_sec=0.01,
            app=app,
            vision=vision,
        )

        self.assertEqual(out["outcome"], "normal")
        self.assertFalse(out["insufficient_stamina"])
        app.click.assert_not_called()

    def test_every_battle_trigger_has_two_second_stamina_template_guard(self):
        combat_steps = self.combat_task_data["auto_battle_combat_pc"]["steps"]
        self.assertEqual(
            combat_steps["detect_stamina_after_start"]["depends_on"],
            "assert_start_battle_clicked",
        )
        self.assertEqual(
            combat_steps["resolve_battle_result"]["when"],
            "{{ not nodes.detect_stamina_after_start.output.insufficient_stamina }}",
        )

        dispatch_guards = {
            "auto_battle_ct_tie_an_run_one_pc": [
                "detect_stamina_after_go_combat_with_difficulty",
                "detect_stamina_after_go_combat_without_difficulty",
            ],
            "auto_battle_ct_regional_ops_run_one_pc": [
                "detect_stamina_after_go_combat",
            ],
            "auto_battle_gp_action_summary_run_difficulty_pc": [
                "detect_stamina_after_start_battle",
            ],
            "auto_battle_gp_structural_run_one_pc": [
                "detect_stamina_after_start_auto",
            ],
        }
        for task_name, guard_names in dispatch_guards.items():
            steps = self.task_data[task_name]["steps"]
            for guard_name in guard_names:
                with self.subTest(task_name=task_name, guard_name=guard_name):
                    guard = steps[guard_name]
                    self.assertEqual(
                        guard["action"],
                        "resonance_pc.detect_and_cancel_insufficient_stamina",
                    )
                    self.assertEqual(guard["params"]["timeout_sec"], 2.0)
                    self.assertEqual(guard["params"]["interval_sec"], 0.2)

    @patch.object(battle_dispatch_pc_actions, "_recognize_text_items")
    def test_visual_back_button_accepts_already_reached_initial_screen(self, recognize_text_items):
        image = np.zeros((70, 160, 3), dtype=np.uint8)
        app = Mock()
        app.capture.return_value = SimpleNamespace(success=True, image=image)
        vision = Mock()
        missing = SimpleNamespace(
            found=False,
            center_point=None,
            rect=None,
            confidence=0.0,
        )
        target_found = SimpleNamespace(
            found=True,
            center_point=(80, 30),
            rect=(0, 0, 160, 60),
            confidence=0.99,
        )
        vision.find_template.side_effect = [missing, target_found]

        out = resonance_pc_wait_and_click_back_button(
            region=[10, 5, 160, 70],
            timeout_sec=0.1,
            interval_sec=0.01,
            stable_scans=2,
            already_at_target_template="templates/battle_main_terminal_marker.png",
            already_at_target_region=[1080, 375, 190, 80],
            already_at_target_threshold=0.95,
            app=app,
            vision=vision,
        )

        self.assertTrue(out["already_at_target"])
        self.assertFalse(out["clicked"])
        self.assertEqual(out["target_detection"], "template")
        self.assertEqual(out["confidence"], 0.99)
        recognize_text_items.assert_not_called()
        app.click.assert_not_called()

    @patch.object(battle_dispatch_pc_actions, "_recognize_text_items")
    def test_final_back_retries_until_initial_screen_is_stably_confirmed(self, recognize_text_items):
        image = np.zeros((70, 160, 3), dtype=np.uint8)
        app = Mock()
        app.capture.return_value = SimpleNamespace(success=True, image=image)
        found = SimpleNamespace(
            found=True,
            center_point=(80, 31),
            rect=(14, 8, 133, 47),
            confidence=0.99,
        )
        missing = SimpleNamespace(
            found=False,
            center_point=None,
            rect=None,
            confidence=0.0,
        )
        target_found = SimpleNamespace(
            found=True,
            center_point=(80, 30),
            rect=(0, 0, 160, 60),
            confidence=0.99,
        )
        vision = Mock()
        vision.find_template.side_effect = [
            found,
            found,
            found,
            found,
            missing,
            target_found,
            missing,
            target_found,
        ]

        out = resonance_pc_wait_and_click_back_button(
            region=[10, 5, 160, 70],
            timeout_sec=0.2,
            interval_sec=0.01,
            stable_scans=2,
            move_duration_sec=0.0,
            after_click_sec=0.0,
            already_at_target_template="templates/battle_main_terminal_marker.png",
            already_at_target_region=[1080, 375, 190, 80],
            already_at_target_threshold=0.95,
            repeat_until_target=True,
            max_clicks=4,
            target_stable_scans=2,
            app=app,
            vision=vision,
        )

        self.assertTrue(out["already_at_target"])
        self.assertTrue(out["clicked"])
        self.assertEqual(out["click_count"], 2)
        self.assertEqual(len(out["clicks"]), 2)
        self.assertEqual(out["target_detection"], "template")
        self.assertEqual(app.click.call_count, 2)
        recognize_text_items.assert_not_called()

    def test_all_battle_return_steps_use_visual_back_button_detection(self):
        return_steps = {
            "auto_battle_ct_batch_pc": [
                "click_back_after_first_batch",
                "click_back_after_single_batch",
                "click_back_after_second_batch",
            ],
            "auto_battle_gp_action_summary_run_group_pc": ["click_back_to_action_summary"],
            "auto_battle_gp_batch_pc": [
                "click_back_after_first_gp_batch",
                "click_back_after_single_gp_batch",
                "click_back_after_second_gp_batch",
            ],
            "auto_battle_dispatch_pc": [
                "click_back_to_initial_after_single_category",
                "click_back_to_initial_after_second_category",
            ],
        }
        for task_name, step_names in return_steps.items():
            steps = self.task_data[task_name]["steps"]
            for step_name in step_names:
                step = steps[step_name]
                self.assertEqual(step["action"], "resonance_pc.wait_and_click_back_button")
                self.assertEqual(step["params"]["region"], [10, 5, 160, 70])
                self.assertEqual(step["params"]["template"], "templates/battle_back_button.png")
                self.assertEqual(step["params"]["threshold"], 0.9)
                self.assertTrue(step["params"]["use_grayscale"])
                self.assertEqual(step["params"]["after_click_sec"], 1.0)
                self.assertEqual(step["params"]["stable_scans"], 2)
                if task_name != "auto_battle_dispatch_pc":
                    self.assertFalse(step["params"].get("repeat_until_target", False))

        dispatch_steps = self.task_data["auto_battle_dispatch_pc"]["steps"]
        final_back = dispatch_steps["click_back_to_initial_after_single_category"]
        self.assertEqual(
            final_back["depends_on"],
            {
                "all": [
                    {"run_first_ct_batch": "success|failed|skipped"},
                    {"run_first_gp_batch": "success|failed|skipped"},
                ]
            },
        )
        self.assertEqual(
            final_back["when"],
            "{{ nodes.prepare_category_order.first_category != '' and nodes.prepare_category_order.second_category == '' }}",
        )
        self.assertNotIn("already_at_target_text", final_back["params"])
        self.assertEqual(
            final_back["params"]["already_at_target_template"],
            "templates/battle_main_terminal_marker.png",
        )
        self.assertEqual(final_back["params"]["already_at_target_region"], [1080, 375, 190, 80])
        self.assertEqual(final_back["params"]["already_at_target_threshold"], 0.95)
        self.assertTrue(final_back["params"]["already_at_target_use_grayscale"])
        self.assertTrue(final_back["params"]["repeat_until_target"])
        self.assertEqual(final_back["params"]["max_clicks"], 4)
        self.assertEqual(final_back["params"]["target_stable_scans"], 2)
        self.assertEqual(final_back["params"]["timeout_sec"], 20.0)

        final_back = dispatch_steps["click_back_to_initial_after_second_category"]
        self.assertEqual(
            final_back["depends_on"],
            {
                "all": [
                    {"run_second_ct_batch": "success|failed|skipped"},
                    {"run_second_gp_batch": "success|failed|skipped"},
                ]
            },
        )
        self.assertEqual(
            final_back["when"],
            "{{ nodes.prepare_category_order.second_category != '' }}",
        )
        self.assertNotIn("already_at_target_text", final_back["params"])
        self.assertEqual(
            final_back["params"]["already_at_target_template"],
            "templates/battle_main_terminal_marker.png",
        )
        self.assertEqual(final_back["params"]["already_at_target_region"], [1080, 375, 190, 80])
        self.assertEqual(final_back["params"]["already_at_target_threshold"], 0.95)
        self.assertTrue(final_back["params"]["already_at_target_use_grayscale"])
        self.assertTrue(final_back["params"]["repeat_until_target"])
        self.assertEqual(final_back["params"]["max_clicks"], 4)
        self.assertEqual(final_back["params"]["target_stable_scans"], 2)
        self.assertEqual(final_back["params"]["timeout_sec"], 20.0)

    def test_ct_subcategory_branches_reach_shared_terminal_barriers(self):
        steps = self.task_data["auto_battle_ct_batch_pc"]["steps"]

        self.assertNotIn("wait_after_first_tie_an_switch", steps)
        self.assertNotIn("wait_after_first_regional_ops_switch", steps)
        self.assertEqual(
            steps["click_first_tie_an"]["depends_on"],
            {"switch_first_tie_an": "success|skipped"},
        )
        self.assertEqual(
            steps["wait_after_first_ct_switch"]["depends_on"],
            {
                "all": [
                    {"click_first_tie_an": "success|failed|skipped"},
                    {"switch_first_regional_ops": "success|failed|skipped"},
                ]
            },
        )
        self.assertEqual(
            steps["run_first_tie_an_batch"]["depends_on"],
            "wait_after_first_ct_switch",
        )
        self.assertEqual(
            steps["run_first_regional_ops_batch"]["depends_on"],
            "wait_after_first_ct_switch",
        )

        self.assertNotIn("wait_after_second_tie_an_switch", steps)
        self.assertNotIn("wait_after_second_regional_ops_switch", steps)
        self.assertEqual(
            steps["wait_after_back_first_batch"]["depends_on"],
            {"click_back_after_first_batch": "success|skipped"},
        )
        self.assertEqual(
            steps["click_second_tie_an"]["depends_on"],
            {"switch_second_tie_an": "success|skipped"},
        )
        self.assertEqual(
            steps["wait_after_second_ct_switch"]["depends_on"],
            {
                "all": [
                    {"click_second_tie_an": "success|failed|skipped"},
                    {"switch_second_regional_ops": "success|failed|skipped"},
                ]
            },
        )
        self.assertEqual(
            steps["run_second_tie_an_batch"]["depends_on"],
            {"wait_after_second_ct_switch": "success|skipped"},
        )
        self.assertEqual(
            steps["run_second_regional_ops_batch"]["depends_on"],
            {"wait_after_second_ct_switch": "success|skipped"},
        )

    def test_regional_ops_only_ct_dag_finishes_without_pending_nodes(self):
        task = self.task_data["auto_battle_ct_batch_pc"]
        regional_job = {
            "route_id": "ct.regional_ops_center.wilderness_station",
            "city_name": "荒原站",
            "threat_level": 1,
            "difficulty": 1,
        }

        async def fake_execute_single_action(_executor, node_data, _node_context):
            if node_data["action"] == "resonance_pc.group_ct_jobs":
                return {
                    "tie_an_jobs": [],
                    "regional_ops_jobs": [regional_job],
                    "unknown_jobs": [],
                    "category_order": ["regional_ops_center"],
                    "has_tie_an": False,
                    "has_regional_ops_center": True,
                }
            return True

        async def run_task():
            pause_event = asyncio.Event()
            pause_event.set()
            engine = ExecutionEngine(
                orchestrator=SimpleNamespace(debug_mode=False, services={}),
                pause_event=pause_event,
            )
            context = ExecutionContext(inputs={"jobs": [regional_job]}, cid="ct-dag-test")
            with patch.object(
                NodeExecutor,
                "execute_single_action",
                new=fake_execute_single_action,
            ):
                await engine.run(task, "auto_battle_ct_batch_pc", context)
                for _ in range(100):
                    if all(
                        state not in {StepState.PENDING, StepState.RUNNING}
                        for state in engine.step_states.values()
                    ):
                        break
                    await asyncio.sleep(0.01)
            return engine

        engine = asyncio.run(run_task())

        self.assertTrue(
            all(
                state in {StepState.SUCCESS, StepState.SKIPPED}
                for state in engine.step_states.values()
            ),
            engine.step_states,
        )
        self.assertEqual(engine.step_states["run_first_tie_an_batch"], StepState.SKIPPED)
        self.assertEqual(
            engine.step_states["run_first_regional_ops_batch"],
            StepState.SUCCESS,
        )
        self.assertEqual(
            engine.step_states["click_back_after_single_batch"],
            StepState.SUCCESS,
        )

    def test_ct_then_gp_dispatch_dag_finishes_without_pending_template_branches(self):
        task = self.task_data["auto_battle_dispatch_pc"]
        ct_job = {"route_id": "ct.tie_an.shoggolith_city.bounty"}
        gp_job = {
            "route_id": "gp.action_summary.global_supply.magic",
            "difficulty": 2,
        }
        jobs = [ct_job, gp_job]

        async def fake_execute_single_action(_executor, node_data, _node_context):
            action = node_data["action"]
            if action == "resonance_pc.normalize_battle_jobs":
                return {"normalized_jobs": jobs}
            if action == "resonance_pc.validate_battle_jobs":
                return {"normalized_jobs": jobs}
            if action == "resonance_pc.group_battle_jobs":
                return {
                    "ct_jobs": [ct_job],
                    "gp_jobs": [gp_job],
                    "unknown_jobs": [],
                    "category_order": ["ct", "gp"],
                }
            return True

        async def run_task():
            pause_event = asyncio.Event()
            pause_event.set()
            engine = ExecutionEngine(
                orchestrator=SimpleNamespace(debug_mode=False, services={}),
                pause_event=pause_event,
            )
            context = ExecutionContext(
                inputs={"jobs": jobs, "stop_on_failure": True},
                cid="ct-gp-template-dag-test",
            )
            with patch.object(
                NodeExecutor,
                "execute_single_action",
                new=fake_execute_single_action,
            ):
                await engine.run(task, "auto_battle_dispatch_pc", context)
                for _ in range(100):
                    if all(
                        state not in {StepState.PENDING, StepState.RUNNING}
                        for state in engine.step_states.values()
                    ):
                        break
                    await asyncio.sleep(0.01)
            return engine

        engine = asyncio.run(run_task())

        self.assertTrue(
            all(
                state in {StepState.SUCCESS, StepState.SKIPPED}
                for state in engine.step_states.values()
            ),
            engine.step_states,
        )
        self.assertEqual(engine.step_states["run_first_ct_batch"], StepState.SUCCESS)
        self.assertEqual(engine.step_states["run_second_gp_batch"], StepState.SUCCESS)
        self.assertEqual(
            engine.step_states["click_back_to_initial_after_second_category"],
            StepState.SUCCESS,
        )

    def test_optional_category_chains_propagate_skipped_state(self):
        gp_steps = self.task_data["auto_battle_gp_batch_pc"]["steps"]
        self.assertEqual(
            gp_steps["wait_after_back_first_gp_batch"]["depends_on"],
            {"click_back_after_first_gp_batch": "success|skipped"},
        )
        self.assertEqual(
            gp_steps["switch_second_action_summary"]["depends_on"],
            {"wait_after_back_first_gp_batch": "success|skipped"},
        )
        self.assertEqual(
            gp_steps["switch_second_structural"]["depends_on"],
            {"wait_after_back_first_gp_batch": "success|skipped"},
        )
        self.assertEqual(
            gp_steps["run_second_action_summary_batch"]["depends_on"],
            {"wait_after_second_gp_switch": "success|skipped"},
        )
        self.assertEqual(
            gp_steps["run_second_structural_batch"]["depends_on"],
            {"wait_after_second_gp_switch": "success|skipped"},
        )

        dispatch_steps = self.task_data["auto_battle_dispatch_pc"]["steps"]
        self.assertEqual(
            dispatch_steps["wait_after_ct_menu_recovery"]["depends_on"],
            {"recover_ct_menu_after_first_gp": "success|skipped"},
        )
        self.assertEqual(
            dispatch_steps["wait_second_ct_category_template"]["depends_on"],
            {"wait_after_ct_menu_recovery": "success|skipped"},
        )
        self.assertEqual(
            dispatch_steps["switch_second_ct"]["depends_on"],
            {"wait_second_ct_category_template": "success|skipped"},
        )
        self.assertEqual(
            dispatch_steps["run_second_ct_batch"]["depends_on"],
            {"wait_after_second_switch": "success|skipped"},
        )
        self.assertEqual(
            dispatch_steps["run_second_gp_batch"]["depends_on"],
            {"wait_after_second_switch": "success|skipped"},
        )

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
        self.assertEqual(
            steps["detect_stamina_after_start_battle"]["depends_on"],
            "assert_start_battle_clicked",
        )
        self.assertEqual(
            steps["run_combat_after_start_battle"]["depends_on"],
            "detect_stamina_after_start_battle",
        )

    def test_tie_an_expel_waits_after_confirming_difficulty(self):
        steps = self.task_data["auto_battle_ct_tie_an_run_one_pc"]["steps"]
        wait_step = steps["wait_after_confirm_difficulty"]
        self.assertEqual(wait_step["action"], "plans/aura_base/sleep")
        self.assertEqual(wait_step["params"]["seconds"], 0.5)
        self.assertEqual(
            wait_step["depends_on"],
            {"click_confirm_difficulty": "success|skipped"},
        )
        self.assertEqual(
            steps["click_go_combat_with_difficulty"]["depends_on"],
            {"wait_after_confirm_difficulty": "success|skipped"},
        )

    def test_all_pc_battle_task_drags_hold_before_release(self):
        expected_custom_actions = {
            "resonance_pc.select_ordered_city": 2,
            "resonance_pc.select_threat_level_numeric": 1,
            "resonance_pc.try_select_action_summary_stage": 1,
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
        self.assertEqual(
            wait_step["depends_on"],
            {"recover_ct_menu_after_first_gp": "success|skipped"},
        )

        wait_second_ct = steps["wait_second_ct_category_template"]
        self.assertEqual(
            wait_second_ct["depends_on"],
            {"wait_after_ct_menu_recovery": "success|skipped"},
        )

    def test_top_level_category_switches_wait_for_templates_and_assert_clicks(self):
        steps = self.task_data["auto_battle_dispatch_pc"]["steps"]
        cases = (
            ("first", "ct", "templates/battle_category_ct.png"),
            ("first", "gp", "templates/battle_category_gp.png"),
            ("second", "ct", "templates/battle_category_ct.png"),
            ("second", "gp", "templates/battle_category_gp.png"),
        )

        for order, category, template in cases:
            wait_step = steps[f"wait_{order}_{category}_category_template"]
            switch_step = steps[f"switch_{order}_{category}"]
            assert_step = steps[f"assert_{order}_{category}_switched"]

            self.assertEqual(wait_step["action"], "plans/aura_base/wait_for_image")
            self.assertEqual(wait_step["params"]["template"], template)
            self.assertEqual(wait_step["params"]["timeout"], 8.0)
            self.assertEqual(wait_step["params"]["interval"], 0.2)
            self.assertEqual(wait_step["params"]["region"], [20, 180, 200, 160])
            self.assertEqual(wait_step["params"]["threshold"], 0.95)
            self.assertTrue(wait_step["params"]["use_grayscale"])

            self.assertEqual(switch_step["action"], "plans/aura_base/find_image_and_click")
            self.assertEqual(switch_step["params"]["template"], template)
            self.assertEqual(
                switch_step["depends_on"],
                {f"wait_{order}_{category}_category_template": "success|skipped"},
            )
            self.assertEqual(
                switch_step["when"],
                f"{{{{ nodes.prepare_category_order.{order}_category == '{category}' }}}}",
            )

            self.assertEqual(assert_step["action"], "plans/aura_base/assert_condition")
            self.assertEqual(
                assert_step["depends_on"],
                {f"switch_{order}_{category}": "success|skipped"},
            )
            self.assertIn(
                f"nodes.switch_{order}_{category}.output",
                assert_step["params"]["condition"],
            )

        self.assertEqual(
            steps["wait_after_first_switch"]["depends_on"],
            {
                "all": [
                    {"assert_first_ct_switched": "success|skipped"},
                    {"assert_first_gp_switched": "success|skipped"},
                ]
            },
        )
        self.assertEqual(
            steps["wait_after_second_switch"]["depends_on"],
            {
                "all": [
                    {"assert_second_ct_switched": "success|skipped"},
                    {"assert_second_gp_switched": "success|skipped"},
                ]
            },
        )


if __name__ == "__main__":
    unittest.main()
