"""Read-only, one-frame observations for the PC Eternal Scuffle task.

All image matching is delegated to the injected VisionService.  The caller owns
polling, temporal confirmation and input; this class never sleeps or clicks.
Coordinates are client-relative.  Reference boxes describe the supplied 720p
captures; a detected foreground title anchors the cards on each observation.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import cv2
import numpy as np


# Ordered foreground-first. A coin dialog still contains PLAY GAME behind it.
_SCENES = (
    ("abandon_confirm", ("abandon_dialog_prompt",)),
    ("coin", ("C01", "C07")),
    ("defeat", ("battle_lose_title",)),
    ("victory", ("B05",)),
    ("settlement", ("settlement_reward_prompt", "settlement_count_label")),
    ("assign", ("equip_assign_prompt",)),
    ("captain", ("D05",)),
    ("initial_equipment", ("D03",)),
    ("role_select", ("choose_role_phase",)),
    ("loot_select", ("loot_choose_prompt", "loot_victory_title")),
    ("stage", ("B01", "B04")),
    ("home", ("H01", "H02")),
)
_CONTROLS = {
    "home": {"play": "H02"},
    "coin": {"min": "C02", "plus": "C04", "max": "C05", "confirm": "C07"},
    "captain": {"confirm": "D06"},
    "assign": {"confirm": "equip_confirm_button"},
    "stage": {"start": "B01", "abandon": "B02"},
    "victory": {"next": "B06"},
    "defeat": {"next": "battle_lose_next_button"},
    "abandon_confirm": {"confirm": "abandon_confirm_button_core"},
    "settlement": {"back": "settlement_return_button"},
}
_SLOTS = ("attack", "defense", "support")
_TEAM_CARDS = ((229, 35), (494, 35), (758, 35), (229, 297), (494, 297))


class ScuffleVision:
    """Synchronous probes; call from the task's polling worker thread.

    ``observe`` returns ``valid``, ``scene``, ``controls``, ``boxes``,
    ``_image`` and ``diagnostics``. Match dictionaries have ``center``, ``rect``
    and ``score``. ``read_candidates`` and ``read_team`` return an empty list
    when *any* identity is uncertain, retaining top candidates in diagnostics.
    They use exactly the observation's image, never capture another frame.
    """

    def __init__(self, app: Any, vision: Any, catalog: dict, plan_root: str | Path):
        self.app, self.vision = app, vision
        self.catalog, self.plan_root = catalog, Path(plan_root)
        self._images: dict[str, np.ndarray] = {}
        self._variants: dict[tuple, tuple[list, list, list]] = {}

    def _image(self, relative: str) -> np.ndarray:
        if relative not in self._images:
            path = (self.plan_root / relative).resolve()
            if not path.is_relative_to(self.plan_root.resolve()):
                raise ValueError(f"Scuffle template escapes plan: {relative}")
            image = self.vision.load_image_file(str(path), cv2.IMREAD_UNCHANGED)
            if image is None:
                raise ValueError(f"Cannot load Scuffle template: {relative}")
            if image.ndim == 3:
                image = cv2.cvtColor(image, cv2.COLOR_BGRA2RGBA if image.shape[2] == 4 else cv2.COLOR_BGR2RGB)
            self._images[relative] = image
        return self._images[relative]

    @staticmethod
    def _crop(image: np.ndarray, box: list | tuple) -> tuple[np.ndarray, tuple[int, int]]:
        x, y, w, h = map(int, box)
        x, y = max(0, x), max(0, y)
        return image[y:min(image.shape[0], y+h), x:min(image.shape[1], x+w)], (x, y)

    def _control_spec(self, key: str) -> dict:
        return self.catalog.get("controls", {}).get(key, {})

    def _reference(self, key: str) -> list[int]:
        spec = self._control_spec(key)
        box = spec.get("client_box") or spec.get("estimated_client_box")
        if box:
            return list(map(int, box))
        box = spec.get("source_box", spec.get("box"))
        if not box:
            raise ValueError(f"Scuffle control has no reference box: {key}")
        x, y, w, h = box
        return [int(x)-25, int(y)-56, int(w), int(h)]

    @staticmethod
    def _match_dict(result: Any, offset=(0, 0)) -> dict | None:
        if not result.found or result.rect is None or not np.isfinite(result.confidence):
            return None
        x, y, w, h = map(int, result.rect)
        x += offset[0]
        y += offset[1]
        return {"center": [x+w//2, y+h//2], "rect": [x, y, w, h], "score": float(result.confidence)}

    def _control(self, image: np.ndarray, key: str, diagnostics: dict) -> dict | None:
        spec = self._control_spec(key)
        if not spec:
            diagnostics.setdefault("missing_controls", []).append(key)
            return None
        x, y, w, h = self._reference(key)
        source, offset = self._crop(image, (x-28, y-14, w+56, h+28))
        template = self._image(spec["template"])
        mask = self._image(spec["mask"]) if spec.get("mask") else None
        best, best_scale = None, 1.
        # Two frozen reference profiles account for the first 11 supplied
        # captures' 1268px viewport. Never resize a live capture implicitly.
        scales = (1., 1280/1268) if int(spec.get("source_image", 0)) <= 11 else (1.,)
        for scale in scales:
            size = (round(template.shape[1]*scale), template.shape[0])
            result = self.vision.find_template(
                source, template if scale == 1 else cv2.resize(template, size, interpolation=cv2.INTER_LINEAR),
                mask_image=mask if mask is None or scale == 1 else cv2.resize(mask, size, interpolation=cv2.INTER_NEAREST),
                threshold=float(spec.get("threshold", .90)), use_grayscale=True,
            )
            if best is None or result.confidence > best.confidence:
                best, best_scale = result, scale
        diagnostics.setdefault("control_scores", {})[key] = float(best.confidence)
        match = self._match_dict(best, offset)
        if match:
            match["scale_x"] = best_scale
        return match

    def observe(self) -> dict:
        observation = {"valid": False, "scene": "unknown", "controls": {}, "boxes": [], "_image": None, "diagnostics": {}}
        try:
            capture = self.app.capture()
        except Exception as exc:
            observation["diagnostics"]["capture_error"] = str(exc)
            return observation
        image = getattr(capture, "image", None)
        flags = list(getattr(capture, "quality_flags", []) or [])
        observation["diagnostics"]["quality_flags"] = flags
        if not getattr(capture, "success", False) or flags:
            observation["diagnostics"]["capture_error"] = getattr(capture, "error_message", "invalid capture")
            return observation
        if not isinstance(image, np.ndarray) or image.shape != (720, 1280, 3) or image.dtype != np.uint8:
            observation["diagnostics"]["capture_error"] = "Expected uint8 RGB 1280x720 client image"
            return observation
        if float(image.std()) < 2:
            observation["diagnostics"]["capture_error"] = "Blank capture"
            return observation
        observation.update(valid=True, _image=image)
        found = {}
        for scene, identifiers in _SCENES:
            for key in identifiers:
                if key not in found:
                    found[key] = self._control(image, key, observation["diagnostics"])
            if all(found[key] is not None for key in identifiers):
                observation["scene"] = scene
                anchor = identifiers[0]
                expected = self._reference(anchor)
                observed = found[anchor]["rect"]
                scale = found[anchor].get("scale_x", 1.)
                observation["_scale_x"] = scale
                observation["_anchor_offset"] = [observed[0]-round(expected[0]*scale), observed[1]-expected[1]]
                observation["diagnostics"]["anchor"] = anchor
                break
        for name, key in _CONTROLS.get(observation["scene"], {}).items():
            match = found.get(key) or self._control(image, key, observation["diagnostics"])
            if match:
                observation["controls"][name] = match
        if observation["scene"] == "settlement":
            spec = self._control_spec("unopened_box")
            if not spec:
                raise ValueError("Missing masked unopened_box template")
            source, offset = self._crop(image, (140, 345, 1000, 155))
            matches = self.vision.find_all_templates(
                source, self._image(spec["template"]), mask_image=self._image(spec["mask"]),
                threshold=.90, use_grayscale=True, nms_threshold=.4,
            )
            observation["boxes"] = sorted(
                [m for r in matches.matches if (m := self._match_dict(r, offset))], key=lambda m: m["center"][0],
            )
        return observation

    def _identity_variants(self, kind: str, *, profile="template", sizes=()) -> tuple[list, list, list]:
        key = (kind, profile, sizes)
        if key in self._variants:
            return self._variants[key]
        templates, masks, identities = [], [], []
        for row in self.catalog["characters" if kind == "role" else "equipment"]:
            paths = row.get("stand_templates", []) if profile == "stand" else [row.get(profile)]
            mask_paths = row.get("stand_masks", []) if profile == "stand" else [row.get(profile.replace("template", "mask"))]
            for i, path in enumerate(paths):
                if not path:
                    continue
                original = self._image(path)
                mp = mask_paths[i] if i < len(mask_paths) else None
                original_mask = self._image(mp) if mp else (original[:, :, 3] if original.ndim == 3 and original.shape[2] == 4 else None)
                # Body frames have a large native transparent canvas: trim before scale.
                if profile == "stand" and original_mask is not None:
                    yy, xx = np.where(original_mask > 245)
                    if not len(xx):
                        continue
                    box = (slice(yy.min(), yy.max()+1), slice(xx.min(), xx.max()+1))
                    original, original_mask = original[box], original_mask[box]
                for size in sizes or (None,):
                    if size is None:
                        template, mask = original, original_mask
                    else:
                        shape = (round(original.shape[1]*size), round(original.shape[0]*size)) if profile == "stand" else (size, size)
                        template = cv2.resize(original, shape, interpolation=cv2.INTER_AREA)
                        mask = cv2.resize(original_mask, shape, interpolation=cv2.INTER_NEAREST) if original_mask is not None else None
                    if mask is not None and profile != "name_template":
                        mask = (mask > 245).astype(np.uint8)*255
                        yy, xx = np.where(mask)
                        if len(xx) < 20:
                            continue
                        box = (slice(yy.min(), yy.max()+1), slice(xx.min(), xx.max()+1))
                        template, mask = template[box], mask[box]
                    templates.append(template[:, :, :3] if template.ndim == 3 else template)
                    masks.append(mask)
                    identities.append(int(row["id"]))
        self._variants[key] = templates, masks, identities
        return templates, masks, identities

    def _rank(self, image: np.ndarray, kind: str, profile: str, sizes=(), *, method=cv2.TM_CCOEFF_NORMED, template_shape=None) -> list[dict]:
        templates, masks, identities = self._identity_variants(kind, profile=profile, sizes=sizes)
        indices = [i for i, t in enumerate(templates) if t.shape[0] <= image.shape[0] and t.shape[1] <= image.shape[1]
                   and (template_shape is None or t.shape[:2] == template_shape)]
        if not indices:
            return []
        results = self.vision.find_templates_batch(
            image, [templates[i] for i in indices], mask_images=[masks[i] for i in indices],
            threshold=-1., use_grayscale=True, match_method=method,
        )
        # Multiple scales/phases of one identity are not runner-up identities.
        scores: dict[int, float] = {}
        for i, result in zip(indices, results):
            score = float(result.confidence)
            if np.isfinite(score):
                identifier = identities[i]
                scores[identifier] = max(scores.get(identifier, -1.), score)
        return [{"id": k, "score": v} for k, v in sorted(scores.items(), key=lambda item: (-item[1], item[0]))]

    @staticmethod
    def _accepted(ranked: list, threshold: float, margin: float) -> bool:
        return len(ranked) >= 2 and ranked[0]["score"] >= threshold and ranked[0]["score"]-ranked[1]["score"] >= margin

    def _role(self, image: np.ndarray, name_box: list, card_box: list, include_spine: bool) -> tuple[dict | None, list]:
        source, _ = self._crop(image, name_box)
        # Keep the whole name field (including blank margins) when comparing.
        source = np.pad(source, ((4, 4), (5, 5), (0, 0)))
        ranked = self._rank(source, "role", "name_template")
        if self._accepted(ranked, .80, .05):
            return dict(ranked[0], margin=ranked[0]["score"]-ranked[1]["score"], method="name"), ranked[:3]
        if include_spine:
            source, _ = self._crop(image, card_box)
            # The prefab and independent nine-character calibration agree on
            # 2/3 scale; freeze a narrow bank rather than thousands of guesses.
            body = self._rank(source, "role", "stand", (.66, 2/3, .67), method=cv2.TM_CCORR_NORMED)
            if self._accepted(body, .93, .05):
                return dict(body[0], margin=body[0]["score"]-body[1]["score"], method="stand"), body[:3]
            return None, body[:3]
        return None, ranked[:3]

    def _equipment_name(self, image: np.ndarray, name_box: list, scale_x: float = 1.) -> tuple[dict | None, list]:
        # All equipment IDs compete using full centered rendered names, just
        # like role identity. No OCR and no artwork fallback on candidate cards.
        templates, _, _ = self._identity_variants("equipment", profile="name_template")
        center_x = name_box[0] + name_box[2]/2
        names = []
        # The game allows long names to overflow the card in one line. Most
        # names use a 138px context; the longest needs 164px. Compare each width
        # in its OWN centered region so a gold card border does not pollute the
        # short-name score, then rank all IDs together (not per-width winners).
        for height, width in sorted({t.shape[:2] for t in templates}):
            source_width = round(width*scale_x)
            source, _ = self._crop(image, [round(center_x-source_width/2), name_box[1], source_width, height])
            source = np.pad(source, ((4, 4), (5, 5), (0, 0)))
            names.extend(self._rank(source, "equipment", "name_template", template_shape=(height, width)))
        names.sort(key=lambda row: (-row["score"], row["id"]))
        if self._accepted(names, .80, .05):
            return dict(names[0], margin=names[0]["score"]-names[1]["score"], method="equipment_name"), names
        return None, names

    def read_candidates(self, observation: dict, kind: str, include_spine: bool = False) -> list[dict]:
        scene = observation.get("scene")
        if not observation.get("valid") or kind not in {"role", "equipment"}:
            return []
        if scene not in ({"role_select"} if kind == "role" else {"initial_equipment", "loot_select"}):
            return []
        dx, dy = observation.get("_anchor_offset", (0, 0))
        sx = observation.get("_scale_x", 1.)
        left = 309 if scene == "loot_select" else 369
        image, output, diagnostics = observation["_image"], [], []
        for index in range(3):
            x, y = round((left+242*index)*sx)+dx, 191+dy
            rect = [x, y, round(155*sx), 227]
            if kind == "role":
                identity, ranked = self._role(image, [x+round(7*sx), y+145, round(138*sx), 23], rect, include_spine)
            else:
                identity, ranked = self._equipment_name(image, [x-round(6*sx), y+168, round(164*sx), 23], sx)
            detail = {"index": index, "top": ranked[:3]}
            diagnostics.append(detail)
            if identity:
                # SELECT's hit box is checked on this same frame, not inferred only from the page.
                button, _ = self._crop(image, [x-25, 452+dy, 205, 65])
                spec = self._control_spec("D04")
                match = self.vision.find_template(button, self._image(spec["template"]), threshold=.90)
                point = self._match_dict(match, (x-25, 452+dy))
                if point:
                    output.append(dict(identity, index=index, rect=rect, center=[x+77, y+113], select_point=point["center"]))
        observation["diagnostics"]["candidates"] = diagnostics
        return output if len(output) == 3 else []

    def _occupied(self, source: np.ndarray) -> bool:
        for quality in ("UR", "SSR", "SR", "R"):
            spec = self._control_spec(f"occupied_{quality}")
            if not spec:
                continue
            template, mask = self._image(spec["template"]), self._image(spec["mask"])
            for size in (59, 60, 61):
                result = self.vision.find_template(
                    source, cv2.resize(template, (size, size), interpolation=cv2.INTER_AREA),
                    mask_image=cv2.resize(mask, (size, size), interpolation=cv2.INTER_NEAREST),
                    threshold=.90, use_grayscale=False, match_method=cv2.TM_SQDIFF_NORMED,
                )
                if result.found:
                    return True
        return False

    def _selected_banner(self, image: np.ndarray, x: int, y: int) -> tuple[bool, float]:
        """Only the central SELECT text/green fill, never animated corners."""
        source, _ = self._crop(image, [x+5, y+89, 145, 44])
        spec = self._control_spec("D07")
        result = self.vision.find_template(
            source, self._image(spec["template"]),
            mask_image=self._image(spec["mask"]) if spec.get("mask") else None,
            threshold=.90, use_grayscale=True,
        )
        score = float(result.confidence)
        return bool(result.found and np.isfinite(score)), score

    def read_selected_character(self, observation: dict) -> dict | None:
        """Cheap post-click confirmation: five markers and ONE full name.

        Slot verification belongs to read_team before/after this operation;
        repeating fifteen equipment classifications inside a 2s retry window
        cannot reliably collect three observations. This probe captures no new
        frame and rejects zero/multiple markers instead of guessing a target.
        """
        if not observation.get("valid") or observation.get("scene") not in {"captain", "assign"}:
            return None
        dx, dy = observation.get("_anchor_offset", (0, 0))
        sx = observation.get("_scale_x", 1.)
        image, selected, diagnostics = observation["_image"], [], []
        for index, (base_x, base_y) in enumerate(_TEAM_CARDS):
            x, y = round(base_x*sx)+dx, base_y+dy
            found, score = self._selected_banner(image, x, y)
            diagnostics.append({"index": index, "marker_score": score, "found": found})
            if found:
                selected.append((index, x, y, score))
        observation["diagnostics"]["selection_markers"] = diagnostics
        if len(selected) != 1:
            return None
        index, x, y, marker_score = selected[0]
        rect = [x, y, 155, 228]
        identity, ranked = self._role(image, [x+round(8*sx), y+147 if index < 3 else y+145, round(138*sx), 23], rect, False)
        observation["diagnostics"]["selected_name"] = ranked
        if identity is None:
            return None
        return dict(identity, screen_index=index, center=[x+77, y+114], rect=rect,
                    marker_score=marker_score, selected=True)

    def read_team(self, observation: dict, include_spine: bool = False) -> list[dict]:
        if not observation.get("valid") or observation.get("scene") not in {"captain", "assign"}:
            return []
        dx, dy = observation.get("_anchor_offset", (0, 0))
        sx = observation.get("_scale_x", 1.)
        image, output, diagnostics = observation["_image"], [], []
        for index, (base_x, base_y) in enumerate(_TEAM_CARDS):
            x, y = round(base_x*sx)+dx, base_y+dy
            rect = [x, y, 155, 228]
            identity, ranked = self._role(image, [x+round(8*sx), y+147 if index < 3 else y+145, round(138*sx), 23], rect, include_spine)
            detail = {"index": index, "top": ranked}
            diagnostics.append(detail)
            if identity is None:
                continue
            selected, marker_score = self._selected_banner(image, x, y)
            detail["marker_score"] = marker_score
            slots, occupied_ids = {}, {}
            profile = "small_icon_template" if observation["scene"] == "captain" else "small_tips_template"
            for si, slot in enumerate(_SLOTS):
                source, _ = self._crop(image, [x+round(160*sx), y+12+si*68, 72, 72])
                empty_spec = self._control_spec("D09")
                empty = self.vision.find_template(source, self._image(empty_spec["template"]), threshold=.90).found
                if empty:
                    slots[slot] = "empty"
                else:
                    equipment = self._rank(source, "equipment", profile, (56, 58, 60))
                    detail.setdefault("slot_candidates", {})[slot] = equipment[:3]
                    if self._accepted(equipment, .88, .10):
                        slots[slot] = "occupied"
                        occupied_ids[slot] = equipment[0]["id"]
                    else:
                        slots[slot] = "occupied" if self._occupied(source) else "unknown"
            output.append(dict(identity, screen_index=index, center=[x+77, y+114], rect=rect, selected=bool(selected), slots=slots, occupied_ids=occupied_ids))
        observation["diagnostics"]["team"] = diagnostics
        if len(output) != 5 or len({row["id"] for row in output}) != 5:
            return []
        return output
