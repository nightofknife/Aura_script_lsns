"""CPU-only selection-loop checks; no capture device or game input."""
from types import SimpleNamespace
from pathlib import Path

import cv2
import numpy as np
import pytest
from PIL import Image

from plans.aura_base.src.services.vision_service import VisionService, MatchResult
from plans.resonance_pc.src.actions import _trade_buy_selection as m


class CpuVision:
    def __init__(self):
        self.core = VisionService()
        self.shapes = []

    def find_templates_batch(self, *, source_image, template_images, **options):
        assert options == m._match_args()
        self.shapes.append(("batch", source_image.shape))
        return self.core._find_templates_batch_sync(source_image, template_images, None, **options)

    def find_template(self, *, source_image, template_image, **options):
        assert options == m._match_args()
        self.shapes.append(("single", source_image.shape))
        return self.core._match_template_prepared(source_image, template_image, None, **options)


def product(pid="one", seed=1):
    rng = np.random.default_rng(seed)
    available = rng.integers(40, 230, (56, 80, 3), dtype=np.uint8)
    selected = np.rint(available.astype(float)*.4).astype(np.uint8)
    selected[10:25, 30:50] = 255
    return m.ProductTemplate(pid, pid, available, selected)


def catalog(*items):
    return m.ProductCatalog({item.product_id: item for item in items},
                            {item.name: item.product_id for item in items})


def frame(*rows, icon_left=10):
    result = np.full((545, 130, 3), 15, np.uint8)
    for image, top in rows:
        result[top+24:top+80, icon_left+8:icon_left+88] = image
    return result


class App:
    def __init__(self, pages, on_click=None):
        self.pages, self.page, self.on_click = pages, 0, on_click
        self.inputs, self.captures = [], 0

    def capture(self, *, rect):
        assert rect == m.ICON_COLUMN
        self.captures += 1
        return SimpleNamespace(success=True, image=self.pages[self.page].copy())

    def click(self, **point):
        self.inputs.append(("click", point))
        # Only the row's blue blank band responds, never its product icon.
        if self.on_click and 630 <= point["x"] <= 700:
            self.on_click(self, point)

    def move_to(self, **point):
        self.inputs.append(("move", point))

    def drag(self, **points):
        assert points["end_y"] < points["start_y"]
        self.inputs.append(("drag", points))
        self.page = min(self.page+1, len(self.pages)-1)

    @property
    def clicks(self):
        return [point for action, point in self.inputs if action == "click"]

    @property
    def drags(self):
        return [point for action, point in self.inputs if action == "drag"]


@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch):
    clock = [0.]
    monkeypatch.setattr(m.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0]+seconds))
    monkeypatch.setattr(m.time, "monotonic", lambda: clock[0])


def run(app, products, requested=None, vision=None, **options):
    return m.select_buy_products(app=app, vision=vision or CpuVision(), catalog=products,
        product_list=requested if requested is not None else list(products.names),
        check_cancelled=lambda: None, **options)


def test_confirmed_click_targets_blue_row_but_recognizes_only_icon_column():
    item, vision = product(), CpuVision()
    app = App([frame((item.available, 20))],
              lambda app, point: app.pages.__setitem__(0, frame((item.selected, 20))))
    result = run(app, catalog(item), vision=vision)
    assert result["selected_product_ids"] == ["one"] and not result["missing_products"]
    assert app.clicks == [{"x": 656, "y": 208}] and not app.drags
    assert vision.shapes == [("batch", (545, 130, 3)), ("single", (60, 84, 3))]
    click = next(entry for entry in result["scan_trace"] if entry["phase"] == "product_clicked")
    assert click["click_target"] == "row_blank" and click["template_rect"] == [18, 44, 80, 56]


def test_fake_interface_ignores_the_old_icon_center_click():
    item = product()
    original = frame((item.available, 20))
    app = App([original.copy()],
              lambda app, point: app.pages.__setitem__(0, frame((item.selected, 20))))
    app.click(x=558, y=212)
    assert np.array_equal(app.pages[0], original)
    app.click(x=656, y=208)
    assert not np.array_equal(app.pages[0], original)


