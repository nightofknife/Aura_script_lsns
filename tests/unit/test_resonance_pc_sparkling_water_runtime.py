"""Replay the supplied UI crops through the real framework template matcher."""
from __future__ import annotations

import asyncio
import json
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


@pytest.fixture(autouse=True)
def diagnostic_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(water, "DIAGNOSTIC_ROOT", tmp_path / "diagnostics")


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


class BlackTransitionReplay(ReplayApp):
    """Second confirmation is followed by real black captures, not mocked scores."""
    def __init__(self, black_frames):
        super().__init__(confirmations=[False, True])
        self.black_frames = black_frames
        self.black_active = False
        self.black_observations = 0

    def click(self, x, y, *args, **kwargs):
        assert not self.black_active, "No input is allowed while the frame is uncertain"
        old = self.page
        super().click(x, y, *args, **kwargs)
        if old == "confirm" and self.completed == 1:
            self.black_active = True

    def capture(self, rect):
        if self.black_active:
            if tuple(rect) == (735, 460, 70, 65):
                self.black_observations += 1
                if self.black_observations > self.black_frames:
                    self.black_active = False
            if self.black_active:
                return SimpleNamespace(success=True, image=np.zeros((rect[3], rect[2], 3), dtype=np.uint8))
        return super().capture(rect)


@pytest.mark.parametrize("black_frames", [1, 3, 8])
def test_second_cup_black_transition_recovers_without_extra_click_or_count(
    tmp_path, monkeypatch, fast_time, black_frames,
):
    app = BlackTransitionReplay(black_frames)
    run, store = run_drinks(app, 2, tmp_path, monkeypatch)
    result = asyncio.run(run())
    assert result["completed_count"] == app.completed == 2
    assert result["uncertain_frames"] == black_frames
    assert result["page_state"] == "city_panel"
    assert sum(page == "drink_menu" and x > 170 for page, x, _ in app.clicks) == 2
    assert sum(page == "confirm" for page, _, _ in app.clicks) == 1
    assert store.read("user-info.json")["recovery"]["sparkling_water"] == {
        "remaining_free_uses": 4, "daily_free_limit": 6,
    }
    evidence = result["first_uncertain_frame"]
    assert evidence["target"] == "rest_menu"
    assert evidence["stage"] == "drink_animation"
    assert evidence["completed_count"] == 1
    assert evidence["raw_score"] == "-inf"
    images = list(water.DIAGNOSTIC_ROOT.glob("*.png"))
    assert len(images) == len(list(water.DIAGNOSTIC_ROOT.glob("*.json"))) == 1
    assert not np.array(Image.open(images[0])).any()
    assert json.loads(images[0].with_suffix(".json").read_text(encoding="utf-8")) == evidence


def test_persistent_black_second_cup_times_out_without_counting_or_reclicking(tmp_path, monkeypatch, fast_time):
    app = BlackTransitionReplay(999)
    run, store = run_drinks(app, 2, tmp_path, monkeypatch)
    with pytest.raises(water.SparklingWaterError) as caught:
        asyncio.run(run())
    assert caught.value.code == "sparkling_water_animation_timeout"
    assert caught.value.detail["completed_count"] == app.completed == 1
    assert caught.value.detail["uncertain_frames"] > 1
    assert store.read("user-info.json")["recovery"]["sparkling_water"] == {
        "remaining_free_uses": 5, "daily_free_limit": 6, "requires_refresh": True,
    }
    assert app.clicks[-1][0] == "confirm"
    assert len(list(water.DIAGNOSTIC_ROOT.glob("*.png"))) == 1


def test_cancel_during_black_second_cup_preserves_uncertain_consumption(tmp_path, monkeypatch, fast_time):
    app = BlackTransitionReplay(999)
    run, store = run_drinks(app, 2, tmp_path, monkeypatch)
    monkeypatch.setattr(water, "is_current_task_cancel_requested", lambda: app.black_observations >= 1)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(run())
    assert app.clicks[-1][0] == "confirm"
    assert store.read("user-info.json")["recovery"]["sparkling_water"] == {
        "remaining_free_uses": 5, "daily_free_limit": 6, "requires_refresh": True,
    }


@pytest.mark.parametrize("return_page", ["animation", "drink_menu", "black"])
def test_unknown_post_click_is_not_disappearance_or_permission_to_reclick(fast_time, return_page):
    async def run():
        vision = VisionService()
        vision._loop = asyncio.get_running_loop()
        app = ReplayApp()
        app.frames["black"] = np.zeros((720, 1280, 3), dtype=np.uint8)
        app.page = "drink_menu"
        session = water.SparklingWaterSession(app=app, vision=vision, layout=water.load_sparkling_water_layout(vision))
        clicks, observations = [], []

        def source():
            observations.append(app.page)
            try:
                return session.match("sparkling_water")
            finally:
                if app.page == "black":
                    app.page = return_page

        def click(_):
            clicks.append(True)
            app.page = "black"

        if return_page == "animation":
            await session.click_until_gone(source, click, label="consume")
        else:
            with pytest.raises(water.SparklingWaterError) as caught:
                await session.click_until_gone(source, click, label="consume")
            assert caught.value.code == "sparkling_water_transition_timeout"
        assert observations[:2] == ["drink_menu", "black"]
        assert len(observations) > 2
        assert clicks == [True]
    asyncio.run(run())


