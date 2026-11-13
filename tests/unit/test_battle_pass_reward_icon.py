"""Reward overlays depend on the box icon, not localized text or the plus."""

import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from packages.aura_core.scheduler.operation_scope import OperationScope
from plans.aura_base.src.services.interaction_service import InteractionService
from plans.aura_base.src.services.observation_service import Frame, ObservationService
from plans.aura_base.src.services.vision_service import VisionService
from plans.resonance_pc.src.actions import _daily_reward_flow
from plans.resonance_pc.src.actions._battle_pass_reward_flow import BattlePassRewardFlow
from plans.resonance_pc.src.actions._battle_pass_reward_vision import BattlePassRewardReader
from tests.interaction_clock import install_clock


ROOT = Path(__file__).resolve().parents[2]
PLAN = ROOT / "plans/resonance_pc"
SCREENSHOTS = PLAN / "development/template_sources/rewards/battle_pass/screenshots"
CATALOG = PLAN / "data/meta/battle_pass_templates_pc.json"


class Replay(ObservationService):
    def __init__(self, image):
        super().__init__(vision=VisionService())
        self.image = image
        self.captures = 0

    def capture(self, **_kwargs):
        self.captures += 1
        return Frame.from_image(self.image, color_space="BGR")


def screenshot(name):
    return cv2.imread(str(SCREENSHOTS / f"{name}.png"))[31:751, 1:1281].copy()


def isolated_reward_icon(name):
    """Keep actual screenshot pixels only where the icon's white mask applies."""
    image = screenshot(name)
    page = BattlePassRewardReader(Replay(image)).observe(OperationScope(5))
    assert page.page_state == "reward"
    x, y, width, height = page.hits["reward_title"].value.rect
    profile = json.loads(CATALOG.read_text(encoding="utf-8"))["templates"]["reward_title"]
    mask = cv2.imread(str(PLAN / profile["mask"]), cv2.IMREAD_GRAYSCALE) > 0
    isolated = np.zeros_like(image)
    isolated[y:y + height, x:x + width][mask] = image[y:y + height, x:x + width][mask]
    return isolated


@pytest.mark.parametrize("name", ["01_mission_reward", "02_level_reward"])
def test_reward_overlay_survives_removing_text_exit_and_plus(name):
    page = BattlePassRewardReader(Replay(isolated_reward_icon(name))).observe(OperationScope(5))
    assert page.page_state == "reward"
    assert page.present("reward_title")
    assert "reward_exit" not in page.hits


@pytest.mark.parametrize("name", ["01_mission_reward", "02_level_reward"])
def test_reward_text_and_exit_without_box_do_not_authorize_overlay(name):
    image = screenshot(name)
    # Preserve the localized title and exit hint, but erase the complete icon.
    image[155:270, 475:590] = 0
    page = BattlePassRewardReader(Replay(image)).observe(OperationScope(5))
    assert page.page_state != "reward"


@pytest.mark.parametrize("name, expected", [
    ("03_tasks", "tasks"), ("04_manual", "manual"), ("05_main", "main"),
])
def test_normal_pages_are_not_reward_overlays(name, expected):
    page = BattlePassRewardReader(Replay(screenshot(name))).observe(OperationScope(5))
    assert page.page_state == expected


@pytest.mark.parametrize("value", [0, 255])
def test_flat_title_region_is_not_a_reward_icon(value):
    image = screenshot("04_manual")
    image[155:270, 475:815] = value
    page = BattlePassRewardReader(Replay(image)).observe(OperationScope(5))
    assert page.page_state == "manual"


@pytest.mark.parametrize("reward, destination, kind", [
    ("01_mission_reward", "03_tasks", "tasks"),
    ("02_level_reward", "04_manual", "manual"),
])
def test_dismiss_icon_only_reward_confirms_return_to_same_pass(monkeypatch, reward, destination, kind):
    observation = Replay(isolated_reward_icon(reward))
    clicks = []
    captures_before_click = []

    def click(*, x, y):
        clicks.append((x, y))
        captures_before_click.append(observation.captures)
        observation.image = screenshot(destination)
        return True

    app = SimpleNamespace(click=click)
    with install_clock(monkeypatch, _daily_reward_flow):
        flow = BattlePassRewardFlow(app, observation, InteractionService(app, observation))
        flow.active_row = 112
        flow._dismiss(kind)

    assert clicks == [(500, 500)]
    assert captures_before_click[0] >= 2
    assert observation.captures - captures_before_click[0] >= 2
    assert flow.last.page_state == kind
    assert flow.last.selected_row == 112


def test_icon_assets_rebuild_from_native_source_without_text_or_plus(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("battle_pass_icon_author", ROOT / "tools/build_battle_pass_templates.py")
    author = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(author)
    # save_render requires paths relative to PLAN, so use an enclosing authoring
    # root while retaining the native source image's original relative path.
    plan = tmp_path / "plan"
    source = plan / "templates/rewards/daily/reward_title.png"
    source.parent.mkdir(parents=True)
    source.write_bytes((PLAN / "templates/rewards/daily/reward_title.png").read_bytes())
    native = plan / "templates/rewards/battle_pass"
    native.mkdir()
    monkeypatch.setattr(author, "ROOT", tmp_path)
    monkeypatch.setattr(author, "PLAN", plan)
    monkeypatch.setattr(author, "NATIVE", native)
    profile = json.loads(CATALOG.read_text(encoding="utf-8"))["templates"]["reward_title"]
    rebuilt, _ = author.save_reward_icon(profile)
    for key in ("template", "mask"):
        # PNG encoders can emit different compressed bytes for identical
        # pixels. Each catalog must fingerprint its own encoded file.
        generated, checked_in = plan / rebuilt[key], PLAN / profile[key]
        np.testing.assert_array_equal(cv2.imread(str(generated), cv2.IMREAD_UNCHANGED),
                                      cv2.imread(str(checked_in), cv2.IMREAD_UNCHANGED))
        fingerprint = "sha256" if key == "template" else "mask_sha256"
        assert hashlib.sha256(generated.read_bytes()).hexdigest() == rebuilt[fingerprint]
        assert hashlib.sha256(checked_in.read_bytes()).hexdigest() == profile[fingerprint]
    for key in ("size", "score_scale"):
        assert rebuilt[key] == profile[key]
    template = cv2.imread(str(plan / rebuilt["template"]), cv2.IMREAD_GRAYSCALE)
    mask = cv2.imread(str(plan / rebuilt["mask"]), cv2.IMREAD_GRAYSCALE)
    assert set(np.unique(mask)) == {0, 255}
    assert np.all(template[mask > 0] >= 224)
    assert np.all(template[mask == 0] == 0)
