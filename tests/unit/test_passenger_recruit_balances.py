"""Flyer receipts and irreversible recruitment boundaries."""
import asyncio
from types import SimpleNamespace as NS

import pytest
import numpy as np

from plans.resonance_pc.src.actions import passenger_pc_actions as p


@pytest.fixture
def recruit_rig(monkeypatch):
    rig = NS(clicks=[], submitted=[], captures=0, before={"current": 200, "total": 450},
             frames=[[{"text": "70"}], [{"text": "70"}]], missing_confirm=False, after_submit=None,
             capture_options=[])
    monkeypatch.setattr(p, "_check_cancelled", lambda: None)
    monkeypatch.setattr(p.time, "sleep", lambda _: None)
    monkeypatch.setattr(p, "_match_template", lambda *a, **k: {"found": True})
    monkeypatch.setattr(p, "_open_recruitment_hub_from_score",
                        lambda *a, **k: {"flyer_ratio": rig.before})
    monkeypatch.setattr(p, "_select_destination_city", lambda *a, **k: {"city": "C"})
    monkeypatch.setattr(p, "_select_rightmost_dispatch", lambda *a: {"selected": True})
    monkeypatch.setattr(p, "_wait_template", lambda *a, **k: {"found": True})

    def click(app, vision, template, region, *, error_code, before_click=None):
        if template == p._AMOUNT_CONFIRM_TEMPLATE and rig.missing_confirm:
            raise p.PassengerPcError(error_code, "missing confirm")
        if before_click:
            before_click()
            rig.submitted.append("submitted")
        rig.clicks.append(template)
        if template == p._AMOUNT_CONFIRM_TEMPLATE and rig.after_submit:
            raise rig.after_submit

    def capture(*a, **kwargs):
        rig.capture_options.append((a[-1], kwargs))
        frame = rig.frames[min(rig.captures, len(rig.frames)-1)]
        rig.captures += 1
        return frame

    monkeypatch.setattr(p, "_click_template_required", click)
    monkeypatch.setattr(p, "_capture_text_items", capture)
    monkeypatch.setattr(p, "_click_blank_and_confirm_main", lambda *a, **k: {"success": True})
    rig.run = lambda **kw: p._recruit_passengers_by_flyer("C", app=object(), ocr=object(), vision=object(), **kw)
    return rig


def test_no_count_mode_never_parses_passengers_and_reads_net_consumption(recruit_rig, monkeypatch):
    monkeypatch.setattr(p, "_numeric_value_near_label", lambda *a: pytest.fail("passenger count parsed"))
    monkeypatch.setattr(p, "_passenger_ratio", lambda *a: pytest.fail("passenger capacity parsed"))
    calls = []
    result = recruit_rig.run(read_passenger_count=False, submission_observer=lambda: calls.append("before"))
    assert result["success"] is True and result["page_state"] == "city_main"
    assert (result["flyers_before"], result["flyers_remaining"], result["flyers_used"]) == (200, 70, 130)
    assert result["recruited_passengers"] is result["seat_capacity"] is result["remaining_seats"] is None
    assert recruit_rig.clicks[-2:] == [p._AMOUNT_MAX_TEMPLATE, p._AMOUNT_CONFIRM_TEMPLATE]
    assert calls == ["before"]
    assert len(recruit_rig.capture_options) == 2
    assert all(region == [256,134,56,34] for region, _ in recruit_rig.capture_options)


def test_cached_before_balance_skips_header_ocr_and_only_reads_result(recruit_rig, monkeypatch):
    opened = []
    monkeypatch.setattr(p, '_open_recruitment_hub_from_score',
                        lambda *a, **kw: opened.append(kw) or {'success':True})
    monkeypatch.setattr(p, '_read_flyer_balance', lambda *a, **kw: pytest.fail('cached balance reread'))
    monkeypatch.setattr(p, '_passenger_ratio', lambda *a: pytest.fail('passenger count read'))
    result = recruit_rig.run(read_passenger_count=False, known_flyers_before=200)
    assert opened == [{'read_balance':False}]
    assert result['flyers_before_source'] == 'cached_balance'
    assert (result['flyers_before'], result['flyers_remaining'], result['flyers_used']) == (200,70,130)
    assert len(recruit_rig.capture_options) == 2
    assert all(region == p._FLYER_RESULT_BALANCE_REGION for region, _ in recruit_rig.capture_options)


