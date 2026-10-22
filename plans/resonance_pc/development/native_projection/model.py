"""Offline native-resource projection, independent of runtime recognition code.

The caller supplies audited asset exports and the camera candidate explicitly.
Only the cube root rotation varies. Translation, scale and the camera remain
fixed; loading these resources does not establish that a live frame satisfies
the normal-camera, completed-animation assumptions of the native audit.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
import re
from typing import Any, Mapping

import numpy as np


def quat(value: Mapping[str, float]) -> np.ndarray:
    """Convert a Unity xyzw quaternion to a normalized rotation matrix."""
    q = np.array([value[key] for key in "xyzw"], dtype=float)
    norm = np.linalg.norm(q)
    if not np.isfinite(q).all() or norm <= 0:
        raise ValueError("Quaternion must be finite and nonzero")
    x, y, z, w = q / norm
    return np.array([
        [1 - 2 * (y*y + z*z), 2 * (x*y - z*w), 2 * (x*z + y*w)],
        [2 * (x*y + z*w), 1 - 2 * (x*x + z*z), 2 * (y*z - x*w)],
        [2 * (x*z - y*w), 2 * (y*z + x*w), 1 - 2 * (x*x + y*y)],
    ])


def vec(value: Mapping[str, float]) -> np.ndarray:
    return np.array([value[key] for key in "xyz"], dtype=float)


def trs(row: Mapping[str, Any]) -> np.ndarray:
    """Compose the exported local translation, rotation and scale unchanged."""
    matrix = np.eye(4)
    matrix[:3, :3] = quat(row["rotation"]) @ np.diag(vec(row["scale"]))
    matrix[:3, 3] = vec(row["position"])
    if not np.isfinite(matrix).all():
        raise ValueError("Native transform contains nonfinite values")
    return matrix


def transform(points: np.ndarray, matrix: np.ndarray) -> np.ndarray:
    points = np.asarray(points, dtype=float)
    return points @ matrix[:3, :3].T + matrix[:3, 3]


def _read(path: Path) -> Any:
    # Missing/malformed resources are errors, never grounds for legacy fallback.
    return json.loads(path.read_text(encoding="utf-8"))


def _rotation(value: np.ndarray) -> np.ndarray:
    matrix = np.asarray(value, dtype=float)
    if (matrix.shape != (3, 3) or not np.isfinite(matrix).all()
            or not np.allclose(matrix.T @ matrix, np.eye(3), atol=1e-6, rtol=0)
            or not math.isclose(float(np.linalg.det(matrix)), 1, abs_tol=1e-6)):
        raise ValueError("Expected a finite proper 3x3 rotation matrix")
    return matrix


class NativeModel:
    """Fixed native camera and 54-slot geometry, with per-frame root rotation.

    ``cells`` retains key/matrix/icon/rim/normal dictionaries. Icon/rim and
    shell vertices use the audited Empty variant as a geometry proxy; their
    coordinates are not adjusted to improve a screenshot fit. ``visible`` is
    only a front-facing test. Use ``raster_depth`` for geometric self-occlusion.
    """

    def __init__(
        self,
        asset_dir: str | Path,
        camera_path: str | Path,
        triangles_path: str | Path,
        *,
        width: int = 1280,
        height: int = 720,
    ) -> None:
        self.asset_dir = Path(asset_dir)
        self.camera_path = Path(camera_path)
        self.triangles_path = Path(triangles_path)
        self.width, self.height = self._dimensions(width, height)
        hierarchy = _read(self.asset_dir / "rubik_hierarchy.json")
        self.hierarchy = {row["path"]: row for row in hierarchy}
        self.variants = {
            row["prefab"]: row
            for row in _read(self.asset_dir / "cell_mesh_geometry.json")
        }
        empty_meshes = self.variants["Rogue_Cube_MF_N_Empty"]["meshes"]
        icon = next(mesh for mesh in empty_meshes if mesh["path"].endswith("/Icon"))
        vertices = np.asarray(icon["root_vertices"], dtype=float)
        uv = np.asarray(icon["uv0"], dtype=float)
        if vertices.shape != (4, 3) or uv.shape != (4, 2):
            raise ValueError("Expected the audited four-vertex native Icon/UV")
        order = np.argsort(np.arctan2(
            vertices[:, 1] - vertices[:, 1].mean(),
            vertices[:, 0] - vertices[:, 0].mean(),
        ))
        self.icon_vertices = vertices[order]
        self.icon_uv = uv[order]

        self.triangle_metadata = _read(self.triangles_path)
        spec = next(row for row in self.triangle_metadata["variants"]
                    if row["prefab"] == "Rogue_Cube_MF_N_Empty")
        shell = empty_meshes[spec["mesh_index_in_cell_mesh_geometry"]]
        self.shell_vertices = np.asarray(shell["root_vertices"], dtype=float)
        if (shell["mesh_ref"]["m_PathID"] != spec["mesh_path_id"]
                or self.shell_vertices.shape != (spec["vertex_count"], 3)
                or spec["vertex_count"] != 122):
            raise ValueError("Shell triangles do not match the audited native mesh")
        self.shell_triangles = np.concatenate([
            np.asarray(submesh, dtype=np.int64).reshape(-1, 3)
            for submesh in spec["submesh_triangle_indices"]
        ])
        if (not self.shell_triangles.size or self.shell_triangles.min() < 0
                or self.shell_triangles.max() >= len(self.shell_vertices)):
            raise ValueError("Invalid native shell triangle indices")
        if not all(np.isfinite(array).all() for array in
                   (self.icon_vertices, self.icon_uv, self.shell_vertices)):
            raise ValueError("Native mesh contains nonfinite coordinates")

        # Front outer rim comes from shell extrema, not the inset Icon plane.
        z = self.shell_vertices[:, 2].max()
        front = self.shell_vertices[np.abs(self.shell_vertices[:, 2] - z) < 1e-4]
        x0, x1 = front[:, 0].min(), front[:, 0].max()
        y0, y1 = front[:, 1].min(), front[:, 1].max()
        self.rim_vertices = np.array([[x0, y0, z], [x1, y0, z],
                                      [x1, y1, z], [x0, y1, z]])
        self.cells: list[dict[str, Any]] = []
        for row in hierarchy:
            match = re.fullmatch(r"/Rubik/Cube \((\d+),(\d+),(\d+)\)", row["path"])
            if match is None:
                continue
            anchor = self.hierarchy[row["path"] + "/Rogue_Cube_MF_02"]
            matrix = trs(row) @ trs(anchor)
            self.cells.append(dict(
                key=tuple(map(int, match.groups())), matrix=matrix,
                icon=transform(self.icon_vertices, matrix),
                rim=transform(self.rim_vertices, matrix),
                normal=matrix[:3, :3] @ np.array([0., 0., 1.]),
            ))
        expected = {(face, row, col) for face in range(1, 7)
                    for row in range(1, 4) for col in range(1, 4)}
        if len(self.cells) != 54 or {cell["key"] for cell in self.cells} != expected:
            raise ValueError("NativeModel requires the audited 3x3, 54-slot model")
        self._shell_points = np.array([
            transform(self.shell_vertices, cell["matrix"]) for cell in self.cells
        ])

        self.camera_metadata = _read(self.camera_path)
        self.camera_R = _rotation(self.camera_metadata["camera_to_world_rotation"])
        self.camera_C = np.asarray(self.camera_metadata["camera_center_world"], dtype=float)
        if self.camera_C.shape != (3,) or not np.isfinite(self.camera_C).all():
            raise ValueError("Invalid native camera center")
        self.vertical_fov_degrees = float(self.camera_metadata["vertical_fov_degrees"])
        if not 0 < self.vertical_fov_degrees < 180:
            raise ValueError("Invalid native vertical field of view")
        self.f = self.height / (2 * math.tan(math.radians(self.vertical_fov_degrees) / 2))
        self.K = np.array([[self.f, 0, self.width / 2],
                           [0, self.f, self.height / 2], [0, 0, 1.]])
        self.to_cv = np.diag([1., -1., 1.])

    @staticmethod
    def _dimensions(width: int, height: int) -> tuple[int, int]:
        if (not isinstance(width, (int, np.integer))
                or not isinstance(height, (int, np.integer))
                or width <= 0 or height <= 0):
            raise ValueError("Viewport dimensions must be positive integers")
        return int(width), int(height)

    def camera_points(self, points: np.ndarray, root_R: np.ndarray | None = None) -> np.ndarray:
        rotation = np.eye(3) if root_R is None else _rotation(root_R)
        world = np.asarray(points, dtype=float) @ rotation.T
        return ((world - self.camera_C) @ self.camera_R) @ self.to_cv

    def project(self, points: np.ndarray, root_R: np.ndarray | None = None) -> np.ndarray:
        camera = self.camera_points(points, root_R)
        with np.errstate(divide="ignore", invalid="ignore"):
            projected = camera[..., :2] / camera[..., 2:3] * self.f
        return projected + np.array([self.width / 2, self.height / 2])

    def visible(self, cell: Mapping[str, Any], root_R: np.ndarray) -> bool:
        """Front-facing only; not evidence of visibility through other cells."""
        rotation = _rotation(root_R)
        center = cell["icon"].mean(0) @ rotation.T
        normal = cell["normal"] @ rotation.T
        return bool(normal @ (self.camera_C - center) > 0)

    def raster_depth(
        self, root_R: np.ndarray, width: int = 1280, height: int = 720,
    ) -> np.ndarray:
        """Return camera-Z depth (H,W), infinity for uncovered pixel centers.

        Uses actual native Empty shell triangles plus two opaque Icon-plane
        triangles per slot, rasterized two-sided with perspective-correct Z.
        This intentionally conservative geometry proxy may hide a point behind
        a transparent Icon region. It is NOT shader-exact: variant differences,
        material culling, actors, effects and UI occlusion are not reproduced.
        A visible proxy result therefore does not prove a usable observed pixel.
        """
        width, height = self._dimensions(width, height)
        rotation = _rotation(root_R)
        focal = height / (2 * math.tan(math.radians(self.vertical_fov_degrees) / 2))
        depth = np.full((height, width), np.inf, dtype=float)
        for cell, shell in zip(self.cells, self._shell_points):
            camera_shell = self.camera_points(shell, rotation)
            for triangle in camera_shell[self.shell_triangles]:
                _raster_triangle(depth, triangle, focal)
            icon = self.camera_points(cell["icon"], rotation)
            _raster_triangle(depth, icon[[0, 1, 2]], focal)
            _raster_triangle(depth, icon[[0, 2, 3]], focal)
        return depth


def _raster_triangle(depth: np.ndarray, points: np.ndarray, focal: float) -> None:
    # The audited camera is well outside this fixed-origin cube. Rejecting
    # near-plane crossings avoids pretending that absent clipping is correct.
    if not np.isfinite(points).all() or np.any(points[:, 2] <= 0):
        raise ValueError("Native depth proxy requires finite triangles ahead of camera")
    height, width = depth.shape
    projected = points[:, :2] / points[:, 2, None] * focal + [width / 2, height / 2]
    xmin, ymin = np.maximum(np.floor(projected.min(0)).astype(int), [0, 0])
    xmax, ymax = np.minimum(np.ceil(projected.max(0)).astype(int), [width - 1, height - 1])
    if xmax < xmin or ymax < ymin:
        return
    a, b, c = projected
    denominator = ((b[1] - c[1]) * (a[0] - c[0])
                   + (c[0] - b[0]) * (a[1] - c[1]))
    if abs(denominator) < 1e-8:
        return
    x, y = np.meshgrid(np.arange(xmin, xmax + 1) + .5,
                       np.arange(ymin, ymax + 1) + .5)
    u = ((b[1] - c[1]) * (x - c[0]) + (c[0] - b[0]) * (y - c[1])) / denominator
    v = ((c[1] - a[1]) * (x - c[0]) + (a[0] - c[0]) * (y - c[1])) / denominator
    w = 1 - u - v
    inside = (u >= -1e-6) & (v >= -1e-6) & (w >= -1e-6)
    inverse_z = u / points[0, 2] + v / points[1, 2] + w / points[2, 2]
    z = np.divide(1., inverse_z, out=np.full_like(inverse_z, np.inf), where=inverse_z > 0)
    target = depth[ymin:ymax + 1, xmin:xmax + 1]
    np.minimum(target, np.where(inside, z, np.inf), out=target)
