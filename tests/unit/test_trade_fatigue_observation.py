"""Trade fatigue uses split digit OCR on a fresh frame and stable pair evidence."""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import cv2
import pytest

from packages.aura_core.scheduler.operation_scope import OperationTimeout
from packages.aura_core.utils.exceptions import StopTaskException
from plans.resonance_pc.src.actions import _trade_fatigue_observation as reader


FIXTURES = Path(__file__).parents[1] / 'fixtures' / 'fatigue_observation'


class App:
    def __init__(self, readings=(), fixture='buy'):
        self.calls = []
        self.success = True
        self.readings = iter(readings)
        self.current = None
        self.side = 0
        bgr = cv2.imread(str(FIXTURES / f'{fixture}.png'))
        assert bgr is not None
        self.pixels = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)

    def capture(self, *, rect):
        self.calls.append(rect)
        self.side = 0
        if self.success:
            self.current = next(self.readings)
        return SimpleNamespace(success=self.success, relative_rect=rect,
                               image=self.pixels.copy())


class Ocr:
    def __init__(self, app):
        self.app = app
        self.calls = []

    def recognize_all(self, *, source_image):
        self.calls.append(source_image.copy())
        value = self.app.current[self.app.side]
        self.app.side += 1
        if isinstance(value, BaseException):
            raise value
        if isinstance(value, str):
            return result(value)
        return value


def result(text, confidence=0.99, **kwargs):
    return SimpleNamespace(success=True, results=[SimpleNamespace(text=text, confidence=confidence)], **kwargs)


def read(readings, fixture='buy', **kwargs):
    app = App(readings, fixture)
    ocr = Ocr(app)
    return reader.read_trade_fatigue(app, ocr, poll_seconds=0, **kwargs), app, ocr


@pytest.mark.parametrize('fixture', ['buy', 'sell'])
@pytest.mark.parametrize('pair', [('77', '872'), ('0', '872'), ('872', '872'), ('900', '872'),
                                 ('1234', '1872'), ('99999', '99999')])
def test_two_matching_fresh_roi_reads_split_both_numbers(fixture, pair):
    observed, app, ocr = read([pair, pair], fixture=fixture)
    assert observed == {'success': True, 'current_fatigue': int(pair[0]), 'max_fatigue': int(pair[1]),
                        'reason': 'stable', 'source': 'trade_page', 'attempts': 2}
    assert app.calls == [reader.TRADE_FATIGUE_ROI] * 2
    assert len(ocr.calls) == 4
    # The real HUD crop goes through the shared geometric splitter. Neither OCR
    # input contains the whole ratio, and each side is from the same capture.
    assert all(image.shape[1] < 3 * reader.TRADE_FATIGUE_ROI[2] for image in ocr.calls)
    assert all(image.size for image in ocr.calls)
    assert (ocr.calls[0] == ocr.calls[2]).all()
    assert (ocr.calls[1] == ocr.calls[3]).all()


def test_changed_then_stable_value_does_not_reuse_preceding_read():
    observed, _, _ = read([('77', '872'), ('85', '872'), ('85', '872')])
    assert observed['current_fatigue'] == 85 and observed['attempts'] == 3


def test_maximum_must_also_be_stable():
    observed, _, _ = read([('77', '872'), ('77', '900'), ('77', '872'), ('77', '900')])
    assert observed['success'] is False and observed['reason'] == 'unstable_fatigue'
    assert observed['current_fatigue'] is None


@pytest.mark.parametrize('pair', [('-1', '872'), ('77/872', '872'), ('77', '872+'),
                                 ('a77', '872'), ('77', '8.72'), ('', '872'),
                                 ('77', ''), ('77', '8 72')])
def test_both_sides_require_complete_pure_digits(pair):
    observed, app, _ = read([pair] * 4)
    assert observed['success'] is False and observed['reason'] == 'invalid_fatigue_digits'
    assert observed['current_fatigue'] is observed['max_fatigue'] is None
    assert len(app.calls) == 4


@pytest.mark.parametrize('pair', [('77', '0'), ('0', '0')])
def test_zero_maximum_never_supplies_a_fatigue_baseline(pair):
    observed, _, _ = read([pair] * 4)
    assert observed['success'] is False and observed['reason'] == 'invalid_max_fatigue'


@pytest.mark.parametrize('side', [0, 1])
@pytest.mark.parametrize('confidence', [0.79, float('nan'), float('inf'), None, True])
def test_low_or_invalid_confidence_on_either_side_is_unknown(side, confidence):
    pair = ['77', '872']
    pair[side] = result(pair[side], confidence)
    observed, _, _ = read([pair] * 4)
    assert observed['reason'] == 'low_confidence' and observed['success'] is False


def test_invalid_frame_resets_consecutive_confirmation():
    observed, _, _ = read([('77', '872'), ('invalid', '872'), ('77', '872'), ('77', '872')])
    assert observed['success'] and observed['attempts'] == 4


def test_ocr_exception_returns_unknown_after_bounded_attempts():
    observed, app, _ = read([(RuntimeError('ocr down'), '872')] * 4)
    assert observed['reason'] == 'ocr_failed' and observed['attempts'] == 4
    assert len(app.calls) == 4


def test_capture_failure_is_unknown_without_ocr():
    app = App()
    ocr = Ocr(app)
    app.success = False
    observed = reader.read_trade_fatigue(app, ocr, poll_seconds=0)
    assert observed['reason'] == 'capture_failed' and observed['current_fatigue'] is None
    assert ocr.calls == []


@pytest.mark.parametrize('error', [asyncio.CancelledError(), StopTaskException('cancelled', False), OperationTimeout('deadline')])
def test_ocr_cancellation_and_enclosing_deadline_propagate(error):
    with pytest.raises(type(error)):
        read([(error, '872')])


def test_cooperative_cancellation_stops_before_any_capture():
    app = App()
    ocr = Ocr(app)

    def cancel():
        raise RuntimeError('cancelled')

    with pytest.raises(RuntimeError, match='cancelled'):
        reader.read_trade_fatigue(app, ocr, check_cancelled=cancel)
    assert app.calls == []


def test_cancellation_between_left_and_right_stops_second_ocr():
    app = App([('77', '872')])
    ocr = Ocr(app)

    def cancel_after_left():
        if len(ocr.calls) == 1:
            raise StopTaskException('cancelled', False)

    with pytest.raises(StopTaskException):
        reader.read_trade_fatigue(app, ocr, check_cancelled=cancel_after_left)
    assert len(ocr.calls) == 1 and len(app.calls) == 1


def test_slow_ocr_cannot_authorize_success(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(reader.time, 'monotonic', lambda: now[0])
    app = App([('77', '872')] * 4)
    ocr = Ocr(app)
    original = ocr.recognize_all

    def recognize(**kwargs):
        value = original(**kwargs)
        now[0] += 0.6
        return value

    ocr.recognize_all = recognize
    observed = reader.read_trade_fatigue(app, ocr, poll_seconds=0, timeout_sec=1.0)
    assert observed['reason'] == 'observation_timeout' and observed['success'] is False
    assert observed['current_fatigue'] is None
