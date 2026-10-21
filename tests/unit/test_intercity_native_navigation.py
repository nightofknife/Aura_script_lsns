"""Offline navigation contracts: native coarse batches precede fresh OCR."""

import asyncio
from types import SimpleNamespace

import pytest

from plans.resonance_pc.src.actions import city_travel_pc_actions as travel


def label(text, x=640, y=360, confidence=0.99):
    """OCR coordinates are relative to the real map crop, not the client."""
    return SimpleNamespace(text=text, center_point=(x - 120, y - 120), confidence=confidence)


@pytest.fixture
def scene(monkeypatch):
    events, drags, clicks = [], [], []
    state = SimpleNamespace(size=(1280, 720), cancelled=False, frames=[], on_drag=None, on_ocr=None)

    def capture(*, rect):
        assert rect == (120, 120, 1100, 560)
        events.append("capture")
        return SimpleNamespace(success=True, image=object())

    def recognize_all(*, source_image):
        events.append("ocr")
        if state.on_ocr:
            state.on_ocr()
        assert state.frames, "Navigation performed an unexpected extra OCR pass"
        return SimpleNamespace(results=state.frames.pop(0))

    def drag(**kwargs):
        kwargs["check_cancelled"]()
        events.append("drag")
        drags.append(kwargs["effective_delta"])
        assert kwargs["safe_rect"] == (160, 140, 960, 460)
        assert kwargs["duration"] == 1.0
        assert kwargs["hold_sec"] == 0.5
        if state.on_drag:
            state.on_drag()
        return {"backend": "fake", "effective_delta": list(kwargs["effective_delta"])}

    def click(**kwargs):
        events.append("click")
        clicks.append(kwargs)

    state.app = SimpleNamespace(get_window_size=lambda: state.size, capture=capture, click=click)
    state.ocr = SimpleNamespace(recognize_all=recognize_all)
    state.events, state.drags, state.clicks = events, drags, clicks
    monkeypatch.setattr(travel, "perform_map_drag", drag)
    monkeypatch.setattr(travel, "_sleep_with_cancel", lambda seconds: None)
    monkeypatch.setattr(travel, "is_current_task_cancel_requested", lambda: state.cancelled)
    return state


def select(scene, **kwargs):
    return travel.resonance_pc_select_intercity_destination(
        to_city_name="栖羽站", from_city_name=kwargs.pop("from_city_name", "海角城"),
        app=scene.app, ocr=scene.ocr, **kwargs,
    )


def test_long_native_route_finishes_entire_blind_batch_before_first_capture(scene):
    scene.frames = [[label("栖羽站", 650, 405)]]
    result = select(scene)

    # Independent native coordinates: Cape (-1300,-1650), Qiyu (2350,675).
    # Multiply by 2/3, invert Y, then negate for map content motion.
    assert len(scene.drags) == 4
    assert tuple(map(sum, zip(*scene.drags))) == (-2433, 1550)
    assert scene.events == ["drag"] * 4 + ["capture", "ocr", "click"]
    assert scene.clicks == [{"x": 650, "y": 390}]
    assert result["mode"] == "native_coarse"
    assert result["screen_scale"] == pytest.approx(2 / 3)
    assert result["total_drag_count"] == 4 and result["ocr_count"] == 1


def test_correction_uses_observed_anchor_then_reobserves_target_before_click(scene):
    # Lanxin is 600 native units east of Qiyu, hence 400 screen pixels east.
    scene.frames = [[label("岚心城", 1000, 360)], [label("栖羽站", 644, 400)]]
    result = select(scene)

    assert scene.drags[-1] == (40, 0)
    assert scene.events == ["drag"] * 4 + ["capture", "ocr", "drag", "capture", "ocr", "click"]
    assert scene.clicks == [{"x": 644, "y": 385}]
    assert result["mode"] == "native_corrected"
    assert result["correction_rounds"] == 1 and result["ocr_count"] == 2


def test_blank_frames_retry_once_without_more_blind_movement_or_guessed_click(scene):
    scene.frames = [[], []]
    with pytest.raises(travel.IntercityDestinationError) as error:
        select(scene)
    assert error.value.detail["stop_reason"] == "no_supported_city_labels"
    assert scene.events == ["drag"] * 4 + ["capture", "ocr"] * 2
    assert not scene.clicks


def test_blank_first_frame_can_recover_without_extra_drag(scene):
    scene.frames = [[], [label("栖羽站")]]
    result = select(scene)
    assert scene.events == ["drag"] * 4 + ["capture", "ocr"] * 2 + ["click"]
    assert result["ocr_count"] == 2 and result["total_drag_count"] == 4


def test_predicted_target_at_center_is_never_clicked_without_recognition(scene):
    scene.frames = [[label("岚心城", 1040, 360)]]
    with pytest.raises(travel.IntercityDestinationError) as error:
        select(scene)
    assert error.value.detail["stop_reason"] == "target_label_not_confirmed_near_prediction"
    assert len(scene.drags) == 4 and not scene.clicks


def test_coarse_budget_rejects_whole_batch_before_any_input(scene):
    with pytest.raises(travel.IntercityDestinationError) as error:
        select(scene, max_search_steps=3)
    assert error.value.detail["phase"] == "coarse"
    assert error.value.detail["required_drags"] == 4
    assert scene.events == []