def test_open_hub_with_cached_balance_confirms_template_without_ocr(monkeypatch):
    monkeypatch.setattr(p, '_click_template_required', lambda *a, **kw: None)
    monkeypatch.setattr(p, '_wait_template', lambda *a, **kw: {'found':True})
    monkeypatch.setattr(p, '_capture_text_items', lambda *a, **kw: pytest.fail('hub OCR must be skipped'))
    result = p._open_recruitment_hub_from_score(object(), object(), object(), read_balance=False)
    assert result['page_state'] == 'recruitment_hub' and result['flyer_ratio'] is None


def test_initial_inventory_ocr_is_limited_to_flyer_header(monkeypatch):
    captures = []
    monkeypatch.setattr(p, '_click_template_required', lambda *a, **kw: None)
    monkeypatch.setattr(p, '_wait_template', lambda *a, **kw: {'found':True})
    monkeypatch.setattr(p, '_capture_text_items', lambda *a, **kw: captures.append(a[-1]) or [{'text':'60/100'}])
    monkeypatch.setattr(p, '_passenger_ratio', lambda *a: pytest.fail('existing passengers must not be read'))
    hub = p._open_recruitment_hub_from_score(object(), object(), object())
    assert hub['flyer_ratio'] == {'current':60, 'total':100}
    assert p._read_flyer_balance(object(),object(),initial_ratio=hub['flyer_ratio']) == hub['flyer_ratio']
    assert captures == [p._FLYER_HUB_BALANCE_REGION]


def test_default_mode_still_requires_and_returns_legacy_passenger_count(recruit_rig, monkeypatch):
    monkeypatch.setattr(p, "_numeric_value_near_label", lambda *a: 182)
    result = recruit_rig.run()
    assert result["recruited_passengers"] == 182
    assert recruit_rig.capture_options[-1][0] == p._FULL_SCREEN


def test_missing_after_balance_retries_and_never_becomes_zero(recruit_rig):
    recruit_rig.frames = [[{"text": "unreadable"}]]
    with pytest.raises(p.PassengerPcError) as caught:
        recruit_rig.run(read_passenger_count=False)
    assert caught.value.code == "flyer_balance_unreadable"
    assert recruit_rig.captures == 3
    assert caught.value.detail["recruitment"]["flyers_remaining"] is None
    assert caught.value.detail["submission_state"] == "submitted_unknown"


def test_missing_before_balance_never_submits(recruit_rig):
    recruit_rig.before = None
    recruit_rig.frames = [[{"text": "unreadable"}]]
    with pytest.raises(p.PassengerPcError) as caught:
        recruit_rig.run(read_passenger_count=False)
    assert recruit_rig.captures == 3 and recruit_rig.clicks == []
    assert caught.value.detail["submission_state"] == "not_submitted"


def test_valid_zero_after_balance_is_real_consumption(recruit_rig):
    recruit_rig.frames = [[{"text": "0"}], [{"text": "0"}]]
    assert recruit_rig.run(read_passenger_count=False)["flyers_used"] == 200


def test_balance_retry_can_recover(recruit_rig):
    recruit_rig.frames = [[{"text": "unknown"}], [{"text": "190"}], [{"text": "190"}]]
    assert recruit_rig.run(read_passenger_count=False)["flyers_used"] == 10
    assert recruit_rig.captures == 3


def test_live_success_overlay_standalone_balance_ignores_seats(recruit_rig):
    recruit_rig.before = {"current": 455, "total": 475}
    recruit_rig.frames = [[{"text": "385"}], [{"text": "385"}]]
    result = recruit_rig.run(read_passenger_count=False)
    assert (result["flyers_remaining"], result["flyers_used"]) == (385, 70)


