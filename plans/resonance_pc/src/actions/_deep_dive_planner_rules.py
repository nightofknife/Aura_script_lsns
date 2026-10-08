"""Pure, three-by-three deep-dive rules in the scanner's logical coordinates.

Slots describe space, not persistent tiles. A layer rotation moves all attached
contents with the same permutation. Ordinary moves never cross a face. Random
boss choices are uniform, as explicitly assumed by the planning model.
"""
from __future__ import annotations

from dataclasses import dataclass
from copy import deepcopy
from functools import lru_cache
import itertools
from numbers import Integral

import numpy as np

FACES = ("U", "R", "F", "D", "L", "B")
BASES = {
    "U": ((0, -1, 0), (1, 0, 0), (0, 0, -1)),
    "R": ((1, 0, 0), (0, 0, 1), (0, 1, 0)),
    "F": ((0, 0, -1), (1, 0, 0), (0, 1, 0)),
    "D": ((0, 1, 0), (1, 0, 0), (0, 0, 1)),
    "L": ((-1, 0, 0), (0, 0, -1), (0, 1, 0)),
    "B": ((0, 0, 1), (-1, 0, 0), (0, 1, 0)),
}
EMPTY_INSPIRATION = 54
RULES_VERSION = "cube3-uniform-boss-v1"


@dataclass(frozen=True)
class Cell:
    face: str
    row: int
    col: int


def cell_to_slot(cell: Cell | dict | tuple) -> int:
    if isinstance(cell, dict):
        cell = Cell(cell.get("face", cell.get("face_id")), cell["row"], cell["col"])
    elif isinstance(cell, tuple):
        cell = Cell(*cell)
    if not isinstance(cell, Cell) or cell.face not in FACES:
        raise ValueError("Cell requires a face in U/R/F/D/L/B")
    if (not isinstance(cell.row, Integral) or isinstance(cell.row, bool)
            or not isinstance(cell.col, Integral) or isinstance(cell.col, bool)
            or not 0 <= cell.row < 3 or not 0 <= cell.col < 3):
        raise ValueError("Row and column must be integers in [0, 2]")
    return FACES.index(cell.face) * 9 + int(cell.row) * 3 + int(cell.col)


def validate_slot(slot: int) -> int:
    if not isinstance(slot, Integral) or isinstance(slot, bool) or not 0 <= slot < 54:
        raise ValueError("Slot must be an integer in [0, 53]")
    return int(slot)


def slot_to_cell(slot: int) -> Cell:
    face, remainder = divmod(validate_slot(slot), 9)
    row, col = divmod(remainder, 3)
    return Cell(FACES[face], row, col)


def coord_dict(slot: int) -> dict:
    cell = slot_to_cell(slot)
    return dict(slot=int(slot), face=cell.face, row=cell.row, col=cell.col)


