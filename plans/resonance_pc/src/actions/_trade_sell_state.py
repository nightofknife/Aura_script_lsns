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
    def state(o, allow_local):
        if not o['commit']['found']:
            return 'unknown'
        if o['cancel']['found']:
            return 'selected' if not o['all']['found'] and not o['empty']['found'] else 'unknown'
        if not o['all']['found']:
            return 'unknown'
        if o['empty']['found']:
            return 'empty_cargo' if not o['local']['found'] else 'unknown'
        if allow_local and o['local']['found']:
            return 'no_sellable_goods'
        return 'candidate'

    def rewind(self):
        # The list may retain its scroll position. Downward swipes move it to
        # its top; compare only the stationary list, excluding animated NPCs.
        prior = self.capture()[145:675, 505:855]
        stationary = 0
        for attempt in range(10):
            self.guard()
            result = self.app.drag(start_x=700, start_y=230, end_x=700, end_y=650,
                                   duration=.5, hold_before_release_sec=.2)
            if getattr(result, 'success', True) is False:
                self.fail('sell_rewind_failed', '货舱列表回顶输入失败', {})
            self.pause(.3)
            frame = self.capture()
            o = self.observe(('all', 'cancel', 'commit'), frame)
            if not o['all']['found'] or o['cancel']['found'] or not o['commit']['found']:
                self.fail('sell_rewind_failed', '货舱回顶时页面状态变化', o)
            current = frame[145:675, 505:855]
            delta = np.max(np.abs(current.astype(np.int16)-prior.astype(np.int16)), axis=2)
            stationary = stationary+1 if float(np.mean(delta > 15)) < .01 else 0
            logger.info('[TradeSellSelection] phase=rewind drag=%s stationary=%s/2', attempt+1, stationary)
            if stationary >= 2:
                return
            prior = current
        self.fail('sell_rewind_failed', '未能确认货舱列表已回到顶部', {})

    def select(self):
        keys = ('empty', 'local', 'all', 'cancel', 'commit')
        previous, stable, clicks, rewound = None, 0, [], False
        deadline = time.monotonic()+6.
        next_click = 0.
        while time.monotonic() < deadline:
            o = self.observe(keys)
            state = self.state(o, rewound)
            stable = stable+1 if state == previous else 1
            previous = state
            logger.info('[TradeSellSelection] state=%s stable=%s/2 clicks=%s observation=%s', state, stable, len(clicks), o)
            if state in ('selected', 'empty_cargo', 'no_sellable_goods') and stable >= 2:
                return {'status': state, 'clicks': clicks, 'observation': o}
            if state == 'candidate' and stable >= 2:
                if not rewound:
                    self.rewind()
                    rewound = True
                    previous, stable = None, 0
                    deadline = time.monotonic()+6.  # selection budget starts after bounded rewind
                    continue
                if len(clicks) < 3 and time.monotonic() >= next_click:
                    clicks.append(self.click(o['all']))
                    next_click = time.monotonic()+.8
                    previous, stable = None, 0
            self.pause(.2)
        self.fail('sell_all_selection_unconfirmed', '全部卖出选择未确认', {'clicks': clicks, 'last': o})

    def submit(self):
        deadline = time.monotonic()+8.
        transition = False
        absence = 0
        clicks = []
        next_click = 0.
        while time.monotonic() < deadline:
            o = self.observe(('settlement', 'commit', 'cancel'))
            logger.info('[TradeSellCommit] transition=%s clicks=%s absence=%s/2 observation=%s', transition, len(clicks), absence, o)
            if o['settlement']['found']:
                return {'clicks': clicks, 'transition_confirmed': transition, 'settlement': o['settlement']}
            if not transition:
                absence = absence+1 if clicks and not o['commit']['found'] else 0
                if absence >= 2:
                    transition = True
                    deadline = time.monotonic()+8.
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