def test_result_balance_uses_only_two_fixed_digit_crops(recruit_rig):
    recruit_rig.before = {"current":475, "total":475}
    recruit_rig.frames = [[{"text":"415"}], [{"text":"415"}]]
    result = recruit_rig.run(read_passenger_count=False)
    assert result["flyers_used"] == 60 and result["flyers_remaining"] == 415
    assert result["flyer_balance_evidence"]["method"] == "isolated_neutral_digits_two_reads"
    assert result["flyer_balance_evidence"]["region"] == [256,134,56,34]
    assert recruit_rig.captures == 2
    assert all(kwargs.get('preprocess') is p._prepare_flyer_digit_image
               for _, kwargs in recruit_rig.capture_options[-2:])


@pytest.mark.parametrize("reads", [["415","416"], ["S415"], ["999"], [""], ["0","1"]])
def test_isolated_balance_requires_two_matching_valid_numeric_reads(monkeypatch, reads):
    frames = iter(reads)
    monkeypatch.setattr(p, '_capture_text_items', lambda *a, **k: [{"text":next(frames)}])
    monkeypatch.setattr(p, '_match_template', lambda *a, **k: {'found':True})
    assert p._read_recruitment_result_flyer_balance(object(), object(), object(), 475) is None


@pytest.mark.parametrize('disappears_after_first', [False, True])
def test_result_template_is_required_before_each_digit_read(monkeypatch, disappears_after_first):
    matches = iter([{'found':True}, {'found':False}] if disappears_after_first else [{'found':False}])
    monkeypatch.setattr(p, '_match_template', lambda *a, **k: next(matches))
    captures = []
    monkeypatch.setattr(p, '_capture_text_items', lambda *a, **k: captures.append(a[-1]) or [{'text':'415'}])
    assert p._read_recruitment_result_flyer_balance(object(), object(), object(), 475) is None
    assert len(captures) == int(disappears_after_first)


@pytest.mark.parametrize('value', [0, 1, 9, 10, 99, 100, 405, 999])
def test_primary_crop_reads_one_two_and_three_digits(monkeypatch, value):
    calls = []
    def capture(*args, **kwargs):
        calls.append((args[-1], kwargs))
        return [{'text':str(value)}]
    monkeypatch.setattr(p, '_capture_text_items', capture)
    monkeypatch.setattr(p, '_match_template', lambda *a, **k: {'found':True})
    result = p._read_recruitment_result_flyer_balance(object(), object(), object(), 999)
    assert result['current'] == value
    assert len(calls) == 2 and all(region == [256,134,56,34] for region, _ in calls)


def test_digit_preprocessing_removes_icon_noise_without_moving_neutral_digit():
    image = np.full((34,56,3),235,dtype=np.uint8)
    image[3:25,:10] = [20,50,95]
    image[27:30,:12] = [245,190,30]
    image[8:27,25:29] = 48
    image[-1,:] = 100
    image[4,15] = 48
    prepared = p._prepare_flyer_digit_image(image)
    assert np.all(prepared[8:27,25:29] == 48)
    outside = np.ones((34,56),dtype=bool)
    outside[8:27,25:29] = False
    assert np.all(prepared[outside] == 255)
    assert np.all(image[-1,:] == 100), 'source frame must be preserved'


@pytest.mark.parametrize('image', [None, np.zeros((34,56,4),np.uint8), np.zeros((34,56,3),np.float32)])
def test_invalid_digit_crop_is_unknown_not_a_numeric_zero(image):
    with pytest.raises(p.PassengerPcError, match='flyer_digit_capture_invalid'):
        p._prepare_flyer_digit_image(image)


@pytest.mark.parametrize("failure", [RuntimeError("input failed"), asyncio.CancelledError("cancel")])
def test_submission_metadata_survives_error_or_cancellation(recruit_rig, failure):
    recruit_rig.after_submit = failure
    with pytest.raises(type(failure)) as caught:
        recruit_rig.run(read_passenger_count=False)
    assert caught.value.detail["submission_state"] == "submitted_unknown"
    assert caught.value.detail["recruitment"]["flyers_before"] == 200


