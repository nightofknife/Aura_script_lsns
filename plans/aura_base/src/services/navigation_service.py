# -*- coding: utf-8 -*-

import json
import math
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import cv2
import numpy as np

from packages.aura_core.api import service_info
from packages.aura_core.observability.logging.core_logger import logger
from .app_provider_service import AppProviderService
from .config_service import ConfigService


def _normalize_angle_deg(angle: float) -> float:
    value = angle % 360.0
    return value if value >= 0 else value + 360.0


def _angle_diff_deg(target: float, current: float) -> float:
    diff = (_normalize_angle_deg(target) - _normalize_angle_deg(current) + 180.0) % 360.0 - 180.0
    return diff


def _bw_map_is_black(img_bgr: np.ndarray, v_min: int, v_max: int) -> np.ndarray:
    hsv = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2HSV)
    v = hsv[..., 2].astype(np.uint8)
    bw = np.full(v.shape, 255, np.uint8)
    bw[(v >= v_min) & (v <= v_max)] = 0
    return bw


class _TemplateHeadingDetector:
    def __init__(self, template_dir: Path, match_threshold: float):
        self.template_dir = template_dir
        self.match_threshold = match_threshold
        self.templates: List[Dict[str, Any]] = []
        self._load_templates()

    def _load_templates(self):
        if not self.template_dir.exists():
            logger.warning("Heading template directory not found: %s", self.template_dir)
            return

        templates = []
        for file in self.template_dir.glob("*.png"):
            try:
                angle = int(file.stem)
            except ValueError:
                continue
            if not (0 <= angle < 360):
                continue

            img = cv2.imread(str(file))
            if img is None:
                continue
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            mask = (gray > 5).astype(np.uint8) * 255
            templates.append({
                "angle": angle,
                "template": img,
                "mask": mask,
            })

        templates.sort(key=lambda item: item["angle"])
        self.templates = templates
        logger.info("Heading templates loaded: %s", len(self.templates))

    def detect(self, roi_bgr: np.ndarray) -> Tuple[bool, float, float]:
        if not self.templates:
            return False, 0.0, 0.0

        best_angle = None
        best_score = -1.0
        for tmpl in self.templates:
            template_img = tmpl["template"]
            mask = tmpl["mask"]
            th, tw = template_img.shape[:2]
            h_roi, w_roi = roi_bgr.shape[:2]
            if th > h_roi or tw > w_roi:
                continue
            res = cv2.matchTemplate(
                roi_bgr,
                template_img,
                cv2.TM_CCOEFF_NORMED,
                mask=mask,
            )
            _, max_val, _, _ = cv2.minMaxLoc(res)
            if max_val > best_score:
                best_score = float(max_val)
                best_angle = tmpl["angle"]

        if best_angle is None or best_score < self.match_threshold:
            return False, 0.0, best_score if best_score > 0 else 0.0
        return True, float(best_angle), best_score


