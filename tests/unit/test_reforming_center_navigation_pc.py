"""Template-only room entry contracts; ordinary overview is supplied by caller."""

import inspect
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from plans.resonance_pc.src.actions import reforming_center_navigation_pc_actions as nav
from plans.resonance_pc.src.actions.runtime_preflight_pc_actions import PcClientResolutionError

ROOM = "植物纤维原料车间"
FIELDS = {"success", "status", "room_name", "entry_type", "room_entered", "reason", "data"}


class App:
    def __init__(self, images=None, size=(1280, 720), click_success=True):
        self.images = images or [np.zeros((720, 1280, 3), np.uint8)]
        self.size = size
        self.click_success = click_success
        self.clicks, self.drags, self.moves, self.captures = [], [], [], []

    def get_window_size(self):
        return self.size

    def capture(self, *, rect):
        index = min(len(self.captures), len(self.images)-1)
        self.captures.append(rect)
        return SimpleNamespace(success=True, image=self.images[index])

    def click(self, *, x, y):
        self.clicks.append((x, y))
        return SimpleNamespace(success=self.click_success)

    def move_to(self, **kwargs):
        self.moves.append(kwargs)

    def drag(self, **kwargs):
        self.drags.append(kwargs)


@pytest.fixture(autouse=True)
def no_wait(monkeypatch):
    monkeypatch.setattr(nav, "_settle", lambda seconds: None)
    monkeypatch.setattr(nav.checked, "_sleep", lambda seconds: None)


def patch_flow(monkeypatch, app, room=ROOM, *, button=True, destination=True):
    kind = nav.vision.load_catalog()["rooms"][room]["entry_type"]
    located, buttons, destinations = [], [], []

    def locate(*args, **kwargs):
        located.append((args, kwargs))
        return {"point": [640, 355], "score": .99, "drag_count": 0, "trace": [],
                "click": {"control": "room", "point": [640, 355],
                          "input_attempted": True, "call_returned": True}}

    def entry(image, spec, button_spec):
        assert located, "No initial page classification or already-in-room check"
        buttons.append(button_spec)
        return {"center": [795, 325], "score": .99} if button else None

    def confirmed(image, spec):
        assert located, "Destination check must follow room navigation"
        destinations.append(spec)
        return destination

    monkeypatch.setattr(nav, "_locate_and_click", locate)
    monkeypatch.setattr(nav.vision, "entry_button", entry)
    monkeypatch.setattr(nav.vision, "destination_confirmed", confirmed)
    return kind, located, buttons, destinations


def test_public_action_has_no_ocr_parameter():
    assert "ocr" not in inspect.signature(nav.resonance_pc_open_reforming_center_room).parameters


@pytest.mark.parametrize("room,kind", [
    (ROOM, "production"), ("动物纤维原料车间", "production"),
    ("缝纫工具原料车间", "production"), ("人造纤维原料车间", "production"),
    ("研发中心", "detail"), ("转运中心", "detail"), ("禁闭室", "detail"),
    ("医务室", "detail"), ("假释室", "detail"),
    ("1号整顿宿舍", "direct"), ("2号整顿宿舍", "direct"),
    ("1号生产车间", "direct"), ("2号生产车间", "direct"),
    ("原料采购站", "direct"), ("原料仓库", "direct"), ("成品仓库", "direct"),
    ("黑月订单站", "direct"), ("囚犯管理中心", "direct"), ("财务中心", "direct"),
    ("餐厅", "selection_only"), ("电力中心", "selection_only"),
    ("放风场地", "selection_only"), ("探视室", "selection_only"), ("安保中心", "selection_only"),
])
def test_screenshot_catalog_routes_follow_user_confirmed_operations(room, kind):
    assert nav.vision.load_catalog()["rooms"][room]["entry_type"] == kind


@pytest.mark.parametrize("room", [ROOM, "医务室", "2号生产车间", "餐厅"])
def test_four_entry_types_without_initial_recognition(monkeypatch, room):
    app = App()
    kind, located, buttons, destinations = patch_flow(monkeypatch, app, room)
    result = nav.resonance_pc_open_reforming_center_room(room, app=app, settle_sec=.05)
    assert FIELDS <= result.keys()
    assert result["success"] and result["entry_type"] == kind
    assert result["room_entered"] == (kind != "selection_only")
    assert len(located) == 1
    assert len(buttons) == (0 if kind == "direct" else 3)
    if buttons:
        button_kind = "construction" if kind == "selection_only" else kind
        assert all(spec == nav.vision.load_catalog()["buttons"][button_kind] for spec in buttons)
    assert len(destinations) == (0 if kind == "selection_only" else 3)
    assert app.clicks == ([(795, 325)] if kind in {"production", "detail"} else [])


