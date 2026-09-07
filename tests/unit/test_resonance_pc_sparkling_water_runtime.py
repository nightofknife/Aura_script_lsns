"""Replay the supplied UI crops through the real framework template matcher."""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image

from packages.aura_core.context.persistence.persistent_data_service import PersistentDataService
from plans.aura_base.src.services.vision_service import VisionService
from plans.resonance_pc.src.actions import sparkling_water_pc_actions as water
from plans.resonance_pc.src.actions import city_trade_flow_pc_actions as trade
from plans.resonance_pc.src.services.city_shop_data_pc_service import ResonancePcCityShopDataService

FIXTURES = Path("tests/fixtures/resonance_pc_sparkling_water")
PAGE_NAMES = ["rest_menu", "drink_menu", "confirm", "animation"]


def frames():
    result = {}
    for index, name in enumerate(PAGE_NAMES, 1):
        frame = np.full((720, 1280, 3), 128, dtype=np.uint8)
        frame[285:555, 730:1000] = np.array(Image.open(FIXTURES / f"page_{index}_controls.png").convert("RGB"))
        frame[:80, :170] = np.array(Image.open(FIXTURES / f"page_{index}_back.png").convert("RGB"))
        result[name] = frame
    city = np.full((720, 1280, 3), 128, dtype=np.uint8)
    city[520:570, 170:570] = np.random.default_rng(9).integers(0, 256, (50, 400, 3), dtype=np.uint8)
    result["city_panel"] = city
    return result


class ReplayApp:
    def __init__(self, *, remaining=6, confirmations=(), misses=None, stuck_animation=False):
        self.frames = frames()
        self.page = "city_panel"
        self.remaining = remaining
        self.confirmations = list(confirmations)
        self.misses = dict(misses or {})
        self.stuck_animation = stuck_animation
        self.completed = 0
        self.clicks = []
        self.captures = []
        self.failed_capture = False

    def capture(self, rect):
        x, y, w, h = rect
        self.captures.append((self.page, rect))
        return SimpleNamespace(success=not self.failed_capture, image=self.frames[self.page][y:y+h, x:x+w].copy())

    def click(self, x, y, *args, **kwargs):
        old = self.page
        self.clicks.append((old, x, y))
        if self.misses.get(old, 0):
            self.misses[old] -= 1
            return
        if old == "city_panel":
            assert (x, y) == (670, 240)
            self.page = "rest_menu"
        elif old == "rest_menu":
            self.page = "city_panel" if x < 170 else "drink_menu"
        elif old == "drink_menu":
            if x < 170:
                self.page = "rest_menu"
            else:
                assert self.remaining > 0, "Must not buy a non-free cup"
                self.page = "confirm" if self.completed < len(self.confirmations) and self.confirmations[self.completed] else "animation"
        elif old == "confirm":
            assert 895 <= x < 975 and 465 <= y < 545
            self.page = "animation"
        elif old == "animation":
            assert (x, y) == (1207, 36)
            if not self.stuck_animation:
                self.completed += 1
                self.remaining -= 1
                self.page = "drink_menu" if self.remaining else "rest_menu"


