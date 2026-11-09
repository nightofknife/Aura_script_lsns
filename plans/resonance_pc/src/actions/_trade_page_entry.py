"""Confirm trade-page transitions; retry only on a stable exchange menu."""
from __future__ import annotations

import json
import math
import time
from collections.abc import Mapping
from pathlib import Path

import cv2
import numpy as np

from packages.aura_core.observability.failure_evidence import attach_evidence, save_evidence_png
from packages.aura_core.observability.logging.core_logger import logger
from ._trade_sell_state import TARGETS as SELL_TARGETS

ROOT = Path(__file__).resolve().parents[2]
BUY_TARGET = ('commerce/books/buy_page_ready.png', (850, 590, 380, 110), .80)
TARGETS = {key: SELL_TARGETS[key] for key in ('all', 'cancel', 'commit', 'shop')}
TARGETS['buy'] = BUY_TARGET
TIMEOUT_SEC = 8.0
POLL_SECONDS = .2
RETRY_SECONDS = 1.0
MAX_CLICKS = 3


class TradePageEntryError(RuntimeError):
    def __init__(self, code, message, detail):
        super().__init__(message)
        self.code, self.message, self.detail = code, message, detail


class TradePageEntry:
    def __init__(self, *, kind, app, vision, click, check_cancelled, sleep, context=None):
        if kind not in ('buy', 'sell'):
            raise ValueError('Trade page must be buy or sell')
        self.kind, self.app, self.vision = kind, app, vision
        self.click, self.guard, self.sleep = click, check_cancelled, sleep
        self.context = dict(context or {})
        self.frame = None
        self.captured_at = None
        self.clicks, self.polls, self.last = [], 0, {}

    def fail(self, suffix, message, started, **extra):
        self.guard()
        detail = {'target_page': self.kind, 'clicks': list(self.clicks), 'polls': self.polls,
                  'last': self.last, 'elapsed_sec': round(time.monotonic() - started, 3),
                  'timeout_sec': TIMEOUT_SEC, 'context': self.context, **extra}
        error = TradePageEntryError(f'{self.kind}_page_{suffix}', message, detail)
        if self.frame is not None:
            try:
                # App captures are RGB; OpenCV's PNG encoder expects BGR.
                ok, png = cv2.imencode('.png', cv2.cvtColor(self.frame, cv2.COLOR_RGB2BGR))
                if not ok:
                    raise ValueError('PNG encoding failed')
                self.guard()
                ref = save_evidence_png(png.tobytes())
                ref.update(origin='trade_page_entry', error_code=error.code,
                           captured_at=self.captured_at, scene_may_have_changed=True,
                           context=self.context)
                attach_evidence(error, [ref])
                logger.info('[FailureEvidence] %s', json.dumps(ref, ensure_ascii=False))
            except Exception as exc:
                self.guard()
                detail['evidence_error'] = str(exc)
                logger.warning('[TradePageEntry] evidence_failed error=%s context=%s', exc, self.context)
        raise error

    def observe(self, started):
        self.guard()
        try:
            capture = self.app.capture(rect=(0, 0, 1280, 720))
        except Exception as exc:
            self.guard()
            self.fail('capture_failed', '交易页面截图失败', started, error=str(exc))
        self.guard()
        frame = getattr(capture, 'image', None)
        if (not getattr(capture, 'success', False) or not isinstance(frame, np.ndarray)
                or frame.shape != (720, 1280, 3) or frame.dtype != np.uint8):
            self.fail('capture_failed', '交易页面截图失败或尺寸不正确', started)
        self.frame = frame.copy()
        frame = self.frame
        self.captured_at = time.time()
        observation = {}
        for key, (name, region, threshold) in TARGETS.items():
            self.guard()
            x, y, w, h = region
            try:
                hit = self.vision.find_template(
                    source_image=frame[y:y+h, x:x+w], template_image=str(ROOT/'templates'/name),
                    threshold=threshold, use_grayscale=True, match_method=cv2.TM_CCOEFF_NORMED,
                )
                debug = getattr(hit, 'debug_info', None)
                score = float(hit.confidence)
                valid = (type(hit.found) in (bool, np.bool_) and math.isfinite(score)
                         and (debug is None or isinstance(debug, Mapping) and not debug.get('error')))
            except Exception as exc:
                self.guard()
                self.last = observation
                self.fail('match_failed', '交易页面模板识别失败', started, target=key, error=str(exc))
            self.guard()
            if not valid:
                self.last = observation
                self.fail('match_failed', '交易页面模板结果无效', started, target=key)
            observation[key] = {'found': bool(hit.found) and score >= threshold,
                                'confidence': score, 'threshold': threshold, 'region': list(region)}
        self.last = observation
        return observation

    @staticmethod
    def state(observation, kind):
        def hit(key):
            return observation[key]['found']
        sell = hit('commit') and (hit('all') != hit('cancel'))
        buy = hit('buy')
        target, other = ((sell, buy) if kind == 'sell'
                         else (buy, any(hit(key) for key in ('all', 'cancel', 'commit'))))
        if target and not other and not hit('shop'):
            return 'target'
        # A partial or conflicting target observation cannot authorize a retry.
        if hit('shop') and not any(hit(key) for key in ('all', 'cancel', 'commit', 'buy')):
            return 'menu'
        return 'unknown'

    def run(self):
        started = time.monotonic()
        deadline = started + TIMEOUT_SEC
        previous, stable, next_click = None, 0, started
        while time.monotonic() < deadline:
            self.guard()
            self.polls += 1
            observation = self.observe(started)
            self.guard()
            if time.monotonic() >= deadline:
                break
            state = self.state(observation, self.kind)
            stable = stable + 1 if state == previous else 1
            previous = state
            logger.info('[TradePageEntry] target=%s state=%s stable=%s/2 clicks=%s elapsed=%.3f '
                        'observation=%s context=%s', self.kind, state, stable, len(self.clicks),
                        time.monotonic()-started, observation, self.context)
            self.guard()
            if time.monotonic() >= deadline:
                break
            if state == 'target' and stable >= 2:
                return {'success': True, 'page_state': f'{self.kind}_page', 'clicks': list(self.clicks),
                        'polls': self.polls, 'observation': observation,
                        'elapsed_sec': round(time.monotonic()-started, 3)}
            if state == 'menu' and stable >= 2 and len(self.clicks) < MAX_CLICKS and time.monotonic() >= next_click:
                self.guard()
                try:
                    result = self.click()
                except Exception as exc:
                    self.guard()
                    self.fail('input_failed', '交易菜单点击发送失败', started, error=str(exc))
                self.guard()
                if (result is False or getattr(result, 'success', True) is False
                        or isinstance(result, dict) and result.get('success') is False):
                    self.fail('input_failed', '交易菜单点击发送失败', started)
                self.clicks.append({'attempt': len(self.clicks)+1, 'x': 1160,
                                    'y': 324 if self.kind == 'buy' else 407,
                                    'elapsed_sec': round(time.monotonic()-started, 3)})
                next_click = time.monotonic() + RETRY_SECONDS
                previous, stable = None, 0
                # No evidence from before an input may describe the resulting page.
                self.frame = None
                self.captured_at = None
            self.guard()
            remaining = deadline - time.monotonic()
            if remaining > 0:
                self.sleep(min(POLL_SECONDS, remaining))
        self.fail('open_unconfirmed', '未确认交易目标页面已打开', started)


def open_trade_page(**kwargs):
    return TradePageEntry(**kwargs).run()
