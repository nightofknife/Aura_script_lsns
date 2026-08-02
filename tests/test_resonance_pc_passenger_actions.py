from __future__ import annotations

import struct
from pathlib import Path
from types import SimpleNamespace

import pytest

import plans.resonance_pc.src.actions.passenger_pc_actions as actions


REPO_ROOT = Path(__file__).resolve().parents[1]


class _App:
    def __init__(self) -> None:
        self.clicks: list[tuple[int, int]] = []
        self.moves: list[tuple[int, int]] = []

    def click(self, *, x: int, y: int) -> None:
        self.clicks.append((x, y))

    def capture(self, *, rect: tuple[int, int, int, int]) -> SimpleNamespace:
        del rect
        return SimpleNamespace(success=True, image=object())

    def move_to(self, *, x: int, y: int, duration: float) -> None:
        del duration
        self.moves.append((x, y))


class _Controller:
    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []

    def mouse_down(self, button: str) -> None:
        self.events.append(("down", button))

    def mouse_up(self, button: str) -> None:
        self.events.append(("up", button))


def _row(text: str, x: int = 100, y: int = 100) -> dict:
    return {
        "text": text,
        "norm_text": actions._normalize_text(text),
        "center": [x, y],
        "confidence": 0.99,
    }


def test_passenger_and_flyer_ratios_are_separated():
    items = [_row("0/64"), _row("475/475")]

    assert actions._passenger_ratio(items) == {"current": 0, "total": 64}
    assert actions._flyer_ratio(items) == {"current": 475, "total": 475}


def test_numeric_value_can_be_read_from_nearby_ocr_item():
    items = [_row("招揽乘客人数", 600, 610), _row("35", 900, 610)]

    assert actions._numeric_value_near_label(items, ("招揽乘客人数",)) == 35


def test_destination_match_requires_ocr_to_contain_full_target_city():
    aliases = ("岚心城",)

    assert actions._find_alias_hit([_row("城")], aliases) is None
    assert actions._find_alias_hit([_row("岚心")], aliases) is None
    hit = actions._find_alias_hit([_row("目的地：岚心城")], aliases)
    assert hit is not None
    assert hit["text"] == "目的地：岚心城"


def test_destination_search_uses_vertical_drag_until_target_appears(monkeypatch):
    app = _App()
    controller = _Controller()
    sleeps: list[float] = []
    observations = iter(
        [
            [_row("修格里城", 800, 200)],
            [_row("岚心城", 830, 360)],
        ]
    )
    monkeypatch.setattr(actions, "_capture_text_items", lambda *_args, **_kwargs: next(observations))
    monkeypatch.setattr(actions.time, "sleep", sleeps.append)

    result = actions._select_destination_city(
        "岚心城",
        app,
        object(),
        controller,
        max_search_steps=6,
    )

    assert result["success"] is True
    assert app.clicks[-1] == (830, 360)
    assert app.moves == [actions._DESTINATION_DRAG_UP[0], actions._DESTINATION_DRAG_UP[1]]
    assert controller.events == [("down", "left"), ("up", "left")]
    assert sleeps == [0.25, 0.5]


