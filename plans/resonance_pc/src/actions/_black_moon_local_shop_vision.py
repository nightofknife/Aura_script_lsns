"""Native LOCAL-store observations; no OCR, input, or runtime asset writes."""

from __future__ import annotations

from copy import deepcopy
from functools import lru_cache
import json
from pathlib import Path

import cv2
import numpy as np
from PIL import Image


PLAN_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE_ROOT = PLAN_ROOT / "templates/black_moon_local_shop"
CATALOG_PATH = PLAN_ROOT / "data/meta/black_moon_local_shop_catalog.json"


class BlackMoonLocalShopRecognitionError(RuntimeError):
    """A frame or native resource cannot support a safe LOCAL observation."""

    def __init__(self, code: str, detail: str):
        self.code, self.detail = code, detail
        super().__init__(f"{code}: {detail}")


def _entries(value):
    if isinstance(value, dict):
        return [dict(entry, name=name) for name, entry in value.items()]
    return list(value)


@lru_cache(maxsize=4)
def _resources(root: Path):
    try:
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        if manifest["reference_client"] != [1280, 720]:
            raise ValueError("Only RGB 1280x720 is supported")
        cards = manifest["cards"]
        entries = _entries(manifest["templates"])
        entries += cards.get("anchors", []) + cards.get("frame_variants", [])
        entries += cards.get("items", manifest.get("items", []))
        files = {entry["file"] for entry in entries}
        files.update(entry["mask_file"] for entry in entries if entry.get("mask_file"))
        pixels = {}
        for filename in files:
            path = (root / filename).resolve()
            if not path.is_relative_to(root):
                raise ValueError(f"Resource outside template root: {filename}")
            with Image.open(path) as asset:
                mode = "RGBA" if asset.mode == "RGBA" else "L" if asset.mode == "L" else "RGB"
                array = np.array(asset.convert(mode))
            array.setflags(write=False)
            pixels[filename] = array
        if len(cards["columns"]) != 2 or not cards["anchors"] or not cards["frame_variants"]:
            raise ValueError("Native two-column geometry, anchors, and frames are required")
        return manifest, pixels
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise BlackMoonLocalShopRecognitionError("template_load_failed", str(exc)) from exc


@lru_cache(maxsize=4)
def _catalog(path: Path, signature: tuple[int, int]):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        rows = data if isinstance(data, list) else data["products"]
        products = {row["item_id"]: row for row in rows}
        if not products or len(products) != len(rows):
            raise ValueError("Canonical product IDs must be nonempty and unique")
        return products
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise BlackMoonLocalShopRecognitionError("catalog_load_failed", str(exc)) from exc


def _image(frame):
    if not isinstance(frame, np.ndarray) or frame.shape != (720, 1280, 3) or frame.dtype != np.uint8:
        raise BlackMoonLocalShopRecognitionError("invalid_image", "Expected uint8 RGB 1280x720")
    return frame


def _clip(box, bounds=(0, 0, 1280, 720)):
    x, y, w, h = map(int, box)
    bx, by, bw, bh = map(int, bounds)
    left, top = max(x, bx), max(y, by)
    return left, top, max(0, min(x + w, bx + bw) - left), max(0, min(y + h, by + bh) - top)


def _crop(frame, box):
    x, y, w, h = _clip(box)
    return frame[y:y + h, x:x + w]


def _prepare(pixels, mode="rgb"):
    if pixels.ndim == 2:
        return pixels
    pixels = pixels[:, :, :3]
    if mode == "gray":
        return cv2.cvtColor(pixels, cv2.COLOR_RGB2GRAY)
    if mode == "bright":
        spread = np.ptp(pixels.astype(np.int16), axis=2)
        return np.where((pixels.min(axis=2) >= 205) & (spread < 38), 255, 0).astype(np.uint8)
    if mode == "dark":
        return np.where(pixels.max(axis=2) < 90, 255, 0).astype(np.uint8)
    if mode == "yellow":
        return cv2.inRange(cv2.cvtColor(pixels, cv2.COLOR_RGB2HSV), (16, 160, 170), (40, 255, 255))
    if mode not in (None, "rgb"):
        raise BlackMoonLocalShopRecognitionError("template_load_failed", f"Unsupported preprocessing {mode}")
    return pixels


