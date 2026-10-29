"""Read-only warehouse observations from one immutable 1280x720 BGR frame.

No capture, input, waits, task calls, or OCR-engine construction live here. Card
rows follow observed border/cube anchors, not a fixed first-row Y coordinate.
"""
from __future__ import annotations

from hashlib import sha256
import json
import math
from pathlib import Path
import re

import cv2
import numpy as np

from ._prison_warehouse_quantity import WarehouseQuantityReader

PLAN = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = PLAN / "data/meta/prison_warehouse_runtime_pc.json"


def _field(value, key, default=None):
    return value.get(key, default) if isinstance(value, dict) else getattr(value, key, default)


def _path(relative):
    path = (PLAN / relative).resolve()
    if not path.is_relative_to(PLAN.resolve()):
        raise ValueError("Warehouse assets must stay inside the plan")
    return path


def load_runtime_config(path=DEFAULT_CONFIG):
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if value.get("reference_client") != [1280, 720]:
        raise ValueError("Warehouse observations require the 1280x720 configuration")
    return value


def _rect(value):
    if len(value) != 4 or any(type(item) is not int for item in value):
        raise ValueError("Warehouse rectangles must be integer XYWH values")
    x, y, width, height = value
    if x < 0 or y < 0 or width < 1 or height < 1 or x+width > 1280 or y+height > 720:
        raise ValueError("Warehouse rectangle is outside the client frame")
    return x, y, width, height


def _crop(image, rect):
    x, y, width, height = map(int, rect)
    return image[y:y+height, x:x+width]


def _inside(inner, outer):
    x, y, width, height = inner
    ox, oy, ow, oh = outer
    return x >= ox and y >= oy and x+width <= ox+ow and y+height <= oy+oh