@pytest.mark.parametrize("left,top", [(5, 20), (10, 150), (20, 380)])
def test_click_position_tracks_icon_origin_not_the_cropped_template_center(left, top):
    item = product()
    expected = {"x": 500+left+96+50, "y": 140+top+48}
    def clicked(app, point):
        if point == expected:
            app.pages[0] = frame((item.selected, top), icon_left=left)
    app = App([frame((item.available, top), icon_left=left)], clicked)
    result = run(app, catalog(item))
    assert result["selected_product_ids"] == ["one"] and app.clicks == [expected]


def test_lost_click_retries_once_only_while_available():
    item = product()
    def clicked(app, point):
        if len(app.clicks) == 2:
            app.pages[0] = frame((item.selected, 20))
    app = App([frame((item.available, 20))], clicked)
    assert run(app, catalog(item))["selected_product_ids"] == ["one"]
    assert app.clicks == [{"x": 656, "y": 208}]*2


def test_failed_two_clicks_is_missing_not_fatal_or_retried_after_scrolling():
    item, other = product(), product("absent", 2)
    app = App([frame((item.available, 20))])
    result = run(app, catalog(item, other))
    assert not result["selected_products"] and len(app.clicks) == 2
    assert app.clicks == [{"x": 656, "y": 208}]*2
    assert result["missing_products"] == ["one", "absent"]
    assert result["warnings"][0]["reason"] == "selection_not_confirmed"


def test_uncertain_after_click_does_not_retry():
    item = product()
    app = App([frame((item.available, 20))], lambda app, point: app.pages.__setitem__(0, frame()))
    result = run(app, catalog(item))
    assert not result["selected_products"] and len(app.clicks) == 1


def test_selected_other_row_never_confirms_clicked_row():
    item = product()
    app = App([frame((item.available, 20), (item.selected, 200))])
    result = run(app, catalog(item))
    assert not result["selected_products"] and len(app.clicks) == 2


def test_new_frame_coordinates_are_used_after_each_confirmed_click():
    one, two = product(), product("two", 2)
    def clicked(app, point):
        app.pages[0] = (frame((one.selected, 20), (two.available, 250)) if len(app.clicks) == 1
                        else frame((one.selected, 20), (two.selected, 250)))
    app = App([frame((one.available, 20), (two.available, 150))], clicked)
    result = run(app, catalog(one, two), ["one", "two", "one"])
    assert result["selected_product_ids"] == ["one", "two"]
    assert app.clicks == [{"x": 656, "y": 208}, {"x": 656, "y": 438}]
    assert not app.drags


def test_start_current_view_and_scroll_down_without_rewind():
    item = product()
    app = App([frame(), frame((item.available, 150))],
        lambda app, point: app.pages.__setitem__(1, frame((item.selected, 150))))
    result = run(app, catalog(item))
    assert result["selected_product_ids"] == ["one"]
    assert len(app.drags) == 1 and app.inputs[0][0] == "move"


def test_unchanged_list_stops_after_two_scrolls():
    app = App([frame()])
    result = run(app, catalog(product()))
    assert result["stop_reason"] == "list_end" and len(app.drags) == 2
    assert not app.clicks


def test_scan_cap_prevents_unbounded_dragging():
    app = App([frame()])
    result = run(app, catalog(product()), max_scan_rounds=2)
    assert result["stop_reason"] == "scan_limit" and len(app.drags) == 1


def test_unknown_and_unsupported_products_warn_without_screen_capture():
    unsupported = m.ProductTemplate("unsupported", "unsupported", None, None, "missing_sprite")
    app = App([frame()])
    result = run(app, catalog(unsupported), ["unknown", "unsupported"])
    assert app.captures == 0 and not app.inputs
    assert [warning["reason"] for warning in result["warnings"]] == ["unknown_product", "missing_sprite"]