def test_no_initial_page_read_or_idempotence_shortcut(monkeypatch):
    app = App()
    _, located, _, _ = patch_flow(monkeypatch, app, "2号生产车间")
    for _ in range(2):
        assert nav.resonance_pc_open_reforming_center_room("2号生产车间", app=app, settle_sec=.05)["success"]
    assert len(located) == 2


def test_alias_resolves_to_canonical_room(monkeypatch):
    app = App()
    _, located, _, _ = patch_flow(monkeypatch, app)
    result = nav.resonance_pc_open_reforming_center_room("植物纤维车间", app=app, settle_sec=.05)
    assert result["room_name"] == ROOM
    assert located[0][0][1] == ROOM


def test_second_button_absent_stops_without_click(monkeypatch):
    app = App()
    _, _, buttons, destinations = patch_flow(monkeypatch, app, button=False)
    result = nav.resonance_pc_open_reforming_center_room(ROOM, app=app, settle_sec=.05)
    assert not result["success"] and result["reason"] == "target_selection_not_confirmed"
    assert len(buttons) == 10 and destinations == [] and app.clicks == []


def test_destination_absent_does_not_retry_second_button(monkeypatch):
    app = App()
    _, _, buttons, destinations = patch_flow(monkeypatch, app, destination=False)
    result = nav.resonance_pc_open_reforming_center_room(ROOM, app=app, settle_sec=.05)
    assert not result["success"] and not result["room_entered"]
    assert len(buttons) == 3 and len(destinations) == 10
    assert app.clicks == [(795, 325)]


def test_second_click_failure_stops_before_destination(monkeypatch):
    app = App(click_success=False)
    _, _, _, destinations = patch_flow(monkeypatch, app)
    result = nav.resonance_pc_open_reforming_center_room(ROOM, app=app, settle_sec=.05)
    assert result["reason"] == "entry_click_failed" and not result["success"]
    assert app.clicks == [(795, 325)] and destinations == []


def test_room_and_entry_click_coordinates_are_multiples_of_five(monkeypatch):
    app = App()
    rooms = nav.layout._load_reforming_center_room_layout()
    monkeypatch.setattr(nav.room_map, "load_map", lambda: {})
    monkeypatch.setattr(nav.room_map, "locate_viewport", lambda hits, model: {"anchors": [ROOM]})
    monkeypatch.setattr(nav.vision, "room_hits", lambda *args: [hit(center=(523, 302))])
    located = nav._locate_and_click(app, ROOM, rooms, {}, 1, .05)
    assert located["point"] == [645, 355]
    assert app.clicks == [(645, 355)]
    assert located["click"]["input_attempted"] and located["click"]["call_returned"]
    patch_flow(monkeypatch, app)
    monkeypatch.setattr(nav.vision, "entry_button", lambda *args: {"center": [797, 327], "score": .99})
    result = nav.resonance_pc_open_reforming_center_room(ROOM, app=app, settle_sec=.05)
    assert result["success"] and app.clicks[-1] == (795, 325)
    assert all(coordinate % 5 == 0 for point in app.clicks for coordinate in point)
    entry = result["data"]["entry_button"]
    assert entry["effect_confirmed"] and entry["transition_ready"]


@pytest.mark.parametrize("stage", ["room", "entry"])
def test_explicit_false_input_stops_without_confirmation_or_retry(monkeypatch, stage):
    app = App()
    def rejected_click(*, x, y):
        app.clicks.append((x, y))
        return False
    monkeypatch.setattr(app, "click", rejected_click)
    if stage == "room":
        with pytest.raises(nav.layout.ReformingCenterNavigationError) as exc:
            run_locator(monkeypatch, app, [[hit()]])
        assert exc.value.code == "room_click_failed"
        assert len(app.captures) == 1  # only locate frame, no UI success probe
    else:
        _, _, buttons, destinations = patch_flow(monkeypatch, app)
        result = nav.resonance_pc_open_reforming_center_room(ROOM, app=app, settle_sec=.05)
        assert result["reason"] == "entry_click_failed" and not result["success"]
        assert len(buttons) == 3 and destinations == []
        assert result["data"]["entry_button"]["input_acknowledged"] is False
    assert len(app.clicks) == 1 and app.drags == []