@pytest.fixture
def fast_time(monkeypatch):
    now = [100.0]
    real_sleep = asyncio.sleep

    async def sleep(delay):
        now[0] += delay
        await real_sleep(0)

    monkeypatch.setattr(water.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(water.asyncio, "sleep", sleep)
    return now


def saved_player(remaining=6):
    return {
        "status": {"fatigue": {"current": 201, "max": 856}},
        "recovery": {"sparkling_water": {"remaining_free_uses": remaining, "daily_free_limit": 6}, "bento": {"available_count": 2}},
        "metadata": {"custom": "preserve"},
    }


def run_drinks(app, count, tmp_path, monkeypatch, recorded_remaining=6):
    service = PersistentDataService(tmp_path)
    service.set("user-info.json", [], saved_player(recorded_remaining))
    monkeypatch.setattr(trade, "resonance_pc_read_city_name_on_city_panel", lambda **kw: {
        "city_key": "lanxin_city" if app.page == "city_panel" else None,
    })

    async def run():
        vision = VisionService()
        vision._loop = asyncio.get_running_loop()
        return await water.resonance_pc_drink_sparkling_water_from_city_panel(
            city_name="岚心城", drink_count=count, app=app, vision=vision, ocr=object(),
            resonance_pc_city_shop_data=ResonancePcCityShopDataService(), persistent_data=service,
        )

    return run, service


@pytest.mark.parametrize("page", PAGE_NAMES)
def test_approved_icon_rois_distinguish_pages(page):
    async def run():
        vision = VisionService()
        vision._loop = asyncio.get_running_loop()
        app = ReplayApp()
        app.page = page
        layout = water.load_sparkling_water_layout(vision)
        session = water.SparklingWaterSession(app=app, vision=vision, layout=layout)
        found = [key for key in layout["templates"] if (await asyncio.to_thread(session.match, key))["found"]]
        expected = {"rest_menu": ["drink_entry", "rest_menu"], "drink_menu": ["sparkling_water"], "confirm": ["drink_again"], "animation": []}
        assert found == expected[page]
        assert all(rect[2] <= 80 and rect[3] <= 80 for _, rect in app.captures)
    asyncio.run(run())


@pytest.mark.parametrize("count,remaining,confirmations", [(1, 6, []), (2, 6, [True, True]), (6, 6, [False, True, True, True, True, True])])
def test_complete_drinking_chain_and_preserve_other_data(tmp_path, monkeypatch, fast_time, count, remaining, confirmations):
    app = ReplayApp(remaining=remaining, confirmations=confirmations)
    run, store = run_drinks(app, count, tmp_path, monkeypatch)
    result = asyncio.run(run())
    assert result["success"] and result["page_state"] == app.page == "city_panel"
    assert result["completed_count"] == app.completed == count
    assert result["remaining_free_uses"] == remaining - count
    saved = store.read("user-info.json")
    assert saved["recovery"]["sparkling_water"]["remaining_free_uses"] == remaining - count
    assert saved["recovery"]["bento"] == {"available_count": 2}
    assert saved["status"]["fatigue"] == {"current": 201, "max": 856}
    assert saved["metadata"]["custom"] == "preserve"
    assert "sparkling_water" in saved["metadata"]["profile_section_updated_at"]
    assert sum(row[0] == "animation" for row in app.clicks) == count


def test_early_exhaustion_returns_city_and_records_observed_empty(tmp_path, monkeypatch, fast_time):
    app = ReplayApp(remaining=1)
    run, store = run_drinks(app, 4, tmp_path, monkeypatch)
    result = asyncio.run(run())
    assert result["reason"] == "free_uses_exhausted"
    assert result["completed_count"] == 1
    assert result["page_state"] == "city_panel"
    assert store.read("user-info.json")["recovery"]["sparkling_water"]["remaining_free_uses"] == 0


def test_ineffective_click_retries_but_does_not_count_a_cup_twice(tmp_path, monkeypatch, fast_time):
    app = ReplayApp(confirmations=[True], misses={"rest_menu": 1, "drink_menu": 1, "confirm": 1})
    run, _ = run_drinks(app, 1, tmp_path, monkeypatch)
    result = asyncio.run(run())
    assert result["completed_count"] == 1
    assert sum(page == "confirm" for page, _, _ in app.clicks) == 2
    assert sum(page == "drink_menu" and x > 170 for page, x, _ in app.clicks) == 2


def test_stuck_original_gets_only_three_clicks(tmp_path, monkeypatch, fast_time):
    app = ReplayApp(misses={"drink_menu": 99})
    run, store = run_drinks(app, 1, tmp_path, monkeypatch)
    with pytest.raises(water.SparklingWaterError, match="Original target still present"):
        asyncio.run(run())
    assert sum(page == "drink_menu" for page, _, _ in app.clicks) == 3
    saved = store.read("user-info.json")
    assert saved["recovery"]["sparkling_water"]["remaining_free_uses"] == 6
    assert saved["recovery"]["sparkling_water"]["requires_refresh"] is True


def test_animation_timeout_keeps_prior_confirmed_cups(tmp_path, monkeypatch, fast_time):
    app = ReplayApp(stuck_animation=True)
    run, store = run_drinks(app, 1, tmp_path, monkeypatch)
    with pytest.raises(water.SparklingWaterError, match="animation did not return"):
        asyncio.run(run())
    assert app.completed == 0
    saved = store.read("user-info.json")
    assert saved["recovery"]["sparkling_water"]["remaining_free_uses"] == 6
    assert saved["recovery"]["sparkling_water"]["requires_refresh"] is True
    assert app.clicks[-1][0] == "animation"


def test_capture_failure_is_not_target_disappearance(tmp_path, monkeypatch, fast_time):
    app = ReplayApp()
    app.failed_capture = True
    run, store = run_drinks(app, 1, tmp_path, monkeypatch)
    with pytest.raises(water.SparklingWaterError, match="capture failed"):
        asyncio.run(run())
    assert app.clicks == []
    assert store.read("user-info.json") == saved_player()


def test_request_exceeding_recorded_quota_fails_before_click(tmp_path, monkeypatch, fast_time):
    app = ReplayApp()
    run, _ = run_drinks(app, 3, tmp_path, monkeypatch, recorded_remaining=2)
    with pytest.raises(ValueError, match="exceed recorded"):
        asyncio.run(run())
    assert app.clicks == []


def test_cancel_after_first_cup_preserves_that_cup(tmp_path, monkeypatch, fast_time):
    app = ReplayApp()
    run, store = run_drinks(app, 3, tmp_path, monkeypatch)
    original = water.record_completed_cup
    cancelled = [False]
    monkeypatch.setattr(water, "is_current_task_cancel_requested", lambda: cancelled[0])

    def record(*args, **kwargs):
        result = original(*args, **kwargs)
        cancelled[0] = True
        return result

    monkeypatch.setattr(water, "record_completed_cup", record)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(run())
    assert app.completed == 1
    assert store.read("user-info.json")["recovery"]["sparkling_water"]["remaining_free_uses"] == 5
    assert app.page == "drink_menu"


def test_cancel_during_animation_requires_refresh_before_next_drink(tmp_path, monkeypatch, fast_time):
    app = ReplayApp()
    run, store = run_drinks(app, 1, tmp_path, monkeypatch)
    monkeypatch.setattr(water, "is_current_task_cancel_requested", lambda: app.page == "animation")
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(run())
    saved = store.read("user-info.json")
    assert saved["recovery"]["sparkling_water"]["requires_refresh"] is True
    with pytest.raises(ValueError, match="interrupted"):
        water._water_counts(saved)
    from plans.resonance_pc.src.actions.player_data_pc_actions import _merge_latest
    fresh = _merge_latest(saved, {"recovery": {"sparkling_water": {"remaining_free_uses": 5, "daily_free_limit": 6}}},
                          section_updated_at={"profile": "now"}, updated_at="now",
                          profile_section_updated_at={"sparkling_water": "now"})
    assert water._water_counts(fresh) == (5, 6)


def test_source_disappearance_is_checked_before_destination_and_not_reclicked(monkeypatch, fast_time):
    events = []
    session = water.SparklingWaterSession(app=None, vision=None, layout={"timeout_sec": 3, "poll_interval_sec": 0.4, "after_click_sec": 0.5, "max_clicks": 3})
    visible = [True]

    def source():
        events.append("source")
        return {"found": visible[0]}

    def click(_):
        events.append("click")
        visible[0] = False

    probes = [0]

    def target():
        events.append("target")
        probes[0] += 1
        return probes[0] > 2

    async def run():
        await session.click_until_gone(source, click, label="transition")
        await session.wait_for(target, bool, label="target")
    asyncio.run(run())
    assert events[:4] == ["source", "click", "source", "target"]
    assert events.count("click") == 1


def test_delayed_transition_between_retry_probes_does_not_click_again(fast_time):
    session = water.SparklingWaterSession(app=None, vision=None, layout={"timeout_sec": 3, "poll_interval_sec": 0.4, "after_click_sec": 0.5, "max_clicks": 3})
    probes = iter([True, True, False])
    clicks = []
    asyncio.run(session.click_until_gone(
        lambda: {"found": next(probes)}, lambda _: clicks.append(True), label="late transition",
    ))
    assert clicks == [True]
