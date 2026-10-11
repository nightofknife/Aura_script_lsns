"""Wishlist-only local-shop identity matching and local selection feedback."""
from __future__ import annotations

from functools import lru_cache
import json
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from ._black_moon_local_prepare_vision import BlackMoonLocalPrepareVision
from ._black_moon_local_shop_vision import _crop, _image, _response


ROOT = Path(__file__).resolve().parents[2] / "templates/black_moon_local_select"


@lru_cache(maxsize=256)
def _pixels(root, filename):
    path = (root / filename).resolve()
    if not path.is_relative_to(root):
        raise ValueError("Selection template path escapes bank")
    with Image.open(path) as image:
        array = np.asarray(image.convert("L" if image.mode == "L" else "RGB")).copy()
    array.setflags(write=False)
    return array


class BlackMoonLocalSelectVision:
    def __init__(self, wanted_ids=(), template_root=None):
        self.root = (Path(template_root) if template_root else ROOT).resolve()
        self.meta = json.loads((self.root / "manifest.json").read_text(encoding="utf-8"))
        self.known_ids = {entry["item_id"] for entry in self.meta["items"]}
        self._all_entries = {entry["item_id"]: entry for entry in self.meta["items"]}
        wanted = set(wanted_ids)
        if not wanted <= self.known_ids:
            raise ValueError("Purchase targets have no native selection template")
        self.entries = {entry["item_id"]: entry for entry in self.meta["items"] if entry["item_id"] in wanted}
        self.prepare = BlackMoonLocalPrepareVision()
        self.VIEWPORT = tuple(self.meta["viewport"])
        self._small = {}
        for item_id, entry in self.entries.items():
            template, mask = self._pair(entry)
            size = (round(template.shape[1] / 4), round(template.shape[0] / 4))
            self._small[item_id] = (cv2.resize(template, size, interpolation=cv2.INTER_AREA),
                                    cv2.resize(mask, size, interpolation=cv2.INTER_NEAREST) if mask is not None else None)

    def _pair(self, entry):
        return (_pixels(self.root, entry["file"]),
                _pixels(self.root, entry["mask_file"]) if entry.get("mask_file") else None)

    def _hit(self, frame, entry, box):
        template, mask = self._pair(entry)
        response = _response(_crop(frame, box), template, mask, "1-TM_SQDIFF_NORMED")
        if not response.size:
            return {"found": False, "score": 0., "center": None}
        _, score, _, point = cv2.minMaxLoc(response)
        rect = (box[0] + point[0], box[1] + point[1], template.shape[1], template.shape[0])
        return {"found": score >= entry.get("threshold", .9), "score": float(score),
                "rect": rect, "center": (rect[0] + rect[2] // 2, rect[1] + rect[3] // 2)}

    def ready(self, frame):
        return self.prepare.observe(_image(frame))["ready"]

    def total_zero(self, frame):
        entry = self.meta["total_zero"]
        return self._hit(frame, entry, tuple(entry["roi"]))["found"]

    def button(self, frame):
        entry = self.meta["confirm_purchase"]
        return self._hit(frame, entry, tuple(entry["roi"]))

    def cards(self, frame):
        _image(frame)
        vx, vy, vw, vh = self.VIEWPORT
        width, height = self.meta["card_size"]
        found = []
        for column in self.meta["card_columns"]:
            for name in ("normal_corner", "selected_corner"):
                entry = self.meta["geometry"][name]
                template, mask = self._pair(entry)
                box = (column - 3, vy, template.shape[1] + 6, vh)
                response = _response(_crop(frame, box), template, mask, "1-TM_SQDIFF_NORMED")
                if not response.size:
                    continue
                # Rounded-corner backgrounds can exceed threshold across many nearby
                # rows. Keep the strongest peak within one physical card height.
                response = response.copy()
                while response.size:
                    _, score, _, (px, py) = cv2.minMaxLoc(response)
                    if score < entry.get("threshold", .9):
                        break
                    rect = (column, vy + int(py), width, height)
                    found.append({"rect": rect, "partial": rect[1] + height > vy + vh,
                                  "geometry_score": float(score)})
                    response[max(0, py - 100):min(response.shape[0], py + 101), :] = -1.
        unique = []
        for card in sorted(found, key=lambda c: c["geometry_score"], reverse=True):
            if not any(abs(card["rect"][0] - other["rect"][0]) < 5
                       and abs(card["rect"][1] - other["rect"][1]) < 8 for other in unique):
                card["state"] = self.state(frame, card) if not card["partial"] else "unknown"
                unique.append(card)
        return sorted(unique, key=lambda c: (c["rect"][1], c["rect"][0]))

    def state(self, frame, card):
        x, y, w, h = card["rect"]
        vx, vy, vw, vh = self.VIEWPORT
        if card.get("partial") or x < vx or y < vy or x + w > vx + vw or y + h > vy + vh:
            return "unknown"
        entry = self.meta["selected"]
        rx, ry, rw, rh = entry["roi_relative"]
        selected = self._hit(frame, entry, (x + rx - 2, y + ry - 2, rw + 4, rh + 4))
        normal = self.meta["geometry"]["normal_corner"]
        template, _ = self._pair(normal)
        corner = self._hit(frame, normal, (x - 2, y - 2, template.shape[1] + 4, template.shape[0] + 4))
        if selected["found"]:
            return "selected"
        return "unselected" if corner["found"] else "unknown"

    def identify(self, frame, card, candidates=None):
        if card.get("partial") or card.get("state") != "unselected":
            return None
        ids = list(self.entries) if candidates is None else [key for key in candidates if key in self.entries]
        if not ids:
            return None
        x, y, _, _ = card["rect"]
        rx, ry, rw, rh = self.meta["icon_search_relative"]
        box = (x + rx, y + ry, rw, rh)
        # Small matching only orders candidates. Full-resolution thresholds are authoritative.
        if len(ids) > 32:
            patch = _crop(frame, box)
            small = cv2.resize(patch, (round(patch.shape[1] / 4), round(patch.shape[0] / 4)), interpolation=cv2.INTER_AREA)
            ranked = []
            for key in ids:
                template, mask = self._small[key]
                response = _response(small, template, mask, "1-TM_SQDIFF_NORMED")
                ranked.append((key, float(response.max()) if response.size else -1.))
            ids = [key for key, _ in sorted(ranked, key=lambda pair: pair[1], reverse=True)[:8]]
        hits = [{"item_id": key, **self._hit(frame, self.entries[key], box)} for key in ids]
        hit = max(hits, key=lambda value: value["score"])
        if not hit["found"]:
            return None
        # Only a positive candidate incurs cheap local lookalike rejection.
        # Disabled lookalikes can reject a hit but can never become purchase targets.
        for key in self.entries[hit["item_id"]].get("confuser_ids", []):
            other = self._hit(frame, self._all_entries[key], box)
            if other["score"] >= hit["score"] - 1e-6:
                return None
        return {"item_id": hit["item_id"], "score": hit["score"]}