def test_running_workshop_original_frame_confirms_destination_without_choose_button():
    fixture = Path(__file__).resolve().parents[1] / "fixtures/raw_workshop_ui/running.png"
    image = cv2.imread(str(fixture))
    assert image is not None and image.shape == (720, 1280, 3)
    room = nav.vision.load_catalog()["rooms"][ROOM]
    assert nav.vision.match_asset(image, room["destination_markers"][1]) is None
    assert nav.vision.destination_confirmed(image, room)
    other = nav.vision.load_catalog()["rooms"]["动物纤维原料车间"]
    assert not nav.vision.destination_confirmed(image, other)


@pytest.mark.parametrize("room", [f"{number}号{kind}" for number in range(3, 7) for kind in ("整顿宿舍", "生产车间")])
def test_locked_room_without_user_template_does_not_locate_or_click(monkeypatch, room):
    app = App()
    monkeypatch.setattr(nav, "_locate_and_click", lambda *args: pytest.fail("unavailable template must not navigate"))
    result = nav.resonance_pc_open_reforming_center_room(room, app=app)
    assert not result["success"] and result["reason"] == "room_template_unavailable"
    assert app.captures == app.drags == app.clicks == []


@pytest.mark.parametrize("kwargs", [
    {"room_name": ""}, {"room_name": "不存在的房间"},
    {"max_search_steps": 0}, {"max_search_steps": 25}, {"max_search_steps": True},
    {"max_search_steps": 1.5}, {"max_search_steps": "bad"},
    {"settle_sec": -.1}, {"settle_sec": float("nan")}, {"settle_sec": float("inf")},
])
def test_invalid_inputs_no_capture_or_mutation(monkeypatch, kwargs):
    app = App()
    monkeypatch.setattr(nav, "_locate_and_click", lambda *args: pytest.fail("invalid input must not navigate"))
    with pytest.raises((ValueError, nav.layout.ReformingCenterNavigationError)):
        nav.resonance_pc_open_reforming_center_room(app=app, **{"room_name": ROOM, **kwargs})
    assert app.captures == app.drags == app.clicks == []


def test_resolution_guard_precedes_navigation(monkeypatch):
    app = App(size=(1920, 1080))
    monkeypatch.setattr(nav, "_locate_and_click", lambda *args: pytest.fail("bad resolution must not navigate"))
    with pytest.raises(PcClientResolutionError):
        nav.resonance_pc_open_reforming_center_room(ROOM, app=app)
    assert app.captures == app.drags == app.clicks == []


def test_confirmation_does_not_span_unknown_frame():
    app = App()
    sequence = iter([True, False, True, True])
    assert nav._wait_confirmed(app, .05, lambda image: next(sequence)) is True
    assert len(app.captures) == 4


def test_unstable_button_coordinates_never_confirm():
    app = App()
    sequence = iter([(795, 325), (805, 325)] * 3)
    assert nav._wait_confirmed(app, .05, lambda image: next(sequence)) is None
    assert len(app.captures) == 6


def hit(room=ROOM, center=(520, 300), score=.99):
    return {"room_name": room, "center": list(center), "score": score, "rect": [center[0]-80, center[1]-10, 160, 20]}


def run_locator(monkeypatch, app, hits, maximum=3):
    sequence = iter(hits)
    rooms = nav.layout._load_reforming_center_room_layout()
    # Synthetic geometry for input-control tests; real coordinates are verified
    # separately by the measured map's calibration evidence.
    model = {"rooms": {name: {"world_anchor": spec["map_point"], "component": 0,
                              "neighbors": [other for other in rooms if other != name]}
                       for name, spec in rooms.items()}}
    monkeypatch.setattr(nav.room_map, "load_map", lambda: model)
    monkeypatch.setattr(nav.vision, "room_hits", lambda image, catalog: next(sequence, hits[-1]))
    return nav._locate_and_click(app, ROOM, rooms, {}, maximum, .05)


def assert_safe_input(app):
    for drag in app.drags:
        assert nav.layout._point_in_region((drag["start_x"], drag["start_y"]), nav._SAFE_MAP_REGION, margin=20)
        assert nav.layout._point_in_region((drag["end_x"], drag["end_y"]), nav._SAFE_MAP_REGION, margin=20)
    for point in app.clicks:
        assert nav.layout._point_in_region(point, nav._SAFE_MAP_REGION, margin=10)


