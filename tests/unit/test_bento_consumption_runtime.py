"""Offline bento state-machine, persistence and screenshot contracts."""
from __future__ import annotations

import copy
import asyncio
from pathlib import Path
from types import SimpleNamespace as NS

import numpy as np
import pytest
from PIL import Image

from packages.aura_core.context.persistence.persistent_data_service import PersistentDataService
from plans.aura_base.src.services.vision_service import VisionService
from plans.resonance_pc.src.actions import bento_consumption_pc_actions as bento
from plans.resonance_pc.src.actions import _bento_consumption_store as storage
from plans.resonance_pc.src.actions._player_data_persistence import PlayerDataPersistenceError


FIXTURES = Path("tests/fixtures/bento_consumption")
TIMES = ["05:00", "12:00", "18:00"]


def offline_vision():
    vision = VisionService()
    vision._submit_to_loop_and_wait = asyncio.run
    return vision


def inventory():
    return {"work_meals": {"available_count": 2, "slots": [
        {"issue_time": t, "available": i < 2} for i, t in enumerate(TIMES)
    ]}, "love_bentos": {"count": 1, "items": [{"role_id": 7, "role_name": "Role",
          "food_id": 83300011, "food_name": "Food", "remaining_days": 1}]}}


def seed_inventory(store, values=None):
    store.merge("user-info.json", ["recovery"], inventory() if values is None else values)