def _response(source, template, mask=None, method=None):
    if source.shape[0] < template.shape[0] or source.shape[1] < template.shape[1]:
        return np.empty((0, 0), dtype=np.float32)
    if source.ndim != template.ndim:
        raise BlackMoonLocalShopRecognitionError("template_load_failed", "Source/template channel mismatch")
    # Squared difference retains color and brightness, unlike correlation alone.
    squared = method not in ("TM_CCOEFF_NORMED", "ccoeff")
    algorithm = cv2.TM_SQDIFF_NORMED if squared else cv2.TM_CCOEFF_NORMED
    if mask is not None and (mask.shape != template.shape[:2] or not np.any(mask > 0)):
        raise BlackMoonLocalShopRecognitionError("template_load_failed", "Empty or incompatible native mask")
    valid = template[mask > 0] if mask is not None else template.reshape((-1,) + template.shape[2:])
    if not np.any(valid):
        raise BlackMoonLocalShopRecognitionError("template_load_failed", "Native template has zero matching energy")
    if not squared and np.max(np.std(valid.astype(np.float32), axis=0)) < 1e-5:
        raise BlackMoonLocalShopRecognitionError("template_load_failed", "Constant native template cannot establish correlation")
    try:
        result = cv2.matchTemplate(np.ascontiguousarray(source), np.ascontiguousarray(template), algorithm, mask=mask)
    except cv2.error as exc:
        raise BlackMoonLocalShopRecognitionError("template_load_failed", str(exc)) from exc
    if squared:
        result = 1. - result
    return np.nan_to_num(result, nan=-1., posinf=-1., neginf=-1.)