@pytest.mark.parametrize("score", [float("nan"), float("inf"), float("-inf")])
def test_all_nonfinite_scores_are_unknown_even_if_matcher_claims_found(score):
    app = ReplayApp()
    layout = {"templates": {"rest_menu": {"roi": [0, 0, 20, 20], "resolved_path": "unused", "threshold": 0.9}}}
    vision = SimpleNamespace(find_template=lambda **_: SimpleNamespace(found=True, confidence=score, debug_info={}))
    session = water.SparklingWaterSession(app=app, vision=vision, layout=layout)
    with pytest.raises(water._UncertainFrame):
        session.match("rest_menu")
    assert session.last_matches["rest_menu"]["valid"] is False
    assert "found" not in session.last_matches["rest_menu"]


def test_real_missing_template_remains_fatal_not_unknown():
    async def run():
        vision = VisionService()
        vision._loop = asyncio.get_running_loop()
        layout = water.load_sparkling_water_layout(vision)
        layout["templates"]["rest_menu"]["resolved_path"] = str(water.DIAGNOSTIC_ROOT / "missing.png")
        session = water.SparklingWaterSession(app=ReplayApp(), vision=vision, layout=layout)
        with pytest.raises(water.SparklingWaterError) as caught:
            await asyncio.to_thread(session.match, "rest_menu")
        assert caught.value.code == "sparkling_water_match_failed"
        assert session.uncertain_frames == 0
    asyncio.run(run())


def test_wrong_capture_size_remains_fatal():
    app = SimpleNamespace(capture=lambda **_: SimpleNamespace(success=True, image=np.zeros((1, 1, 3))))
    session = water.SparklingWaterSession(app=app, vision=None, layout={})
    with pytest.raises(water.SparklingWaterError) as caught:
        session.capture([0, 0, 20, 20])
    assert caught.value.code == "sparkling_water_capture_size_invalid"


def test_matcher_execution_error_is_not_retried_as_transition(fast_time):
    session = water.SparklingWaterSession(app=None, vision=None, layout={"timeout_sec": 3, "poll_interval_sec": 0.4})
    calls = []

    def broken():
        calls.append(True)
        raise RuntimeError("matcher execution failed")

    with pytest.raises(RuntimeError, match="matcher execution failed"):
        asyncio.run(session.wait_for(broken, bool, label="broken matcher"))
    assert calls == [True]


def test_debug_error_takes_precedence_over_nonfinite_score():
    layout = {"templates": {"rest_menu": {"roi": [0, 0, 20, 20], "resolved_path": "unused", "threshold": 0.9}}}
    vision = SimpleNamespace(find_template=lambda **_: SimpleNamespace(
        found=False, confidence=float("nan"), debug_info={"error": "invalid template"},
    ))
    session = water.SparklingWaterSession(app=ReplayApp(), vision=vision, layout=layout)
    with pytest.raises(water.SparklingWaterError) as caught:
        session.match("rest_menu")
    assert caught.value.code == "sparkling_water_match_failed"
    assert caught.value.detail["matches"]["rest_menu"]["error"] == "invalid template"
    assert session.uncertain_frames == 0


def test_diagnostic_write_failure_does_not_turn_uncertain_frame_into_match_failure(tmp_path, monkeypatch):
    blocked_path = tmp_path / "not-a-directory"
    blocked_path.write_text("existing", encoding="utf-8")
    monkeypatch.setattr(water, "DIAGNOSTIC_ROOT", blocked_path)
    layout = {"templates": {"rest_menu": {"roi": [0, 0, 20, 20], "resolved_path": "unused", "threshold": 0.9}}}
    vision = SimpleNamespace(find_template=lambda **_: SimpleNamespace(found=False, confidence=float("-inf"), debug_info={}))
    session = water.SparklingWaterSession(app=ReplayApp(), vision=vision, layout=layout)
    for _ in range(2):
        with pytest.raises(water._UncertainFrame):
            session.match("rest_menu")
    assert session.uncertain_frames == 2
    assert session.first_uncertain_frame["diagnostic_error"]
    assert blocked_path.read_text(encoding="utf-8") == "existing"


def test_uncertain_initial_target_never_clicks_or_marks_consumption(fast_time):
    session = water.SparklingWaterSession(app=None, vision=None, layout={"timeout_sec": 3, "poll_interval_sec": 0.4})
    clicks, writes = [], []

    def unknown():
        raise water._UncertainFrame("sparkling_water")

    with pytest.raises(water.SparklingWaterError) as caught:
        asyncio.run(session.click_until_gone(
            unknown, lambda _: clicks.append(True), label="consume", before_click=lambda: writes.append(True),
        ))
    assert caught.value.code == "sparkling_water_transition_timeout"
    assert clicks == writes == []