def test_real_locator_drags_edge_target_before_safe_click(monkeypatch):
    app = App()
    result = run_locator(monkeypatch, app, [[hit(center=(1080, 600))], [hit()]])
    assert result["drag_count"] == 1 and app.clicks == [(640, 355)]
    assert len(app.drags) == len(app.moves) == 1
    assert len(app.captures) == 2
    assert_safe_input(app)


def test_room_title_inside_region_but_click_offset_outside_still_drags(monkeypatch):
    app = App()
    run_locator(monkeypatch, app, [[hit(center=(850, 300))], [hit()]])
    assert len(app.drags) == 1 and app.clicks == [(640, 355)]
    assert_safe_input(app)


def test_anchor_translation_drives_target_search_not_blind_drag(monkeypatch):
    app = App()
    run_locator(monkeypatch, app, [[hit("餐厅", (225, 525))], [hit()]])
    drag = app.drags[0]
    # A single anchor takes a short horizontal hop, not a long diagonal drag.
    assert drag["end_x"] - drag["start_x"] == -120
    assert drag["end_y"] == drag["start_y"]
    assert_safe_input(app)


def test_missing_anchor_no_blind_drag_or_click(monkeypatch):
    app = App()
    with pytest.raises(nav.room_map.RoomMapError) as error:
        run_locator(monkeypatch, app, [[]])
    assert error.value.code == "room_anchor_not_found"
    assert app.drags == app.moves == app.clicks == []


def test_locator_drag_budget_is_bounded_without_edge_click(monkeypatch):
    app = App()
    with pytest.raises(nav.layout.ReformingCenterNavigationError) as error:
        run_locator(monkeypatch, app, [[hit(center=(1100, 650))]] * 2, maximum=2)
    assert error.value.code == "reforming_center_room_not_found"
    assert len(app.drags) == 2 and app.clicks == []
    assert_safe_input(app)


def test_failed_room_click_is_not_navigation_success(monkeypatch):
    app = App(click_success=False)
    with pytest.raises(nav.layout.ReformingCenterNavigationError) as error:
        run_locator(monkeypatch, app, [[hit()]])
    assert error.value.code == "room_click_failed" and app.drags == []


@pytest.mark.parametrize("image", [None, np.zeros((480, 640, 3), np.uint8),
                                 np.zeros((720, 1280), np.uint8),
                                 np.zeros((720, 1280, 4), np.uint8)])
def test_capture_invalid_shape_stops_before_template_matching(image):
    app = SimpleNamespace(capture=lambda **kwargs: SimpleNamespace(success=True, image=image))
    with pytest.raises(nav.layout.ReformingCenterNavigationError):
        nav._read_frame(app)


def test_capture_rgb_is_converted_once_to_bgr_without_mutation():
    rgb = np.full((720, 1280, 3), (255, 190, 0), np.uint8)
    app = App(images=[rgb])
    bgr = nav._read_frame(app)
    assert bgr[0, 0].tolist() == [0, 190, 255]
    assert rgb[0, 0].tolist() == [255, 190, 0]
    assert not np.shares_memory(rgb, bgr)
    assert app.captures == [(0, 0, 1280, 720)]


def test_rgb_capture_confirms_selected_animal_room_and_production_button():
    catalog = nav.vision.load_catalog()
    room = catalog["rooms"]["动物纤维原料车间"]
    button = catalog["buttons"]["production"]
    bgr = np.zeros((720, 1280, 3), np.uint8)
    cv2.rectangle(bgr, (90, 250), (649, 439), (0, 190, 255), 6)
    for spec, x, y in [(room["selected_title"], 110, 265), (button, 710, 310)]:
        asset = nav.vision._asset(spec["image"])
        h, w = asset.shape[:2]
        bgr[y:y+h, x:x+w] = asset
    rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    assert nav.vision.selected_rectangles(rgb) == []
    normalized = nav._read_frame(App(images=[rgb]))
    assert len(nav.vision.selected_rectangles(normalized)) == 1
    found = nav.vision.entry_button(normalized, room, button)
    assert found is not None and found["score"] >= .92
    assert found["rect"][:2] == [710, 310]


def texture():
    random = np.random.default_rng(471)
    plane = (random.integers(0, 2, (14, 20)) * 255).astype(np.uint8)
    return np.repeat(plane[:, :, None], 3, axis=2)


