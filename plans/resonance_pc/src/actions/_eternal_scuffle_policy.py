"""Deterministic, UI-independent choices for the Eternal Scuffle task."""
from __future__ import annotations

from datetime import date
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

CHARACTER_RARITIES = ("SSR", "SR", "R", "N")
EQUIPMENT_RARITIES = ("UR", "SSR", "SR", "R")
SLOT_TYPES = ("attack", "defense", "support")


def validate_inputs(coins_per_run: int, run_count: int) -> dict:
    """Reject bools, fractional numbers and coercions before spending coins."""
    for key, value, maximum in (("coins_per_run", coins_per_run, 5), ("run_count", run_count, 9999)):
        if type(value) is not int or not 1 <= value <= maximum:
            raise ValueError(f"{key} must be an integer from 1 to {maximum}")
    return {"coins_per_run": coins_per_run, "run_count": run_count}


def rank_entries(entries: Sequence[Mapping], rarity_order: Sequence[str]) -> list[dict]:
    """Known dates reorder only their ID-derived slots within each rarity.

    Unknown-date entries retain their ID-descending positions. The returned
    rows are copies and carry one-based ranks; inputs are never mutated.
    """
    order = {rarity: i for i, rarity in enumerate(rarity_order)}
    rows = [dict(entry) for entry in entries]
    ids = [int(row["id"]) for row in rows]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate catalog IDs")
    for row in rows:
        if row["rarity"] not in order:
            raise ValueError(f"Unknown rarity: {row['rarity']}")
        row["id"] = int(row["id"])
        if row.get("release_date"):
            date.fromisoformat(row["release_date"])
    rows.sort(key=lambda row: (order[row["rarity"]], -row["id"]))
    for rarity in rarity_order:
        positions = [i for i, row in enumerate(rows) if row["rarity"] == rarity and row.get("release_date")]
        dated = sorted((rows[i] for i in positions), key=lambda row: (row["release_date"], row["id"]), reverse=True)
        for position, row in zip(positions, dated):
            rows[position] = row
    for rank, row in enumerate(rows, 1):
        row["rank"] = rank
    return rows


def load_catalog(plan_root: Path | None = None) -> dict:
    root = Path(plan_root) if plan_root is not None else Path(__file__).resolve().parents[2]
    with (root / "data/meta/eternal_scuffle.json").open(encoding="utf-8") as stream:
        catalog = json.load(stream)
    if catalog.get("schema_version") != 1:
        raise ValueError("Unsupported eternal scuffle catalog schema")
    return catalog


def _index(catalog: Mapping, kind: str) -> dict[int, Mapping]:
    return {int(row["id"]): row for row in catalog[kind]}


def _candidate_id(candidate: Any) -> int:
    return int(candidate["id"]) if isinstance(candidate, Mapping) else int(candidate)


def _choose(candidates: Sequence, catalog: Mapping, kind: str):
    if not candidates:
        raise ValueError("No identified candidates")
    rows = _index(catalog, kind)
    for candidate in candidates:
        if _candidate_id(candidate) not in rows:
            raise ValueError(f"Unrecognized {kind} ID: {_candidate_id(candidate)}")
    # Input order is screen order and resolves duplicate or equally ranked cards.
    return min(candidates, key=lambda c: rows[_candidate_id(c)]["rank"])


def choose_character(candidates: Sequence, catalog: Mapping):
    return _choose(candidates, catalog, "characters")


def choose_equipment(candidates: Sequence, catalog: Mapping):
    return _choose(candidates, catalog, "equipment")


def choose_loot(candidates: Sequence, team: Sequence[Mapping], catalog: Mapping) -> dict:
    """Fill empty slots, improve weaker gear, then use the screen-first fallback."""
    choose_equipment(candidates, catalog)  # Validate all candidates, even losers.
    equipment = _index(catalog, "equipment")
    characters = _index(catalog, "characters")
    if not team or len({int(member["id"]) for member in team}) != len(team):
        raise ValueError("Team must contain distinct identified characters")
    if len({member["screen_index"] for member in team}) != len(team):
        raise ValueError("Team screen positions must be distinct")
    for member in team:
        if int(member["id"]) not in characters:
            raise ValueError(f"Unrecognized character: {member['id']}")
        for slot in SLOT_TYPES:
            # Missing is not empty: observations must explicitly confirm None.
            if slot not in member["equipment"]:
                raise ValueError(f"Unconfirmed slot: {slot}")
            old = member["equipment"][slot]
            if old is not None and (int(old) not in equipment or equipment[int(old)]["slot_type"] != slot):
                raise ValueError(f"Invalid equipment mapping in {slot}: {old}")
    ordered = sorted(candidates, key=lambda c: equipment[_candidate_id(c)]["rank"])

    def member_order(member):
        return characters[int(member["id"])]["rank"], member["screen_index"]

    def result(candidate, member, reason):
        equip_id = _candidate_id(candidate)
        slot = equipment[equip_id]["slot_type"]
        return {"equipment_id": equip_id, "character_id": int(member["id"]),
                "reason": reason, "replaced_equipment_id": member["equipment"][slot]}

    for candidate in ordered:
        slot = equipment[_candidate_id(candidate)]["slot_type"]
        empty = [member for member in team if member["equipment"][slot] is None]
        if empty:
            return result(candidate, min(empty, key=member_order), "fill_empty")
    for candidate in ordered:
        entry = equipment[_candidate_id(candidate)]
        weaker = [member for member in team if equipment[int(member["equipment"][entry["slot_type"]])]["rank"] > entry["rank"]]
        if weaker:
            member = min(weaker, key=lambda m: (-equipment[int(m["equipment"][entry["slot_type"]])]["rank"], *member_order(m)))
            return result(candidate, member, "upgrade")
    return result(ordered[0], min(team, key=lambda m: m["screen_index"]), "fallback_first")
