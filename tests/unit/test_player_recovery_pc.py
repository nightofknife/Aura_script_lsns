from __future__ import annotations

import copy
import itertools
from pathlib import Path
from types import SimpleNamespace as NS

import numpy as np
import pytest

from packages.aura_core.context.persistence.persistent_data_service import PersistentDataService
from plans.aura_base.src.services.vision_service import VisionService
from plans.resonance_pc.src.actions import player_data_pc_actions as data
from plans.resonance_pc.src.actions import player_recovery_pc_actions as recovery


SECTIONS = data._PROFILE_SECTION_ORDER
COMBINATIONS = [list(combo) for size in range(1, 6) for combo in itertools.combinations(SECTIONS, size)]


@pytest.fixture
def fast_clock(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(recovery.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(recovery.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))
    return now


@pytest.fixture
def layout():
    return recovery.load_recovery_layout(VisionService())


@pytest.mark.parametrize("sections", COMBINATIONS)
def test_all_profile_combinations_read_only_selected_fields(tmp_path, monkeypatch, sections):
    assert data._normalize_profile_sections(sections[::-1] + sections, required=True) == tuple(sections)
    service = PersistentDataService(tmp_path / "install")
    monkeypatch.setattr(data, "_wait_for_any_marker", lambda *args, **kwargs: [])
    monkeypatch.setattr(data, "_close_profile_panel_to_main", lambda *args: None)
    monkeypatch.setattr(data, "load_recovery_layout", lambda vision: {})
    captured = []

    def read_region(app, ocr, region):
        section = next(key for key, value in data._PROFILE_FIELD_REGIONS.items() if value == region)
        captured.append(section)
        return "12/100"

    monkeypatch.setattr(data, "_read_region_text", read_region)

    class Reader:
        def __init__(self, *args, **kwargs):
            pass

        def read(self, selected, *, on_updated):
            result = {}
            for key in ("sparkling_water", "bento"):
                if key in selected:
                    on_updated(key)
                    result[key] = {"remaining_free_uses": 0} if key == "sparkling_water" else {"available_count": 0}
            return result

    monkeypatch.setattr(data, "RecoveryReader", Reader)
    result = data.resonance_pc_player_data_refresh(
        stages=["profile"], profile_sections=sections,
        app=NS(click=lambda **kwargs: None), ocr=object(), vision=object(), persistent_data=service,
    )
    assert captured == [key for key in data._DEFAULT_PROFILE_SECTIONS if key in sections]
    assert set(result.get("status", {})) | set(result.get("recovery", {})) == set(sections)
    assert set(result["metadata"]["profile_section_updated_at"]) == set(sections)
    assert result["metadata"]["executed_profile_sections"] == sections
    assert result["metadata"]["skipped_profile_sections"] == [key for key in SECTIONS if key not in sections]
    assert result["metadata"]["persisted"] is True
    saved = service.read(data.USER_INFO_FILE)
    assert set(saved.get("status", {})) | set(saved.get("recovery", {})) == set(sections)


@pytest.mark.parametrize("value", [[], ["uid"], [True], [None], "cargo", ("cargo",), {}])
def test_invalid_profile_selection(value):
    with pytest.raises(ValueError):
        data._normalize_profile_sections(value, required=True)


def test_default_and_disabled_profile_selection():
    assert data._normalize_profile_sections(None, required=True) == data._DEFAULT_PROFILE_SECTIONS
    assert data._normalize_profile_sections([], required=False) == ()


def test_partial_merge_preserves_data_and_legacy_times():
    old = {"status": {"cargo": {"current": 1, "max": 2}, "fatigue": {"current": 3, "max": 4}},
           "recovery": {"bento": {"available_count": 3}},
           "metadata": {"section_updated_at": {"profile": "old"}}}
    original = copy.deepcopy(old)
    merged = data._merge_latest(old, {"recovery": {"sparkling_water": {"remaining_free_uses": 0}}},
                               section_updated_at={"profile": "new"}, updated_at="new",
                               profile_section_updated_at={"sparkling_water": "new"})
    assert old == original
    assert merged["status"] == old["status"]
    assert merged["recovery"]["bento"] == old["recovery"]["bento"]
    assert merged["metadata"]["profile_legacy_updated_at"] == "old"
    second = data._merge_latest(merged, {"status": {"cargo": {"current": 2, "max": 2}}},
                               section_updated_at={"profile": "newer"}, updated_at="newer",
                               profile_section_updated_at={"cargo": "newer"})
    assert second["metadata"]["profile_legacy_updated_at"] == "old"
    assert second["metadata"]["profile_section_updated_at"] == {"sparkling_water": "new", "cargo": "newer"}
    assert second["status"]["fatigue"] == old["status"]["fatigue"]


@pytest.mark.parametrize("old_exists", [False, True])
def test_failed_recovery_never_writes(tmp_path, monkeypatch, old_exists):
    service = PersistentDataService(tmp_path / "install")
    old = {"status": {"cargo": {"current": 1, "max": 2}}, "metadata": {"updated_at": "old"}}
    if old_exists:
        service.set(data.USER_INFO_FILE, [], old)
    monkeypatch.setattr(data, "_wait_for_any_marker", lambda *args, **kwargs: [])
    monkeypatch.setattr(data, "_read_region_text", lambda *args: "5/6")
    monkeypatch.setattr(data, "load_recovery_layout", lambda vision: {})
    monkeypatch.setattr(data, "_best_effort_return_to_main", lambda *args: None)
    error = RuntimeError("original recovery error")

    class Reader:
        def __init__(self, *args, **kwargs):
            pass
        def read(self, *args, **kwargs):
            raise error

    monkeypatch.setattr(data, "RecoveryReader", Reader)
    with pytest.raises(RuntimeError) as caught:
        data.resonance_pc_player_data_refresh(stages=["profile"], profile_sections=["cargo", "bento"],
                                             app=NS(click=lambda **kwargs: None), ocr=object(), vision=object(),
                                             persistent_data=service)
    assert caught.value is error
    assert service.exists(data.USER_INFO_FILE) == old_exists
    if old_exists:
        assert service.read(data.USER_INFO_FILE) == old


@pytest.mark.parametrize("states", list(itertools.product([True, False], repeat=3)))
def test_bento_each_slot_is_explicit_and_no_ocr(layout, states, monkeypatch):
    calls = []
    def batch(**kwargs):
        assert kwargs["source_image"].shape[:2] == (105,165)
        present = states[len(calls)]
        calls.append(kwargs)
        return [NS(found=present,confidence=1.0), NS(found=not present,confidence=1.0)]
    reader = recovery.RecoveryReader(None, None, NS(find_templates_batch=batch), layout)
    monkeypatch.setattr(reader, "is_page", lambda page: page == "bento_cabinet")
    monkeypatch.setattr(reader, "capture", lambda roi: np.zeros((roi[3],roi[2],3),dtype=np.uint8))
    result = reader.read_bento()
    assert result["available_count"] == sum(states)
    assert [slot["available"] for slot in result["slots"]] == list(states)
    assert len(calls) == 3


@pytest.mark.parametrize("matched", [True, False])
def test_bento_unknown_or_conflicting_match_fails(layout, matched, monkeypatch, fast_clock):
    vision = NS(find_templates_batch=lambda **kwargs: [NS(found=matched,confidence=1.0)]*2)
    reader = recovery.RecoveryReader(None, None, vision, layout)
    monkeypatch.setattr(reader, "is_page", lambda page: True)
    monkeypatch.setattr(reader, "capture", lambda roi: np.zeros((roi[3],roi[2],3),dtype=np.uint8))
    with pytest.raises(recovery.StopTaskException, match="ambiguous"):
        reader.read_bento()


@pytest.mark.parametrize("remaining,limit", [(i,6) for i in range(7)] + [(2,3)])
def test_water_matches_two_small_raw_regions_without_ocr(layout, monkeypatch, remaining, limit):
    import cv2
    values = iter((remaining,limit))
    calls = []
    def batch(**kwargs):
        im = kwargs["source_image"]
        assert im.shape == (52,36 if not calls else 48,3)
        assert np.all(im == (remaining if not calls else limit))
        assert kwargs["use_grayscale"] is False
        assert kwargs["match_method"] == cv2.TM_SQDIFF_NORMED
        assert kwargs["preprocess"] == "none"
        assert len(kwargs["template_images"]) == len(kwargs["mask_images"]) == 7
        assert all(Path(p).name == f"{i}_mask.png" for i,p in enumerate(kwargs["mask_images"]))
        calls.append(kwargs)
        chosen = next(values)
        return [NS(found=i==chosen,confidence=1.0 if i==chosen else .5) for i in range(7)]
    ocr = NS(recognize_all=lambda **kwargs: pytest.fail("OCR must not run"))
    reader = recovery.RecoveryReader(None,ocr,NS(find_templates_batch=batch),layout)
    monkeypatch.setattr(reader,"is_page",lambda page: True)
    image = np.full((52,88,3),255,dtype=np.uint8)
    image[:,:36] = remaining
    image[:,40:] = limit
    monkeypatch.setattr(reader,"capture",lambda roi:image)
    assert reader.read_sparkling_water() == {"remaining_free_uses":remaining,"daily_free_limit":limit}
    assert len(calls) == 2


@pytest.mark.parametrize("scores", [[.94]+[.2]*6, [.98,.97]+[.2]*5, [float('nan')]+[.2]*6,
                                  [float('inf')]+[.2]*6, [1.0,1.0]+[.2]*5])
def test_water_unreliable_digits_fail_without_ocr_fallback(layout, monkeypatch, fast_clock, scores):
    vision=NS(find_templates_batch=lambda **kwargs:[NS(found=True,confidence=s) for s in scores])
    reader=recovery.RecoveryReader(None,None,vision,layout)
    monkeypatch.setattr(reader,"is_page",lambda page:True)
    monkeypatch.setattr(reader,"capture",lambda roi:np.zeros((52,88,3),dtype=np.uint8))
    with pytest.raises(recovery.StopTaskException,match="unable to match free uses"):
        reader.read_sparkling_water()


@pytest.mark.parametrize("values", [(6,0),(6,3)])
def test_water_rejects_invalid_ratio(layout, monkeypatch, fast_clock, values):
    reader=recovery.RecoveryReader(None,None,None,layout)
    monkeypatch.setattr(reader,"is_page",lambda page:True)
    monkeypatch.setattr(reader,"capture",lambda roi:np.zeros((52,88,3),dtype=np.uint8))
    sequence=itertools.cycle(values)
    monkeypatch.setattr(reader,"_match_free_uses_digit",lambda image:(next(sequence),'test'))
    with pytest.raises(recovery.StopTaskException,match="unable to match free uses"):
        reader.read_sparkling_water()


@pytest.mark.parametrize("digit", range(7))
def test_water_digit_uses_real_framework_mask_matching(layout, digit):
    import asyncio
    from PIL import Image
    async def run():
        vision=VisionService()
        vision._loop=asyncio.get_running_loop()
        reader=recovery.RecoveryReader(None,None,vision,layout)
        image=np.zeros((52,36,3),dtype=np.uint8)
        image[2:50,2:34]=np.array(Image.open(layout["sparkling_water_digits"]["resolved_templates"][digit]))
        matched,diagnostic=await asyncio.to_thread(reader._match_free_uses_digit,image)
        assert matched == digit,diagnostic
    asyncio.run(run())


@pytest.mark.parametrize("problem", ["missing", "size", "empty_mask", "color_mask", "path_escape"])
def test_digit_assets_fail_preflight(problem, monkeypatch, tmp_path):
    vision=VisionService()
    original_load=vision.load_image_file
    original_resolve=vision.resolve_template
    def resolve(plan,ref,root):
        if ref.endswith("free_uses_digits/0.png") or ref.endswith("free_uses_digits\\0.png"):
            if problem=="missing":
                raise FileNotFoundError(ref)
            if problem=="path_escape":
                return tmp_path / "outside.png"
        return original_resolve(plan,ref,root)
    def load(path,flags):
        if path.name=="0.png" and problem=="size":
            return np.zeros((47,32,3),dtype=np.uint8)
        if path.name=="0_mask.png":
            if problem=="empty_mask":
                return np.zeros((48,32),dtype=np.uint8)
            if problem=="color_mask":
                return np.full((48,32,3),255,dtype=np.uint8)
        return original_load(path,flags)
    monkeypatch.setattr(vision,"resolve_template",resolve)
    monkeypatch.setattr(vision,"load_image_file",load)
    with pytest.raises((ValueError,FileNotFoundError)):
        recovery.load_recovery_layout(vision)


def test_move_checks_destination_before_reclick(layout, monkeypatch, fast_clock):
    clicks = []
    reader = recovery.RecoveryReader(NS(click=lambda **kwargs: clicks.append(kwargs)), None, None, layout)
    monkeypatch.setattr(reader, "is_page", lambda page: page == ("fatigue_recovery" if clicks else "profile"))
    monkeypatch.setattr(reader, "match", lambda key: NS(found=True,center_point=(26,24)))
    reader.move("profile", "fatigue_recovery", "fatigue_plus")
    assert clicks == [{"x":446,"y":269}]
    reader.move("profile", "fatigue_recovery", "fatigue_plus")
    assert len(clicks) == 1


@pytest.mark.parametrize("fail_read", [False, True])
def test_popup_closes_with_same_info_icon_on_success_and_cleanup(layout, monkeypatch, fast_clock, fail_read):
    state = ["profile"]
    clicks = []
    centers = {"fatigue_plus": (26,24), "rest_info": (30,29), "back": (76,28)}
    def click(*,x,y):
        point = (x,y)
        clicks.append(point)
        if state[0] == "profile" and point == (446,269):
            state[0] = "fatigue_recovery"
        elif state[0] == "fatigue_recovery" and point == (1168,289):
            state[0] = "sparkling_water_popup"
        elif state[0] == "sparkling_water_popup" and point == (1168,289):
            state[0] = "fatigue_recovery"
        elif state[0] == "fatigue_recovery" and point == (86,35):
            state[0] = "profile"
        else:
            pytest.fail(f"unexpected click {state[0]} {point}")
    reader = recovery.RecoveryReader(NS(click=click),None,None,layout)
    monkeypatch.setattr(reader,"is_page",lambda page:page == state[0])
    monkeypatch.setattr(reader,"match",lambda key:NS(found=True,center_point=centers[key]))
    error = RuntimeError("digit failure")
    def read_water():
        if fail_read:
            raise error
        return {"remaining_free_uses":0,"daily_free_limit":6}
    monkeypatch.setattr(reader,"read_sparkling_water",read_water)
    updated=[]
    if fail_read:
        with pytest.raises(RuntimeError) as caught:
            reader.read(["sparkling_water"],on_updated=updated.append)
        assert caught.value is error
        assert updated == []
    else:
        assert reader.read(["sparkling_water"],on_updated=updated.append)["sparkling_water"]["remaining_free_uses"] == 0
        assert updated == ["sparkling_water"]
    assert clicks == [(446,269),(1168,289),(1168,289),(86,35)]
    assert reader.page == state[0] == "profile"
    assert "popup_dismiss_point" not in layout


def test_popup_already_closed_is_not_reopened(layout, monkeypatch, fast_clock):
    reader = recovery.RecoveryReader(NS(click=lambda **kwargs:pytest.fail("must not reopen tooltip")),None,None,layout)
    monkeypatch.setattr(reader,"is_page",lambda page:page == "fatigue_recovery")
    reader.move("sparkling_water_popup","fatigue_recovery","rest_info")
    assert reader.page == "fatigue_recovery"


def test_unknown_page_times_out_without_blind_clicks(layout, monkeypatch, fast_clock):
    reader = recovery.RecoveryReader(NS(click=lambda **kwargs: pytest.fail("unexpected click")), None, None, layout)
    monkeypatch.setattr(reader, "is_page", lambda page: False)
    with pytest.raises(recovery.StopTaskException, match="timed out"):
        reader.move("profile", "fatigue_recovery", "fatigue_plus")
    assert 3 <= fast_clock[0] < 3.3


def test_cleanup_failure_preserves_original_exception(layout, monkeypatch):
    reader = recovery.RecoveryReader(None,None,None,layout)
    error = RuntimeError("first failure")
    def fail(*args):
        raise error
    monkeypatch.setattr(reader, "move", fail)
    monkeypatch.setattr(reader, "restore_profile", lambda: (_ for _ in ()).throw(ValueError("cleanup")))
    with pytest.raises(RuntimeError) as caught:
        reader.read(["bento"],on_updated=lambda key: None)
    assert caught.value is error
    assert reader.page == "unknown"


def test_cancellation_does_not_click(layout, monkeypatch):
    monkeypatch.setattr(recovery,"is_current_task_cancel_requested",lambda:True)
    reader = recovery.RecoveryReader(NS(click=lambda **kwargs: pytest.fail("unexpected click")),None,None,layout)
    with pytest.raises(recovery.StopTaskException,match="cancelled"):
        reader.read(["bento"],on_updated=lambda key: None)


def test_invalid_layout_fails_before_game_input(tmp_path, layout):
    import json
    layout["templates"]["fatigue_plus"]["roi"] = [1270,710,55,50]
    config_path = tmp_path / "invalid.json"
    config_path.write_text(json.dumps(layout),encoding="utf-8")
    with pytest.raises(ValueError, match="outside reference"):
        recovery.load_recovery_layout(VisionService(),config_path=config_path)


@pytest.mark.parametrize("text", ["", "126", "1/0", "货舱", "1/2 3/4"])
def test_unreadable_original_status_is_not_saved_as_zero(text, monkeypatch, fast_clock):
    monkeypatch.setattr(data, "_read_region_text", lambda *args: text)
    with pytest.raises(data.StopTaskException, match="invalid cargo ratio"):
        data._read_profile_stage(None, None, ["cargo"])


def test_original_status_accepts_overcap_and_plus(monkeypatch):
    monkeypatch.setattr(data, "_read_region_text", lambda *args: " 250／200 + ")
    assert data._read_profile_stage(None, None, ["clarity"]) == {"clarity": {"current":250,"max":200}}


def test_task_and_manifest_publish_profile_selection():
    import yaml
    root = Path(__file__).resolve().parents[2] / "plans/resonance_pc"
    task = yaml.safe_load((root / "tasks/player_data_pc.yaml").read_text(encoding="utf-8"))["player_data_refresh"]
    param = next(p for p in task["meta"]["inputs"] if p["name"] == "profile_sections")
    assert param["default"] == list(data._DEFAULT_PROFILE_SECTIONS)
    assert param["item"]["enum"] == list(SECTIONS)
    assert task["steps"]["refresh"]["params"]["profile_sections"] == "{{ inputs.profile_sections }}"
    manifest = yaml.safe_load((root / "manifest.yaml").read_text(encoding="utf-8"))
    action = next(a for a in manifest["exports"]["actions"] if a["name"] == "resonance_pc.player_data_refresh")
    assert any(p["name"] == "profile_sections" and not p["required"] for p in action["parameters"])