@pytest.mark.parametrize("top", [0, 525])
def test_partial_icon_is_not_clicked(top):
    item = product()
    hit = MatchResult(found=True, confidence=.99, rect=(18, top, 80, 56))
    vision = SimpleNamespace(find_templates_batch=lambda **kw: [hit])
    app = App([frame()])
    result = run(app, catalog(item), vision=vision, max_scan_rounds=1)
    assert not result["selected_products"] and not app.clicks
    assert any(entry["phase"] == "partial_icon" for entry in result["scan_trace"])


def test_equal_confidence_identity_conflict_is_skipped():
    one, two = product(), product("two", 2)
    hits = [MatchResult(found=True, confidence=.99, rect=(18, 44, 80, 56))]*2
    app = App([frame()])
    result = run(app, catalog(one, two), vision=SimpleNamespace(find_templates_batch=lambda **kw: hits),
                 max_scan_rounds=1)
    assert not result["selected_products"] and not app.clicks


def test_stronger_unrequested_identity_blocks_similar_requested_false_hit():
    one, two = product(), product("two", 2)
    hits = [MatchResult(found=True, confidence=.996, rect=(18, 44, 80, 56)),
            MatchResult(found=True, confidence=.921, rect=(18, 44, 80, 56))]
    app = App([frame()])
    result = run(app, catalog(one, two), ["two"],
                 vision=SimpleNamespace(find_templates_batch=lambda **kw: hits), max_scan_rounds=1)
    assert not result["selected_products"] and not app.clicks


@pytest.mark.parametrize("hit", [MatchResult(confidence=float("nan")),
                                 MatchResult(debug_info={"error": "failed"}), object()])
def test_invalid_match_result_is_a_structured_failure(hit):
    with pytest.raises(m.TradeBuySelectionError, match="Invalid matching"):
        run(App([frame()]), catalog(product()),
            vision=SimpleNamespace(find_templates_batch=lambda **kw: [hit]))


def test_capture_failure_is_not_a_missing_product():
    app = SimpleNamespace(capture=lambda **kw: None)
    with pytest.raises(m.TradeBuySelectionError) as error:
        run(app, catalog(product()))
    assert error.value.code == "buy_capture_failed"


def test_cancel_after_click_stops_before_confirmation_or_retry():
    item = product()
    app = App([frame((item.available, 20))])
    def cancelled():
        if app.clicks:
            raise RuntimeError("cancelled")
    with pytest.raises(RuntimeError, match="cancelled"):
        m.select_buy_products(app=app, vision=CpuVision(), catalog=catalog(item),
                              product_list=["one"], check_cancelled=cancelled)
    assert len(app.clicks) == 1 and not app.drags


def test_catalog_loads_opaque_assets_and_caches_without_game_dependency():
    first = m.load_product_templates()
    assert first is m.load_product_templates()
    assert len(first.products) == 229
    assert sum(item.available is not None for item in first.products.values()) == 228
    assert all(item.available is None or not item.available.flags.writeable for item in first.products.values())


def test_malformed_catalog_is_structured_failure(tmp_path):
    path = tmp_path / "data/meta/templates.json"
    path.parent.mkdir(parents=True)
    path.write_text("[]", encoding="utf-8")
    with pytest.raises(m.TradeBuySelectionError) as error:
        m.load_product_templates(str(path))
    assert error.value.code == "trade_product_templates_invalid"


def test_real_quilt_crop_selects_correct_id_and_not_requested_paraffin():
    products = m.load_product_templates()
    fixture = Path(__file__).resolve().parents[1] / "fixtures/trade_product_templates/176_available.png"
    icon = np.asarray(Image.open(fixture))
    available = frame((icon[24:80, 8:88], 20))
    paraffin = next(item.name for item in products.products.values() if item.name == "\u77f3\u8721")
    app = App([available])
    result = run(app, products, [paraffin], max_scan_rounds=1)
    assert not app.clicks and result["missing_products"] == [paraffin]
    app = App([available], lambda app, point: app.pages.__setitem__(0,
               frame((products.products["176"].selected, 20))))
    result = run(app, products, [products.products["176"].name, paraffin], max_scan_rounds=1)
    assert result["selected_product_ids"] == ["176"] and result["missing_products"] == [paraffin]