def test_cancel_while_post_click_target_is_uncertain_does_not_replay(fast_time, monkeypatch):
    session = water.SparklingWaterSession(app=None, vision=None, layout={
        "timeout_sec": 3, "poll_interval_sec": 0.4, "after_click_sec": 0.5, "max_clicks": 3,
    })
    clicks, cancelled = [], [False]
    monkeypatch.setattr(water, "is_current_task_cancel_requested", lambda: cancelled[0])

    def source():
        if clicks:
            cancelled[0] = True
            raise water._UncertainFrame("sparkling_water")
        return {"found": True}

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(session.click_until_gone(source, lambda _: clicks.append(True), label="consume"))
    assert clicks == [True]


@pytest.mark.parametrize("uncertain_key", ["drink_again", "sparkling_water"])
def test_uncertain_confirmation_or_second_menu_probe_skips_all_input(fast_time, monkeypatch, uncertain_key):
    session = water.SparklingWaterSession(app=None, vision=None, layout={
        "animation_timeout_sec": 15, "poll_interval_sec": 0.4, "skip_point": [1207, 36],
    })
    seen, clicks = [], []
    uncertain = [True]

    def match(key):
        seen.append(key)
        if key == uncertain_key and uncertain[0]:
            uncertain[0] = False
            raise water._UncertainFrame(key)
        return {"found": key == "sparkling_water"}

    monkeypatch.setattr(session, "match", match)
    monkeypatch.setattr(session, "click", lambda point: clicks.append(point))
    assert asyncio.run(session.finish_cup()) == ("drink_menu", False)
    assert clicks == []
    assert seen.count(uncertain_key) == 2
    assert session.completed == 0


def test_unknown_during_pre_retry_observation_cannot_authorize_another_click(fast_time):
    session = water.SparklingWaterSession(app=None, vision=None, layout={
        "timeout_sec": 3, "poll_interval_sec": 0.4, "after_click_sec": 0.5, "max_clicks": 3,
    })
    observations = iter([True, True, None, True, False])
    clicks = []

    def source():
        value = next(observations)
        if value is None:
            raise water._UncertainFrame("sparkling_water")
        return {"found": value}

    asyncio.run(session.click_until_gone(source, lambda _: clicks.append(True), label="consume"))
    assert clicks == [True]


@pytest.mark.parametrize("has_confirmation", [False, True])
def test_one_second_delay_ignores_menu_flash_before_animation(fast_time, monkeypatch, has_confirmation):
    session = water.SparklingWaterSession(app=None, vision=None, layout={
        "animation_timeout_sec": 15, "poll_interval_sec": 0.4, "skip_point": [1207, 36],
    })
    started = fast_time[0]
    state = {"phase": "confirm" if has_confirmation else "flash", "flash_until": started + 0.8}
    probes, clicks, confirmations = [], [], []

    def match(key):
        now = fast_time[0]
        probes.append((key, now))
        if state["phase"] == "confirm":
            return {"found": key == "drink_again"}
        menu_visible = state["phase"] == "menu" or now < state["flash_until"]
        return {"found": menu_visible and key == "sparkling_water"}

    async def confirm(key):
        assert key == "drink_again"
        confirmations.append(fast_time[0])
        state.update(phase="flash", flash_until=fast_time[0] + 0.8)

    def skip(point):
        assert point == [1207, 36]
        assert fast_time[0] >= state["flash_until"]
        clicks.append(fast_time[0])
        state["phase"] = "menu"

    monkeypatch.setattr(session, "match", match)
    monkeypatch.setattr(session, "click_icon_until_gone", confirm)
    monkeypatch.setattr(session, "click", skip)
    assert asyncio.run(session.finish_cup()) == ("drink_menu", has_confirmation)
    assert probes[0][1] == pytest.approx(started + 1.0)
    assert len(clicks) == 1  # The transient menu must not finish the cup before animation.
    if has_confirmation:
        assert probes[1][1] == pytest.approx(confirmations[0] + 1.0)
    assert session.completed == 0


@pytest.mark.parametrize("cancel_delay", [1, 2])
def test_cancel_during_one_second_settle_prevents_next_probe_or_click(monkeypatch, cancel_delay):
    session = water.SparklingWaterSession(app=None, vision=None, layout={
        "animation_timeout_sec": 15, "poll_interval_sec": 0.4, "skip_point": [1207, 36],
    })
    delays, probes, confirmations, clicks = [], [], [], []
    cancelled = [False]

    async def sleep(delay):
        assert delay == 1.0
        delays.append(delay)
        if len(delays) == cancel_delay:
            cancelled[0] = True

    def match(key):
        probes.append(key)
        return {"found": True}

    async def confirm(key):
        confirmations.append(key)

    monkeypatch.setattr(water.asyncio, "sleep", sleep)
    monkeypatch.setattr(water, "is_current_task_cancel_requested", lambda: cancelled[0])
    monkeypatch.setattr(session, "match", match)
    monkeypatch.setattr(session, "click_icon_until_gone", confirm)
    monkeypatch.setattr(session, "click", lambda point: clicks.append(point))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(session.finish_cup())
    assert len(delays) == cancel_delay
    assert len(probes) == len(confirmations) == cancel_delay - 1
    assert clicks == []
    assert session.completed == 0