class PrisonWarehouseUi:
    """Image-only probes; inject the existing OCR service when numbers are needed."""

    def __init__(self, ocr=None, *, config=DEFAULT_CONFIG):
        self.config = dict(config) if isinstance(config, dict) else load_runtime_config(config)
        self.ocr = ocr
        self._assets = {}
        catalogs = self.config["catalogs"]
        self.controls_catalog = json.loads(_path(catalogs["controls"]).read_text(encoding="utf-8"))
        self.goods_catalog = json.loads(_path(catalogs["goods"]).read_text(encoding="utf-8"))
        self.quantity_reader = WarehouseQuantityReader(ocr, catalog=_path(catalogs["quantity"])) if ocr is not None else None
        self.viewport = _rect(self.config["list"]["viewport"])
        self.scan_viewport = _rect(self.config["list"]["scan_viewport"])
        if not _inside(self.scan_viewport, self.viewport):
            raise ValueError("Warehouse scanning area must stay inside its native viewport")
        if self.config["list"]["card_size"] != [84, 84]:
            raise ValueError("Warehouse quantity and goods catalogs require 84x84 cards")
        self.goods = []
        for product_id, entry in self.goods_catalog["products"].items():
            if not entry.get("supported"):
                continue
            template, mask = self._asset(entry["image"]), self._asset(entry["mask"], gray=True)
            if template.shape[:2] != mask.shape or np.count_nonzero(mask) < self.config["goods"]["minimum_mask_pixels"]:
                raise ValueError(f"Invalid warehouse goods mask: {product_id}")
            self.goods.append((str(product_id), entry, template, mask))
        if len(self.goods) < 2:
            raise ValueError("Warehouse identity matching requires competing commodity templates")
        for entry in self.controls_catalog["templates"].values():
            self._asset(entry["image"])
            for variant in entry.get("variants", []):
                self._asset(variant["image"])
        self._asset(self.config["controls"]["back"]["image"], gray=True)

    def _asset(self, relative, gray=False):
        key = (relative, gray)
        if key not in self._assets:
            flag = cv2.IMREAD_GRAYSCALE if gray else cv2.IMREAD_COLOR
            image = cv2.imdecode(np.fromfile(_path(relative), np.uint8), flag)
            if image is None or image.size == 0:
                raise ValueError(f"Unreadable warehouse image asset: {relative}")
            image.flags.writeable = False
            self._assets[key] = image
        return self._assets[key]

    def validate_assets(self):
        return {"success": True, "goods_templates": len(self.goods),
                "controls": len(self.controls_catalog["templates"]), "reference_client": [1280, 720]}

    def _control(self, image, name, *, region=None):
        source = self.controls_catalog["templates"][name]
        config = self.config["controls"]
        variants = [source, *source.get("variants", [])]
        results = []
        for index, variant in enumerate(variants):
            spec = {**source, **variant, **config.get("overrides", {}).get(name, {})}
            search_region = _rect(region if region is not None else
                                  config.get("regions", {}).get(name, spec["search_region"]))
            hit = self._match_control(image, spec, search_region)
            results.append({**hit, "variant_index": index, "image": spec["image"]})
        # A supported alternative must pass its own gates. A high correlation
        # from a rejected color state cannot hide a valid alternative.
        accepted = [hit for hit in results if hit.get("found") is True]
        valid = [hit for hit in results if hit.get("valid")]
        best = max(accepted or valid or results, key=lambda hit: hit.get("score", -1))
        return {**best, "variants": results}

    def _match_control(self, image, spec, region):
        grayscale = spec.get("use_grayscale", False)
        method = spec.get("method", "TM_SQDIFF_NORMED")
        if method not in {"TM_SQDIFF_NORMED", "TM_CCOEFF_NORMED"}:
            raise ValueError(f"Unsupported warehouse control match method: {method}")
        needle = self._asset(spec["image"], gray=grayscale)
        search = _crop(image, region)
        if grayscale:
            search = cv2.cvtColor(search, cv2.COLOR_BGR2GRAY)
        if search.shape[0] < needle.shape[0] or search.shape[1] < needle.shape[1]:
            return {"valid": False, "found": None, "reason": "control_search_smaller_than_template"}
        if method == "TM_CCOEFF_NORMED" and (float(needle.std()) < 1 or float(search.std()) < 1):
            return {"valid": True, "found": False, "score": -1.0,
                    "reason": "control_structure_has_no_contrast"}
        scores = cv2.matchTemplate(search, needle, getattr(cv2, method))
        if not np.isfinite(scores).any():
            return {"valid": False, "found": None, "reason": "control_match_not_finite"}
        difference = method == "TM_SQDIFF_NORMED"
        scores = np.where(np.isfinite(scores), scores, np.inf if difference else -np.inf)
        minimum, maximum, minimum_point, maximum_point = cv2.minMaxLoc(scores)
        point = minimum_point if difference else maximum_point
        x, y = region[0]+point[0], region[1]+point[1]
        height, width = needle.shape[:2]
        score = 1-float(minimum) if difference else float(maximum)
        threshold = spec.get("threshold", self.config["controls"]["score_threshold"])
        # Color gates classify a connection state; page/anchor structure uses
        # grayscale correlation without requiring a particular panel brightness.
        mae_limit = None if grayscale else spec.get("color_mae_limit", self.config["controls"]["color_mae_limit"])
        actual = image[y:y+height, x:x+width]
        mae = None if grayscale else float(np.abs(actual.astype(np.float32)-needle.astype(np.float32)).mean())
        found = score >= threshold and (mae_limit is None or mae <= mae_limit)
        return {"valid": True, "found": bool(found), "score": score, "color_mae": mae,
                "threshold": threshold, "color_mae_limit": mae_limit, "bbox": [x, y, width, height],
                "point": [x+width//2, y+height//2], "search_region": list(region),
                "method": method, "use_grayscale": grayscale}

    def _target_connection(self, image, controls):
        """Locate structure first, then classify only this button's color state."""
        anchor = controls["tp_connection_anchor"]
        states = ("tp_connection_selected", "tp_connection_unselected")
        for name in states:
            if anchor.get("found") is not True:
                controls[name] = {"valid": False, "found": None, "reason": "connection_anchor_unconfirmed"}
                continue
            spec = self.controls_catalog["templates"][name]
            dx, dy = spec["anchor_offset_xy"]
            width, height = spec["bbox"][2:]
            x, y = anchor["bbox"][:2]
            controls[name] = self._control(image, name, region=[x+dx, y+dy, width, height])
            # Click the observed button's center, not a separately searched crop.
            controls[name]["point"] = list(anchor["point"])
        selected, unselected = (controls[name] for name in states)
        if not all(hit.get("valid") is True for hit in (anchor, selected, unselected)):
            return "unknown"
        if selected.get("found") is True and unselected.get("found") is False:
            return "selected"
        if unselected.get("found") is True and selected.get("found") is False:
            return "unselected"
        return "unknown"

    def _back(self, image):
        spec = self.config["controls"]["back"]
        region = _rect(spec["roi"])
        needle = self._asset(spec["image"], gray=True)
        search = cv2.cvtColor(_crop(image, region), cv2.COLOR_BGR2GRAY)
        if search.shape[0] < needle.shape[0] or search.shape[1] < needle.shape[1]:
            return {"valid": False, "found": None, "reason": "back_search_smaller_than_template"}
        scores = cv2.matchTemplate(search, needle, cv2.TM_CCOEFF_NORMED)
        if not np.isfinite(scores).any():
            return {"valid": False, "found": None, "reason": "back_match_not_finite"}
        _, maximum, _, point = cv2.minMaxLoc(np.where(np.isfinite(scores), scores, -1))
        x, y = region[0]+point[0], region[1]+point[1]
        height, width = needle.shape[:2]
        return {"valid": True, "found": bool(maximum >= spec["threshold"]), "score": float(maximum),
                "threshold": spec["threshold"], "bbox": [x, y, width, height],
                "point": [x+width//2, y+height//2], "search_region": list(region)}

    def _capacity(self, image):
        result = {"status": "unknown", "current": None, "limit": None, "remaining": None,
                  "full": None, "text": None, "confidence": None, "raw_results": [], "reasons": []}
        if self.ocr is None:
            result["reasons"].append("ocr_service_not_supplied")
            return result
        spec = self.config["capacity"]
        region = _rect(spec["roi"])
        result["roi"] = list(region)
        rgb = cv2.cvtColor(_crop(image, region), cv2.COLOR_BGR2RGB)
        response = self.ocr.recognize_all(source_image=rgb, recognition_only=True,
                                          use_textline_orientation=False, rotate_tall_crops=False)
        rows = _field(response, "results", ()) or ()
        result["raw_results"] = [{"text": _field(row, "text", ""), "confidence": _field(row, "confidence")} for row in rows]
        if _field(response, "success", True) is False or (_field(response, "debug_info", {}) or {}).get("error"):
            result["reasons"].append("capacity_ocr_failed")
            return result
        if len(rows) != 1:
            result["reasons"].append("capacity_requires_one_result")
            return result
        text, confidence = _field(rows[0], "text", ""), _field(rows[0], "confidence")
        result.update(text=text, confidence=confidence)
        match = re.fullmatch(r"(0|[1-9][0-9]*)/(0|[1-9][0-9]*)", text) if isinstance(text, str) else None
        if match is None:
            result["reasons"].append("capacity_not_a_whole_ascii_fraction")
            return result
        if (isinstance(confidence, bool) or not isinstance(confidence, (int, float))
                or not math.isfinite(confidence) or not spec["minimum_confidence"] <= confidence <= 1):
            result["reasons"].append("capacity_confidence_invalid_or_low")
            return result
        current, limit = map(int, match.groups())
        if limit <= 0 or not 0 <= current <= limit:
            result["reasons"].append("capacity_range_invalid")
            return result
        result.update(status="recognized", current=current, limit=limit,
                      remaining=limit-current, full=current == limit)
        return result

    def _identity(self, card):
        spec = self.config["goods"]
        search = _crop(card, spec["search_crop"])
        ranked = []
        for product_id, entry, needle, mask in self.goods:
            scores = cv2.matchTemplate(search, needle, cv2.TM_CCOEFF_NORMED, mask=mask)
            if not np.isfinite(scores).any():
                continue
            _, maximum, _, point = cv2.minMaxLoc(np.where(np.isfinite(scores), scores, -1))
            ranked.append({"id": product_id, "name": entry["name"], "score": float(maximum),
                           "crop_offset": [spec["search_crop"][0]+point[0], spec["search_crop"][1]+point[1]],
                           "screenshot_calibrated": bool(entry.get("screenshot_calibrated")),
                           "calibrated_sides": entry.get("calibrated_sides", [])})
        ranked.sort(key=lambda item: item["score"], reverse=True)
        if len(ranked) < 2:
            return {"status": "unknown", "id": None, "name": None, "reason": "insufficient_finite_identity_candidates", "candidates": ranked}
        first, second = ranked[:2]
        margin = first["score"]-second["score"]
        recognized = first["score"] >= spec["minimum_score"] and margin >= spec["minimum_margin"]
        return {"status": "recognized" if recognized else "unknown", "id": first["id"] if recognized else None,
                "name": first["name"] if recognized else None, "score": first["score"], "margin": margin,
                "reason": None if recognized else "identity_score_or_margin_unconfirmed",
                "candidates": ranked[:spec["candidate_details"]]}

    def _anchors(self, image):
        config, scan = self.config["list"], self.scan_viewport
        view = _crop(image, scan)
        bar_config = config["bar"]
        white = (view.min(axis=2) >= bar_config["minimum_channel"]).astype(np.uint8)
        horizontal = cv2.morphologyEx(white, cv2.MORPH_OPEN, np.ones((1, bar_config["open_width"]), np.uint8))
        _, _, stats, _ = cv2.connectedComponentsWithStats(horizontal, 8)
        bars = []
        reference_x = config["reference_first_card"][0]
        pitch_x = config["native_pitch_xy"][0]
        for x, y, width, height, area in stats[1:]:
            if not bar_config["minimum_width"] <= width <= bar_config["maximum_width"] or height > bar_config["maximum_height"]:
                continue
            gx, gy = int(scan[0]+x), int(scan[1]+y)
            column = round((gx-reference_x)/pitch_x)
            if not 0 <= column < config["columns"] or abs(gx-(reference_x+column*pitch_x)) > config["column_tolerance_px"]:
                continue
            bars.append({"bbox": [gx, gy, int(width), int(height)], "column": column,
                         "card_bbox": [gx, gy-bar_config["offset_from_card_top"], *config["card_size"]]})

        cubes = []
        cube_config = config["cube"]
        spread = view.max(axis=2).astype(np.int16)-view.min(axis=2).astype(np.int16)
        white_cube = ((view.min(axis=2) >= cube_config["minimum_channel"])
                      & (spread <= cube_config["maximum_channel_spread"])).astype(np.uint8)
        for column in range(config["columns"]):
            column_x = round(reference_x+column*pitch_x)-scan[0]
            left = max(0, column_x+cube_config["x_band_from_card"][0])
            right = min(view.shape[1], column_x+cube_config["x_band_from_card"][1])
            if right <= left:
                continue
            _, _, cube_stats, _ = cv2.connectedComponentsWithStats(white_cube[:, left:right], 8)
            for x, y, width, height, area in cube_stats[1:]:
                if not (cube_config["minimum_width"] <= width <= cube_config["maximum_width"]
                        and cube_config["minimum_height"] <= height <= cube_config["maximum_height"]
                        and cube_config["minimum_area"] <= area <= cube_config["maximum_area"]):
                    continue
                gx, gy = int(scan[0]+left+x), int(scan[1]+y)
                ox, oy = cube_config["offset_from_card"]
                cubes.append({"bbox": [gx, gy, int(width), int(height)], "column": column,
                              "card_bbox": [gx-ox, gy-oy, *config["card_size"]]})
        return bars, cubes

    def _slots(self, image, controls):
        config = self.config["list"]
        bars, cubes = self._anchors(image)
        slots, unresolved, used_cubes = [], [], set()
        tolerance = config["anchor_pair_tolerance_px"]
        for bar in bars:
            bbox = bar["card_bbox"]
            if any(abs(bbox[0]-slot["bbox"][0]) <= tolerance and abs(bbox[1]-slot["bbox"][1]) <= tolerance for slot in slots):
                continue
            matches = [(index, cube) for index, cube in enumerate(cubes)
                       if cube["column"] == bar["column"]
                       and abs(cube["card_bbox"][0]-bbox[0]) <= tolerance
                       and abs(cube["card_bbox"][1]-bbox[1]) <= tolerance]
            visible = _inside(bbox, self.scan_viewport)
            slot = {"bbox": bbox, "column": bar["column"], "fully_visible": visible,
                    "bar_anchor": bar["bbox"], "cube_anchor": matches[0][1]["bbox"] if matches else None,
                    "status": "full" if visible and matches else "unconfirmed"}
            if matches:
                used_cubes.update(index for index, _ in matches)
            if not visible:
                slot["status"] = "clipped_top" if bbox[1] < self.scan_viewport[1] else "clipped_bottom"
                unresolved.append({"reason": slot["status"], "bbox": bbox, "blocking": False, "coverage_only": True})
            elif not matches:
                unresolved.append({"reason": "occupied_border_without_cube_confirmation", "bbox": bbox, "blocking": True})
            slots.append(slot)
        for index, cube in enumerate(cubes):
            if index in used_cubes:
                continue
            bbox = cube["card_bbox"]
            if any(abs(bbox[1]-slot["bbox"][1]) <= tolerance and cube["column"] == slot["column"] for slot in slots):
                continue
            visible = _inside(bbox, self.scan_viewport)
            status = "clipped_top" if bbox[1] < self.scan_viewport[1] else "clipped_bottom" if not visible else "unconfirmed"
            slots.append({"bbox": bbox, "column": cube["column"], "fully_visible": visible,
                          "cube_anchor": cube["bbox"], "bar_anchor": None, "status": status})
            unresolved.append({"reason": status if not visible else "occupied_cube_without_complete_border",
                               "bbox": bbox, "blocking": visible, "coverage_only": not visible})
        slots.sort(key=lambda slot: (slot["bbox"][1], slot["column"]))

        # Search for unaccounted visible artwork/partial cards independently of
        # commodity identity. A failed goods match can never erase occupancy.
        foreground_config = config["unexplained_foreground"]
        view = _crop(image, self.scan_viewport)
        foreground = (cv2.cvtColor(view, cv2.COLOR_BGR2GRAY) >= foreground_config["minimum_gray"]).astype(np.uint8)
        sx, sy, sw, sh = self.scan_viewport
        def exclude(rect, margin):
            x, y, width, height = rect
            left, top = max(0, x-sx-margin), max(0, y-sy-margin)
            right, bottom = min(sw, x+width-sx+margin), min(sh, y+height-sy+margin)
            if right > left and bottom > top:
                foreground[top:bottom, left:right] = 0
        for slot in slots:
            exclude(slot["bbox"], foreground_config["known_box_margin_px"])
        empty = controls["product_empty"]
        if empty.get("found") is True:
            exclude(empty["bbox"], foreground_config["empty_marker_margin_px"])
            # The calibrated EMPTY crop excludes its two short underline
            # decorations. Ignore only those narrow, marker-relative bands;
            # never erase the surrounding warehouse area on an EMPTY match.
            marker_x, marker_y = empty["bbox"][:2]
            for dx, dy, width, height in foreground_config.get("empty_decoration_bands", []):
                exclude([marker_x+dx, marker_y+dy, width, height], 0)
        _, _, stats, _ = cv2.connectedComponentsWithStats(foreground, 8)
        residue = []
        for x, y, width, height, area in stats[1:]:
            if area >= foreground_config["minimum_component_area"]:
                residue.append({"reason": "unexplained_viewport_foreground", "bbox": [sx+int(x), sy+int(y), int(width), int(height)],
                                "pixels": int(area), "blocking": True})
        unresolved.extend(residue[:foreground_config["maximum_regions"]])
        if len(residue) > foreground_config["maximum_regions"]:
            unresolved.append({"reason": "additional_unexplained_regions", "count": len(residue), "blocking": True})

        rows = []
        for slot in slots:
            top = slot["bbox"][1]
            row = next((row for row in rows if abs(row["top"]-top) <= config["row_cluster_tolerance_px"]), None)
            if row is None:
                row = {"top": top, "columns": [], "full_columns": [], "partial_columns": []}
                rows.append(row)
            row["columns"].append(slot["column"])
            row["full_columns" if slot["status"] == "full" else "partial_columns"].append(slot["column"])
        for row in rows:
            present = set(row["columns"])
            for column in range(max(present, default=-1)):
                if column not in present:
                    unresolved.append({"reason": "unconfirmed_hole_before_occupied_column", "column": column,
                                       "row_top": row["top"], "blocking": True})
        for before, after in zip(rows, rows[1:]):
            delta = after["top"]-before["top"]
            if abs(delta-config["native_pitch_xy"][1]) > config["row_pitch_tolerance_px"]:
                unresolved.append({"reason": "row_spacing_or_missing_row_unconfirmed", "row_tops": [before["top"], after["top"]], "blocking": True})
        top = min((slot["bbox"][1] for slot in slots), default=None)
        bottom = max((slot["bbox"][1]+slot["bbox"][3] for slot in slots), default=None)
        geometry = {"slots": slots, "visible_rows": rows,
                    "partial_rows": [row for row in rows if row["partial_columns"]],
                    "first_visible_y": top, "last_visible_y": bottom,
                    "leading_clear_pixels": max(0, top-sy) if top is not None else None,
                    "trailing_clear_pixels": max(0, sy+sh-bottom) if bottom is not None else None,
                    "reference_top_aligned_hint": top is not None and abs(top-config["reference_first_card"][1]) <= config["reference_top_tolerance_px"],
                    "scroll_bounds": {"top": None, "bottom": None},
                    "bar_anchor_count": len(bars), "cube_anchor_count": len(cubes),
                    "native_pitch_xy": config["native_pitch_xy"], "viewport": list(self.viewport),
                    "scan_viewport": list(self.scan_viewport), "global_coverage_complete": False}
        return slots, unresolved, geometry

    def probe(self, image_bgr, read_quantities=False, goods_filter=None, read_list=True):
        result = {"valid": False, "page": "unknown", "connection": "unknown", "tp_connection": "unknown", "controls": {},
                  "capacity": {"status": "not_requested", "current": None, "limit": None, "remaining": None, "full": None},
                  "cards": [], "unresolved": [], "list_state": "unknown", "signature": None,
                  "content_signature": None, "geometry": {}, "viewport": list(self.viewport),
                  "scan_viewport": list(self.scan_viewport), "empty_evidence": {}, "global_coverage_complete": False}
        if not isinstance(image_bgr, np.ndarray) or image_bgr.dtype != np.uint8 or image_bgr.shape != (720, 1280, 3):
            result["unresolved"].append({"reason": "requires_1280x720_uint8_bgr_frame", "blocking": True})
            return result
        result["valid"] = True
        connection_states = {"tp_connection_selected", "tp_connection_unselected"}
        controls = {name: self._control(image_bgr, name) for name in self.controls_catalog["templates"]
                    if name not in connection_states}
        tp_connection = self._target_connection(image_bgr, controls)
        controls["back"] = self._back(image_bgr)
        result["controls"] = controls
        found = lambda name: controls[name].get("found") is True
        overview = found("overview_warehouse_shortcut") and found("overview_event_list") and found("back")
        warehouse = (found("back") and all(found(name) for name in (
            "train_title_identity", "material_title_identity", "product_title_identity")))
        if overview != warehouse:
            result["page"] = "overview" if overview else "warehouse"
        if result["page"] != "warehouse":
            return result
        result["tp_connection"] = tp_connection
        if tp_connection == "selected":
            result["connection"] = "train_product"
        elif tp_connection == "unselected":
            result["connection"] = "not_train_product"
        if not read_list:
            return result
        if read_quantities:
            result["capacity"] = self._capacity(image_bgr)
        slots, unresolved, geometry = self._slots(image_bgr, controls)
        result["unresolved"], result["geometry"] = unresolved, geometry
        wanted = None if goods_filter is None else {str(value) for value in goods_filter}
        for slot in slots:
            if slot["status"] != "full":
                continue
            bbox = slot["bbox"]
            card_bgr = _crop(image_bgr, bbox)
            identity = self._identity(card_bgr)
            if identity["id"] is None:
                unresolved.append({"reason": "occupied_card_identity_unknown", "bbox": bbox, "identity": identity, "blocking": True})
                continue
            target = wanted is None or identity["id"] in wanted
            quantity_result = {"status": "not_requested", "value": None}
            if read_quantities and target and result["connection"] == "train_product":
                if self.quantity_reader is None:
                    quantity_result = {"status": "unknown", "value": None, "reasons": ["ocr_service_not_supplied"]}
                else:
                    quantity_result = self.quantity_reader.read_card(cv2.cvtColor(card_bgr, cv2.COLOR_BGR2RGB))
                if quantity_result.get("value") is None:
                    unresolved.append({"reason": "target_card_quantity_unknown", "bbox": bbox, "id": identity["id"],
                                       "quantity_result": quantity_result, "blocking": True})
            result["cards"].append({"id": identity["id"], "name": identity["name"], "quantity": quantity_result.get("value"),
                                    "quantity_result": quantity_result, "bbox": bbox,
                                    "point": [bbox[0]+bbox[2]//2, bbox[1]+bbox[3]//2], "target": target,
                                    "column": slot["column"], "identity": identity, "fully_visible": True})
        empty_positive = found("product_empty")
        empty_confirmed = empty_positive and not slots and not unresolved
        result["empty_evidence"] = {"marker": controls["product_empty"], "occupied_slots": len(slots),
                                    "unresolved_count": len(unresolved), "confirmed_visible_empty": empty_confirmed}
        if unresolved:
            result["list_state"] = "partial"
        elif empty_confirmed:
            result["list_state"] = "empty"
        elif slots:
            result["list_state"] = "populated"
        else:
            result["unresolved"].append({"reason": "no_occupied_anchor_and_no_positive_empty_evidence", "blocking": True})
        view = _crop(image_bgr, self.scan_viewport)
        config = self.config["list"]
        sample = cv2.resize(view, tuple(config["signature_size"]), interpolation=cv2.INTER_AREA)
        quantized = sample // config["signature_quantum"]
        result["signature"] = sha256(quantized.tobytes()).hexdigest()
        content = [{"id": card["id"], "quantity": card["quantity"], "bbox": card["bbox"]} for card in result["cards"]]
        result["content_signature"] = sha256(json.dumps(content, sort_keys=True).encode("utf-8")).hexdigest()
        geometry["identity_rows"] = [{"id": card["id"], "bbox": card["bbox"], "quantity": card["quantity"]} for card in result["cards"]]
        return result


WarehouseUi = PrisonWarehouseUi