def test_unmatched_confirm_does_not_call_submission_observer(recruit_rig):
    recruit_rig.missing_confirm = True
    calls = []
    with pytest.raises(p.PassengerPcError) as caught:
        recruit_rig.run(read_passenger_count=False, submission_observer=lambda: calls.append(True))
    assert calls == [] and caught.value.detail["submission_state"] == "not_submitted"


def test_cleanup_failure_preserves_confirmed_flyer_receipt(recruit_rig, monkeypatch):
    def fail(*a, **k):
        raise p.PassengerPcError("main_screen_not_restored", "main unknown")
    monkeypatch.setattr(p, "_click_blank_and_confirm_main", fail)
    with pytest.raises(p.PassengerPcError) as caught:
        recruit_rig.run(read_passenger_count=False)
    receipt = caught.value.detail["recruitment"]
    assert receipt["flyers_used"] == 130 and receipt["submission_state"] == "result_confirmed"
    assert receipt["page_state"] == "unknown"


def test_actual_click_callback_is_after_observation_before_input(monkeypatch):
    sequence = []
    monkeypatch.setattr(p, "_check_cancelled", lambda: None)
    monkeypatch.setattr(p, "_wait_template", lambda *a, **k: {"found": True, "center": [10, 20]})
    app = NS(click=lambda **k: sequence.append("click"))
    p._click_template_required(app, object(), "confirm.png", [0, 0, 30, 30], error_code="missing",
                               before_click=lambda: sequence.append("submit"))
    assert sequence == ["submit", "click"]


def test_inventory_hub_returns_via_observed_back_then_confirms_main(monkeypatch):
    clicks = []
    pages = iter([False, False, True])
    observed = iter([{"found": True}, {"found": False}, {"found": True}])
    monkeypatch.setattr(p, "_wait_main_stable", lambda *a, **k: {"confirmed": next(pages)})
    monkeypatch.setattr(p, "_match_template", lambda *a, **k: next(observed))
    monkeypatch.setattr(p, "_wait_template", lambda *a, **k: {"found": True, "center": [82, 37]})
    def close_score(app, vision, *, error_code):
        assert error_code == "main_screen_not_restored"
        app.click(x=640, y=690)
        return {"success": True, "page_state": "city_main"}
    monkeypatch.setattr(p, "_click_blank_and_confirm_main", close_score)
    result = p._return_recruitment_pages_to_main(NS(click=lambda **k: clicks.append(k)), object())
    assert result["success"] is True and len(clicks) == 2
    assert [row["source"] for row in result["attempts"]] == ["recruitment_hub", "passenger_score"]


def test_inventory_return_unknown_page_never_clicks(monkeypatch):
    monkeypatch.setattr(p, "_wait_main_stable", lambda *a, **k: {"confirmed": False})
    monkeypatch.setattr(p, "_match_template", lambda *a, **k: {"found": False})
    with pytest.raises(p.PassengerPcError, match="passenger_return_page_unknown"):
        p._return_recruitment_pages_to_main(NS(click=lambda **k: pytest.fail("unknown page clicked")), object())


@pytest.mark.parametrize('name', ['七号自由港', '7号自由港', 'freeport'])
def test_destination_reuses_canonical_city_aliases(name):
    assert p.destination_vision.resolve_destination(name)['city_key'] == 'freeport'


def test_unsubmitted_destination_returns_via_back_not_blank(monkeypatch):
    pages = iter([False, False, False])
    monkeypatch.setattr(p, '_wait_main_stable', lambda *a, **k: {'confirmed':next(pages)})
    observed = iter([False, False, True, True, False, True])
    monkeypatch.setattr(p, '_match_template', lambda *a, **k: {'found':next(observed)})
    monkeypatch.setattr(p, '_wait_template', lambda *a, **k: {'found':True,'center':[82,37]})
    clicks=[]
    monkeypatch.setattr(p, '_click_blank_and_confirm_main', lambda *a, **k: {'success':True})
    result=p._return_recruitment_pages_to_main(NS(click=lambda **k: clicks.append(k)),object())
    assert result['success'] is True
    assert result['page_state'] == 'city_main'
    assert [row['source'] for row in result['attempts']] == ['destination','recruitment_hub','passenger_score']
    assert len(clicks)==2