class BlackMoonLocalShopVision:
    """Lazy cached native recognition, independent of product slots and prices.

    All rectangles are client RGB coordinates (x, y, width, height). A partial
    card retains its logical rectangle and is never assigned a clickable item.
    Native production assets have not been live- or screenshot-match validated.
    """

    def __init__(self, template_root: Path | None = None, catalog_path: Path | None = None):
        self.template_root = (TEMPLATE_ROOT if template_root is None else Path(template_root)).resolve()
        self.catalog_path = (CATALOG_PATH if catalog_path is None else Path(catalog_path)).resolve()

    @property
    def _data(self):
        return _resources(self.template_root)

    @property
    def metadata(self):
        manifest = deepcopy(self._data[0])
        manifest["cards"].setdefault("native_viewport", manifest["cards"].get(
            "raw_native_viewport", manifest["cards"]["viewport"]))
        manifest["cards"]["viewport"] = list(self.VIEWPORT)
        return manifest

    @property
    def VIEWPORT(self):
        spec = self._data[0]["cards"]
        x, y, w, h = spec["viewport"]
        occlusion = spec.get("viewport_batch_occlusion")
        if occlusion is not None:
            ox, oy, ow, oh = occlusion
            left = min(spec["columns"])
            right = max(spec["columns"]) + spec["size"][0]
            if ox <= left + 3 and ox + ow >= right - 3 and y < oy < y + h and oh > 0:
                h = oy - y
        return tuple(map(int, (x, y, w, h)))

    @property
    def _templates(self):
        return {entry["name"]: entry for entry in _entries(self._data[0]["templates"])}

    def _pixels(self, entry):
        arrays = self._data[1]
        raw = arrays[entry["file"]]
        mask = arrays[entry["mask_file"]] if entry.get("mask_file") else None
        if raw.ndim == 3 and raw.shape[2] == 4:
            if mask is None:
                mask = raw[:, :, 3]
            raw = raw[:, :, :3]
        if mask is not None and mask.ndim == 3:
            mask = cv2.cvtColor(mask[:, :, :3], cv2.COLOR_RGB2GRAY)
        return _prepare(raw, entry.get("preprocess", "rgb")), mask

    def _entry_response(self, frame, entry, box):
        template, mask = self._pixels(entry)
        source = _prepare(_crop(frame, box), entry.get("preprocess", "rgb"))
        return _response(source, template, mask, entry.get("method"))

    def _match_entry(self, frame, entry, box):
        box = _clip(box)
        response = self._entry_response(frame, entry, box)
        if not response.size:
            return {"found": False, "score": 0., "center": None, "rect": None}
        _, score, _, point = cv2.minMaxLoc(response)
        raw = self._data[1][entry["file"]]
        rect = (box[0] + point[0], box[1] + point[1], raw.shape[1], raw.shape[0])
        found = score >= entry.get("threshold", .88)
        limit = entry.get("color_mae_limit")
        if found and limit is not None and raw.ndim == 3:
            observed = _crop(frame, rect).astype(np.float32)
            expected = raw[:, :, :3].astype(np.float32)
            _, mask = self._pixels(entry)
            delta = np.abs(observed - expected)
            found = float(delta[mask > 0].mean() if mask is not None else delta.mean()) <= limit
        return {"found": bool(found), "score": float(score), "rect": rect if found else None,
                "center": (rect[0] + rect[2] // 2, rect[1] + rect[3] // 2) if found else None}

    def _match_box(self, frame, name, box):
        entry = self._templates.get(name)
        if entry is None:
            raise BlackMoonLocalShopRecognitionError("unknown_template", name)
        return self._match_entry(frame, entry, box)

    def match(self, frame, name):
        frame = _image(frame)
        entry = self._templates.get(name)
        if entry is None:
            raise BlackMoonLocalShopRecognitionError("unknown_template", name)
        return self._match_entry(frame, entry, entry["roi"])

    def is_local(self, frame):
        frame = _image(frame)
        if self.match(frame, "reward")["found"] or not self.match(frame, "shop_page")["found"]:
            return False
        selected = self.match(frame, "local_tab_selected")
        unselected = self.match(frame, "local_tab_unselected")
        margin = self._data[0].get("state_comparisons", {}).get("local_tab", {}).get(
            "minimum_margin", self._data[0].get("state_minimum_margin", .035))
        return bool(selected["found"] and selected["score"] - unselected["score"] >= margin)

    def mode(self, frame):
        """Return batch/single only on an unobscured LOCAL page; else unknown."""
        if not self.is_local(frame):
            return "unknown"
        on, off = self.match(frame, "batch_on"), self.match(frame, "batch_off")
        margin = self._data[0].get("state_comparisons", {}).get("batch", {}).get(
            "minimum_margin", self._data[0].get("state_minimum_margin", .035))
        if on["found"] and on["score"] - off["score"] >= margin:
            return "batch"
        if off["found"] and off["score"] - on["score"] >= margin:
            return "single"
        return "unknown"

    def _products(self):
        try:
            stat = self.catalog_path.stat()
        except OSError as exc:
            raise BlackMoonLocalShopRecognitionError("catalog_load_failed", str(exc)) from exc
        products = _catalog(self.catalog_path, (stat.st_mtime_ns, stat.st_size))
        spec = self._data[0]["cards"]
        bank = spec.get("items", self._data[0].get("items", []))
        covered = {entry["item_id"] for entry in bank}
        if covered != set(products):
            raise BlackMoonLocalShopRecognitionError(
                "catalog_templates_incomplete", "Native item bank must cover exactly the canonical product catalog")
        return products

    def _card_origins(self, frame):
        spec = self._data[0]["cards"]
        vx, vy, vw, vh = self.VIEWPORT
        width, height = spec["size"]
        tolerance = spec.get("position_tolerance", 3)
        candidates = []
        for anchor in spec["anchors"]:
            raw = self._data[1][anchor["file"]]
            ox, oy = anchor["offset"]
            for column in spec["columns"]:
                box = _clip((column + ox - tolerance, vy, raw.shape[1] + tolerance * 2, vh), self.VIEWPORT)
                response = self._entry_response(frame, anchor, box)
                if not response.size:
                    continue
                maxima = response == cv2.dilate(response, np.ones((9, 3), dtype=np.uint8))
                ys, xs = np.where(maxima & (response >= anchor.get("threshold", .90)))
                for px, py in zip(xs, ys):
                    x, y = int(box[0] + px - ox), int(box[1] + py - oy)
                    rect = (x, y, width, height)
                    visible = _clip(rect, self.VIEWPORT)
                    if abs(x - column) > tolerance or visible[3] < spec.get("minimum_visible_height", 12):
                        continue
                    candidates.append({"rect": rect, "anchor_score": float(response[py, px])})
        unique = []
        for candidate in sorted(candidates, key=lambda row: row["anchor_score"], reverse=True):
            x, y, _, _ = candidate["rect"]
            if not any(abs(x - old["rect"][0]) <= tolerance * 2
                       and abs(y - old["rect"][1]) <= tolerance * 2 for old in unique):
                unique.append(candidate)
        return unique

    def _identify(self, frame, rect, products, selected=False):
        spec = self._data[0]["cards"]
        ix, iy, iw, ih = spec["icon_rect"]
        tolerance = spec.get("icon_position_tolerance", 2)
        box = (rect[0] + ix - tolerance, rect[1] + iy - tolerance,
               iw + tolerance * 2, ih + tolerance * 2)
        scores, thresholds, accepted = {}, {}, {}
        for entry in spec.get("items", self._data[0].get("items", [])):
            if entry.get("state") not in (None, "selected" if selected else "normal"):
                continue
            item_id = entry["item_id"]
            if item_id not in products:
                raise BlackMoonLocalShopRecognitionError("catalog_template_mismatch", str(item_id))
            result = self._match_entry(frame, entry, box)
            if result["score"] > scores.get(item_id, -1.):
                scores[item_id] = result["score"]
                thresholds[item_id] = entry.get("threshold", spec.get("item_threshold", .88))
                accepted[item_id] = result["found"]
        ranked = sorted(scores.items(), key=lambda row: row[1], reverse=True)
        if not ranked:
            return None, 0., None
        item_id, score = ranked[0]
        margin = score - ranked[1][1] if len(ranked) > 1 else 1.
        if not accepted[item_id] or score < thresholds[item_id] or margin < spec.get("minimum_item_margin", .04):
            return None, float(score), None
        return item_id, float(score), products[item_id]["quality"]

    def detect_cards(self, frame):
        frame = _image(frame)
        if not self.is_local(frame):
            raise BlackMoonLocalShopRecognitionError("not_local", "LOCAL tab/page missing or reward overlay present")
        if self.mode(frame) == "unknown":
            raise BlackMoonLocalShopRecognitionError("mode_uncertain", "Batch toggle state is ambiguous or obscured")
        products = self._products()
        spec = self._data[0]["cards"]
        candidates = self._card_origins(frame)
        if not candidates:
            raise BlackMoonLocalShopRecognitionError("layout_uncertain", "No native card boundaries; not proof of an empty store")
        cards = []
        for candidate in candidates:
            rect = candidate["rect"]
            partial = _clip(rect, self.VIEWPORT) != rect
            tolerance = spec.get("position_tolerance", 3)
            frame_score = None
            if not partial:
                search = _clip((rect[0] - tolerance, rect[1] - tolerance,
                                rect[2] + tolerance * 2, rect[3] + tolerance * 2), self.VIEWPORT)
                frames = [(self._match_entry(frame, entry, search), entry) for entry in spec["frame_variants"]]
                result, _ = max(frames, key=lambda pair: pair[0]["score"])
                frame_score = result["score"]
                if not result["found"]:
                    raise BlackMoonLocalShopRecognitionError("card_frame_uncertain", f"Native boundary at {rect} has no confirmed frame")
                rect = result["rect"]
                partial = _clip(rect, self.VIEWPORT) != rect
            states = {}
            for name in ("selected", "sold_out"):
                relative = spec[f"{name}_roi"]
                box = (rect[0] + relative[0] - tolerance, rect[1] + relative[1] - tolerance,
                       relative[2] + tolerance * 2, relative[3] + tolerance * 2)
                visible_box = _clip(box, self.VIEWPORT)
                states[name] = bool(self._match_box(frame, name, visible_box)["found"])
            item_id, item_score, quality = (None, 0., None)
            if not partial and not states["sold_out"]:
                item_id, item_score, quality = self._identify(frame, rect, products, states["selected"])
            card = {"rect": tuple(map(int, rect)), "partial": bool(partial), **states,
                    "item_id": item_id, "item_score": item_score, "quality": quality,
                    "anchor_score": candidate["anchor_score"], "frame_score": frame_score,
                    "clickable": bool(not partial and not states["sold_out"] and item_id is not None)}
            if not partial and not states["sold_out"]:
                try:
                    card["fingerprint"] = self.fingerprint(frame, card)
                except BlackMoonLocalShopRecognitionError:
                    card["fingerprint"] = None
            else:
                card["fingerprint"] = None
            if not any(abs(rect[0] - old["rect"][0]) <= tolerance * 2
                       and abs(rect[1] - old["rect"][1]) <= tolerance * 2 for old in cards):
                cards.append(card)
        for column in spec["columns"]:
            ordered = sorted((card for card in cards if abs(card["rect"][0] - column) <= tolerance),
                             key=lambda card: card["rect"][1])
            for before, after in zip(ordered, ordered[1:]):
                if before["rect"][1] + before["rect"][3] > after["rect"][1] + tolerance:
                    raise BlackMoonLocalShopRecognitionError("layout_uncertain", "Native card boundaries overlap inconsistently")
        rows = []
        for card in sorted(cards, key=lambda row: (row["rect"][1], row["rect"][0])):
            if not rows or abs(card["rect"][1] - rows[-1][0]["rect"][1]) > tolerance * 2:
                rows.append([])
            rows[-1].append(card)
        return [card for row in rows for card in sorted(row, key=lambda card: card["rect"][0])]

    def fingerprint(self, frame, card):
        """Interior visual evidence only, not identity or purchase/completion proof."""
        frame = _image(frame)
        try:
            rect = tuple(map(int, card["rect"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise BlackMoonLocalShopRecognitionError("invalid_card", "Missing card rectangle") from exc
        if len(rect) != 4 or rect[2] <= 0 or rect[3] <= 0:
            raise BlackMoonLocalShopRecognitionError("invalid_card", "Invalid card dimensions")
        if card.get("partial") or card.get("sold_out") or card.get("occluded") or _clip(rect, self.VIEWPORT) != rect:
            raise BlackMoonLocalShopRecognitionError("fingerprint_unavailable", "Clipped, sold-out, or obscured card")
        if not self.is_local(frame):
            raise BlackMoonLocalShopRecognitionError("fingerprint_unavailable", "LOCAL page is obscured or missing")
        spec = self._data[0]["cards"]
        relative = spec.get("fingerprint_box", (32, 32, rect[2] - 44, rect[3] - 40))
        x, y, w, h = relative
        patch = _crop(frame, (rect[0] + x, rect[1] + y, w, h)).copy()
        for ex, ey, ew, eh in spec.get("fingerprint_exclude", []):
            cx, cy, cw, ch = _clip((ex - x, ey - y, ew, eh), (0, 0, w, h))
            patch[cy:cy + ch, cx:cx + cw] = 0
        small = cv2.resize(patch, (48, 28), interpolation=cv2.INTER_AREA).astype(np.float32) / 255.
        deviation = float(small.std())
        if deviation < .035:
            raise BlackMoonLocalShopRecognitionError("fingerprint_unavailable", "Interior patch has insufficient contrast")
        # The native selected overlay uniformly darkens the interior; geometry,
        # commodity text, and pack differences must survive that state change.
        return (small - float(small.mean())) / deviation

    @staticmethod
    def same_fingerprint(a, b):
        """Conservative overlap comparison; identical-looking packs can still recur."""
        if not isinstance(a, np.ndarray) or not isinstance(b, np.ndarray) or a.shape != (28, 48, 3) or b.shape != a.shape:
            return False
        if not np.isfinite(a).all() or not np.isfinite(b).all():
            return False
        av, bv = a.astype(np.float64).ravel(), b.astype(np.float64).ravel()
        if min(float(av.std()), float(bv.std())) < .035:
            return False
        difference = float(np.abs(av - bv).mean())
        av, bv = av - av.mean(), bv - bv.mean()
        norm = float(np.linalg.norm(av) * np.linalg.norm(bv))
        return bool(norm > 1e-8 and float(np.dot(av, bv) / norm) >= .992 and difference <= .08)