@pytest.fixture(autouse=True)
def forbid_inventory_scans(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Consumption must use cached inventory, never read the whole pantry")
    monkeypatch.setattr(bento.RecoveryReader, "read_work_meals", forbidden)
    for name in ("read", "read_page", "read_frame", "classify"):
        monkeypatch.setattr(bento.LoveBentoScanner, name, forbidden)


def meal(kind="work_meals", stars=1):
    if kind == "work_meals":
        return {"kind": kind, "issue_time": TIMES[0], "fatigue_recovery": 36, "rating_stars": None}
    return {"kind": kind, **inventory()[kind]["items"][0], "fatigue_recovery": 42, "rating_stars": stars}


def request(chosen):
    keys = ("kind", "issue_time") if chosen["kind"] == "work_meals" else (
        "kind", "role_id", "food_id", "remaining_days")
    return {key: chosen[key] for key in keys}


@pytest.fixture
def clock(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(bento.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(bento.time, "sleep", lambda s: now.__setitem__(0, now[0]+s))
    return now


def test_portion_is_deducted_once_and_unrelated_data_survives(tmp_path):
    store = PersistentDataService(tmp_path)
    fatigue = {"current": 0, "max": 600, "sentinel": "unchanged"}
    store.set("user-info.json", None, {"sentinel": [1], "fatigue": fatigue,
        "recovery": {"sparkling_water": {"remaining_free_uses": 6}},
        "metadata": {"profile_section_updated_at": {"fatigue": "unchanged"}}})
    seed_inventory(store)
    chosen = meal()
    for phase in ("consumption_pending", "consumption_confirmed", "consumption_confirmed", "completed"):
        updated = storage.record_portion(store, "test", 1, chosen, phase)
        assert updated == store.read("user-info.json")["recovery"]["work_meals"]
        assert updated["available_count"] == (2 if phase == "consumption_pending" else 1)
    storage.record_portion(store, "test", 1, chosen, "consumption_confirmed")
    value = store.read("user-info.json")
    assert value["recovery"]["work_meals"]["available_count"] == 1
    assert value["recovery"]["sparkling_water"]["remaining_free_uses"] == 6
    assert value["recovery"]["love_bentos"] == inventory()["love_bentos"]
    assert value["recovery"]["work_meals"]["requires_refresh"] is False
    assert value["sentinel"] == [1]
    assert value["fatigue"] == fatigue
    assert value["metadata"]["profile_section_updated_at"]["fatigue"] == "unchanged"
    assert value["metadata"]["bento_consumption"]["phase"] == "completed"
    with pytest.raises(ValueError, match="transition"):
        storage.record_portion(store, "test", 1, chosen, "consumption_pending")


def test_rating_pending_keeps_consumption_and_refresh_flag(tmp_path):
    store = PersistentDataService(tmp_path)
    seed_inventory(store)
    chosen = meal("love_bentos")
    for phase in ("consumption_pending", "consumption_confirmed", "rating_pending"):
        storage.record_portion(store, "test", 1, chosen, phase)
    value = store.read("user-info.json")
    assert value["recovery"]["love_bentos"]["count"] == 0
    assert value["metadata"]["bento_consumption"]["items"][0]["consumed"]
    assert value["metadata"]["bento_consumption"]["requires_refresh"]
    assert value["recovery"]["love_bentos"]["requires_refresh"]


class Replay:
    def __init__(self, chosen, *, prompt=True, effect=True, rating=True, return_ok=True):
        self.chosen = chosen
        self.page = "cabinet"
        self.prompt, self.effect, self.rating, self.return_ok = prompt, effect, rating, return_ok
        self.selected_stars = 0
        self.used = 0
        self.clicks = []

    def click(self, x, y):
        point = [x, y]
        self.clicks.append((self.page, point))
        if point == [10, 10]:
            return
        if self.page == "cabinet" and point == [20, 20]:
            self.used += 1
            self.page = "confirm" if self.prompt else "effect" if self.effect else "animation"
        elif self.page == "confirm" and point == [30, 30]:
            self.page = "effect" if self.effect else "animation"
        elif self.page == "effect" and point == [40, 40]:
            if self.chosen["kind"] == "work_meals":
                self.page = "cabinet"
            else:
                self.page = "rating" if self.rating else "animation"
        elif self.page == "rating" and y == 50:
            self.selected_stars = x // 10
        elif self.page == "rating" and point == [60, 60]:
            if self.return_ok:
                self.page = "cabinet"
        else:
            raise AssertionError((self.page, point))


def replay_session(tmp_path, chosen, monkeypatch, *, seed=True, **kwargs):
    app = Replay(chosen, **kwargs)
    layout = {
        "total_timeout_sec": 30, "timeout_sec": 1, "animation_timeout_sec": 2,
        "poll_interval_sec": .1, "blank_point": [40, 40],
        "star_points": [[10*i, 50] for i in range(1, 6)],
        "templates": {key: {"roi": [0, 0, 4, 4]} for key in ("star_on", "star_off")},
        "detail_name_roi": "detail", "confirm_text_roi": "confirm",
    }
    store = PersistentDataService(tmp_path)
    catalog = {"items": [{"id": chosen["food_id"], "name": chosen["food_name"],
                         "fatigue_recovery": chosen["fatigue_recovery"],
                         "rating_stars": chosen["rating_stars"]}]} if chosen["kind"] == "love_bentos" else {"items": []}
    session = bento.BentoConsumptionSession(app, None, None, store, layout, {}, catalog)
    if seed:
        seed_inventory(store)
        session.inventory = storage.load_cached_inventory(store, [chosen["kind"]], catalog)
        assert bento.resolve_meal(request(chosen), session.inventory, catalog) == chosen
    session.page = "bento_cabinet"
    monkeypatch.setattr(session, "locate", lambda m: [10, 10])
    monkeypatch.setattr(session, "is_cabinet", lambda: app.page == "cabinet")
    name = chosen.get("food_name", "铁盟工作餐")
    monkeypatch.setattr(session, "text", lambda roi: name)
    def match(key, roi=None):
        found, center = False, None
        if key in ("use_work", "use_love", "selected_marker"):
            found, center = app.page == "cabinet", [20, 20]
        elif key == "confirm_use":
            found, center = app.page == "confirm", [30, 30]
        elif key == "effect":
            found = app.page == "effect"
        elif key == "rating_page":
            found = app.page == "rating"
        elif key == "rating_confirm":
            found, center = app.page == "rating" and app.selected_stars > 0, [60, 60]
        elif key in ("star_on", "star_off"):
            index = (roi[0]+2)//10
            found = app.page == "rating" and ((index <= app.selected_stars) == (key == "star_on"))
        return {"found": found, "center": center}
    monkeypatch.setattr(session, "match", match)
    return session, app, store


@pytest.mark.parametrize("kind,stars,prompt", [("work_meals", None, True), ("work_meals", None, False),
    ("love_bentos", 1, True), ("love_bentos", 5, True), ("love_bentos", 5, False)])
def test_single_portion_paths(tmp_path, monkeypatch, clock, kind, stars, prompt):
    chosen = meal(kind, stars)
    session, app, store = replay_session(tmp_path, chosen, monkeypatch, prompt=prompt)
    assert session.consume(chosen)
    assert app.used == 1
    assert app.page == "cabinet"
    assert session.items[0]["consumed"] and session.items[0]["completed"]
    assert not session.requires_refresh
    assert session.inventory[kind] == store.read("user-info.json")["recovery"][kind]
    assert session.inventory[kind]["requires_refresh"] is False
    if kind == "love_bentos":
        assert app.selected_stars == stars


@pytest.mark.parametrize("fault,consumed", [("effect", False), ("rating", True), ("return_ok", True)])
def test_no_duplicate_consumption_after_effect_or_rating_failure(tmp_path, monkeypatch, clock, fault, consumed):
    chosen = meal("love_bentos")
    session, app, store = replay_session(tmp_path, chosen, monkeypatch, **{fault: False})
    monkeypatch.setattr(session, "enter", lambda: None)
    monkeypatch.setattr(session, "return_main", lambda: pytest.fail("Failed consumption must stop the run"))
    with pytest.raises(bento.BentoConsumptionError):
        session.run([request(chosen), {"kind": "work_meals", "issue_time": "12:00"}])
    assert app.used == 1
    assert len(session.items) == 1
    assert session.items[0]["consumed"] is consumed
    assert not session.items[0]["completed"]
    assert session.requires_refresh
    assert store.read("user-info.json")["metadata"]["bento_consumption"]["requires_refresh"]


def test_consumption_does_not_read_or_check_fatigue(tmp_path, monkeypatch, clock):
    chosen = meal()
    session, app, store = replay_session(tmp_path, chosen, monkeypatch)
    store.set("user-info.json", ["status", "fatigue"], {"current": 0, "max": 600})
    assert not hasattr(session, "fatigue") and not hasattr(session, "read_fatigue")
    def names_only(roi):
        assert roi in ("detail", "confirm"), "Fatigue must not be OCR-read"
        return "铁盟工作餐"
    monkeypatch.setattr(session, "text", names_only)
    assert session.consume(chosen)
    assert app.used == 1 and session.items[0]["completed"]
    assert store.read("user-info.json")["status"]["fatigue"] == {"current": 0, "max": 600}


def test_cancelled_session_never_clicks(tmp_path, monkeypatch, clock):
    session, app, store = replay_session(tmp_path, meal(), monkeypatch)
    monkeypatch.setattr(bento, "is_current_task_cancel_requested", lambda: True)
    with pytest.raises(bento.BentoConsumptionError, match="cancelled"):
        session.click([10, 10])
    assert not app.clicks


def test_failed_pending_write_prevents_use(tmp_path, monkeypatch, clock):
    session, app, store = replay_session(tmp_path, meal(), monkeypatch)
    monkeypatch.setattr(store, "update", lambda *args: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        session.consume(meal())
    assert app.used == 0


def test_cancellation_after_effect_observed_still_records_consumption(tmp_path, monkeypatch, clock):
    chosen = meal("love_bentos")
    session, app, store = replay_session(tmp_path, chosen, monkeypatch)
    original = session.match
    cancelled = [False]
    observed = [0]
    def match(key, roi=None):
        value = original(key, roi)
        if key == "effect" and value["found"]:
            observed[0] += 1
            if observed[0] == 2:
                cancelled[0] = True
        return value
    monkeypatch.setattr(session, "match", match)
    monkeypatch.setattr(bento, "is_current_task_cancel_requested", lambda: cancelled[0])
    with pytest.raises(bento.BentoConsumptionError, match="cancelled"):
        session.consume(chosen)
    assert session.items[0]["consumed"]
    assert store.read("user-info.json")["recovery"]["love_bentos"]["count"] == 0
    assert app.page == "effect"
    assert not any(point == [40, 40] for _, point in app.clicks)
    result = session.result("cancelled", "bento_cancelled")
    assert result["status"] == "cancelled" and result["consumed_count"] == 1
    assert "final_fatigue" not in result


def test_completion_write_failure_requires_refresh(tmp_path, monkeypatch, clock):
    session, app, store = replay_session(tmp_path, meal(), monkeypatch)
    original = bento.record_portion
    def record(*args):
        if args[-1] == "completed":
            raise OSError("completion write failed")
        return original(*args)
    monkeypatch.setattr(bento, "record_portion", record)
    with pytest.raises(OSError):
        session.consume(meal())
    assert session.items[0]["consumed"] and not session.items[0]["completed"]
    assert session.requires_refresh


def test_result_has_no_fatigue_state_or_budget_output(tmp_path, monkeypatch, clock):
    session, app, store = replay_session(tmp_path, meal(), monkeypatch)
    assert session.consume(meal())
    result = session.result("completed", "requested_meals_completed")
    assert not {"initial_fatigue", "final_fatigue", "last_observed_fatigue", "base_fatigue_reserve"} & result.keys()
    assert result["recovered_fatigue"] == 36  # Informational base-value sum, not a limit or live reading.
    assert not {"reserve", "reserve_fatigue", "floor", "meal_priority", "max_count"} & result.keys()


@pytest.mark.parametrize("phase", ["consumption_pending", "consumption_confirmed", "rating_pending"])
def test_pending_cache_is_refused_without_reconciliation_or_ui(tmp_path, monkeypatch, clock, phase):
    chosen = meal("love_bentos")
    session, app, store = replay_session(tmp_path, chosen, monkeypatch)
    for step in ("consumption_pending", "consumption_confirmed", "rating_pending"):
        storage.record_portion(store, "old", 1, chosen, step)
        if step == phase:
            break
    before = store.read("user-info.json")
    before_bytes = (store.root / "user-info.json").read_bytes()
    monkeypatch.setattr(session, "enter", lambda: pytest.fail("Pending cache must fail before UI"))
    for name in ("set", "merge", "update"):
        monkeypatch.setattr(store, name, lambda *args, **kwargs: pytest.fail("Reading must not clear pending evidence"))
    with pytest.raises(ValueError, match="requires an explicit inventory refresh"):
        session.run([request(chosen)])
    assert session.stage == "load_cached_inventory"
    assert session.items == [] and app.clicks == []
    assert store.read("user-info.json") == before
    assert (store.root / "user-info.json").read_bytes() == before_bytes


def test_consumption_uses_catalog_amount_without_recovery_ocr(tmp_path, monkeypatch, clock):
    chosen = meal("love_bentos")
    session, app, store = replay_session(tmp_path, chosen, monkeypatch)
    def names_only(roi):
        assert roi in ("detail", "confirm"), "Recovery value must not be OCR-read"
        return chosen["food_name"]
    monkeypatch.setattr(session, "text", names_only)
    assert session.consume(chosen)
    assert chosen["fatigue_recovery"] == 42
    assert session.catalog["items"][0]["fatigue_recovery"] == 42
    assert app.used == 1


def test_confirmation_waits_for_confident_stable_text(tmp_path, monkeypatch, clock):
    chosen = meal("love_bentos", 5)
    session, app, store = replay_session(tmp_path, chosen, monkeypatch)
    original_text = session.text
    reads = []
    def text(roi):
        if roi == "confirm":
            reads.append(True)
            if len(reads) == 1:
                return ""  # Low-confidence OCR during the popup transition.
        return original_text(roi)
    monkeypatch.setattr(session, "text", text)
    assert session.consume(chosen)
    assert len(reads) >= 3
    assert app.used == 1
    assert sum(point == [30, 30] for _, point in app.clicks) == 1


def test_confident_different_food_is_not_confirmed(tmp_path, monkeypatch, clock):
    chosen = meal("love_bentos")
    session, app, store = replay_session(tmp_path, chosen, monkeypatch)
    original_text = session.text
    monkeypatch.setattr(session, "text", lambda roi: "OtherDish" if roi == "confirm" else original_text(roi))
    with pytest.raises(bento.BentoConsumptionError) as error:
        session.consume(chosen)
    assert error.value.code == "bento_confirmation_mismatch"
    assert app.page == "confirm"
    assert not any(point == [30, 30] for _, point in app.clicks)


@pytest.mark.parametrize("frame_name,key,expected", [
    ("work_usable", "use_work", True), ("work_disabled", "use_work", False),
    ("work_not_issued", "use_work", False), ("love_usable", "use_love", True),
    ("work_disabled", "use_disabled", True), ("rating_empty", "rating_confirm", False),
    ("rating_one", "rating_confirm", True), ("rating_five", "rating_confirm", True),
    ("effect", "effect", True), ("work_confirmation", "confirm_use", True),
])
def test_real_screenshot_template_states(tmp_path, frame_name, key, expected):
    frame = np.array(Image.open(FIXTURES / f"{frame_name}.png").convert("RGB"))
    app = NS(capture=lambda rect: NS(success=True, image=frame[rect[1]:rect[1]+rect[3],rect[0]:rect[0]+rect[2]]))
    vision = offline_vision()
    session = bento.BentoConsumptionSession(app, None, vision, PersistentDataService(tmp_path),
        bento.load_consumption_layout(vision), {}, {})
    assert session.match(key)["found"] is expected


@pytest.mark.parametrize("frame_name", ["work_usable", "love_usable", "work_confirmation", "effect", "rating_empty", "rating_one", "rating_five"])
def test_wrong_start_page_has_no_clicks(tmp_path, monkeypatch, clock, frame_name):
    frame = np.array(Image.open(FIXTURES / f"{frame_name}.png").convert("RGB"))
    clicks = []
    app = NS(get_window_size=lambda: (1280,720), click=lambda **kwargs: clicks.append(kwargs),
             capture=lambda rect: NS(success=True, image=frame[rect[1]:rect[1]+rect[3],rect[0]:rect[0]+rect[2]]))
    store = PersistentDataService(tmp_path)
    seed_inventory(store, {"work_meals": inventory()["work_meals"]})
    result = bento.resonance_pc_consume_bentos(meals=[{"kind": "work_meals", "issue_time": "12:00"}], app=app,
        ocr=NS(), vision=offline_vision(), persistent_data=store)
    assert result["success"] is False
    assert result["failure_stage"] == "require_main"
    assert clicks == []


@pytest.mark.parametrize("frame_name,stars", [("rating_empty", 0), ("rating_one", 1), ("rating_five", 5)])
def test_real_rating_stars_are_checked_individually(tmp_path, frame_name, stars):
    frame = np.array(Image.open(FIXTURES / f"{frame_name}.png").convert("RGB"))
    app = NS(capture=lambda rect: NS(success=True, image=frame[rect[1]:rect[1]+rect[3],rect[0]:rect[0]+rect[2]]))
    vision = offline_vision()
    layout = bento.load_consumption_layout(vision)
    session = bento.BentoConsumptionSession(app, None, vision, PersistentDataService(tmp_path), layout, {}, {})
    for index, (x, y) in enumerate(layout["star_points"]):
        assert session.match("star_on", [x-25, y-25, 50, 50])["found"] is (index < stars)
        assert session.match("star_off", [x-25, y-25, 50, 50])["found"] is (index >= stars)


def test_only_selected_work_slot_has_marker(tmp_path):
    frame = np.array(Image.open(FIXTURES / "work_usable.png").convert("RGB"))
    app = NS(capture=lambda rect: NS(success=True, image=frame[rect[1]:rect[1]+rect[3],rect[0]:rect[0]+rect[2]]))
    vision = offline_vision()
    layout = bento.load_consumption_layout(vision)
    session = bento.BentoConsumptionSession(app, None, vision, PersistentDataService(tmp_path), layout, {}, {})
    assert [session.match("selected_marker", roi)["found"] for roi in layout["work_selected_rois"]] == [True, False, False]


def test_explicit_order_12_then_05_and_no_unrequested_consumption(tmp_path, monkeypatch, clock):
    session, app, store = replay_session(tmp_path, meal(), monkeypatch)
    data = inventory()
    data["work_meals"]["slots"][2]["available"] = True
    data["work_meals"]["available_count"] = 3
    seed_inventory(store, data)
    monkeypatch.setattr(session, "enter", lambda: None)
    selected = []
    def locate(chosen):
        remaining = session.inventory["work_meals"]
        assert remaining["available_count"] == 3 - len(selected)
        for previous in selected:
            assert not next(row for row in remaining["slots"] if row["issue_time"] == previous)["available"]
        selected.append(chosen["issue_time"])
        return [10, 10]
    monkeypatch.setattr(session, "locate", locate)
    returned = []
    def return_main():
        session.page = "city_main"
        returned.append(True)
    monkeypatch.setattr(session, "return_main", return_main)
    loads = []
    original_load = bento.load_cached_inventory
    def load(store, kinds, catalog):
        loads.append(list(kinds))
        assert not app.clicks, "Cache must load before entering UI"
        return original_load(store, kinds, catalog)
    monkeypatch.setattr(bento, "load_cached_inventory", load)
    meals = [{"kind": "work_meals", "issue_time": value} for value in ("12:00", "05:00")]
    original = copy.deepcopy(meals)
    result = session.run(meals)
    assert meals == original
    assert selected == ["12:00", "05:00"]
    assert result["status"] == "completed" and result["reason"] == "requested_meals_completed"
    assert result["requested_count"] == result["consumed_count"] == result["completed_count"] == 2
    assert [row["meal"]["issue_time"] for row in result["items"]] == selected
    assert app.used == 2
    saved = store.read("user-info.json")["recovery"]
    assert saved["work_meals"]["available_count"] == 1
    assert saved["work_meals"]["slots"][2]["available"]
    assert saved["love_bentos"] == data["love_bentos"]
    assert session.inventory == {"work_meals": saved["work_meals"]}
    assert loads == [["work_meals"]]
    assert returned == [True] and result["page_state"] == "city_main"


@pytest.mark.parametrize("missing", [
    {"kind": "work_meals", "issue_time": "18:00"},
    {"kind": "love_bentos", "role_id": 7, "food_id": 83300011, "remaining_days": 2},
])
def test_missing_target_fails_before_any_consumption_without_substitution(tmp_path, monkeypatch, clock, missing):
    session, app, store = replay_session(tmp_path, meal("love_bentos"), monkeypatch)
    before = copy.deepcopy(store.read("user-info.json")["recovery"])
    monkeypatch.setattr(session, "enter", lambda: pytest.fail("All targets must resolve before entering UI"))
    monkeypatch.setattr(session, "consume", lambda chosen: pytest.fail("All targets must resolve before any consumption"))
    with pytest.raises(ValueError, match="identity must exist exactly once"):
        session.run([{"kind": "work_meals", "issue_time": "12:00"}, missing])
    assert session.items == [] and app.used == 0 and app.clicks == []
    assert store.read("user-info.json")["recovery"] == before


@pytest.mark.parametrize("on_main", [True, False])
def test_empty_list_requires_main_without_input_or_inventory_scan(tmp_path, monkeypatch, clock, on_main):
    session, app, store = replay_session(tmp_path, meal(), monkeypatch, seed=False)
    assert not store.exists("user-info.json")
    checked = []
    def match(key, roi=None):
        assert key == "main"
        checked.append(key)
        return {"found": on_main, "center": None}
    monkeypatch.setattr(session, "match", match)
    monkeypatch.setattr(session, "enter", lambda: pytest.fail("Empty request must not enter cabinet"))
    monkeypatch.setattr(bento, "load_cached_inventory", lambda *args: pytest.fail("Empty request needs no cache"))
    monkeypatch.setattr(session, "return_main", lambda: pytest.fail("Empty request must not navigate"))
    if on_main:
        result = session.run([])
        assert result["success"] and result["status"] == "skipped" and result["reason"] == "empty_request"
        assert result["page_state"] == "city_main"
        assert result["requested_count"] == result["consumed_count"] == result["completed_count"] == 0
    else:
        with pytest.raises(bento.BentoConsumptionError, match="main screen"):
            session.run([])
    assert len(checked) >= 2 and session.stage == "require_main"
    assert app.clicks == [] and session.items == []
    assert not store.exists("user-info.json")


@pytest.mark.parametrize("chosen", [meal(), meal("love_bentos")])
def test_public_rejects_duplicate_requests_before_ui(chosen):
    with pytest.raises(ValueError, match="duplicate"):
        bento.resonance_pc_consume_bentos(meals=[request(chosen), request(chosen)])


@pytest.mark.parametrize("chosen", [meal(), meal("love_bentos")])
def test_public_rejects_non_identity_request_fields_before_ui(chosen):
    with pytest.raises(ValueError, match="exactly"):
        bento.resonance_pc_consume_bentos(meals=[chosen])


@pytest.mark.parametrize("kind", ["work_meals", "love_bentos"])
def test_cached_inventory_read_has_no_writes_and_returns_detached_data(tmp_path, monkeypatch, clock, kind):
    session, app, store = replay_session(tmp_path, meal("love_bentos"), monkeypatch)
    other = "love_bentos" if kind == "work_meals" else "work_meals"
    storage.record_portion(store, "other-kind-pending", 1, meal(other), "consumption_pending")
    before = store.read("user-info.json")
    assert before["metadata"]["bento_consumption"]["requires_refresh"]
    before_bytes = (store.root / "user-info.json").read_bytes()
    for name in ("set", "merge", "update"):
        monkeypatch.setattr(store, name, lambda *args, **kwargs: pytest.fail("Cache load must be read-only"))
    cached = storage.load_cached_inventory(store, [kind], session.catalog)
    assert cached == {kind: before["recovery"][kind]}
    rows_key = "slots" if kind == "work_meals" else "items"
    cached[kind][rows_key][0]["sentinel"] = "local only"
    assert store.read("user-info.json") == before
    assert (store.root / "user-info.json").read_bytes() == before_bytes
    assert app.clicks == []


@pytest.mark.parametrize("document", [None, {"sentinel": "no recovery"}, {"recovery": {}},
    {"recovery": {"love_bentos": inventory()["love_bentos"]}}])
def test_missing_cached_work_meals_fails_before_ui(tmp_path, monkeypatch, clock, document):
    session, app, store = replay_session(tmp_path, meal(), monkeypatch, seed=False)
    if document is not None:
        store.set("user-info.json", None, document)
    monkeypatch.setattr(session, "enter", lambda: pytest.fail("Missing cache must fail before UI"))
    monkeypatch.setattr(session, "match", lambda *args: pytest.fail("Missing cache must not inspect UI"))
    expected = PlayerDataPersistenceError if document is None else ValueError
    with pytest.raises(expected, match="cached|inventory"):
        session.run([request(meal())])
    assert session.stage == "load_cached_inventory"
    assert app.clicks == [] and app.used == 0 and session.items == []
    assert store.read("user-info.json", default=None) == document


def test_public_missing_cache_returns_failure_without_ui(tmp_path):
    def forbidden(*args, **kwargs):
        pytest.fail("Missing cache must not capture, click or drag")
    app = NS(get_window_size=lambda: (1280, 720), capture=forbidden, click=forbidden, drag=forbidden)
    store = PersistentDataService(tmp_path)
    result = bento.resonance_pc_consume_bentos(meals=[request(meal())], app=app,
        ocr=NS(), vision=offline_vision(), persistent_data=store)
    assert not result["success"] and result["status"] == "failed"
    assert result["failure_stage"] == "load_cached_inventory"
    assert result["reason"] == "player_data_incomplete"
    assert result["consumed_count"] == result["completed_count"] == 0
    assert not store.exists("user-info.json")


@pytest.mark.parametrize("kind", ["work_meals", "love_bentos"])
@pytest.mark.parametrize("flag,value,message", [
    ("requires_refresh", True, "requires an explicit inventory refresh"),
    ("requires_refresh", "false", "requires an explicit inventory refresh"),
    ("requires_refresh", 0, "requires an explicit inventory refresh"),
    ("degraded", True, "is degraded"),
    ("degraded", "false", "must be a boolean"),
])
def test_declared_invalid_cache_flags_fail_before_ui(tmp_path, monkeypatch, clock, kind, flag, value, message):
    chosen = meal(kind)
    session, app, store = replay_session(tmp_path, chosen, monkeypatch)
    store.set("user-info.json", ["recovery", kind, flag], value)
    before = store.read("user-info.json")
    monkeypatch.setattr(session, "enter", lambda: pytest.fail("Invalid cache must fail before UI"))
    with pytest.raises(ValueError, match=message):
        session.run([request(chosen)])
    assert session.stage == "load_cached_inventory"
    assert app.clicks == [] and session.items == []
    assert store.read("user-info.json") == before


@pytest.mark.parametrize("kind", ["work_meals", "love_bentos"])
@pytest.mark.parametrize("invalid", [None, {"requires_refresh": True, "degraded": "invalid"}])
def test_only_selected_cached_kind_is_loaded_and_consumed(tmp_path, monkeypatch, clock, kind, invalid):
    chosen = meal(kind)
    session, app, store = replay_session(tmp_path, chosen, monkeypatch)
    other = "love_bentos" if kind == "work_meals" else "work_meals"
    store.set("user-info.json", ["recovery", other], invalid)
    if kind == "work_meals":
        session.catalog = None  # Unselected love-bento data must not require a catalog.
    monkeypatch.setattr(session, "enter", lambda: None)
    monkeypatch.setattr(session, "return_main", lambda: session.set_page("city_main"))
    result = session.run([request(chosen)])
    assert result["success"] and result["completed_count"] == 1 and app.used == 1
    assert set(session.inventory) == {kind}
    assert store.read("user-info.json")["recovery"][other] == invalid


@pytest.mark.parametrize("kind", ["work_meals", "love_bentos"])
def test_completion_records_one_deduction_without_full_snapshot_rewrite(tmp_path, monkeypatch, clock, kind):
    chosen = meal(kind)
    session, app, store = replay_session(tmp_path, chosen, monkeypatch)
    store.merge("user-info.json", None, {
        "sentinel": [1], "fatigue": {"current": 213, "max": 600},
        "status": {"fatigue": {"current": 213, "max": 600}},
        "metadata": {"profile_section_updated_at": {kind: "unchanged", "fatigue": "unchanged"}},
    })
    store.set("user-info.json", ["recovery", "sparkling_water"], {"remaining_free_uses": 4})
    store.set("user-info.json", ["recovery", kind, "sentinel"], "newer than the session cache")
    before = store.read("user-info.json")
    original_update = store.update
    writes = []
    def update(*args, **kwargs):
        result = original_update(*args, **kwargs)
        writes.append(result.new_value)
        return result
    monkeypatch.setattr(store, "update", update)
    for name in ("set", "merge"):
        monkeypatch.setattr(store, name, lambda *args, **kwargs: pytest.fail("No inventory snapshot writes"))
    assert session.consume(chosen)
    phases = ["consumption_pending", "consumption_confirmed"]
    if kind == "love_bentos":
        phases.append("rating_pending")
    phases.append("completed")
    assert [value["metadata"]["bento_consumption"]["phase"] for value in writes] == phases
    count_key = "available_count" if kind == "work_meals" else "count"
    initial_count = before["recovery"][kind][count_key]
    assert [value["recovery"][kind][count_key] for value in writes] == [
        initial_count, *([initial_count - 1] * (len(phases) - 1))]
    for value in writes:
        for key in ("sentinel", "fatigue", "status"):
            assert value[key] == before[key]
        assert value["metadata"]["profile_section_updated_at"] == before["metadata"]["profile_section_updated_at"]
        for other in before["recovery"]:
            if other != kind:
                assert value["recovery"][other] == before["recovery"][other]
        assert value["recovery"][kind]["sentinel"] == "newer than the session cache"
    assert session.inventory[kind] == writes[-1]["recovery"][kind]
    assert not session.inventory[kind]["requires_refresh"]
    assert app.used == 1 and session.items[0]["completed"]


def test_love_locate_current_viewport_uses_two_frames_without_scrolling(tmp_path, monkeypatch, clock):
    chosen = meal("love_bentos")
    session, app, store = replay_session(tmp_path, chosen, monkeypatch)
    frames = [np.full((2, 2, 3), value, dtype=np.uint8) for value in (1, 2)]
    pending = iter(frames)
    seen = []
    point = [120, 80]
    before = copy.deepcopy(session.inventory)
    saved = store.read("user-info.json")
    class Scanner:
        def __init__(self, reader, catalog):
            self.cfg = {"viewport": [0, 0, 200, 200], "max_drags": 5, "scroll_distance": 100}

        def stable_frame(self):
            return next(pending)

        def forbidden(self, *args, **kwargs):
            pytest.fail("Target location must not read or classify inventory")

        read = read_page = read_frame = classify = forbidden

    def find(frame, target, catalog, vision):
        assert target == chosen
        assert frame is frames[len(seen)]
        seen.append(frame)
        return point
    monkeypatch.setattr(bento, "LoveBentoScanner", Scanner)
    monkeypatch.setattr(bento, "find_target_on_frame", find)
    monkeypatch.setattr(session, "scroll_top", lambda: pytest.fail("Current target must not scroll to top"))
    monkeypatch.setattr(app, "drag", lambda *args, **kwargs: pytest.fail("Current target must not drag"), raising=False)
    monkeypatch.setattr(session, "target_click_point", lambda value: [value[0] + 10, value[1] + 20])
    assert bento.BentoConsumptionSession.locate(session, chosen) == [130, 100]
    assert len(seen) == 2
    assert session.inventory == before and store.read("user-info.json") == saved
    assert app.clicks == []


def test_love_locate_miss_scrolls_top_once_and_stops_at_target(tmp_path, monkeypatch, clock):
    chosen = meal("love_bentos")
    session, app, store = replay_session(tmp_path, chosen, monkeypatch)
    frames = [np.full((2, 2, 3), value, dtype=np.uint8) for value in range(5)]
    pending = iter(frames)
    events = []
    point = [120, 80]
    before = copy.deepcopy(session.inventory)
    saved = store.read("user-info.json")
    class Scanner:
        def __init__(self, reader, catalog):
            self.cfg = {"viewport": [0, 0, 200, 200], "max_drags": 5, "scroll_distance": 100}

        def stable_frame(self):
            frame = next(pending)
            events.append(("frame", int(frame[0, 0, 0])))
            return frame

        def forbidden(self, *args, **kwargs):
            pytest.fail("Target location must not rebuild the inventory")

        read = read_page = read_frame = classify = forbidden

    def find(frame, target, catalog, vision):
        assert target == chosen
        index = int(frame[0, 0, 0])
        assert frame is frames[index]
        return point if index >= 3 else None
    monkeypatch.setattr(bento, "LoveBentoScanner", Scanner)
    monkeypatch.setattr(bento, "find_target_on_frame", find)
    monkeypatch.setattr(session, "scroll_top", lambda: events.append(("top",)))
    monkeypatch.setattr(app, "drag", lambda *args, **kwargs: events.append(("drag", args, kwargs)), raising=False)
    monkeypatch.setattr(session, "target_click_point", lambda value: value)
    assert bento.BentoConsumptionSession.locate(session, chosen) == point
    drag = ("drag", (100, 160, 100, 60), {"duration": .6, "hold_before_release_sec": .2})
    assert events == [("frame", 0), ("top",), ("frame", 1), drag,
                      ("frame", 2), drag, ("frame", 3), ("frame", 4)]
    assert session.inventory == before and store.read("user-info.json") == saved
    assert app.clicks == []


@pytest.mark.parametrize("index", [0, 1])
def test_work_locate_uses_cached_slot_and_static_coordinates_only(tmp_path, monkeypatch, clock, index):
    session, app, store = replay_session(tmp_path, meal(), monkeypatch)
    session.reader.layout = {"bento_slots": [
        {"issue_time": issue_time, "roi": [100 + 200 * i, 100, 120, 80]}
        for i, issue_time in enumerate(TIMES)
    ]}
    session.layout["work_selected_rois"] = [[100 + 200 * i, 190, 80, 20] for i in range(3)]
    chosen = bento.resolve_meal({"kind": "work_meals", "issue_time": TIMES[index]},
                               session.inventory, session.catalog)
    tops = []
    before = copy.deepcopy(session.inventory)
    saved = store.read("user-info.json")
    monkeypatch.setattr(session, "scroll_top", lambda: tops.append(True))
    monkeypatch.setattr(bento, "find_target_on_frame", lambda *args: pytest.fail("Work slots use static coordinates"))
    monkeypatch.setattr(session, "match", lambda *args: pytest.fail("Selected UI is checked during consumption"))
    monkeypatch.setattr(session, "text", lambda *args: pytest.fail("Work location needs no OCR"))
    assert bento.BentoConsumptionSession.locate(session, chosen) == [160 + 200 * index, 140]
    assert session.selected_roi == session.layout["work_selected_rois"][index]
    assert tops == [True] and app.clicks == []
    assert session.inventory == before and store.read("user-info.json") == saved


def test_work_only_navigation_does_not_load_love_matching_assets():
    from plans.resonance_pc.src.actions.love_bento_pc_actions import load_love_bento_catalog
    catalog = load_love_bento_catalog(object(), navigation_only=True)
    assert catalog["items"] == []
    assert catalog["scanner"]["max_drags"] == 15
    assert catalog["scanner"]["viewport"]


def test_low_confidence_detail_ocr_is_rejected(tmp_path):
    layout = bento.load_consumption_layout(offline_vision())
    ocr = NS(recognize_all=lambda **kwargs: NS(results=[NS(text="Food", confidence=.5)]))
    app = NS(capture=lambda rect: NS(success=True, image=np.zeros((rect[3], rect[2], 3), dtype=np.uint8)))
    session = bento.BentoConsumptionSession(app, ocr, None, PersistentDataService(tmp_path), layout, {}, {})
    assert session.text([0, 0, 100, 24]) == ""