def test_correction_budget_does_not_start_a_partial_batch(scene):
    scene.frames = [[label("岚心城", 1000, 360)]]
    with pytest.raises(travel.IntercityDestinationError) as error:
        select(scene, max_search_steps=4)
    assert error.value.detail["phase"] == "correction"
    assert error.value.detail["remaining_drags"] == 0
    assert scene.events == ["drag"] * 4 + ["capture", "ocr"]
    assert not scene.clicks


def test_multigesture_correction_budget_is_checked_before_its_first_gesture(scene):
    # An observed Cape label implies another four-gesture correction, while
    # only two gestures remain in the budget after the initial coarse batch.
    scene.frames = [[label("海角城")]]
    with pytest.raises(travel.IntercityDestinationError) as error:
        select(scene, max_search_steps=6)
    assert error.value.detail["phase"] == "correction"
    assert error.value.detail["required_drags"] == 4
    assert error.value.detail["remaining_drags"] == 2
    assert scene.events == ["drag"] * 4 + ["capture", "ocr"]


@pytest.mark.parametrize("size", [(1920, 1080), (1280, 721), (1279, 720), None])
def test_nonreference_client_is_rejected_before_moving(scene, size):
    scene.size = size
    with pytest.raises(travel.IntercityDestinationError) as error:
        select(scene)
    assert error.value.code == "map_navigation_window_size_invalid"
    assert scene.events == []


@pytest.mark.parametrize("source", [None, "", "unknown_start_city"])
def test_unconfirmed_native_source_is_rejected_before_moving(scene, source):
    with pytest.raises(travel.IntercityDestinationError):
        select(scene, from_city_name=source)
    assert scene.events == []


def test_cancellation_before_start_performs_no_capture_or_input(scene):
    scene.cancelled = True
    with pytest.raises(asyncio.CancelledError):
        select(scene)
    assert scene.events == []


def test_cancellation_after_first_drag_aborts_remaining_batch_without_ocr(scene):
    scene.on_drag = lambda: setattr(scene, "cancelled", True)
    with pytest.raises(asyncio.CancelledError):
        select(scene)
    assert scene.events == ["drag"]


def test_cancellation_returned_from_ocr_prevents_destination_click(scene):
    scene.frames = [[label("栖羽站")]]
    scene.on_ocr = lambda: setattr(scene, "cancelled", True)
    with pytest.raises(asyncio.CancelledError):
        select(scene)
    assert scene.events == ["drag"] * 4 + ["capture", "ocr"]
    assert not scene.clicks


def test_resize_during_coarse_batch_aborts_before_next_gesture(scene):
    scene.on_drag = lambda: setattr(scene, "size", (1920, 1080))
    with pytest.raises(travel.IntercityDestinationError) as error:
        select(scene)
    assert error.value.code == "map_navigation_window_size_invalid"
    assert scene.events == ["drag"]


def test_resize_during_ocr_prevents_click_in_old_coordinate_system(scene):
    scene.frames = [[label("栖羽站")]]
    scene.on_ocr = lambda: setattr(scene, "size", (1920, 1080))
    with pytest.raises(travel.IntercityDestinationError) as error:
        select(scene)
    assert error.value.code == "map_navigation_window_size_invalid"
    assert not scene.clicks


@pytest.mark.parametrize("invalid", [
    label("栖羽站", confidence=0.79),
    label("栖羽站", confidence=float("nan")),
    label("栖羽站", confidence=1.01),
    label("栖羽站", x=1220),
    label("栖羽站", y=119),
    label("栖羽站", x=float("inf")),
])
def test_unreliable_or_outside_crop_targets_are_never_clicked(scene, invalid):
    scene.frames = [[invalid], [invalid]]
    with pytest.raises(travel.IntercityDestinationError) as error:
        select(scene)
    assert error.value.detail["stop_reason"] == "no_supported_city_labels"
    assert not scene.clicks


def test_disabled_correction_still_observes_target_after_coarse_movement(scene):
    scene.frames = [[label("岚心城", 1000, 360)]]
    with pytest.raises(travel.IntercityDestinationError) as error:
        select(scene, fallback_enabled=False)
    assert error.value.detail["stop_reason"] == "correction_disabled"
    assert len(scene.drags) == 4 and not scene.clicks


def test_unchanged_anchor_pose_detects_stalled_map_instead_of_looping(scene):
    scene.frames = [[label("岚心城", 1000, 360)]] * 2
    with pytest.raises(travel.IntercityDestinationError) as error:
        select(scene)
    assert error.value.detail["stop_reason"] == "map_movement_stalled"
    assert len(scene.drags) == 5 and not scene.clicks


def test_correction_rounds_are_bounded_when_target_remains_unrecognized(scene):
    scene.frames = [[label("岚心城", x, 360)] for x in (1000, 950, 900, 850)]
    with pytest.raises(travel.IntercityDestinationError) as error:
        select(scene)
    assert error.value.detail["stop_reason"] == "correction_budget_exhausted"
    assert scene.drags[4:] == [(40, 0), (90, 0), (140, 0)]
    assert error.value.detail["ocr_count"] == 4
    assert not scene.clicks


def test_conflicting_anchors_stop_before_correction_input(scene):
    scene.frames = [[label("岚心城", 1000, 360), label("云岫桥基地", 600, 360)]]
    with pytest.raises(travel.IntercityDestinationError) as error:
        select(scene)
    assert error.value.code == "map_anchors_inconsistent"
    assert scene.events == ["drag"] * 4 + ["capture", "ocr"]
    assert not scene.clicks
