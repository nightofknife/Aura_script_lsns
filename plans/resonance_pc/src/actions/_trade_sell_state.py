"""Positive visual evidence for sell selection and sale submission."""
import math
import time
from pathlib import Path

import cv2
import numpy as np

from packages.aura_core.observability.logging.core_logger import logger

ROOT = Path(__file__).resolve().parents[2]
TARGETS = {
    'empty': ('trade_sell_empty_cargo.png', (500, 330, 390, 150), .86),
    'local': ('trade_sell_local_badge.png', (515, 175, 120, 65), .80),
    'all': ('trade_sell_all_button.png', (1140, 80, 110, 50), .86),
    'cancel': ('trade_sell_all_cancel_button.png', (1140, 80, 110, 50), .86),
    'commit': ('trade_sell_commit_button.png', (930, 620, 320, 100), .86),
    'settlement': ('sell_settlement_scale_badge.png', (930, 240, 300, 300), .82),
    'shop': ('trade_shop_menu_ready.png', (720, 350, 220, 120), .86),
    'back': ('nav_back_button.png', (0, 0, 170, 80), .86),
}


class SellSession:
    def __init__(self, app, vision, guard, fail):
        self.app, self.vision, self.guard, self.fail = app, vision, guard, fail

    def pause(self, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.guard()
            time.sleep(min(.05, max(0., end-time.monotonic())))
        self.guard()

    def capture(self):
        self.guard()
        result = self.app.capture(rect=(0, 0, 1280, 720))
        self.guard()
        frame = getattr(result, 'image', None)
        if not result.success or not isinstance(frame, np.ndarray) or frame.shape[:2] != (720, 1280):
            self.fail('sell_capture_failed', '卖货截图失败或尺寸不正确', {})
        return frame

    def observe(self, keys, frame=None):
        frame = self.capture() if frame is None else frame
        results = {}
        for key in keys:
            self.guard()
            name, (x, y, w, h), threshold = TARGETS[key]
            hit = self.vision.find_template(
                source_image=frame[y:y+h, x:x+w], template_image=str(ROOT/'templates'/name),
                threshold=threshold, use_grayscale=True, match_method=cv2.TM_CCOEFF_NORMED,
            )
            self.guard()
            if (getattr(hit, 'debug_info', {}) or {}).get('error'):
                self.fail('sell_match_failed', '卖货模板匹配失败', {'target': key, 'detail': hit.debug_info})
            if not math.isfinite(float(hit.confidence)):
                self.fail('sell_match_invalid', '卖货截图匹配结果无效', {'target': key})
            found = bool(hit.found)
            if key == 'local' and found:
                # Position for the approved 45x26 glyph crop, not the old crop.
                lx, ly = hit.top_left
                found = 26 <= lx <= 34 and 11 <= ly <= 21
            results[key] = {'found': found, 'confidence': float(hit.confidence)}
            if hit.center_point is not None:
                cx, cy = hit.center_point
                results[key]['center'] = (x+int(cx), y+int(cy))
        return results

    def click(self, hit):
        self.guard()
        x, y = hit['center']
        result = self.app.click(x=x, y=y)
        if getattr(result, 'success', True) is False:
            self.fail('sell_input_failed', '卖货点击发送失败', {'point': (x, y)})
        return {'clicked': True, 'x': x, 'y': y, 'method': 'template'}

    @staticmethod
    def state(o):
        if not o['commit']['found']:
            return 'unknown'
        if o['cancel']['found']:
            return 'selected' if not o['all']['found'] and not o['empty']['found'] else 'unknown'
        if not o['all']['found']:
            return 'unknown'
        if o['empty']['found']:
            return 'empty_cargo' if not o['local']['found'] else 'unknown'
        if o['local']['found']:
            return 'no_sellable_goods'
        return 'candidate'

    def select(self):
        keys = ('empty', 'local', 'all', 'cancel', 'commit')
        # Opening the sell page places the cargo list at its top.
        previous, stable, clicks = None, 0, []
        deadline = time.monotonic()+6.
        next_click = 0.
        while time.monotonic() < deadline:
            o = self.observe(keys)
            state = self.state(o)
            stable = stable+1 if state == previous else 1
            previous = state
            logger.info('[TradeSellSelection] state=%s stable=%s/2 clicks=%s observation=%s', state, stable, len(clicks), o)
            if state in ('selected', 'empty_cargo', 'no_sellable_goods') and stable >= 2:
                return {'status': state, 'clicks': clicks, 'observation': o}
            if state == 'candidate' and stable >= 2:
                if len(clicks) < 3 and time.monotonic() >= next_click:
                    clicks.append(self.click(o['all']))
                    next_click = time.monotonic()+.8
                    previous, stable = None, 0
            self.pause(.2)
        self.fail('sell_all_selection_unconfirmed', '全部卖出选择未确认', {'clicks': clicks, 'last': o})

    def submit(self):
        deadline = time.monotonic()+8.
        transition = False
        transition_seen = False
        restored = 0
        absence = 0
        clicks = []
        next_click = 0.
        while time.monotonic() < deadline:
            o = self.observe(('settlement', 'commit', 'cancel'))
            logger.info('[TradeSellCommit] transition=%s clicks=%s absence=%s/2 restored=%s/2 observation=%s', transition, len(clicks), absence, restored, o)
            if o['settlement']['found']:
                return {'clicks': clicks, 'transition_confirmed': transition, 'settlement': o['settlement']}
            if time.monotonic() >= deadline:
                break
            if transition:
                restored = restored+1 if o['commit']['found'] and o['cancel']['found'] else 0
                if restored >= 2:
                    transition = False
                    absence = 0
                    restored = 0
                    logger.info('[TradeSellCommit] phase=sell_page_restored clicks=%s/3 remaining_sec=%.3f',
                                len(clicks), max(0., deadline-time.monotonic()))
                # Re-observe before retrying: settlement retains priority even
                # when it appears immediately after the restored page.
                self.pause(.2)
                continue
            if not transition:
                absence = absence+1 if clicks and not o['commit']['found'] else 0
                if absence >= 2:
                    transition = True
                    restored = 0
                    # Start the settlement allowance once. Subsequent returns
                    # to the sell page share this deadline and the click budget.
                    if not transition_seen:
                        deadline = time.monotonic()+8.
                        transition_seen = True
                    logger.info('[TradeSellCommit] phase=transition_confirmed')
                elif o['commit']['found'] and o['cancel']['found'] and len(clicks) < 3 and time.monotonic() >= next_click:
                    clicks.append(self.click(o['commit']))
                    next_click = time.monotonic()+2.
                    self.pause(.3)
            self.pause(.2)
        code = 'sell_settlement_timeout' if transition else 'sell_click_unconfirmed'
        self.fail(code, '等待卖货结算超时' if transition else '卖出点击未确认', {'clicks': clicks, 'last': o})

    def return_to_shop(self, sold):
        deadline = time.monotonic()+8.
        stable = 0
        clicked = False
        while time.monotonic() < deadline:
            o = self.observe(('shop', 'back', 'all', 'cancel', 'commit'))
            stable = stable+1 if o['shop']['found'] else 0
            if stable >= 2:
                return {'success': True, 'page_state': 'shop_page', 'clicked': clicked}
            if not sold and not clicked and not o['shop']['found'] and o['back']['found'] and o['all']['found'] and not o['cancel']['found'] and o['commit']['found']:
                self.click(o['back'])
                clicked = True
            self.pause(.2)
        self.fail('sell_return_unconfirmed', '卖货结束后未确认交易所菜单', {'sold_confirmed': sold, 'last': o})