def _visual_match(x: int, y: int, *, width: int = 20, height: int = 30) -> SimpleNamespace:
    region_x, region_y = actions._DISPATCH_CARD_REGION[:2]
    return SimpleNamespace(
        center_point=(x - region_x, y - region_y),
        rect=(x - region_x - width // 2, y - region_y - height // 2, width, height),
        confidence=0.96,
    )


class _DispatchVision:
    def __init__(self, locations: list[tuple[int, int]], locks: list[tuple[int, int]]) -> None:
        self.locations = locations
        self.locks = locks
        self.calls: list[dict] = []

    def find_all_templates_batch(self, **kwargs):
        self.calls.append(kwargs)
        return [
            SimpleNamespace(matches=[_visual_match(x, y) for x, y in self.locations]),
            SimpleNamespace(matches=[_visual_match(x, y, width=38, height=46) for x, y in self.locks]),
        ]


def test_dispatch_selection_uses_location_and_lock_templates_without_ocr(monkeypatch):
    app = _App()
    vision = _DispatchVision(
        locations=[
            (55, 431),
            (57, 432),
            (285, 431),
            (483, 431),
            (692, 431),
            (890, 431),
            (1119, 431),
            (1121, 432),
        ],
        locks=[(745, 341), (747, 342), (954, 341), (1162, 341)],
    )
    monkeypatch.setattr(
        actions,
        "_wait_template",
        lambda *_args, **_kwargs: {"found": True, "center": [640, 640]},
    )

    result = actions._select_rightmost_dispatch(app, vision)

    assert app.clicks == [(483, 431)]
    assert result["selected"]["center"] == [483, 431]
    assert len(result["detection"]["locations"]) == 6
    assert len(result["detection"]["locks"]) == 3
    assert result["detection"]["locked_location_indexes"] == [3, 4, 5]
    assert len(vision.calls) == 1
    assert [Path(path).name for path in vision.calls[0]["template_images"]] == [
        "passenger_dispatch_location_marker.png",
        "passenger_dispatch_lock.png",
    ]


@pytest.mark.parametrize("location_xs", [[300], [300, 520], [120, 360, 640, 900]])
def test_dispatch_selection_chooses_rightmost_unlocked_location(monkeypatch, location_xs):
    app = _App()
    vision = _DispatchVision([(x, 431) for x in location_xs], [])
    monkeypatch.setattr(
        actions,
        "_wait_template",
        lambda *_args, **_kwargs: {"found": True, "center": [640, 640]},
    )

    actions._select_rightmost_dispatch(app, vision)

    assert app.clicks == [(max(location_xs), 431)]


def test_dispatch_selection_stops_when_every_location_is_locked():
    app = _App()
    vision = _DispatchVision(
        locations=[(300, 431), (520, 431)],
        locks=[(350, 341), (570, 341)],
    )

    with pytest.raises(actions.PassengerPcError) as exc_info:
        actions._select_rightmost_dispatch(app, vision)

    assert exc_info.value.code == "dispatch_location_not_found"
    assert app.clicks == []


def test_open_passenger_management_waits_for_stable_main_and_opens_score(monkeypatch):
    app = _App()
    monkeypatch.setattr(
        actions,
        "_wait_main_stable",
        lambda *_args, **_kwargs: {
            "confirmed": True,
            "confirmations": 2,
            "match": {"found": True, "center": [1020, 660], "confidence": 0.99},
        },
    )
    monkeypatch.setattr(
        actions,
        "_wait_template",
        lambda *_args, **_kwargs: {"found": True, "center": [260, 140], "confidence": 0.99},
    )

    result = actions.resonance_pc_open_passenger_management(app=app, vision=object())

    assert result["page_state"] == "passenger_score"
    assert app.clicks == [(1020, 660)]
    assert len(result["attempts"]) == 1


def test_open_passenger_management_retries_when_arrival_input_lock_ignores_first_click(monkeypatch):
    app = _App()
    stable_calls: list[float] = []
    score_results = iter(
        [
            {"found": False, "confidence": 0.02},
            {"found": True, "center": [260, 140], "confidence": 0.99},
        ]
    )

    def stable(*_args, **kwargs):
        stable_calls.append(float(kwargs["timeout_sec"]))
        return {
            "confirmed": True,
            "confirmations": 2,
            "match": {"found": True, "center": [1020, 660], "confidence": 0.98},
        }

    monkeypatch.setattr(actions, "_wait_main_stable", stable)
    monkeypatch.setattr(actions, "_wait_template", lambda *_args, **_kwargs: next(score_results))
    monkeypatch.setattr(actions.time, "sleep", lambda _seconds: None)

    result = actions.resonance_pc_open_passenger_management(app=app, vision=object())

    assert app.clicks == [(1020, 660), (1020, 660)]
    assert stable_calls == [12.0, 4.0]
    assert len(result["attempts"]) == 2
    assert result["attempts"][0]["score_marker"]["found"] is False
    assert result["attempts"][1]["score_marker"]["found"] is True


def test_open_passenger_management_fails_after_three_confirmed_clicks(monkeypatch):
    app = _App()
    monkeypatch.setattr(
        actions,
        "_wait_main_stable",
        lambda *_args, **_kwargs: {
            "confirmed": True,
            "confirmations": 2,
            "match": {"found": True, "center": [1020, 660], "confidence": 0.98},
        },
    )
    monkeypatch.setattr(
        actions,
        "_wait_template",
        lambda *_args, **_kwargs: {"found": False, "confidence": 0.01},
    )
    monkeypatch.setattr(actions.time, "sleep", lambda _seconds: None)

    with pytest.raises(actions.PassengerPcError) as exc_info:
        actions.resonance_pc_open_passenger_management(app=app, vision=object())

    assert exc_info.value.code == "passenger_management_not_found"
    assert app.clicks == [(1020, 660), (1020, 660), (1020, 660)]
    assert len(exc_info.value.detail["attempts"]) == 3


def test_passenger_management_template_excludes_adjacent_achievement_button():
    template = (
        REPO_ROOT
        / "plans"
        / "resonance_pc"
        / "templates"
        / "passenger_management_button.png"
    )
    payload = template.read_bytes()
    assert payload[:8] == b"\x89PNG\r\n\x1a\n"
    width, height = struct.unpack(">II", payload[16:24])

    assert (width, height) == (62, 100)


def test_recruitment_continues_when_passengers_are_already_onboard(monkeypatch):
    app = _App()
    clicked_templates: list[str] = []
    result_items = [
        _row("招揽乘客人数", 600, 610),
        _row("20", 900, 610),
        _row("0/64", 700, 100),
        _row("415/475", 900, 100),
    ]

    monkeypatch.setattr(
        actions,
        "_match_template",
        lambda *_args, **_kwargs: {"found": True},
    )
    monkeypatch.setattr(
        actions,
        "_open_recruitment_hub_from_score",
        lambda *_args, **_kwargs: {
            "flyer_ratio": {"current": 475, "total": 475},
        },
    )

    def click_required(_app, _vision, template, _region, **_kwargs):
        clicked_templates.append(template)
        return {"found": True, "center": [640, 640]}

    monkeypatch.setattr(actions, "_click_template_required", click_required)
    monkeypatch.setattr(
        actions,
        "_wait_template",
        lambda *_args, **_kwargs: {"found": True, "center": [640, 640]},
    )
    monkeypatch.setattr(
        actions,
        "_select_destination_city",
        lambda *_args, **_kwargs: {"success": True},
    )
    monkeypatch.setattr(
        actions,
        "_select_rightmost_dispatch",
        lambda *_args, **_kwargs: {"confirm_match": {"center": [700, 650]}},
    )
    monkeypatch.setattr(actions, "_capture_text_items", lambda *_args, **_kwargs: result_items)
    monkeypatch.setattr(
        actions,
        "_click_blank_and_confirm_main",
        lambda *_args, **_kwargs: {"confirmed": True},
    )

    result = actions.resonance_pc_recruit_passengers_by_flyer(
        to_city_name="海角城",
        app=app,
        ocr=object(),
        vision=object(),
        controller=object(),
    )

    assert result["success"] is True
    assert result["recruited_passengers"] == 20
    assert actions._FLYER_MODE_TEMPLATE in clicked_templates


@pytest.mark.parametrize(
    ("name", "expected_size"),
    [
        ("passenger_dispatch_location_marker.png", (22, 30)),
        ("passenger_dispatch_lock.png", (38, 46)),
    ],
)
def test_dispatch_visual_templates_have_tight_bounds(name, expected_size):
    template = REPO_ROOT / "plans" / "resonance_pc" / "templates" / name
    payload = template.read_bytes()
    assert payload[:8] == b"\x89PNG\r\n\x1a\n"
    assert struct.unpack(">II", payload[16:24]) == expected_size


def test_settlement_requires_marker_but_revenue_ocr_is_best_effort(monkeypatch):
    app = _App()
    settlement_timeouts: list[float] = []
    visit = [_row("访问城市", 1120, 480)]
    settlement_items = [
        _row("车票收益", 520, 570),
        _row("116,193", 750, 570),
        _row("车票外收益", 520, 600),
        _row("56,710", 750, 600),
        _row("总收益", 900, 590),
        _row("172,903", 1110, 590),
    ]
    captures = iter([visit, settlement_items, visit])
    monkeypatch.setattr(actions, "_capture_text_items", lambda *_args, **_kwargs: next(captures))
    matches = iter(
        [
            {"found": False},
            {"found": True, "center": [200, 550]},
        ]
    )
    def wait_template(*_args, **kwargs):
        settlement_timeouts.append(float(kwargs["timeout_sec"]))
        return next(matches)

    monkeypatch.setattr(actions, "_wait_template", wait_template)
    monkeypatch.setattr(actions, "_wait_template_absent", lambda *_args, **_kwargs: {"absent": True})
    monkeypatch.setattr(actions, "_wait_main_stable", lambda *_args, **_kwargs: {"confirmed": True})

    result = actions.resonance_pc_enter_city_and_settle_passengers(
        app=app,
        ocr=object(),
        vision=object(),
    )

    assert result["success"] is True
    assert result["ticket_revenue"] == 116193
    assert result["extra_revenue"] == 56710
    assert result["total_revenue"] == 172903
    assert app.clicks[0] == (1120, 480)
    assert settlement_timeouts == [12.0, 30.0]


def test_settlement_already_open_skips_entry_ocr_and_dismisses_level_up(monkeypatch):
    app = _App()
    visit = [_row("访问城市", 1120, 480)]
    settlement_items = [
        _row("车票收益", 520, 570),
        _row("156,928", 750, 570),
        _row("车票外收益", 520, 600),
        _row("63,196", 750, 600),
        _row("总收益", 900, 590),
        _row("220,124", 1110, 590),
    ]
    captures = iter([settlement_items, [], visit])
    monkeypatch.setattr(actions, "_capture_text_items", lambda *_args, **_kwargs: next(captures))
    monkeypatch.setattr(
        actions,
        "_wait_template",
        lambda *_args, **_kwargs: {"found": True, "center": [200, 550]},
    )
    monkeypatch.setattr(actions, "_wait_template_absent", lambda *_args, **_kwargs: {"absent": True})
    monkeypatch.setattr(actions, "_wait_main_stable", lambda *_args, **_kwargs: {"confirmed": True})

    result = actions.resonance_pc_enter_city_and_settle_passengers(
        app=app,
        ocr=object(),
        vision=object(),
    )

    assert result["success"] is True
    assert result["total_revenue"] == 220124
    assert app.clicks == [actions._SAFE_EXIT_POINT, actions._SAFE_EXIT_POINT]
    assert len(result["dismiss_attempts"]) == 2
    assert result["dismiss_attempts"][0]["visit_city"]["found"] is False
    assert result["dismiss_attempts"][1]["visit_city"]["found"] is True
