"""Collection matching contracts with real catalog crops and a small oracle.

Synthetic clients contain real catalog templates. They establish pixel and
observation contracts, not live-game acceptance or animation behavior.
"""
from __future__ import annotations

from pathlib import Path
import os
import errno
import stat
import subprocess
from types import SimpleNamespace

import cv2
import numpy as np
import pytest
import yaml

from plans.resonance_pc.src.actions import _product_collection_matching as matching
from plans.resonance_pc.src.actions import _product_collection_ui as ui


ROOT = Path(__file__).resolve().parents[2]
PLAN = ROOT / "plans/resonance_pc"


def reference_match_asset(frame, spec, plan, region=None):
    """Uncached CCOEFF oracle; no production cache or frame helpers are reused."""
    region = region or spec.get("search_region") or [0, 0, 1280, 720]
    x, y, width, height = map(int, region)
    x, y = max(x, 0), max(y, 0)
    width, height = min(width, 1280-x), min(height, 720-y)
    path = (plan / spec["image"]).resolve()
    if not path.is_relative_to(plan.resolve()):
        raise ValueError("Template path must remain inside this plan")
    pixels = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)
    if pixels is None:
        raise ValueError(f"Template not readable: {spec['image']}")
    source = frame[y:y+height, x:x+width, :3]
    target = cv2.cvtColor(pixels, cv2.COLOR_BGR2GRAY)
    source = cv2.cvtColor(source, cv2.COLOR_BGR2GRAY)
    th, tw = target.shape[:2]
    if height < th or width < tw or float(target.std()) < 1:
        return None
    scores = cv2.matchTemplate(source, target, cv2.TM_CCOEFF_NORMED)
    scores[~np.isfinite(scores)] = -1
    _, score, _, point = cv2.minMaxLoc(scores)
    if not np.isfinite(score) or score < float(spec.get("threshold", .92)):
        return None
    return {"score": float(score), "rect": [x+point[0], y+point[1], tw, th],
            "center": [x+point[0]+tw//2, y+point[1]+th//2]}


def template_pixels(spec):
    return cv2.imdecode(np.fromfile(PLAN / spec["image"], dtype=np.uint8), cv2.IMREAD_COLOR)


def paste(frame, pixels, point):
    x, y = map(int, point)
    height, width = pixels.shape[:2]
    left, top, right, bottom = max(0, x), max(0, y), min(1280, x + width), min(720, y + height)
    if right > left and bottom > top:
        frame[top:bottom, left:right] = pixels[top-y:bottom-y, left-x:right-x]


def collection_cases():
    """Return deterministic BGR clients with real titles, cards and modal crops."""
    data = ui.catalog()
    specs = data["templates"]
    random = np.random.default_rng(20261022)
    background = random.integers(12, 40, size=(720, 1280, 3), dtype=np.uint8)

    def place(frame, name, point=None):
        spec = specs[name]
        paste(frame, template_pixels(spec), point or spec["bbox"][:2])

    cases = [{"name": "blank", "frame": np.zeros_like(background), "kind": "room", "number": 1}]
    for number in range(1, 7):
        physical = background.copy()
        place(physical, f"room_{number}_title")
        place(physical, "workshop_edit_marker")
        cases.append({"name": f"room_{number}_idle", "frame": physical, "kind": "room", "number": number})
        collectable = physical.copy()
        for name in ("collect_all_block", "collectable_badge", "collect_all_icon", "collect_all_text"):
            place(collectable, name)
        cases.append({"name": f"room_{number}_collectable", "frame": collectable, "kind": "room", "number": number})
        if number == 1:
            for key, label in (("capacity_warning_text", "capacity"), ("collection_complete", "reward")):
                modal = collectable.copy()
                place(modal, key)
                cases.append({"name": label, "frame": modal, "kind": "room", "number": number})
            title = specs["room_1_title"]
            x, y, width, height = title["bbox"]
            occluded = physical.copy()
            occluded[y:y+height, x:x+width//2] = 0
            cases.append({"name": "title_occluded", "frame": occluded, "kind": "room", "number": number})
            partial = physical.copy()
            place(partial, "collect_all_text")
            cases.append({"name": "partial_collect_control", "frame": partial, "kind": "room", "number": number})

    def navigation(name, cards, shift=(0, 0), edge=False, alter_luminance=False):
        frame = background.copy()
        popup = specs["navigation_popup_title"]
        px, py = popup["bbox"][:2]
        px, py = px + shift[0], py + shift[1]
        place(frame, "navigation_popup_title", (px, py))
        ox, oy, _, _ = data["navigation_viewport"]["offset_xywh"]
        viewport = (px + ox, py + oy)
        for index, (number, state) in enumerate(cards):
            primary = specs[f"navigation_{number}_{state}_identity"]
            variant = primary.get("variants", [primary])[0]
            dx, dy, _, _ = variant["card_offset"]
            card_x = viewport[0] + (-60 if edge else 20) + index * 210
            card_y = viewport[1] + 1
            pixels = template_pixels(variant).copy()
            if alter_luminance:
                nx, ny, nw, nh = variant["number_region"]
                pixels[ny:ny+nh, nx:nx+nw] = np.clip(
                    pixels[ny:ny+nh, nx:nx+nw].astype(np.int16) + 70, 0, 255).astype(np.uint8)
            paste(frame, pixels, (card_x-dx, card_y-dy))
            if state == "locked":
                place(frame, "locked_hint", (card_x+42, card_y+115))
        return {"name": name, "frame": frame, "kind": "navigation", "number": cards[0][0]}

    for number in range(1, 7):
        for state in ("open", "selected", "locked"):
            cases.append(navigation(f"navigation_{number}_{state}", [(number, state)]))
    cases.extend((navigation("navigation_mixed", [(1, "open"), (2, "selected"), (3, "locked")]),
                  navigation("navigation_moved", [(4, "open"), (5, "locked")], shift=(24, -18)),
                  navigation("navigation_partial_edge", [(1, "open")], edge=True),
                  navigation("navigation_locked_brightened", [(2, "locked")], alter_luminance=True)))
    for name in ("live_room1_entry_20261017", "live_room1_editor_20261017"):
        fixture = ROOT / "tests/fixtures/product_workshop_staff" / (name + ".png")
        if fixture.is_file():
            frame = cv2.imdecode(np.fromfile(fixture, dtype=np.uint8), cv2.IMREAD_COLOR)
            cases.append({"name": name, "frame": frame, "kind": "real_fixture", "number": 1,
                          "fixture": fixture.relative_to(ROOT).as_posix()})
    return cases


def observation_calls(module, case):
    frame, number = case["frame"], case["number"]
    if case["kind"] == "navigation":
        return {"navigation_open": lambda: module.navigation_open(frame),
                "navigation_region": lambda: module.navigation_region(frame),
                "navigation_close_point": lambda: module.navigation_close_point(frame),
                "navigation_cards": lambda: module.navigation_cards(frame),
                "physical_modal_guard": lambda: module.physical(frame, f"{number}号生产车间")}
    return {"title": lambda: module._title(frame, number),
            "physical": lambda: module.physical(frame, f"{number}号生产车间"),
            "collection_state": lambda: module.collection_state(frame, f"{number}号生产车间"),
            "collect_all": lambda: module.control(frame, "collect_all"),
            "complete": lambda: module.control(frame, "complete"),
            "capacity_warning": lambda: module.control(frame, "capacity_warning")}


def captured(function):
    try:
        return {"value": function()}
    except Exception as error:
        return {"exception": type(error).__name__, "message": str(error)}


def compare_fields(expected, actual, path="result"):
    """Yield every differing leaf; coordinates/identity and error text are exact."""
    if isinstance(expected, float) and isinstance(actual, (float, int)):
        if not np.isclose(expected, actual, rtol=0, atol=1e-7, equal_nan=True):
            yield {"field": path, "old": expected, "new": actual}
    elif type(expected) is not type(actual):
        yield {"field": path, "old": repr(expected), "new": repr(actual)}
    elif isinstance(expected, dict):
        if expected.keys() != actual.keys():
            yield {"field": path + ".keys", "old": sorted(expected), "new": sorted(actual)}
        for key in expected.keys() & actual.keys():
            yield from compare_fields(expected[key], actual[key], f"{path}.{key}")
    elif isinstance(expected, (list, tuple)):
        if len(expected) != len(actual):
            yield {"field": path + ".length", "old": len(expected), "new": len(actual)}
        for index, (left, right) in enumerate(zip(expected, actual)):
            yield from compare_fields(left, right, f"{path}[{index}]")
    elif expected != actual:
        yield {"field": path, "old": expected, "new": actual}


@pytest.fixture(scope="module")
def real_cases():
    return collection_cases()


def test_real_room_identity_and_modal_priority(real_cases):
    expected = {"blank": None, "capacity": "capacity_blocked", "reward": "complete",
                "title_occluded": None, "partial_collect_control": None}
    for case in real_cases:
        if case["kind"] != "room":
            continue
        state = expected.get(case["name"], "collectable" if case["name"].endswith("_collectable") else "idle")
        assert ui.collection_state(case["frame"], f"{case['number']}号生产车间") == state, case["name"]
        if state in {"idle", "collectable"}:
            assert ui.physical(case["frame"], f"{case['number']}号生产车间") is True
            rival = case["number"] % 6 + 1
            assert ui.physical(case["frame"], f"{rival}号生产车间") is False


def test_real_navigation_identity_locked_luminance_and_partial_edges(real_cases):
    for case in real_cases:
        if case["kind"] != "navigation":
            continue
        cards = ui.navigation_cards(case["frame"])
        if case["name"] in {"navigation_partial_edge", "navigation_locked_brightened"}:
            assert cards == [], case["name"]
        elif case["name"] == "navigation_mixed":
            assert [(c["number"], c["state"]) for c in cards] == [(1, "open"), (2, "open"), (3, "locked")]
        elif case["name"] == "navigation_moved":
            assert [(c["number"], c["state"]) for c in cards] == [(4, "open"), (5, "locked")]
        else:
            state = "locked" if case["name"].endswith("_locked") else "open"
            assert [(c["number"], c["state"]) for c in cards] == [(case["number"], state)], case["name"]


@pytest.fixture
def isolated_asset(tmp_path, monkeypatch):
    monkeypatch.setattr(matching, "_PLAN", tmp_path)
    matching._load_template.cache_clear()
    rng = np.random.default_rng(42)
    pixels = rng.integers(0, 256, (16, 21, 3), dtype=np.uint8)
    path = tmp_path / "asset.png"
    path.write_bytes(cv2.imencode(".png", pixels)[1].tobytes())
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    frame[30:46, 40:61] = pixels
    yield tmp_path, path, pixels, frame
    matching._load_template.cache_clear()


def test_template_snapshots_are_immutable_and_refresh_on_file_version(isolated_asset):
    _, path, pixels, _ = isolated_asset
    first = matching._asset("asset.png")
    for image in (first.image, first.gray()):
        assert image.flags.writeable is False
        with pytest.raises(ValueError):
            image.setflags(write=True)
    replacement = 255 - pixels
    stamp = path.stat().st_mtime_ns
    path.write_bytes(cv2.imencode(".png", replacement)[1].tobytes())
    os.utime(path, ns=(stamp + 2_000_000_000, stamp + 2_000_000_000))
    second = matching._asset("asset.png")
    assert second is not first
    np.testing.assert_array_equal(second.image, replacement)
    np.testing.assert_array_equal(first.image, pixels)
    path.unlink()
    with pytest.raises(FileNotFoundError):
        matching._asset("asset.png")


def test_frame_scope_reuses_scores_but_respects_thresholds_coordinates_and_new_frames(isolated_asset, monkeypatch):
    _, _, _, frame = isolated_asset
    spec = {"image": "asset.png", "threshold": 1.01}
    calls = []
    original = cv2.matchTemplate
    def counted(*args, **kwargs):
        calls.append(args[0].shape)
        return original(*args, **kwargs)
    monkeypatch.setattr(matching.cv2, "matchTemplate", counted)
    with matching.observe_frame(frame):
        assert matching.match_asset(frame, spec, (20, 10, 100, 80)) is None
        accepted = matching.match_asset(frame, {**spec, "threshold": .9}, (20, 10, 100, 80))
        assert accepted["rect"] == [40, 30, 21, 16]
        assert accepted["center"] == [50, 38]
        accepted["rect"][0] = -99
        assert matching.match_asset(frame, {**spec, "threshold": .9}, (20, 10, 100, 80))["rect"][0] == 40
        assert len(calls) == 1
        assert matching.match_asset(frame, {**spec, "threshold": .9}, (0, 0, 100, 80)) is not None
        assert len(calls) == 2
    frame[:] = 0
    with matching.observe_frame(frame):
        assert matching.match_asset(frame, {**spec, "threshold": .9}, (20, 10, 100, 80)) is None
    assert len(calls) == 3
    assert matching._OBSERVATION.get() is None


def test_nested_frame_scope_and_exception_restore_outer_context(isolated_asset):
    _, _, _, frame = isolated_asset
    with matching.observe_frame(frame):
        outer = matching._OBSERVATION.get()
        with matching.observe_frame(frame):
            assert matching._OBSERVATION.get() is outer
        with pytest.raises(RuntimeError, match="interrupt"):
            with matching.observe_frame(frame.copy()):
                raise RuntimeError("interrupt")
        assert matching._OBSERVATION.get() is outer
    assert matching._OBSERVATION.get() is None


def test_one_observation_pins_versions_but_the_next_rechecks_replacement_and_deletion(isolated_asset):
    _, path, pixels, frame = isolated_asset
    with matching.observe_frame(frame):
        first = matching._asset("asset.png", frame=frame)
        stamp = path.stat().st_mtime_ns
        path.write_bytes(cv2.imencode(".png", 255-pixels)[1].tobytes())
        # Equal-sized writes can share a Windows filesystem timestamp tick.
        # This contract deliberately exercises an observable version change.
        os.utime(path, ns=(stamp + 2_000_000_000, stamp + 2_000_000_000))
        assert matching._asset("asset.png", frame=frame) is first
        # A different frame cannot borrow the outer frame's pinned template.
        assert matching._asset("asset.png", frame=frame.copy()) is not first
    with matching.observe_frame(frame):
        replaced = matching._asset("asset.png", frame=frame)
        np.testing.assert_array_equal(replaced.image, 255-pixels)
        path.unlink()
        assert matching._asset("asset.png", frame=frame) is replaced
    with matching.observe_frame(frame):
        with pytest.raises(FileNotFoundError):
            matching._asset("asset.png", frame=frame)


def test_directory_and_file_link_redirects_are_rechecked_between_observations(isolated_asset, monkeypatch):
    plan, _, pixels, frame = isolated_asset
    inside = plan / "second.png"
    inside.write_bytes(cv2.imencode(".png", 255-pixels)[1].tobytes())
    outside = plan.parent / "outside.png"
    outside.write_bytes(cv2.imencode(".png", pixels)[1].tobytes())
    alias = plan / "alias.png"
    destination = [plan / "asset.png"]
    directory_alias = plan / "redirected"
    first_directory, second_directory = plan / "first", plan / "second"
    for directory, image in ((first_directory, pixels), (second_directory, 255-pixels)):
        directory.mkdir()
        (directory / "asset.png").write_bytes(cv2.imencode(".png", image)[1].tobytes())
    directory_destination = [first_directory]
    original_resolve, original_lstat = Path.resolve, Path.lstat
    def resolved(path, *args, **kwargs):
        if path == alias:
            return destination[0]
        if path == directory_alias:
            return directory_destination[0]
        return original_resolve(path, *args, **kwargs)
    def inspected(path, *args, **kwargs):
        if path == alias:
            return SimpleNamespace(st_mode=stat.S_IFLNK, st_file_attributes=getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
        return original_lstat(path, *args, **kwargs)
    monkeypatch.setattr(Path, "resolve", resolved)
    monkeypatch.setattr(Path, "lstat", inspected)
    with matching.observe_frame(frame):
        first = matching._asset("alias.png", frame=frame)
        destination[0] = inside
        assert matching._asset("alias.png", frame=frame) is first
    with matching.observe_frame(frame):
        np.testing.assert_array_equal(matching._asset("alias.png", frame=frame).image, 255-pixels)
        destination[0] = outside
        assert matching._asset("alias.png", frame=frame).image.shape == pixels.shape
    with matching.observe_frame(frame), pytest.raises(ValueError, match="Template path must remain inside this plan"):
        matching._asset("alias.png", frame=frame)
    with matching.observe_frame(frame), pytest.raises(ValueError, match="Template path must remain inside this plan"):
        matching._asset("..", frame=frame)
    with matching.observe_frame(frame):
        first = matching._asset("redirected/asset.png", frame=frame)
        directory_destination[0] = second_directory
        assert matching._asset("redirected/asset.png", frame=frame) is first
    with matching.observe_frame(frame):
        np.testing.assert_array_equal(matching._asset("redirected/asset.png", frame=frame).image, 255-pixels)
    directory_destination[0] = plan.parent
    with matching.observe_frame(frame), pytest.raises(ValueError, match="Template path must remain inside this plan"):
        matching._asset("redirected/asset.png", frame=frame)


def test_shared_directory_resolution_is_scoped_not_retained_across_preflights(isolated_asset, monkeypatch):
    plan, path, pixels, _ = isolated_asset
    (plan / "second.png").write_bytes(cv2.imencode(".png", 255-pixels)[1].tobytes())
    root_calls = []
    original = Path.resolve
    def resolved(current, *args, **kwargs):
        if current == plan:
            root_calls.append(current)
        return original(current, *args, **kwargs)
    monkeypatch.setattr(Path, "resolve", resolved)
    for _ in range(2):
        start = len(root_calls)
        with matching.observe_frame(None):
            matching.validate_asset("asset.png")
            matching.validate_asset("second.png")
            matching.validate_asset("asset.png")
        assert len(root_calls) - start == 2  # root + shared parent, independent of sibling count
    path.unlink()
    with matching.observe_frame(None), pytest.raises(FileNotFoundError):
        matching.validate_asset("asset.png")


def test_sibling_containment_check_is_shared_but_file_versions_are_still_checked(isolated_asset, monkeypatch):
    plan, path, pixels, _ = isolated_asset
    second = plan / "second.png"
    second.write_bytes(cv2.imencode(".png", 255-pixels)[1].tobytes())
    checks, inspected = [], []
    relative_to, lstat = Path.is_relative_to, Path.lstat
    def contained(current, *args):
        checks.append(current)
        return relative_to(current, *args)
    def inspect(current, *args, **kwargs):
        inspected.append(current)
        return lstat(current, *args, **kwargs)
    monkeypatch.setattr(Path, "is_relative_to", contained)
    monkeypatch.setattr(Path, "lstat", inspect)
    for _ in range(2):
        checks.clear()
        inspected.clear()
        with matching.observe_frame(None):
            matching.validate_asset("asset.png")
            matching.validate_asset("second.png")
        assert checks == [plan]
        assert inspected == [path, second]
    second.unlink()
    with matching.observe_frame(None), pytest.raises(FileNotFoundError):
        matching.validate_asset("second.png")


@pytest.mark.parametrize("text,plain", [
    (r"D:\project\aura_s_lsns", True), (r"D:\.pytest_tmp\case_1\folder.name", True),
    (r"d:\MixedCase\a-b_12", True), ("D:\\", False), ("relative", False),
    (r"D:\a\..\b", False), (r"D:\a\.\b", False), (r"D:\a\\b", False),
    (r"D:\a\CON.txt\b", False), (r"D:\PRN.ext\b", False), (r"D:\COM1.log\b", False),
    (r"D:\LPT0\b", False), (r"D:\normal\NUL", False), (r"D:\a.\b", False),
    ("D:\\a \\b", False), (r"D:\a\b:stream", False), (r"D:\PROGRA~1\a", False),
    (r"D:\中文\a", False), (r"D:\space here\a", False), (r"D:\a?\b", False),
    (r"\\server\share\a", False), (r"\\?\D:\a", False),
    ("D:\\" + "a"*245, False),
])
def test_directory_fast_path_only_accepts_unambiguous_short_drive_syntax(text, plain):
    assert matching._plain_directory_text(text) is plain


@pytest.mark.skipif(not matching._NATIVE_DIRECTORY_RESOLUTION, reason="CPython 3.12 Windows fast path")
def test_directory_resolution_removes_only_redundant_native_query(isolated_asset, monkeypatch):
    import ntpath
    plan, _, _, _ = isolated_asset
    native, stat_file, cwd = ntpath._getfinalpathname, Path.stat, os.getcwd
    queries, stats, working_directories = [], [], []
    def query(path):
        queries.append(str(path))
        return native(path)
    def checked(path, *args, **kwargs):
        stats.append(path)
        return stat_file(path, *args, **kwargs)
    def current_directory():
        working_directories.append(True)
        return cwd()
    monkeypatch.setattr(ntpath, "_getfinalpathname", query)
    monkeypatch.setattr(matching, "_native_final_path", query)
    monkeypatch.setattr(Path, "stat", checked)
    monkeypatch.setattr(os, "getcwd", current_directory)
    expected = plan.resolve()
    assert len(queries) == 2 and len(stats) == 1 and len(working_directories) == 1
    queries.clear()
    stats.clear()
    working_directories.clear()
    assert matching._resolve_directory(plan) == expected
    assert len(queries) == 1 and len(stats) == 1 and len(working_directories) == 1


@pytest.mark.skipif(not matching._NATIVE_DIRECTORY_RESOLUTION, reason="CPython 3.12 Windows fast path")
@pytest.mark.parametrize("output", [r"\\?\UNC\server\share\folder", r"\\?\Volume{abc}\folder",
                                  r"\\?\D:\a\CON.txt", r"\\?\D:\a.\folder",
                                  r"\\?\D:\space here\folder", r"D:\unprefixed", None])
def test_unusual_native_output_falls_back_to_complete_directory_resolution(isolated_asset, monkeypatch, output):
    plan, _, _, _ = isolated_asset
    expected = plan.resolve()
    monkeypatch.setattr(matching, "_native_final_path", lambda _: output)
    assert matching._resolve_directory(plan) == expected


@pytest.mark.skipif(not matching._NATIVE_DIRECTORY_RESOLUTION, reason="CPython 3.12 Windows fast path")
@pytest.mark.parametrize("kind", ["unavailable", "permission", "invalid"])
def test_unavailable_or_failing_native_directory_probe_uses_original_resolution(isolated_asset, monkeypatch, kind):
    plan, _, _, _ = isolated_asset
    expected = plan.resolve()
    def fail(_):
        raise PermissionError("probe denied") if kind == "permission" else ValueError("probe invalid")
    monkeypatch.setattr(matching, "_native_final_path", None if kind == "unavailable" else fail)
    assert matching._resolve_directory(plan) == expected


@pytest.mark.skipif(not matching._NATIVE_DIRECTORY_RESOLUTION, reason="CPython 3.12 Windows fast path")
@pytest.mark.parametrize("kind", ["eloop", "winloop", "permission", "other_oserror", "value"])
def test_fast_directory_resolution_preserves_non_strict_stat_and_loop_errors(isolated_asset, monkeypatch, kind):
    plan, _, _, _ = isolated_asset
    if kind == "value":
        error = ValueError("stat value error")
    else:
        error = OSError(errno.ELOOP if kind == "eloop" else errno.EACCES, "stat failure", str(plan))
        if kind == "winloop":
            error.winerror = 1921
    def fail(*args, **kwargs):
        raise error
    monkeypatch.setattr(Path, "stat", fail)
    assert not list(compare_fields(captured(plan.resolve), captured(lambda: matching._resolve_directory(plan))))


@pytest.mark.skipif(not matching._NATIVE_DIRECTORY_RESOLUTION, reason="CPython 3.12 Windows fast path")
@pytest.mark.parametrize("kind", ["missing", "eloop", "winloop"])
def test_fast_directory_resolution_preserves_getcwd_failure(isolated_asset, monkeypatch, kind):
    plan, _, _, _ = isolated_asset
    error = OSError(errno.ELOOP if kind == "eloop" else errno.ENOENT, "working directory unavailable", str(plan))
    if kind == "winloop":
        error.winerror = 1921
    def fail():
        raise error
    monkeypatch.setattr(os, "getcwd", fail)
    assert not list(compare_fields(captured(plan.resolve), captured(lambda: matching._resolve_directory(plan))))


@pytest.mark.skipif(os.name != "nt", reason="Windows junction contract")
@pytest.mark.parametrize("redirect_root", [False, True])
def test_new_directory_observation_rechecks_retargeted_ancestor(isolated_asset, monkeypatch, redirect_root):
    plan, _, pixels, frame = isolated_asset
    first, second = plan / "first", plan / "second"
    for target, image in ((first, pixels), (second, 255-pixels)):
        for child in ("old", "new"):
            directory = target / child
            directory.mkdir(parents=True)
            (directory / "asset.png").write_bytes(cv2.imencode(".png", image)[1].tobytes())
    alias = plan / "linked"
    def point_at(target):
        if alias.exists():
            alias.rmdir()
        subprocess.run(["cmd", "/c", "mklink", "/J", str(alias), str(target)],
                       check=True, capture_output=True)
    try:
        point_at(first)
        if redirect_root:
            monkeypatch.setattr(matching, "_PLAN", alias)
        prefix = "" if redirect_root else "linked/"
        with matching.observe_frame(frame):
            pinned = matching._asset(prefix+"old/asset.png", frame=frame)
            point_at(second)
            assert matching._asset(prefix+"old/asset.png", frame=frame) is pinned
            if redirect_root:
                with pytest.raises(ValueError, match="Template path must remain inside this plan"):
                    matching._asset(prefix+"new/asset.png", frame=frame)
            else:
                np.testing.assert_array_equal(matching._asset(prefix+"new/asset.png", frame=frame).image, 255-pixels)
        with matching.observe_frame(frame):
            np.testing.assert_array_equal(matching._asset(prefix+"new/asset.png", frame=frame).image, 255-pixels)
    finally:
        if alias.exists():
            alias.rmdir()


@pytest.mark.parametrize("kind", ["root", "absolute_root", "absolute_asset", "parent", "back_inside",
                                  "missing_parent", "nul", "path_object", "list", "dict", "none"])
def test_containment_shortcut_keeps_root_unusual_path_and_read_errors(isolated_asset, kind):
    plan, path, _, frame = isolated_asset
    relative = {"root": ".", "absolute_root": str(plan), "absolute_asset": str(path),
                "parent": "..", "back_inside": f"../{plan.name}/asset.png",
                "missing_parent": "missing/asset.png", "nul": "bad\0.png",
                "path_object": Path("asset.png"), "list": [], "dict": {}, "none": None}[kind]
    spec = {"image": relative, "threshold": .9}
    with matching.observe_frame(frame):
        assert not list(compare_fields(captured(lambda: reference_match_asset(frame, spec, plan)),
                                       captured(lambda: matching.match_asset(frame, spec))))


@pytest.mark.skipif(os.name != "nt", reason="Windows junction contract")
@pytest.mark.parametrize("redirect_root", [False, True])
def test_real_junction_retarget_is_visible_next_frame_and_still_checks_boundary(isolated_asset, monkeypatch, redirect_root):
    plan, _, pixels, frame = isolated_asset
    first, second = plan / "first", plan / "second"
    outside = plan.parent / (plan.name + "-outside")
    for directory, image in ((first, pixels), (second, 255-pixels), (outside, pixels)):
        directory.mkdir()
        (directory / "asset.png").write_bytes(cv2.imencode(".png", image)[1].tobytes())
    alias = plan / "linked"
    def point_at(directory):
        if alias.exists():
            alias.rmdir()  # Remove only the junction, preserving its target.
        subprocess.run(["cmd", "/c", "mklink", "/J", str(alias), str(directory)],
                       check=True, capture_output=True)
    try:
        point_at(first)
        if redirect_root:
            monkeypatch.setattr(matching, "_PLAN", alias)
        relative = "asset.png" if redirect_root else "linked/asset.png"
        with matching.observe_frame(frame):
            original = matching._asset(relative, frame=frame)
            point_at(second)
            assert matching._asset(relative, frame=frame) is original
        with matching.observe_frame(frame):
            np.testing.assert_array_equal(matching._asset(relative, frame=frame).image, 255-pixels)
        point_at(outside)
        with matching.observe_frame(frame):
            if redirect_root:
                # The plan's new canonical root is authoritative on each observation.
                np.testing.assert_array_equal(matching._asset(relative, frame=frame).image, pixels)
            else:
                with pytest.raises(ValueError, match="Template path must remain inside this plan"):
                    matching._asset(relative, frame=frame)
    finally:
        if alias.exists():
            alias.rmdir()


@pytest.mark.parametrize("invalid", [None, np.zeros((3, 4), dtype=np.uint8),
                                     np.zeros((720, 1280, 4), dtype=np.uint8),
                                     np.zeros((720, 1280, 3), dtype=np.float32)])
def test_invalid_frames_do_not_load_collection_assets(monkeypatch, invalid):
    def forbidden(*args, **kwargs):
        raise AssertionError("invalid frame must not read a template")
    monkeypatch.setattr(matching, "_asset", forbidden)
    assert ui._hit(invalid, "navigation_popup_title") is None
    assert ui._title(invalid, 1) is None
    assert ui.physical(invalid, "1号生产车间") is False
    assert ui.collection_state(invalid, "1号生产车间") is None
    assert ui.navigation_cards(invalid) == []
    assert ui.navigation_region(invalid) is None
    assert ui.control(invalid, "collect_all") is None


@pytest.mark.parametrize("region", [None, (20, 10, 100, 80), (-2, -3, 100, 80),
                                    (0, 0, 2, 2), (1280, 0, 10, 10), (0, 0, 2), (0, 0, "bad", 8)])
def test_matcher_field_and_exception_parity_for_regions(isolated_asset, region):
    plan, _, _, frame = isolated_asset
    spec = {"image": "asset.png", "threshold": .9}
    assert not list(compare_fields(captured(lambda: reference_match_asset(frame, spec, plan, region)),
                                   captured(lambda: matching.match_asset(frame, spec, region))))


@pytest.mark.parametrize("kind", ["missing", "unreadable", "escape", "missing_key", "bad_threshold"])
def test_file_and_spec_exception_type_and_message_parity(isolated_asset, kind):
    plan, path, _, frame = isolated_asset
    spec = {"image": "asset.png", "threshold": .9}
    if kind == "missing":
        path.unlink()
    elif kind == "unreadable":
        path.write_bytes(b"not an encoded image")
    elif kind == "escape":
        spec["image"] = "../outside.png"
    elif kind == "missing_key":
        spec.pop("image")
    else:
        spec["threshold"] = "bad"
    assert not list(compare_fields(captured(lambda: reference_match_asset(frame, spec, plan)),
                                   captured(lambda: matching.match_asset(frame, spec))))


def test_dispatch_category_clicks_require_the_requested_terminal_category():
    tasks = yaml.safe_load((PLAN / "tasks/auto_battle_dispatch_pc.yaml").read_text(encoding="utf-8"))
    # Daily jobs now retain their list order, so category selection is one
    # internal navigation call for the current job rather than two batch arms.
    steps = tasks["auto_battle_enter_terminal_pc"]["steps"]
    step = steps["select_main_category"]
    assert step["action"] == "plans/aura_base/find_image_and_click"
    assert step["params"] == {
        "template": "{{ 'templates/battle/dispatch/category_ct.png' if inputs.main_category == 'ct' else 'templates/battle/dispatch/category_gp.png' }}",
        "timeout": 8.0, "interval": .2, "region": [20, 180, 200, 160],
        "threshold": .95, "use_grayscale": True, "required": True,
    }
    assert step["depends_on"] == "settle_terminal"
    assert steps["settle_category"]["depends_on"] == "select_main_category"
    play_steps = tasks["auto_battle_enter_category_pc"]["steps"]
    assert play_steps["select_sub_category"]["depends_on"] == "require_category_pair"
    assert play_steps["select_sub_category"]["params"]["required"] is True