def manhattan(player: int, boss: int) -> int:
    """Face-local distance; callers must separately check face equality."""
    return abs((player % 9) // 3 - (boss % 9) // 3) + abs(player % 3 - boss % 3)


@dataclass(frozen=True)
class Geometry:
    points: np.ndarray
    normals: np.ndarray
    rotations: np.ndarray
    actor_rotations: np.ndarray
    symmetries: np.ndarray
    moves: np.ndarray
    counts: np.ndarray
    rotation_specs: tuple[tuple[int, int, int], ...]


@lru_cache(maxsize=1)
def geometry() -> Geometry:
    """Cached integer layer permutations; sentinel 54 maps to itself."""
    bases = np.asarray([BASES[face] for face in FACES], dtype=np.int64)
    points = np.asarray([n + u * (col - 1) + v * (row - 1)
                         for n, u, v in bases for row in range(3) for col in range(3)])
    normals = np.repeat(bases[:, 0], 9, axis=0)
    inverse = {tuple(p) + tuple(n): i for i, (p, n) in enumerate(zip(points, normals))}
    specs = tuple((axis, layer, sign) for axis in range(3)
                  for layer in (-1, 0, 1) for sign in (-1, 1))
    ids = {spec: i for i, spec in enumerate(specs)}
    rotations = np.tile(np.arange(55, dtype=np.int64), (18, 1))
    for rotation_id, (axis, layer, sign) in enumerate(specs):
        unit = np.eye(3, dtype=np.int64)[axis]
        for slot, (point, normal) in enumerate(zip(points, normals)):
            if point[axis] == layer:
                moved = sign * np.cross(unit, point) + unit * (unit @ point)
                turned = sign * np.cross(unit, normal) + unit * (unit @ normal)
                rotations[rotation_id, slot] = inverse[tuple(moved) + tuple(turned)]
    actor = np.empty((54, 4), dtype=np.int64)
    for slot in range(54):
        for tangent_index, tangent in enumerate(bases[slot // 9, 1:]):
            axis = int(np.flatnonzero(tangent)[0])
            for sign_index, sign in enumerate((-1, 1)):
                actor[slot, 2 * tangent_index + sign_index] = ids[axis, int(points[slot, axis]), sign]
    symmetries = []
    for axes in itertools.permutations(range(3)):
        for signs in itertools.product((-1, 1), repeat=3):
            matrix = np.zeros((3, 3), dtype=np.int64)
            for row, col in enumerate(axes):
                matrix[row, col] = signs[row]
            if round(np.linalg.det(matrix)) == 1:
                symmetries.append([inverse[tuple(matrix @ p) + tuple(matrix @ n)]
                                   for p, n in zip(points, normals)] + [54])
    symmetries = np.asarray(symmetries, dtype=np.int64)
    moves = np.full((54, 4), -1, dtype=np.int64)
    counts = np.zeros(54, dtype=np.int64)
    for slot in range(54):
        face, remainder = divmod(slot, 9)
        row, col = divmod(remainder, 3)
        for dr, dc in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            if 0 <= row + dr < 3 and 0 <= col + dc < 3:
                moves[slot, counts[slot]] = face * 9 + (row + dr) * 3 + col + dc
                counts[slot] += 1
    for array in (points, normals, rotations, actor, symmetries, moves, counts):
        array.flags.writeable = False
    return Geometry(points, normals, rotations, actor, symmetries, moves, counts, specs)


def move_dict(origin: int, destination: int, actor: str = "player") -> dict:
    return dict(kind="move", actor=actor, origin=coord_dict(origin), destination=coord_dict(destination))


def rotation_dict(rotation_id: int, actor_slot: int, actor: str = "player") -> dict:
    axis, layer, sign = geometry().rotation_specs[rotation_id]
    return dict(kind="rotate_layer", actor=actor, actor_cell=coord_dict(actor_slot),
                rotation_id=int(rotation_id), world_axis=("X", "Y", "Z")[axis],
                world_axis_index=axis, layer=layer, sign=sign, angle_degrees=sign * 90,
                sign_convention="right_hand_about_positive_world_axis")


@dataclass(frozen=True)
class PlayerAction:
    final_player: int
    final_boss: int
    rotation_id: int | None
    pickup_original_slot: int
    sequence: tuple[dict, ...]
    terminal: bool


@lru_cache(maxsize=54 * 54)
def player_actions(player: int, boss: int) -> tuple[PlayerAction, ...]:
    """Both orders for one move plus one rotation; collision stops immediately.

    pickup_original_slot is the tile's slot before either primitive. Thus callers
    remove inspiration there, then apply rotation_id to the remaining items.
    """
    player, boss = validate_slot(player), validate_slot(boss)
    if player == boss:
        return ()
    geo = geometry()
    result = []
    for destination in geo.moves[player, :geo.counts[player]]:
        destination = int(destination)
        move = move_dict(player, destination)
        if destination == boss:
            result.append(PlayerAction(destination, boss, None, destination, (move,), True))
            continue
        for rotation_id in geo.actor_rotations[destination]:
            rotation_id = int(rotation_id)
            perm = geo.rotations[rotation_id]
            result.append(PlayerAction(int(perm[destination]), int(perm[boss]), rotation_id,
                                       destination, (move, rotation_dict(rotation_id, destination)), False))
    for rotation_id in geo.actor_rotations[player]:
        rotation_id = int(rotation_id)
        perm = geo.rotations[rotation_id]
        rotated_player, rotated_boss = int(perm[player]), int(perm[boss])
        inverse = np.argsort(perm)
        rotation = rotation_dict(rotation_id, player)
        for destination in geo.moves[rotated_player, :geo.counts[rotated_player]]:
            destination = int(destination)
            result.append(PlayerAction(destination, rotated_boss, rotation_id, int(inverse[destination]),
                                       (rotation, move_dict(rotated_player, destination)),
                                       destination == rotated_boss))
    return tuple(result)


def remaining_player_actions(player: int, boss: int, moves_left: int = 1,
                             rotations_left: int = 1) -> tuple[PlayerAction, ...]:
    """Actions still available in the current turn, without replenishing quotas.

    A rotation-only action never picks up an inspiration: its tile remains
    distinct from the player's tile under the same bijective permutation.
    """
    player, boss = validate_slot(player), validate_slot(boss)
    for name, quota in (("moves_left", moves_left), ("rotations_left", rotations_left)):
        if not isinstance(quota, Integral) or isinstance(quota, bool) or quota not in (0, 1):
            raise ValueError(f"{name} must be 0 or 1")
    if moves_left and rotations_left:
        return player_actions(player, boss)
    if player == boss or not (moves_left or rotations_left):
        return ()
    geo = geometry()
    if moves_left:
        return tuple(PlayerAction(int(destination), boss, None, int(destination),
                                  (move_dict(player, int(destination)),), int(destination) == boss)
                     for destination in geo.moves[player, :geo.counts[player]])
    return tuple(PlayerAction(int(geo.rotations[int(rotation_id), player]),
                              int(geo.rotations[int(rotation_id), boss]), int(rotation_id),
                              EMPTY_INSPIRATION, (rotation_dict(int(rotation_id), player),), False)
                 for rotation_id in geo.actor_rotations[player])


@dataclass(frozen=True)
class BossOutcome:
    final_player: int
    final_boss: int
    rotation_id: int | None
    eat_original_slot: int
    probability: float
    terminal: bool
    sequence: tuple[dict, ...]


@lru_cache(maxsize=54 * 54)
def boss_outcomes(player: int, boss: int) -> tuple[BossOutcome, ...]:
    player, boss = validate_slot(player), validate_slot(boss)
    if player == boss:
        return (BossOutcome(player, boss, None, EMPTY_INSPIRATION, 1.0, True, ()),)
    geo = geometry()
    destinations = [int(m) for m in geo.moves[boss, :geo.counts[boss]]
                    if player // 9 != boss // 9 or manhattan(player, int(m)) == manhattan(player, boss) - 1]
    result = []
    for destination in destinations:
        move = move_dict(boss, destination, "boss")
        if destination == player:
            result.append(BossOutcome(player, destination, None, EMPTY_INSPIRATION,
                                      1.0 / len(destinations), True, (move,)))
            continue
        for rotation_id in geo.actor_rotations[destination]:
            rotation_id = int(rotation_id)
            perm = geo.rotations[rotation_id]
            result.append(BossOutcome(int(perm[player]), int(perm[destination]), rotation_id,
                                      destination, 1.0 / (4 * len(destinations)), False,
                                      (move, rotation_dict(rotation_id, destination, "boss"))))
    return tuple(result)


def action_dict(action: PlayerAction) -> dict:
    return dict(sequence=deepcopy(list(action.sequence)), terminal=action.terminal,
                after_player=dict(player=coord_dict(action.final_player), boss=coord_dict(action.final_boss)),
                rotation_id=action.rotation_id, pickup_original_slot=action.pickup_original_slot)


def boss_branch_dict(outcome: BossOutcome) -> dict:
    return dict(probability=outcome.probability, terminal=outcome.terminal,
                player=coord_dict(outcome.final_player), boss=coord_dict(outcome.final_boss),
                rotation_id=outcome.rotation_id, eat_original_slot=outcome.eat_original_slot,
                sequence=deepcopy(list(outcome.sequence)))