@pytest.mark.parametrize("text,clicked", [("客运等级提升", True), ("未知弹窗", False)])
def test_level_up_dismiss_requires_exact_observed_label(monkeypatch, text, clicked):
    clicks = []
    monkeypatch.setattr(p, '_capture_text_items', lambda *a: [{"text":text}])
    result = p._dismiss_passenger_level_up(NS(click=lambda **k: clicks.append(k)), object())
    assert bool(result.get('clicked')) is clicked
    assert bool(clicks) is clicked


def test_settlement_grouped_dot_values_stay_on_their_own_rows():
    rows = [{"text":"车票收益","center":[497,511]}, {"text":"235,392","center":[730,512]},
            {"text":"车票外收益","center":[506,544]}, {"text":"72.802","center":[733,544]},
            {"text":"总收益","center":[895,523]}, {"text":"308.194","center":[1096,532]}]
    for row in rows:
        row['norm_text'] = p._normalize_text(row['text'])
    assert p._numeric_value_near_label(rows, ('车票收益',)) == 235392
    assert p._numeric_value_near_label(rows, ('车票外收益',)) == 72802
    assert p._numeric_value_near_label(rows, ('总收益',)) == 308194
    assert p._numeric_value_near_label(rows[:3], ('车票外收益',)) is None


def test_decimal_or_malformed_grouping_is_not_a_money_amount():
    for text in ('13.25','1,23'):
        assert p._numeric_value_near_label([{'text':'总收益','norm_text':'总收益','center':[500,500]},
                                           {'text':text,'center':[700,500]}], ('总收益',)) is None


def test_settlement_waits_for_main_after_upgrade_animation(monkeypatch):
    monkeypatch.setattr(p, '_wait_template', lambda *a, **k: {'found':True})
    monkeypatch.setattr(p, '_capture_text_items', lambda *a: [])
    monkeypatch.setattr(p, 'observe_template', lambda **k: {'valid':True,'found':True})
    monkeypatch.setattr(p, '_wait_template_absent', lambda *a, **k: {'absent':True,'last_match':{'valid':True}})
    mains = iter([False, True])
    monkeypatch.setattr(p, '_wait_main_stable', lambda *a, **k: {'confirmed':next(mains)})
    monkeypatch.setattr(p, '_dismiss_passenger_level_up', lambda *a: {'found':True,'clicked':True})
    result = p.resonance_pc_enter_city_and_settle_passengers(app=NS(click=lambda **k: None),ocr=object(),vision=object())
    assert result['success'] is True
    assert len(result['dismiss_attempts']) == 1
    assert result['dismiss_attempts'][0]['level_up']['clicked'] is True


def test_settlement_closed_and_main_confirmed_does_not_read_income_or_city_label(monkeypatch):
    monkeypatch.setattr(p, '_wait_template', lambda *a, **k: {'found':True})
    monkeypatch.setattr(p, '_capture_text_items', lambda *a: pytest.fail('settlement must not OCR income'))
    monkeypatch.setattr(p, '_numeric_value_near_label', lambda *a: pytest.fail('settlement must not parse income'))
    monkeypatch.setattr(p, 'observe_template', lambda **k: {'valid':True,'found':True})
    monkeypatch.setattr(p, '_wait_template_absent', lambda *a, **k: {'absent':True,'last_match':{'valid':True}})
    monkeypatch.setattr(p, '_wait_main_stable', lambda *a, **k: {'confirmed':True,'confirmations':2})
    monkeypatch.setattr(p, '_dismiss_passenger_level_up', lambda *a: pytest.fail('main already confirmed'))
    result = p.resonance_pc_enter_city_and_settle_passengers(app=NS(click=lambda **k: None),ocr=object(),vision=object())
    assert result['success'] is True
    assert result['revenue_reading_enabled'] is False
    assert result['ticket_revenue'] is result['extra_revenue'] is result['total_revenue'] is None
    assert result['ocr_items'] == []