@service_info(alias="navigation", public=True, deps={"app": "app", "config": "core/config"})
class NavigationService:
    def __init__(self, app: AppProviderService, config: ConfigService):
        self.app = app
        self.config = config
        self._heading_detector: Optional[_TemplateHeadingDetector] = None
        self._heading_template_dir: Optional[Path] = None
        self._circle_mask_cache: Dict[Tuple[int, int], np.ndarray] = {}

    def run_from_files(
        self,
        map_image_path: Path,
        passable_image_path: Path,
        goals_json_path: Path,
        base_path: Optional[Path] = None,
        timeout: float = 600.0,
        arrive_px: int = 8,
    ) -> Dict[str, Any]:
        map_image_path = self._resolve_path(map_image_path, base_path)
        passable_image_path = self._resolve_path(passable_image_path, base_path)
        goals_json_path = self._resolve_path(goals_json_path, base_path)

        map_bgr = cv2.imread(str(map_image_path))
        if map_bgr is None:
            raise FileNotFoundError(f"Map image not found: {map_image_path}")

        passable_gray = cv2.imread(str(passable_image_path), cv2.IMREAD_GRAYSCALE)
        if passable_gray is None:
            raise FileNotFoundError(f"Passable image not found: {passable_image_path}")

        goals = self._load_goals(goals_json_path)
        if not goals:
            raise ValueError("Goals JSON does not contain any valid targets.")

        nav_cfg = self.config.get("navigation", {}) or {}
        roi_rect = nav_cfg.get("minimap_roi_rect")
        if not self._is_valid_rect(roi_rect):
            raise ValueError("navigation.minimap_roi_rect is not configured.")

        heading_roi_size = nav_cfg.get("heading_roi_size", [20, 20])
        if not self._is_valid_size(heading_roi_size):
            heading_roi_size = [20, 20]

        heading_template_dir = nav_cfg.get("heading_template_dir", "config/templates/angle")
        heading_template_dir = self._resolve_path(Path(heading_template_dir), base_path)

        map_score_threshold = float(nav_cfg.get("map_score_threshold", 0.6))
        heading_match_threshold = float(nav_cfg.get("heading_match_threshold", 0.6))
        move_step_sec = float(nav_cfg.get("move_step_sec", 0.2))
        dead_zone_px = int(nav_cfg.get("dead_zone_px", 3))
        lookahead_index = int(nav_cfg.get("lookahead_index", 5))
        minimap_mask_circle = bool(nav_cfg.get("minimap_mask_circle", True))
        use_skeleton = bool(nav_cfg.get("use_skeleton", True))

        self._ensure_heading_detector(heading_template_dir, heading_match_threshold)

        v_min = int(nav_cfg.get("black_v_min", 0))
        v_max = int(nav_cfg.get("black_v_max", 20))

        map_val = (_bw_map_is_black(map_bgr, v_min, v_max) == 0).astype(np.float32)
        passable = (passable_gray >= 128).astype(np.uint8)

        try:
            for goal in goals:
                ok = self._navigate_to_goal(
                    map_val=map_val,
                    passable=passable,
                    roi_rect=tuple(roi_rect),
                    heading_roi_size=tuple(heading_roi_size),
                    goal_xy=goal,
                    timeout=timeout,
                    arrive_px=arrive_px,
                    map_score_threshold=map_score_threshold,
                    dead_zone_px=dead_zone_px,
                    move_step_sec=move_step_sec,
                    lookahead_index=lookahead_index,
                    v_min=v_min,
                    v_max=v_max,
                    minimap_mask_circle=minimap_mask_circle,
                    use_skeleton=use_skeleton,
                )
                if not ok:
                    return {"ok": False, "failed_goal": {"x": goal[0], "y": goal[1]}}
        finally:
            self.app.release_all_keys()

        return {"ok": True, "goals_completed": len(goals)}

    def _navigate_to_goal(
        self,
        map_val: np.ndarray,
        passable: np.ndarray,
        roi_rect: Tuple[int, int, int, int],
        heading_roi_size: Tuple[int, int],
        goal_xy: Tuple[int, int],
        timeout: float,
        arrive_px: int,
        map_score_threshold: float,
        dead_zone_px: int,
        move_step_sec: float,
        lookahead_index: int,
        v_min: int,
        v_max: int,
        minimap_mask_circle: bool,
        use_skeleton: bool,
    ) -> bool:
        start_time = time.time()
        while time.time() - start_time < timeout:
            capture = self.app.capture(rect=roi_rect)
            if not capture.success or capture.image is None:
                logger.warning("Navigation capture failed; retrying.")
                time.sleep(0.1)
                continue

            roi_bgr = capture.image
            if minimap_mask_circle:
                roi_bgr = self._apply_circular_mask(roi_bgr)
            roi_val = (_bw_map_is_black(roi_bgr, v_min, v_max) == 0).astype(np.float32)
            match = cv2.matchTemplate(map_val, roi_val, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, max_loc = cv2.minMaxLoc(match)
            if max_val < map_score_threshold:
                logger.debug("Map match score too low: %.3f", max_val)
                time.sleep(0.1)
                continue

            roi_w, roi_h = roi_rect[2], roi_rect[3]
            cur_x = int(max_loc[0] + roi_w / 2)
            cur_y = int(max_loc[1] + roi_h / 2)

            if (cur_x - goal_xy[0]) ** 2 + (cur_y - goal_xy[1]) ** 2 <= arrive_px ** 2:
                return True

            heading_ok, heading_deg, _ = self._detect_heading(roi_bgr, heading_roi_size)
            if not heading_ok:
                logger.warning("Heading detection failed; retrying.")
                time.sleep(0.1)
                continue

            path = self._plan_path(passable, (cur_x, cur_y), goal_xy, step=4, use_skeleton=use_skeleton)
            if not path:
                logger.warning("Path planning failed; retrying.")
                time.sleep(0.1)
                continue

            target_idx = min(len(path) - 1, max(0, lookahead_index))
            target_x, target_y = path[target_idx]

            dx, dy = target_x - cur_x, target_y - cur_y
            target_angle_rad = math.atan2(dx, -dy)
            target_angle_deg = _normalize_angle_deg(math.degrees(target_angle_rad))
            angle_diff = _angle_diff_deg(target_angle_deg, heading_deg)
            self._rotate_camera_dynamic(angle_diff)

            dist = math.hypot(dx, dy)
            if dist > dead_zone_px:
                self.app.key_down("w")
                time.sleep(move_step_sec)
                self.app.key_up("w")
            else:
                self.app.release_all_keys()

        return False

    def _detect_heading(self, roi_bgr: np.ndarray, heading_roi_size: Tuple[int, int]) -> Tuple[bool, float, float]:
        h, w = roi_bgr.shape[:2]
        hw, hh = heading_roi_size
        cx, cy = w // 2, h // 2
        x0 = max(0, cx - hw // 2)
        y0 = max(0, cy - hh // 2)
        x1 = min(w, x0 + hw)
        y1 = min(h, y0 + hh)
        heading_roi = roi_bgr[y0:y1, x0:x1]
        return self._heading_detector.detect(heading_roi) if self._heading_detector else (False, 0.0, 0.0)

    def _apply_circular_mask(self, roi_bgr: np.ndarray) -> np.ndarray:
        h, w = roi_bgr.shape[:2]
        key = (w, h)
        mask = self._circle_mask_cache.get(key)
        if mask is None:
            mask = np.zeros((h, w), dtype=np.uint8)
            radius = min(w, h) // 2
            cv2.circle(mask, (w // 2, h // 2), radius, 255, -1)
            self._circle_mask_cache[key] = mask
        masked = roi_bgr.copy()
        masked[mask == 0] = 0
        return masked

    def _rotate_camera_dynamic(self, angle_diff_deg: float):
        abs_diff = abs(angle_diff_deg)
        if abs_diff <= 5.0:
            return
        if abs_diff > 120.0:
            step = 240
        elif abs_diff > 70.0:
            step = 150
        elif abs_diff > 40.0:
            step = 80
        else:
            step = 10
        dx = step if angle_diff_deg > 0 else -step
        self.app.look_delta(dx, 0)

    def _plan_path(
        self,
        passable: np.ndarray,
        start_xy: Tuple[int, int],
        goal_xy: Tuple[int, int],
        step: int,
        use_skeleton: bool,
    ) -> List[Tuple[int, int]]:
        if use_skeleton:
            skeleton = self._zhang_suen_thinning(passable)
            if skeleton.any():
                start = self._nearest_mask_point(skeleton, start_xy)
                goal = self._nearest_mask_point(skeleton, goal_xy)
                if start and goal:
                    skel_path = self._astar_on_mask(skeleton, start, goal)
                    if skel_path:
                        return skel_path

        grid = self._build_grid(passable, step)
        start_cell = (start_xy[0] // step, start_xy[1] // step)
        goal_cell = (goal_xy[0] // step, goal_xy[1] // step)
        start_cell = self._nearest_passable(grid, start_cell)
        goal_cell = self._nearest_passable(grid, goal_cell)
        if start_cell is None or goal_cell is None:
            return []
        path_cells = self._astar(grid, start_cell, goal_cell)
        if not path_cells:
            return []
        return [(cx * step + step // 2, cy * step + step // 2) for (cx, cy) in path_cells]

    def _zhang_suen_thinning(self, bin_img: np.ndarray) -> np.ndarray:
        img = (bin_img > 0).astype(np.uint8)
        changed = True
        h, w = img.shape
        while changed:
            changed = False
            to_del = []
            for y in range(1, h - 1):
                for x in range(1, w - 1):
                    if img[y, x] == 0:
                        continue
                    p2, p3, p4, p5 = img[y - 1, x], img[y - 1, x + 1], img[y, x + 1], img[y + 1, x + 1]
                    p6, p7, p8, p9 = img[y + 1, x], img[y + 1, x - 1], img[y, x - 1], img[y - 1, x - 1]
                    neighbors = [p2, p3, p4, p5, p6, p7, p8, p9]
                    c = sum((neighbors[i] == 0 and neighbors[(i + 1) % 8] == 1) for i in range(8))
                    n = sum(neighbors)
                    if 2 <= n <= 6 and c == 1 and p2 * p4 * p6 == 0 and p4 * p6 * p8 == 0:
                        to_del.append((y, x))
            if to_del:
                changed = True
                for y, x in to_del:
                    img[y, x] = 0
            to_del = []
            for y in range(1, h - 1):
                for x in range(1, w - 1):
                    if img[y, x] == 0:
                        continue
                    p2, p3, p4, p5 = img[y - 1, x], img[y - 1, x + 1], img[y, x + 1], img[y + 1, x + 1]
                    p6, p7, p8, p9 = img[y + 1, x], img[y + 1, x - 1], img[y, x - 1], img[y - 1, x - 1]
                    neighbors = [p2, p3, p4, p5, p6, p7, p8, p9]
                    c = sum((neighbors[i] == 0 and neighbors[(i + 1) % 8] == 1) for i in range(8))
                    n = sum(neighbors)
                    if 2 <= n <= 6 and c == 1 and p2 * p4 * p8 == 0 and p2 * p6 * p8 == 0:
                        to_del.append((y, x))
            if to_del:
                changed = True
                for y, x in to_del:
                    img[y, x] = 0
        return img

    def _nearest_mask_point(
        self,
        mask: np.ndarray,
        point: Tuple[int, int],
    ) -> Optional[Tuple[int, int]]:
        x, y = point
        if 0 <= y < mask.shape[0] and 0 <= x < mask.shape[1] and mask[y, x] > 0:
            return (x, y)
        ys, xs = np.where(mask > 0)
        if xs.size == 0:
            return None
        dx = xs - x
        dy = ys - y
        idx = int(np.argmin(dx * dx + dy * dy))
        return (int(xs[idx]), int(ys[idx]))

    def _astar_on_mask(
        self,
        mask: np.ndarray,
        start: Tuple[int, int],
        goal: Tuple[int, int],
    ) -> List[Tuple[int, int]]:
        import heapq

        def heuristic(a: Tuple[int, int], b: Tuple[int, int]) -> float:
            return math.hypot(a[0] - b[0], a[1] - b[1])

        neighbors = [
            (-1, 0), (1, 0), (0, -1), (0, 1),
            (-1, -1), (-1, 1), (1, -1), (1, 1),
        ]
        open_set = []
        heapq.heappush(open_set, (0.0, start))
        came_from: Dict[Tuple[int, int], Tuple[int, int]] = {}
        g_score = {start: 0.0}

        max_iters = mask.shape[0] * mask.shape[1] * 2
        iters = 0
        while open_set and iters < max_iters:
            iters += 1
            _, current = heapq.heappop(open_set)
            if current == goal:
                return self._reconstruct_path(came_from, current)
            for dx, dy in neighbors:
                nx, ny = current[0] + dx, current[1] + dy
                if not (0 <= ny < mask.shape[0] and 0 <= nx < mask.shape[1]):
                    continue
                if mask[ny, nx] == 0:
                    continue
                neighbor = (nx, ny)
                tentative = g_score[current] + (1.4142 if dx != 0 and dy != 0 else 1.0)
                if tentative < g_score.get(neighbor, float("inf")):
                    came_from[neighbor] = current
                    g_score[neighbor] = tentative
                    f_score = tentative + heuristic(neighbor, goal)
                    heapq.heappush(open_set, (f_score, neighbor))
        return []

    def _build_grid(self, passable: np.ndarray, step: int) -> np.ndarray:
        h, w = passable.shape[:2]
        gh = (h + step - 1) // step
        gw = (w + step - 1) // step
        grid = np.zeros((gh, gw), dtype=np.uint8)
        for gy in range(gh):
            y0 = gy * step
            y1 = min((gy + 1) * step, h)
            for gx in range(gw):
                x0 = gx * step
                x1 = min((gx + 1) * step, w)
                if np.any(passable[y0:y1, x0:x1] > 0):
                    grid[gy, gx] = 1
        return grid

    def _nearest_passable(self, grid: np.ndarray, cell: Tuple[int, int]) -> Optional[Tuple[int, int]]:
        gx, gy = cell
        if 0 <= gy < grid.shape[0] and 0 <= gx < grid.shape[1] and grid[gy, gx] > 0:
            return cell
        for radius in range(1, 20):
            for dy in range(-radius, radius + 1):
                for dx in range(-radius, radius + 1):
                    nx, ny = gx + dx, gy + dy
                    if 0 <= ny < grid.shape[0] and 0 <= nx < grid.shape[1] and grid[ny, nx] > 0:
                        return (nx, ny)
        return None

    def _astar(
        self,
        grid: np.ndarray,
        start: Tuple[int, int],
        goal: Tuple[int, int],
    ) -> List[Tuple[int, int]]:
        import heapq

        def heuristic(a: Tuple[int, int], b: Tuple[int, int]) -> float:
            return math.hypot(a[0] - b[0], a[1] - b[1])

        neighbors = [
            (-1, 0), (1, 0), (0, -1), (0, 1),
            (-1, -1), (-1, 1), (1, -1), (1, 1),
        ]
        open_set = []
        heapq.heappush(open_set, (0.0, start))
        came_from: Dict[Tuple[int, int], Tuple[int, int]] = {}
        g_score = {start: 0.0}

        max_iters = grid.shape[0] * grid.shape[1] * 2
        iters = 0
        while open_set and iters < max_iters:
            iters += 1
            _, current = heapq.heappop(open_set)
            if current == goal:
                return self._reconstruct_path(came_from, current)

            for dx, dy in neighbors:
                nx, ny = current[0] + dx, current[1] + dy
                if not (0 <= ny < grid.shape[0] and 0 <= nx < grid.shape[1]):
                    continue
                if grid[ny, nx] == 0:
                    continue
                neighbor = (nx, ny)
                tentative = g_score[current] + (1.4142 if dx != 0 and dy != 0 else 1.0)
                if tentative < g_score.get(neighbor, float("inf")):
                    came_from[neighbor] = current
                    g_score[neighbor] = tentative
                    f_score = tentative + heuristic(neighbor, goal)
                    heapq.heappush(open_set, (f_score, neighbor))
        return []

    def _reconstruct_path(
        self,
        came_from: Dict[Tuple[int, int], Tuple[int, int]],
        current: Tuple[int, int],
    ) -> List[Tuple[int, int]]:
        path = [current]
        while current in came_from:
            current = came_from[current]
            path.append(current)
        path.reverse()
        return path

    def _ensure_heading_detector(self, template_dir: Path, match_threshold: float):
        if self._heading_detector is None or self._heading_template_dir != template_dir:
            self._heading_template_dir = template_dir
            self._heading_detector = _TemplateHeadingDetector(template_dir, match_threshold)

    def _resolve_path(self, value: Any, base_path: Optional[Path]) -> Path:
        path = Path(str(value))
        if not path.is_absolute() and base_path:
            path = base_path / path
        return path

    def _is_valid_rect(self, rect: Any) -> bool:
        if not isinstance(rect, (list, tuple)) or len(rect) != 4:
            return False
        return all(isinstance(v, (int, float)) for v in rect) and rect[2] > 0 and rect[3] > 0

    def _is_valid_size(self, size: Any) -> bool:
        if not isinstance(size, (list, tuple)) or len(size) != 2:
            return False
        return all(isinstance(v, (int, float)) for v in size) and size[0] > 0 and size[1] > 0

    def _load_goals(self, goals_json_path: Path) -> List[Tuple[int, int]]:
        raw = goals_json_path.read_text(encoding="utf-8").strip()
        data = json.loads(raw)

        if isinstance(data, dict):
            goals = data.get("goals", [])
        elif isinstance(data, list):
            goals = data
        else:
            goals = []

        parsed: List[Tuple[int, int]] = []
        for item in goals:
            if isinstance(item, dict) and "x" in item and "y" in item:
                parsed.append((int(item["x"]), int(item["y"])))
        return parsed
