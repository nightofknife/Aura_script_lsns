"""Framework-backed ordinary room templates for the navigation migration.

This adapter keeps the room reader's domain rules and BGR convention while
using an explicitly supplied Aura VisionService. It does not modify or bind
the shared matcher used by collection/staffing/raw-workshop performance work.
"""
from __future__ import annotations

from collections import OrderedDict
import math
from pathlib import Path

import cv2
import numpy as np

from . import _reforming_center_room_templates as domain


_PLAN = Path(__file__).resolve().parents[2]


class RoomTemplateMatcher:
    """One navigation action owns one matcher and its bounded template cache."""

    def __init__(self, vision):
        if vision is None:
            raise RuntimeError("Room navigation requires an injected vision service")
        self._vision = vision
        self._assets = OrderedDict()

    @staticmethod
    def load_catalog():
        return domain.load_catalog()

    def _asset(self, relative_path):
        path = (_PLAN / relative_path).resolve()
        if not path.is_relative_to(_PLAN.resolve()):
            raise ValueError("Template path must remain inside this plan")
        if path not in self._assets:
            try:
                image = self._vision.load_image_file(path, flags=cv2.IMREAD_COLOR)
            except FileNotFoundError as exc:
                # Aura's loader groups I/O and decode failures. Keep the
                # existing room-reader exception contract at this adapter:
                # np.fromfile raises the original missing/permission error;
                # a readable but undecodable asset uses its relative path.
                np.fromfile(path, dtype=np.uint8)
                raise ValueError(f"Template not readable: {relative_path}") from exc
            if image is None:
                raise ValueError(f"Template not readable: {relative_path}")
            self._assets[path] = image
            if len(self._assets) > 160:
                self._assets.popitem(last=False)
        self._assets.move_to_end(path)
        return self._assets[path]

    def match_asset(self, image, spec, region=None, *, _gray_rois=None):
        region = region or spec.get("search_region") or [0, 0, 1280, 720]
        x, y, w, h = map(int, region)
        x, y = max(x, 0), max(y, 0)
        w, h = min(w, 1280-x), min(h, 720-y)
        target = self._asset(spec["image"])
        source = image[y:y+h, x:x+w, :3]
        # Preserve antialiased gray edges and the existing BGR conversion.
        target = cv2.cvtColor(target, cv2.COLOR_BGR2GRAY)
        gray_rois = _gray_rois if _gray_rois is not None else {}
        key = (x, y, w, h)
        if key not in gray_rois:
            gray_rois[key] = cv2.cvtColor(source, cv2.COLOR_BGR2GRAY)
        source = gray_rois[key]
        th, tw = target.shape[:2]
        if h < th or w < tw or float(target.std()) < 1:
            return None
        # Already-gray inputs keep Aura from interpreting BGR arrays as RGB.
        result = self._vision.find_template(
            source_image=source, template_image=target, threshold=-1.0,
            use_grayscale=False, match_method=cv2.TM_CCOEFF_NORMED,
            preprocess="none",
        )
        error = result.debug_info.get("error")
        if error or not math.isfinite(result.confidence) or result.top_left is None:
            raise ValueError(f"Room template observation failed: {error or 'invalid match result'}")
        score, location = float(result.confidence), result.top_left
        if score < float(spec.get("threshold", .92)):
            return None
        return {"score": score, "rect": [x+location[0], y+location[1], tw, th],
                "center": [x+location[0]+tw//2, y+location[1]+th//2]}

    def room_hits(self, image, catalog, region=(30, 90, 1250, 490)):
        # Migration adapter: retain same-position rival-name rejection. The
        # gray ROI cache belongs only to this frame and is never held on self.
        matches, gray_rois = [], {}
        for room, spec in catalog["rooms"].items():
            variants = [spec["map_title"], *spec.get("map_title_variants", [])]
            hits = [hit for asset in variants
                    if (hit := self.match_asset(image, asset, region, _gray_rois=gray_rois)) is not None]
            if hits:
                matches.append({**max(hits, key=lambda hit: hit["score"]), "room_name": room})
        return [hit for hit in matches if not any(
            other["room_name"] != hit["room_name"]
            and domain._overlap(hit["rect"], other["rect"]) > .65
            and other["score"] >= hit["score"]-.015 for other in matches)]

    def entry_button(self, image, room_spec, button_spec):
        # Selection-outline color/geometry remains a domain observation.
        gray_rois = {}
        for x, y, w, h in domain.selected_rectangles(image):
            title = self.match_asset(image, room_spec["selected_title"], (x, y, w, 65),
                                     _gray_rois=gray_rois)
            if not title:
                continue
            button = self.match_asset(
                image, button_spec,
                (x+w, max(80, y-20), min(260, 1280-x-w), h+40),
                _gray_rois=gray_rois,
            )
            if button:
                return button
        return None

    def destination_confirmed(self, image, spec):
        # Raw-workshop readiness includes polarity, color and production
        # semantics; it stays in its existing domain reader for this batch.
        if spec.get("room_key") in {
                "植物纤维原料车间", "动物纤维原料车间", "缝纫工具原料车间", "人造纤维原料车间"}:
            from . import _raw_workshop_ui
            return _raw_workshop_ui.room_ready(image, spec["room_key"])
        markers = spec.get("destination_markers", [])
        gray_rois = {}
        return len(markers) >= 2 and all(
            self.match_asset(image, marker, _gray_rois=gray_rois) for marker in markers)