def test_template_matching_respects_region_and_returns_absolute_coordinates(monkeypatch):
    patch = texture()
    monkeypatch.setattr(nav.vision, "_asset", lambda path: patch)
    image = np.zeros((720, 1280, 3), np.uint8)
    image[210:224, 460:480] = patch
    spec = {"image": "synthetic", "kind": "white_text", "threshold": .99}
    found = nav.vision.match_asset(image, spec, (420, 170, 200, 100))
    assert found["rect"] == [460, 210, 20, 14] and found["center"] == [470, 217]
    assert nav.vision.match_asset(image, spec, (0, 0, 100, 100)) is None


def test_nonfinite_template_scores_are_not_matches(monkeypatch):
    monkeypatch.setattr(nav.vision, "_asset", lambda path: texture())
    monkeypatch.setattr(nav.vision.cv2, "matchTemplate", lambda *args: np.array([[np.nan, np.inf]], np.float32))
    assert nav.vision.match_asset(np.zeros((720, 1280, 3), np.uint8), {"image": "synthetic"}) is None


def test_flat_template_and_too_small_search_are_rejected(monkeypatch):
    image = np.zeros((720, 1280, 3), np.uint8)
    monkeypatch.setattr(nav.vision, "_asset", lambda path: texture())
    assert nav.vision.match_asset(image, {"image": "synthetic"}, (0, 0, 5, 5)) is None
    monkeypatch.setattr(nav.vision, "_asset", lambda path: np.full((14, 20, 3), 255, np.uint8))
    assert nav.vision.match_asset(image, {"image": "flat"}) is None


@pytest.mark.parametrize("same_location", [True, False])
def test_ambiguous_room_labels_at_same_position_are_rejected_not_global(monkeypatch, same_location):
    catalog = {"rooms": {"甲": {"map_title": {"image": "a"}}, "乙": {"map_title": {"image": "b"}}}}
    def match(image, spec, region):
        x = 500 if same_location or spec["image"] == "a" else 900
        return {"score": .99, "center": [x+50, 300], "rect": [x, 290, 100, 20]}
    monkeypatch.setattr(nav.vision, "match_asset", match)
    result = nav.vision.room_hits(np.zeros((720, 1280, 3), np.uint8), catalog)
    assert len(result) == (0 if same_location else 2)


def test_selected_outline_requires_all_four_yellow_edges():
    image = np.zeros((720, 1280, 3), np.uint8)
    cv2.rectangle(image, (90, 250), (649, 439), (0, 190, 255), 6)
    assert len(nav.vision.selected_rectangles(image)) == 1
    image[435:445] = 0
    assert nav.vision.selected_rectangles(image) == []


@pytest.mark.parametrize("target_title_matches", [True, False])
def test_entry_button_is_bound_to_target_title_inside_yellow_outline(monkeypatch, target_title_matches):
    image = np.zeros((720, 1280, 3), np.uint8)
    cv2.rectangle(image, (90, 250), (649, 439), (0, 190, 255), 6)
    seen = []

    def match(image, spec, region):
        seen.append((spec, region))
        if spec["image"] == "title":
            assert region[0] < 110 and region[1] < 270
            assert region[3] == 65
            return {"score": .99} if target_title_matches else None
        assert spec["image"] == "production" and region[0] >= 645
        return {"score": .99, "center": [795, 325]}

    monkeypatch.setattr(nav.vision, "match_asset", match)
    result = nav.vision.entry_button(image, {"selected_title": {"image": "title"}}, {"image": "production"})
    if target_title_matches:
        assert result["center"] == [795, 325] and len(seen) == 2
    else:
        assert result is None and len(seen) == 1


def test_entry_button_requires_selected_outline_before_matching(monkeypatch):
    monkeypatch.setattr(nav.vision, "match_asset", lambda *args: pytest.fail("without yellow outline no title or button search"))
    assert nav.vision.entry_button(np.zeros((720, 1280, 3), np.uint8), {"selected_title": {}}, {}) is None


def test_destination_requires_at_least_two_matching_markers(monkeypatch):
    image = np.zeros((720, 1280, 3), np.uint8)
    monkeypatch.setattr(nav.vision, "match_asset", lambda image, marker: {"score": .99} if marker["found"] else None)
    assert nav.vision.destination_confirmed(image, {"destination_markers": [{"found": True}, {"found": True}]})
    assert not nav.vision.destination_confirmed(image, {"destination_markers": [{"found": True}]})
    assert not nav.vision.destination_confirmed(image, {"destination_markers": [{"found": True}, {"found": False}]})
