"""Configure and supervise the client's native structural auto-hunt.

Native code owns selection, squad entry, combat and settlement. Inputs after
startup are reserved for stopping it. Observed settlements are not a census.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from copy import deepcopy
from pathlib import Path

import cv2
import numpy as np

from packages.aura_core.observability.logging.core_logger import logger
from packages.resonance_shared.battle_control import stop_requested
from ._battle_ui import BattleUi, find_text


PLAN = Path(__file__).resolve().parents[2]
TYPE_KEYS = ("disordered_roots", "hetero_branches", "echo_buoy", "birch_buoy")
MAP_AUTO_ROI = (25, 590, 310, 105)
DETAIL_START_ROI = (833, 550, 300, 150)
# Native Battle_DungeonScan prefab: Group_RecoLevel/Txt_RecoLevel is
# [939,553,267,40]; Group_Auto/Txt_T is [727,582,107,20]. The former is
# displayed for all four types; the latter is hidden for manual hidden/resident.
DETAIL_RECOMMENDED_ROI = (930, 545, 285, 55)
DETAIL_AUTO_TEXT_ROI = (720, 575, 125, 35)


class StructuralExplorationError(RuntimeError):
    def __init__(self, code, detail=None):
        self.code, self.detail = code, detail or {}
        super().__init__(f"{code}: {self.detail}")


def callback_page(items):
    if (find_text(items, "开始作战", region=DETAIL_START_ROI)
            and (find_text(items, "推荐等级", region=DETAIL_RECOMMENDED_ROI)
                 or find_text(items, "自动战斗", region=DETAIL_AUTO_TEXT_ROI))):
        return "structural_detail"
    if (find_text(items, ("开启自动讨伐", "关闭自动讨伐"), region=MAP_AUTO_ROI)
            and find_text(items, "全域勘测", region=(1000, 555, 240, 130))
            and not find_text(items, "开始作战", region=DETAIL_START_ROI)):
        return "world_scan"
    return None


class StructuralRuntime:
    def __init__(self, types, *, ui, execution, control_cid=None):
        if not isinstance(types, list) or not types or any(key not in TYPE_KEYS for key in types):
            raise ValueError("structural types must contain only known type keys")
        self.types = list(dict.fromkeys(types))
        self.ui, self.execution = ui, execution
        self.control_cid = str(control_cid or "")
        self.catalog = json.loads((PLAN / "data/meta/structural_exploration_pc.json").read_text(encoding="utf-8"))
        self.native = self.catalog["native_auto"]
        self.templates = {}
        self.images = {}
        for item in [*self.catalog["icons"], *self.catalog["controls"].values()]:
            relative = item["template"]
            raw = np.frombuffer((PLAN / relative).read_bytes(), dtype=np.uint8)
            image = cv2.imdecode(raw, cv2.IMREAD_UNCHANGED)
            if image is None or image.ndim != 3 or image.shape[2] != 4:
                raise StructuralExplorationError("structural_template_unavailable", {"template": relative})
            # Alpha contains the native icon silhouette, independent of selected
            # white/dark tint. Matching its edges also covers both tint states.
            edges = cv2.Canny(image[:, :, 3], 50, 150)
            colour_controls = {self.catalog["controls"][key]["template"]
                               for key in ("detail_auto_on", "detail_auto_off")}
            if relative not in colour_controls and not np.any(edges):
                raise StructuralExplorationError("structural_template_silhouette_missing", {"template": relative})
            self.templates[relative] = edges
            self.images[relative] = image
        self.trace = []
        self.result = None
        self.native_armed = False
        self.stop_reason = None
        self.stop_attempted = False
        self.native_images = {}
        self.allow_refresh = False

    def _scores(self, frame, item, region):
        x, y, width, height = map(int, region)
        image = frame.rgb_image()[y:y+height, x:x+width]
        if image.shape[:2] != (height, width):
            raise StructuralExplorationError("structural_roi_unavailable")
        gray = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)
        edges = cv2.Canny(gray, 50, 150)
        template = self.templates[item["template"]]
        if edges.shape[0] < template.shape[0] or edges.shape[1] < template.shape[1]:
            raise StructuralExplorationError("structural_template_roi_mismatch")
        values = cv2.matchTemplate(edges, template, cv2.TM_CCOEFF_NORMED)
        return np.nan_to_num(values, nan=-1., posinf=-1., neginf=-1.)

    def _control_score(self, frame, key, region):
        if key in {"detail_auto_on", "detail_auto_off"}:
            # Both switches share the rounded silhouette, so their alpha edges
            # cannot identify state. Match their native colour interiors.
            item = self.catalog["controls"][key]
            native = self.images[item["template"]]
            template = cv2.cvtColor(native, cv2.COLOR_BGRA2RGB)
            x, y, w, h = region
            image = frame.rgb_image()[y:y+h, x:x+w]
            values = cv2.matchTemplate(image, template, cv2.TM_SQDIFF_NORMED, mask=native[:, :, 3])
            values = np.nan_to_num(values, nan=1., posinf=1., neginf=1.)
            return max(0., 1.-float(np.min(values)))
        values = self._scores(frame, self.catalog["controls"][key], region)
        return float(np.max(values))

    def _rows(self, frame, items):
        region = self.catalog["list_icon_roi"]
        x, y, _, _ = region
        candidates = []
        threshold = float(self.catalog["icon_match_threshold"])
        for icon in self.catalog["icons"]:
            scores = self._scores(frame, icon, region)
            # Suppress each local peak before collecting the next occurrence.
            while True:
                _, score, _, point = cv2.minMaxLoc(scores)
                if score < threshold:
                    break
                cx, cy = x+point[0]+17, y+point[1]+17
                if 180 <= cy <= 558:
                    title = [i for i in items if 135 <= i["center"][0] <= 322
                             and abs(i["center"][1]-cy) <= 23]
                    text = " ".join(i["text"] for i in sorted(title, key=lambda i: i["center"][0]))
                    candidates.append({"type": icon["type"], "point": (cx, cy),
                                       "score": float(score), "title": text})
                px, py = point
                scores[max(0, py-18):py+19, max(0, px-18):px+19] = -1.
        rows = []
        for candidate in sorted(candidates, key=lambda item: -item["score"]):
            if not any(abs(candidate["point"][1]-item["point"][1]) < 24 for item in rows):
                rows.append(candidate)
        return sorted(rows, key=lambda item: item["point"][1])

    def _signature(self, frame):
        x, y, w, h = self.catalog["list_roi"]
        # Capture includes the entire list viewport, rather than names only:
        # duplicate names must not make different scroll positions equivalent.
        image = cv2.cvtColor(frame.rgb_image()[y:y+h, x:x+w], cv2.COLOR_RGB2GRAY)
        return hashlib.sha256(image.tobytes()).hexdigest()

    async def _read(self):
        self.ui.scope.checkpoint()
        frame, items = await self.ui.read()
        if not self.allow_refresh and find_text(items, "勘测数据生成中"):
            raise StructuralExplorationError("structural_event_refresh_in_progress")
        return frame, items

    async def _wait_page(self, accepted, *, timeout=20.):
        deadline, previous, stable = time.monotonic()+timeout, None, 0
        while time.monotonic() < deadline:
            frame, items = await self._read()
            page = callback_page(items)
            if page in accepted:
                stable = stable+1 if page == previous else 1
                if stable >= 2:
                    return frame, items, page
            else:
                stable = 0
            previous = page
            await self.ui.sleep(.25)
        raise StructuralExplorationError("structural_page_unconfirmed", {"expected": list(accepted)})

    async def _scroll(self, *, down):
        await self.execution.call_action("plans/aura_base/drag", {
            "start_x": 240, "start_y": 520 if down else 220,
            "end_x": 240, "end_y": 220 if down else 520,
            "duration": .5, "hold_before_release_sec": .3,
        })
        await self.ui.sleep(.5)

    async def _open_list(self):
        frame, items = await self._read()
        if callback_page(items) != "world_scan":
            raise StructuralExplorationError("structural_list_source_unconfirmed")
        roi = self.catalog["list_arrow_roi"]
        opened = self._control_score(frame, "event_list_open", roi)
        closed = self._control_score(frame, "event_list_closed", roi)
        if max(opened, closed) < .5:
            title_roi = self.catalog["list_title_roi"]
            if find_text(items, "事件", region=title_roi) or any(
                title_roi[0] <= item["center"][0] < title_roi[0]+title_roi[2]
                and title_roi[1] <= item["center"][1] < title_roi[1]+title_roi[3]
                for item in items
            ):
                raise StructuralExplorationError("structural_event_header_unrecognized")
            if self._rows(frame, items):
                raise StructuralExplorationError("structural_event_header_unrecognized")
            # refreshEventBtn hides Group_Btn only for an empty eventList.
            # Require a complete ready map twice: both native controls, no
            # detail panel, no refresh message, no header text/arrow or rows.
            await self.ui.sleep(.3)
            again, second = await self._read()
            if (callback_page(second) == "world_scan"
                    and max(self._control_score(again, key, roi)
                            for key in ("event_list_open", "event_list_closed")) < .5
                    and not self._rows(again, second)
                    and not any(title_roi[0] <= item["center"][0] < title_roi[0]+title_roi[2]
                                and title_roi[1] <= item["center"][1] < title_roi[1]+title_roi[3]
                                for item in second)):
                return False
            raise StructuralExplorationError("structural_event_list_unknown")
        if max(opened, closed) < .78 or abs(opened-closed) < .03:
            raise StructuralExplorationError("structural_event_list_state_ambiguous")
        if closed > opened:
            await self.ui.click(frame, (180, 120))
            await self.ui.sleep(.35)
        return True

    async def _reset_list(self):
        previous = None
        for _ in range(64):
            if self._requested_stop():
                return
            frame, items = await self._read()
            rows = self._rows(frame, items)
            if not rows:
                raise StructuralExplorationError("structural_event_rows_unrecognized")
            signature = self._signature(frame)
            if previous == signature:
                if len(rows) >= 6 and len({(r["type"], r["title"]) for r in rows}) < 2:
                    raise StructuralExplorationError("structural_list_top_ambiguous")
                return
            previous = signature
            await self._scroll(down=False)
        raise StructuralExplorationError("structural_event_list_top_unconfirmed")

    def _requested_stop(self):
        return bool(self.control_cid and stop_requested(self.control_cid))

    def _colour_score(self, frame, item, roi):
        """Compare native RGBA with the legal range over an unknown background.

        A translucent black UI Image is not a black screenshot pixel. The native
        alpha bounds the possible rendered RGB instead. Include both gamma and
        linear-light compositing, since this catalog does not pin player colour
        space. Opaque pixels still require their actual native colour.
        """
        relative = item['template']
        if relative not in self.native_images:
            raw = np.frombuffer((PLAN / relative).read_bytes(), dtype=np.uint8)
            native = cv2.imdecode(raw, cv2.IMREAD_UNCHANGED)
            if native is None or native.ndim != 3 or native.shape[2] != 4:
                raise StructuralExplorationError('structural_native_template_unavailable', {'template': relative})
            self.native_images[relative] = native
        native = self.native_images[relative]
        if item.get('template_crop') is not None:
            left, top, right, bottom = map(int, item['template_crop'])
            native = native[top:bottom, left:right]
        x, y, w, h = map(int, roi)
        actual = frame.rgb_image()[y:y+h, x:x+w]
        if actual.shape[:2] != (h, w) or native.shape[:2] != (h, w):
            raise StructuralExplorationError('structural_native_template_size_mismatch', {'template': relative, 'roi': roi})
        mask = native[:, :, 3] >= 32
        if not np.any(mask):
            raise StructuralExplorationError('structural_native_template_mask_missing', {'template': relative})
        rgb = cv2.cvtColor(native, cv2.COLOR_BGRA2RGB).astype(np.float32)/255.
        alpha = native[:, :, 3:4].astype(np.float32)/255.
        lower, upper = rgb*alpha, rgb*alpha+(1.-alpha)
        linear = np.where(rgb <= .04045, rgb/12.92, ((rgb+.055)/1.055)**2.4)

        def encoded(value):
            return np.where(value <= .0031308, value*12.92, 1.055*value**(1./2.4)-.055)

        lower = np.minimum(lower, encoded(linear*alpha))
        upper = np.maximum(upper, encoded(linear*alpha+(1.-alpha)))
        actual = actual.astype(np.float32)/255.
        difference = np.maximum(np.maximum(lower-actual, actual-upper), 0.)
        return 1.-float(difference[mask].mean())

    def _cards_visible(self, items):
        return all(find_text(items, card['label'], exact=True, region=card['label_roi'])
                   for card in self.native['cards'].values())

    def _card_selection(self, frame, card):
        on = self._colour_score(frame, card['state_selected'], card['state_roi'])
        off = self._colour_score(frame, card['state_unselected'], card['state_roi'])
        # SetBtnAutoItemGridElelment uses #282F2E for selected type text and
        # #FFFFFF for unselected text. The caller separately requires the OCR
        # type identity in this exact region; these are foreground-colour checks,
        # not a replacement for reading the label.
        x, y, width, height = map(int, card['label_roi'])
        label = frame.rgb_image()[y:y+height, x:x+width].astype(np.float32)
        if label.shape[:2] != (height, width):
            raise StructuralExplorationError('structural_roi_unavailable')
        dark = np.max(np.abs(label-np.array([40., 47., 46.])), axis=2) <= 22.
        white = ((np.min(label, axis=2) >= 220.)
                 & (np.max(label, axis=2)-np.min(label, axis=2) <= 25.))
        minimum_pixels = max(18, int(width*height*.012))
        dark_pixels, white_pixels = int(np.count_nonzero(dark)), int(np.count_nonzero(white))
        # The half-transparent off card has a bounded dark background; white
        # text must stand above it. A bright map/background alone is insufficient.
        white_contrast = (float(label[white].mean())-float(np.median(label))) if white_pixels else 0.
        selected = on >= .94 and dark_pixels >= minimum_pixels
        unselected = (off >= .985 and on < .93 and white_pixels >= minimum_pixels
                      and white_contrast >= 35.)
        if selected != unselected:
            return selected
        raise StructuralExplorationError('structural_type_selection_unconfirmed', {
            'type': card['type'], 'on': on, 'off': off, 'dark_pixels': dark_pixels,
            'white_pixels': white_pixels, 'white_contrast': white_contrast,
        })

    def _selection(self, frame, items):
        if not self._cards_visible(items):
            raise StructuralExplorationError('structural_type_panel_unconfirmed')
        selected = {}
        for card in self.native['cards'].values():
            if not self._card_selection(frame, card):
                continue
            badge = find_text(items, ('1', '2', '3', '4'), exact=True, region=card['priority_roi'])
            if badge is None:
                raise StructuralExplorationError('structural_type_priority_unreadable', {'type': card['type']})
            rank = int(badge['text'].strip())
            if rank in selected:
                raise StructuralExplorationError('structural_type_priority_ambiguous')
            selected[rank] = card['type']
        if sorted(selected) != list(range(1, len(selected)+1)):
            raise StructuralExplorationError('structural_type_priority_not_contiguous')
        return [selected[index] for index in sorted(selected)]

    async def _control_click(self, frame, name):
        await self.ui.click(frame, self.native['controls'][name]['point'])

    async def _panel(self, opened):
        deadline = time.monotonic()+8.
        clicked, stable = False, 0
        while time.monotonic() < deadline:
            frame, items = await self._read()
            if callback_page(items) != 'world_scan' or find_text(items, '关闭自动讨伐', region=MAP_AUTO_ROI):
                raise StructuralExplorationError('structural_type_setup_source_unknown')
            visible = self._cards_visible(items)
            if visible == opened:
                stable += 1
                if stable >= 2:
                    return frame, items
            elif not clicked:
                await self._control_click(frame, 'setup')
                clicked = True
                stable = 0
            await self.ui.sleep(.15)
        raise StructuralExplorationError('structural_type_panel_transition_failed')

    async def _wait_selection(self, expected):
        deadline, stable, previous = time.monotonic()+5., 0, None
        while time.monotonic() < deadline:
            frame, items = await self._read()
            try:
                observed = self._selection(frame, items)
            except StructuralExplorationError as exc:
                if exc.code not in {
                    'structural_type_selection_unconfirmed', 'structural_type_priority_unreadable',
                    'structural_type_priority_not_contiguous', 'structural_type_priority_ambiguous',
                    'structural_type_panel_unconfirmed',
                }:
                    raise
                observed = None
            if observed is not None and (expected is None or observed == expected):
                stable = stable+1 if observed == previous else 1
                if stable >= 2:
                    return frame, items
            else:
                stable = 0
            previous = observed
            await self.ui.sleep(.15)
        raise StructuralExplorationError('structural_type_order_mismatch', {'expected': expected})

    async def _configure(self):
        await self._panel(True)
        # Clear by freshly observed identities; removal renumbers remaining cards.
        for _ in range(4):
            frame, items = await self._wait_selection(None)
            current = self._selection(frame, items)
            if not current:
                break
            frame, items = await self._wait_selection(current)
            card = next(c for c in self.native['cards'].values() if c['type'] == current[0])
            await self.ui.click(frame, card['point'])
            await self._wait_selection(current[1:])
        await self._wait_selection([])
        configured = []
        for key in self.types:
            if self._requested_stop():
                return False
            frame, items = await self._wait_selection(configured)
            card = next(c for c in self.native['cards'].values() if c['type'] == key)
            await self.ui.click(frame, card['point'])
            configured.append(key)
            await self._wait_selection(configured)
        self.result['configured_types'] = list(configured)
        await self._panel(False)
        return True

    def _energy_page(self, items):
        for name, page in self.native['energy_pages'].items():
            if find_text(items, page['identity_text'], region=page['identity_roi']):
                return name
            # Native text overflows its serialized 107px rect. OCR may split
            # the question or normalize punctuation; require both semantic parts.
            if (name == 'buy_tips' and find_text(items, '澄明度不足')
                    and find_text(items, '补充澄明度')):
                return name
        return None

    def _hud(self, frame):
        hud = self.native.get('battle_hud', {})
        if not hud.get('provided'):
            return False
        scores = []
        for state in hud['states'].values():
            if state.get('identity_eligible') and len(state['parts']) >= 2:
                # Match the rendered composite, not its occluded bottom layer.
                # Require the small status glyph separately, so the larger base
                # cannot hide a wrong/missing mark inside an averaged score.
                gx, gy, _, _ = map(int, state['roi'])
                mark_x, mark_y, mark_w, mark_h = map(int, state['parts'][-1]['roi'])
                mark = {**state, 'template_crop': [mark_x-gx, mark_y-gy,
                                                  mark_x-gx+mark_w, mark_y-gy+mark_h]}
                scores.append(min(self._colour_score(frame, state, state['roi']),
                                  self._colour_score(frame, mark, [mark_x, mark_y, mark_w, mark_h])))
        return bool(scores and max(scores) >= .94)

    def _page(self, frame, items):
        energy = self._energy_page(items)
        if energy:
            return 'energy:'+energy
        if find_text(items, '勘测数据生成中'):
            return 'refreshing'
        titles = [i for i in items if i.get('confidence', 0) >= .80]
        defeat = find_text(titles, '作战失败', exact=True)
        victory = find_text(titles, ('作战胜利', '作战完成'), exact=True)
        if defeat and victory:
            raise StructuralExplorationError('structural_settlement_ambiguous')
        if defeat:
            return 'defeat'
        if victory:
            return 'victory'
        if find_text(titles, '严重超时', exact=True):
            return 'battle_timeout'
        page = callback_page(items)
        if page:
            return page
        if (find_text(items, '编队', region=(0, 0, 1280, 180))
                and find_text(items, ('开始', '作战', 'START BATTLE'), region=(1050, 250, 230, 470))):
            return 'preparation'
        if self._hud(frame):
            return 'battle'
        if find_text(items, '作战终端', region=(1100, 380, 180, 140)):
            return 'main'
        return 'unknown'

    def _energy_shortage(self, items):
        return self._energy_page(items) is not None or bool(
            find_text(items, '澄明度不足') and find_text(items, '无法自动战斗'))

    async def _stop_native(self, reason, *, timeout_sec=25.):
        """Stop using original native handlers, within the live parent scope."""
        self.stop_attempted = True
        deadline = time.monotonic()+timeout_sec
        stable, waiting_page, last_input = 0, None, 0.
        while time.monotonic() < deadline:
            frame, items = await self._read()
            page = self._page(frame, items)
            self.result['last_page'] = page
            if page != 'world_scan' or find_text(items, '关闭自动讨伐', region=MAP_AUTO_ROI):
                stable = 0
            if page == 'structural_detail':
                # This handler clears BOTH IsAutoDungeon and WorldScan.isAuto.
                if page != waiting_page or time.monotonic()-last_input > .7:
                    await self._control_click(frame, 'detail_return')
                    stable, waiting_page, last_input = 0, page, time.monotonic()
            elif page == 'world_scan':
                if find_text(items, '关闭自动讨伐', region=MAP_AUTO_ROI):
                    if page != waiting_page or time.monotonic()-last_input > .7:
                        await self._control_click(frame, 'end')
                        stable, waiting_page, last_input = 0, page, time.monotonic()
                elif find_text(items, '开启自动讨伐', region=MAP_AUTO_ROI):
                    if self._cards_visible(items):
                        await self._control_click(frame, 'setup')
                        stable = 0
                    else:
                        stable += 1
                        if stable >= 2:
                            self.native_armed = False
                            self.result.update(native_stopped=True, page_after='world_scan', ready_for_next=True)
                            return
            elif page.startswith('energy:'):
                if page != waiting_page or time.monotonic()-last_input > .7:
                    target = self.native['energy_pages'][page.split(':', 1)[1]]['close']
                    await self.ui.click(frame, target['point'])
                    waiting_page, last_input = page, time.monotonic()
            elif page in {'defeat', 'victory'}:
                next_button = find_text(items, '下一步', exact=True)
                if next_button and (page != waiting_page or time.monotonic()-last_input > .7):
                    await self.ui.click(frame, next_button['center'])
                    waiting_page, last_input = page, time.monotonic()
            elif page == 'preparation':
                if reason == 'defeat':
                    raise StructuralExplorationError('structural_stop_failed', {'reason': reason, 'page': page, 'detail': 'native restart may already have begun'})
                if page != waiting_page:
                    await self.execution.call_action('resonance_pc.wait_and_click_back_button', {
                        'region': [10, 5, 160, 70], 'template': 'templates/battle/navigation/back_button.png',
                        'threshold': .9, 'use_grayscale': True, 'timeout_sec': 2.,
                        'interval_sec': .1, 'stable_scans': 1, 'after_click_sec': .1,
                    })
                    waiting_page = page
            elif page == 'battle' and reason == 'defeat':
                raise StructuralExplorationError('structural_stop_failed', {'reason': reason, 'page': page, 'detail': 'another battle observed after defeat'})
            elif page == 'main':
                # Being home alone does not prove that the auto flags were cleared.
                raise StructuralExplorationError('structural_stop_failed', {'reason': reason, 'page': page})
            else:
                stable = 0
            await self.ui.sleep(.12)
        raise StructuralExplorationError('structural_stop_failed', {'reason': reason, 'last_page': self.result.get('last_page')})

    async def _has_selected_targets(self):
        """Inspect a stopped list only to explain termination; never dispatch it."""
        if not await self._open_list():
            return False
        if self._requested_stop():
            return None
        await self._reset_list()
        previous = None
        for _ in range(64):
            if self._requested_stop():
                return None
            frame, items = await self._read()
            if callback_page(items) != 'world_scan' or find_text(items, '关闭自动讨伐', region=MAP_AUTO_ROI):
                raise StructuralExplorationError('structural_termination_page_changed')
            rows = self._rows(frame, items)
            if not rows:
                raise StructuralExplorationError('structural_event_rows_unrecognized')
            if any(row['type'] in self.types for row in rows):
                return True
            signature = self._signature(frame)
            if signature == previous:
                if len(rows) >= 6 and len({(r['type'], r['title']) for r in rows}) < 2:
                    raise StructuralExplorationError('structural_list_boundary_ambiguous')
                return False
            previous = signature
            await self._scroll(down=True)
        raise StructuralExplorationError('structural_event_list_end_unconfirmed')

    async def _monitor(self):
        previous, page_since, idle_since = None, time.monotonic(), None
        last_known_page = None
        settlement_latched, clear_scans = False, 0
        while True:
            if self._requested_stop():
                self.stop_reason = 'cancelled'
            frame, items = await self._read()
            page = self._page(frame, items)
            self.result['last_page'] = page
            now = time.monotonic()
            if page != previous:
                logger.info('[StructuralNative] page=%s', page)
                self.trace.append({'page': page, 'at': now})
                self.trace = self.trace[-40:]
                # Unknown transitions must not repeatedly reset the single-fight
                # watchdog. Only a positively identified phase advances it.
                if page != 'unknown' and page != last_known_page:
                    page_since = now
                    last_known_page = page
                previous = page
            if page in {'defeat', 'victory'}:
                clear_scans = 0
                if not settlement_latched:
                    self.result['observed']['lost' if page == 'defeat' else 'won'] += 1
                    settlement_latched = True
                if page == 'defeat' and self.stop_reason != 'cancelled':
                    self.stop_reason = 'defeat'
            elif page in {'world_scan', 'structural_detail', 'preparation', 'battle'}:
                clear_scans += 1
                if clear_scans >= 2:
                    settlement_latched = False
            if self._energy_shortage(items) and self.stop_reason is None:
                self.stop_reason = 'insufficient_stamina'
            if self.stop_reason:
                # User cancellation may arrive inside an ongoing fight. Let that
                # fight return naturally, then stop before native continuation.
                budget = 630. if self.stop_reason == 'cancelled' and page in {'battle', 'unknown'} else 25.
                await self._stop_native(self.stop_reason, timeout_sec=budget)
                if self._requested_stop():
                    self.stop_reason = 'cancelled'
                self.result.update(outcome=self.stop_reason, reason=self.stop_reason)
                return self.result
            if page == 'battle_timeout':
                raise StructuralExplorationError('structural_battle_timeout')
            if page == 'refreshing':
                raise StructuralExplorationError('structural_event_refresh_in_progress')
            idle = page == 'world_scan' and find_text(items, '开启自动讨伐', region=MAP_AUTO_ROI)
            if idle:
                idle_since = idle_since if idle_since is not None else now
                if now-idle_since >= .6:
                    # Native may silently end after the final event. Stopped is
                    # NOT completion until the whole selected event pool is empty.
                    self.allow_refresh = False
                    await self._stop_native('explain_idle')
                    remaining = await self._has_selected_targets()
                    if self._requested_stop():
                        self.result.update(outcome='cancelled', reason='cancelled')
                        return self.result
                    if remaining is False:
                        self.result.update(outcome='completed', reason='no_matching_targets')
                        return self.result
                    raise StructuralExplorationError('structural_native_stop_unexplained', {'selected_targets_remain': True})
            else:
                idle_since = None
            budget = 660. if page in {'battle', 'unknown'} else 45.
            if now-page_since > budget:
                raise StructuralExplorationError('structural_native_phase_timeout', {'page': page, 'timeout_sec': budget})
            await self.ui.sleep(.12)

    async def _run(self):
        self.result = {
            'schema': 'resonance_pc.structural_native_result.v1', 'mode': 'native_auto',
            'outcome': None, 'reason': None, 'configured_types': [],
            'native_stopped': False, 'ready_for_next': False, 'page_after': 'unknown',
            'observed': {'won': 0, 'lost': 0}, 'statistics_complete': False,
        }
        await self._stop_native('prepare')
        if self._requested_stop() or not await self._configure():
            await self._stop_native('cancelled')
            self.result.update(outcome='cancelled', reason='cancelled')
            return self.result
        frame, items = await self._read()
        if callback_page(items) != 'world_scan' or not find_text(items, '开启自动讨伐', region=MAP_AUTO_ROI):
            raise StructuralExplorationError('structural_native_start_source_unknown')
        if self._requested_stop():
            self.result.update(outcome='cancelled', reason='cancelled')
            return self.result
        # Mark potentially armed BEFORE submitting input; an input timeout does
        # not prove that the client did not receive the click.
        self.native_armed = True
        self.stop_attempted = False
        self.result.update(native_stopped=False, ready_for_next=False, page_after='unknown')
        await self._control_click(frame, 'start')
        self.allow_refresh = True
        return await self._monitor()

    async def run(self):
        try:
            return await self._run()
        except asyncio.CancelledError as exc:
            # A hard framework cancel prohibits further input. Soft IPC stop is
            # deliberately handled above BEFORE cancellation is latched.
            self._attach_partial(exc)
            raise
        except Exception as exc:
            if self.native_armed and not self.stop_attempted:
                try:
                    await self._stop_native('execution_error')
                except asyncio.CancelledError as cleanup:
                    self._attach_partial(cleanup)
                    raise cleanup from exc
                except Exception as cleanup:
                    logger.error('[StructuralNative] stop unconfirmed: %s', cleanup)
            self._attach_partial(exc)
            raise

    def _attach_partial(self, exc):
        partial = deepcopy(self.result)
        if partial is not None:
            partial.update(trace=deepcopy(self.trace), ready_for_next=False)
            if self.native_armed:
                partial.update(native_stopped=False, page_after='unknown')
            exc.structural_partial_result = partial
            if isinstance(getattr(exc, 'detail', None), dict):
                exc.detail['structural_partial_result'] = partial
            logger.error('[StructuralNativePartial] %s', json.dumps({
                'exception': type(exc).__name__, 'message': str(exc), 'result': partial,
            }, ensure_ascii=False, default=str))


async def run_structural(types, *, app, ocr, interaction, engine, context, control_cid=None):
    if engine is None or context is None:
        raise ValueError('structural exploration requires the current execution context')
    ui = BattleUi(app, ocr, interaction=interaction)
    runtime = StructuralRuntime(types, ui=ui, execution=engine.bind_execution(context), control_cid=control_cid)
    return await runtime.run()
