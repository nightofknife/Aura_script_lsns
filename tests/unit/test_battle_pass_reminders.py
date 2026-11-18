"""Replay the native reminder template with OCR unavailable."""

from pathlib import Path
from types import SimpleNamespace

import cv2
import pytest

from packages.aura_core.scheduler.operation_scope import OperationScope
from plans.aura_base.src.services.interaction_service import InteractionService
from plans.aura_base.src.services.observation_service import Frame, ObservationService
from plans.aura_base.src.services.vision_service import VisionService
from plans.resonance_pc.src.actions import _daily_reward_flow
from plans.resonance_pc.src.actions._battle_pass_entries import reminder_control
from plans.resonance_pc.src.actions._battle_pass_reward_flow import BattlePassRewardFlow
from plans.resonance_pc.src.actions._battle_pass_reward_vision import BattlePassRewardReader
from tests.interaction_clock import install_clock


SCREENSHOTS = (
    Path(__file__).resolve().parents[2]
    / "plans/resonance_pc/development/template_sources/rewards/battle_pass/screenshots"
)


class Replay(ObservationService):
    def __init__(self, image):
        super().__init__(vision=VisionService())
        self.image = image

    def capture(self, **_kwargs):
        return Frame.from_image(self.image, color_space="BGR")


def client_image(name):
    return cv2.imread(str(SCREENSHOTS / f"{name}.png"))[31:751, 1:1281].copy()


@pytest.mark.parametrize("name, state, rows", [
    ("01_mission_reward", "reward", ()),
    ("02_level_reward", "reward", ()),
    ("03_tasks", "tasks", (112, 187)),
    ("04_manual", "manual", (112, 187)),
    ("05_main", "main", ()),
])
def test_only_native_card_reminders_are_counted(name, state, rows):
    page = BattlePassRewardReader(Replay(client_image(name))).observe(OperationScope(5))
    assert page.page_state == state
    assert page.reminders == rows
    for row in rows:
        assert page.hits[reminder_control(row)].client_center == (110, row)


def test_other_selected_card_cannot_authorize_claim_for_reminder_row():
    image = client_image("04_manual")
    image[75:100, 181:209] = 0
    page = BattlePassRewardReader(Replay(image)).observe(OperationScope(5))
    assert page.reminders == (187,)
    assert page.selected_row == 112

    flow = BattlePassRewardFlow.__new__(BattlePassRewardFlow)
    flow.active_row = 187
    assert not flow._current_pass(page, "manual")

    # Move only the selection chevron to the second row, independent of both names.
    image[178:198, 20:34] = image[103:123, 20:34]
    image[103:123, 20:34] = 0
    selected = BattlePassRewardReader(Replay(image)).observe(OperationScope(5))
    assert selected.selected_row == 187
    assert flow._current_pass(selected, "manual")


def test_nearly_flat_background_does_not_hide_the_reminder():
    image = client_image("04_manual")
    image[75:100, 181:209] = 0
    # The failed match window varied by one level in only one masked pixel.
    image[230:502, 181:209] = (239, 251, 255)
    image[324, 202, 1] = 250
    page = BattlePassRewardReader(Replay(image)).observe(OperationScope(5))
    assert page.page_state == "manual"
    assert page.reminders == (187,)
    assert page.present("tasks_off")


@pytest.mark.parametrize("rows", [(), (187,), (112, 187)])
def test_flow_processes_visible_reminders_using_only_clicks(monkeypatch, rows):
    # The app exposes no drag API. The old initial scroll would fail this run.
    flow = BattlePassRewardFlow(SimpleNamespace(), Replay(client_image("04_manual")), None)
    main = SimpleNamespace(page_state="main", absent=lambda _key: False, present=lambda _key: True)
    pages = iter([main, *(SimpleNamespace(page_state="manual", reminders=rows[index:])
                          for index in range(len(rows) + 1))])
    monkeypatch.setattr(flow, "confirmed_page", lambda *_args, **_options: next(pages))
    calls = []
    monkeypatch.setattr(flow, "transition", lambda stage, _source, _target, control: calls.append((stage, control)))
    monkeypatch.setattr(flow, "_collect", lambda kind: calls.append(("collect", kind)))
    monkeypatch.setattr(flow, "_confirm_reminder_cleared", lambda: calls.append(("cleared", flow.active_row)))

    result = flow.run()

    expected = [("enter_passes", "main_entry_with_red")]
    for row in rows:
        expected.extend([("select_pass", reminder_control(row)), ("collect", "tasks"),
                         ("collect", "manual"), ("cleared", row)])
    expected.append(("return_main", "city_main"))
    assert calls == expected
    assert result["passes_processed"] == len(rows)
    assert result["list_scrolls"] == 0


def without_claim_button(image):
    after = image.copy()
    # Keep varied background pixels in the button search region.
    after[630:720, 1015:1280] = image[450:540, 1015:1280]
    return after


@pytest.fixture
def first_manual_after_claim():
    image = without_claim_button(client_image("04_manual"))
    image[75:100, 181:209] = 0
    return image


@pytest.mark.parametrize("first_tasks_overlay", [True, False], ids=["task-popup", "no-task-popup"])
def test_manual_claim_without_overlay_continues_to_second_pass(
    monkeypatch, first_tasks_overlay, first_manual_after_claim,
):
    main = client_image("05_main")
    first_tasks = client_image("03_tasks")
    first_manual = client_image("04_manual")
    first_tasks_after_claim = without_claim_button(first_tasks)

    second_manual = first_manual.copy()
    second_manual[75:100, 181:209] = 0
    second_manual[178:198, 20:34] = second_manual[103:123, 20:34]
    second_manual[103:123, 20:34] = 0
    second_tasks = first_tasks_after_claim.copy()
    second_tasks[75:225, :209] = second_manual[75:225, :209]
    second_manual_after_claim = without_claim_button(second_manual)
    second_manual_after_claim[150:175, 181:209] = 0

    steps = [((871, 50), first_manual), ((708, 670), first_tasks)]
    if first_tasks_overlay:
        steps.extend([((1146, 670), client_image("01_mission_reward")),
                      ((500, 500), first_tasks_after_claim)])
    else:
        steps.append(((1146, 670), first_tasks_after_claim))
    steps.extend([
        ((467, 670), first_manual),
        ((1146, 669), first_manual_after_claim),
        ((110, 187), second_manual),
        ((708, 670), second_tasks),
        ((467, 670), second_manual),
        ((1146, 669), client_image("02_level_reward")),
        ((500, 500), second_manual_after_claim),
        ((197, 37), main),
    ])
    pending = iter(steps)
    observation = Replay(main)

    def click(*, x, y):
        point, image = next(pending)
        assert (x, y) == point
        observation.image = image
        return True

    app = SimpleNamespace(click=click)
    with install_clock(monkeypatch, _daily_reward_flow):
        flow = BattlePassRewardFlow(app, observation, InteractionService(app, observation))
        result = flow.run()

    assert next(pending, None) is None
    assert result["status"] == "collected"
    assert result["passes_processed"] == 2
    assert result["passes"] == [
        {"row_y": 112, "task_claim_clicks": 1, "level_claim_clicks": 1},
        {"row_y": 187, "task_claim_clicks": 0, "level_claim_clicks": 1},
    ]
    assert result["reward_overlays"] == 1 + first_tasks_overlay
    assert result["returned_to_main"] is True
