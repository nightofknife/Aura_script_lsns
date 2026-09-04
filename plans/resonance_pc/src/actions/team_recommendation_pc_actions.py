"""Strict fixed-team matching against the cached player roster and weapons.

The future weapon-recognition stage must persist this exact section contract::

    {
      "weapons": {
        "schema_version": 1,
        "scan_complete": true,
        "entries": [{"weapon_id": "空间切线", "quantity": 1}]
      }
    }

``weapon_id`` is matched exactly against the fixed BWIKI catalog.  Quantity is
consumed during assignment, so one owned copy cannot satisfy two team slots.
"""

from __future__ import annotations

from collections import Counter
import copy
import json
from pathlib import Path
from typing import Any, Iterable, Mapping

from packages.aura_core.api import action_info, requires_services
from packages.aura_core.context.persistence.persistent_data_service import PersistentDataService

from ._player_data_persistence import PlayerDataPersistenceError, load_pc_user_info


_PLAN_ROOT = Path(__file__).resolve().parents[2]
_TEAM_CATALOG_FILE = _PLAN_ROOT / "data" / "meta" / "team_recommendations.json"
_CHARACTER_STATUS_ORDER = {"complete": 0, "basic": 1}
_WEAPON_STATUS_ORDER = {"full": 0, "low": 1, "unmet": 2}


class TeamRecommendationDataError(ValueError):
    """Raised when a persisted snapshot or catalog violates its declared schema."""


def _blocked(
    *,
    reason_code: str,
    message: str,
    missing_sections: Iterable[str] = (),
    invalid_sections: Iterable[str] = (),
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "status": "blocked",
        "reason_code": str(reason_code),
        "message": str(message),
        "missing_sections": list(missing_sections),
        "invalid_sections": list(invalid_sections),
        "recommendation_count": 0,
        "recommendations": [],
    }


def _load_json_object(path: Path, *, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise
    except (OSError, json.JSONDecodeError) as exc:
        raise TeamRecommendationDataError(f"{label} is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise TeamRecommendationDataError(f"{label} must be a JSON object")
    return payload


def load_team_recommendation_catalog(
    catalog_file: Path | None = None,
) -> dict[str, Any]:
    payload = _load_json_object(
        Path(catalog_file or _TEAM_CATALOG_FILE),
        label="team recommendation catalog",
    )
    if payload.get("schema_version") != 1:
        raise TeamRecommendationDataError("team recommendation catalog schema_version must be 1")
    teams = payload.get("teams")
    if not isinstance(teams, list) or not teams:
        raise TeamRecommendationDataError("team recommendation catalog must contain teams")
    if payload.get("team_count") != len(teams):
        raise TeamRecommendationDataError("team recommendation catalog team_count is invalid")

    seen: set[str] = set()
    for team in teams:
        if not isinstance(team, Mapping):
            raise TeamRecommendationDataError("team recommendation entry must be an object")
        team_id = str(team.get("team_id") or "")
        if not team_id or team_id in seen:
            raise TeamRecommendationDataError("team recommendation team_id must be unique")
        seen.add(team_id)
        members = team.get("members")
        if not isinstance(members, list) or len(members) != 5:
            raise TeamRecommendationDataError(f"{team_id}: exactly five members are required")
        for expected_slot, member in enumerate(members, 1):
            if not isinstance(member, Mapping) or member.get("slot") != expected_slot:
                raise TeamRecommendationDataError(f"{team_id}: member slots must be 1..5")
            character_id = str(member.get("character_id") or "")
            minimum = member.get("minimum_awakening")
            recommended = member.get("recommended_awakening")
            if not character_id:
                raise TeamRecommendationDataError(f"{team_id}: character_id is required")
            if not isinstance(minimum, int) or isinstance(minimum, bool) or not 0 <= minimum <= 5:
                raise TeamRecommendationDataError(
                    f"{team_id}/{character_id}: minimum awakening is invalid"
                )
            if recommended is not None and (
                not isinstance(recommended, int)
                or isinstance(recommended, bool)
                or not minimum <= recommended <= 5
            ):
                raise TeamRecommendationDataError(
                    f"{team_id}/{character_id}: recommended awakening is invalid"
                )
    return payload


def _character_inventory(section: Any) -> dict[str, int]:
    if not isinstance(section, Mapping):
        raise TeamRecommendationDataError("characters section must be an object")
    if section.get("schema_version") != 1 or section.get("scan_complete") is not True:
        raise TeamRecommendationDataError(
            "characters section requires schema_version=1 and scan_complete=true"
        )
    entries = section.get("entries")
    if not isinstance(entries, list) or not entries:
        raise TeamRecommendationDataError("characters entries must be a non-empty list")
    result: dict[str, int] = {}
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise TeamRecommendationDataError("character entry must be an object")
        character_id = str(entry.get("character_id") or "")
        stars = entry.get("stars")
        if not character_id or character_id in result:
            raise TeamRecommendationDataError("character_id must be non-empty and unique")
        if not isinstance(stars, int) or isinstance(stars, bool) or not 0 <= stars <= 5:
            raise TeamRecommendationDataError(f"{character_id}: stars must be an integer from 0 to 5")
        result[character_id] = stars
    return result


def _weapon_inventory(section: Any) -> Counter[str]:
    if not isinstance(section, Mapping):
        raise TeamRecommendationDataError("weapons section must be an object")
    if section.get("schema_version") != 1 or section.get("scan_complete") is not True:
        raise TeamRecommendationDataError(
            "weapons section requires schema_version=1 and scan_complete=true"
        )
    entries = section.get("entries")
    if not isinstance(entries, list) or not entries:
        raise TeamRecommendationDataError("weapons entries must be a non-empty list")
    result: Counter[str] = Counter()
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise TeamRecommendationDataError("weapon entry must be an object")
        weapon_id = str(entry.get("weapon_id") or "")
        quantity = entry.get("quantity")
        if not weapon_id:
            raise TeamRecommendationDataError("weapon_id is required")
        if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity <= 0:
            raise TeamRecommendationDataError(f"{weapon_id}: quantity must be a positive integer")
        result[weapon_id] += quantity
    return result


def _assign_weapons(
    choices: list[tuple[str, ...]],
    inventory: Counter[str],
) -> list[str] | None:
    if any(not values for values in choices):
        return None
    order = sorted(
        range(len(choices)),
        key=lambda index: (
            len(choices[index]),
            sum(inventory[name] for name in choices[index]),
            index,
        ),
    )
    remaining = Counter(inventory)
    assigned: list[str | None] = [None] * len(choices)

    def visit(position: int) -> bool:
        if position >= len(order):
            return True
        slot = order[position]
        for weapon_id in choices[slot]:
            if remaining[weapon_id] <= 0:
                continue
            remaining[weapon_id] -= 1
            assigned[slot] = weapon_id
            if visit(position + 1):
                return True
            assigned[slot] = None
            remaining[weapon_id] += 1
        return False

    if not visit(0):
        return None
    return [str(value) for value in assigned]


def _weapon_match(
    members: list[Mapping[str, Any]],
    inventory: Counter[str],
) -> tuple[str, list[str]]:
    full_choices = [
        (str(member.get("full_weapon_id")),)
        if member.get("full_weapon_id")
        else ()
        for member in members
    ]
    full_assignment = _assign_weapons(full_choices, inventory)
    if full_assignment is not None:
        return "full", full_assignment

    low_choices: list[tuple[str, ...]] = []
    for member in members:
        options: list[str] = []
        for key in ("low_weapon_id", "full_weapon_id"):
            weapon_id = str(member.get(key) or "")
            if weapon_id and weapon_id not in options:
                options.append(weapon_id)
        low_choices.append(tuple(options))
    low_assignment = _assign_weapons(low_choices, inventory)
    if low_assignment is not None:
        return "low", low_assignment
    return "unmet", []


def recommend_fixed_teams(
    player_data: Mapping[str, Any],
    catalog: Mapping[str, Any],
) -> dict[str, Any]:
    missing_sections = [
        section
        for section in ("characters", "weapons")
        if not isinstance(player_data.get(section), Mapping)
    ]
    if missing_sections:
        labels = {"characters": "角色数据", "weapons": "武器数据"}
        return _blocked(
            reason_code="player_data_incomplete",
            message="请先更新" + "、".join(labels[item] for item in missing_sections) + "。",
            missing_sections=missing_sections,
        )

    invalid_sections: list[str] = []
    errors: list[str] = []
    try:
        characters = _character_inventory(player_data["characters"])
    except TeamRecommendationDataError as exc:
        invalid_sections.append("characters")
        errors.append(str(exc))
        characters = {}
    try:
        weapons = _weapon_inventory(player_data["weapons"])
    except TeamRecommendationDataError as exc:
        invalid_sections.append("weapons")
        errors.append(str(exc))
        weapons = Counter()
    if invalid_sections:
        return _blocked(
            reason_code="player_data_invalid",
            message="用户数据格式无效：" + "；".join(errors),
            invalid_sections=invalid_sections,
        )

    teams = catalog.get("teams")
    if catalog.get("schema_version") != 1 or not isinstance(teams, list):
        raise TeamRecommendationDataError("team recommendation catalog is invalid")

    recommendations: list[dict[str, Any]] = []
    for team in teams:
        if not isinstance(team, Mapping):
            raise TeamRecommendationDataError("team recommendation entry must be an object")
        raw_members = team.get("members")
        if not isinstance(raw_members, list) or len(raw_members) != 5:
            raise TeamRecommendationDataError("team recommendation roster is invalid")
        members = [dict(member) for member in raw_members if isinstance(member, Mapping)]
        if len(members) != 5:
            raise TeamRecommendationDataError("team recommendation member is invalid")

        character_rows: list[dict[str, Any]] = []
        eligible = True
        all_recommended_known = True
        all_recommended_met = True
        for member in members:
            character_id = str(member.get("character_id") or "")
            minimum = int(member["minimum_awakening"])
            recommended = member.get("recommended_awakening")
            current = characters.get(character_id)
            if current is None or current < minimum:
                eligible = False
                break
            if recommended is None:
                all_recommended_known = False
                all_recommended_met = False
            elif current < int(recommended):
                all_recommended_met = False
            character_rows.append(
                {
                    "slot": int(member["slot"]),
                    "character_id": character_id,
                    "current_awakening": current,
                    "minimum_awakening": minimum,
                    "recommended_awakening": recommended,
                    "full_weapon_id": member.get("full_weapon_id"),
                    "low_weapon_id": member.get("low_weapon_id"),
                }
            )
        if not eligible:
            continue

        character_status = (
            "complete" if all_recommended_known and all_recommended_met else "basic"
        )
        weapon_status, assignment = _weapon_match(members, weapons)
        for index, row in enumerate(character_rows):
            row["assigned_weapon_id"] = assignment[index] if assignment else None
        recommendations.append(
            {
                "team_id": str(team.get("team_id") or ""),
                "title": str(team.get("title") or team.get("team_id") or ""),
                "guide_name": str(team.get("guide_name") or ""),
                "categories": list(team.get("categories") or []),
                "source_url": str(team.get("source_url") or ""),
                "character_status": character_status,
                "weapon_status": weapon_status,
                "members": character_rows,
            }
        )

    recommendations.sort(
        key=lambda item: (
            _CHARACTER_STATUS_ORDER[item["character_status"]],
            _WEAPON_STATUS_ORDER[item["weapon_status"]],
            item["title"],
        )
    )
    counts = {
        "character_complete": sum(
            item["character_status"] == "complete" for item in recommendations
        ),
        "character_basic": sum(
            item["character_status"] == "basic" for item in recommendations
        ),
        "weapon_full": sum(item["weapon_status"] == "full" for item in recommendations),
        "weapon_low": sum(item["weapon_status"] == "low" for item in recommendations),
        "weapon_unmet": sum(item["weapon_status"] == "unmet" for item in recommendations),
    }
    return {
        "schema_version": 1,
        "status": "success",
        "reason_code": "",
        "message": (
            f"找到 {len(recommendations)} 套角色达到最低觉醒要求的固定配队。"
            if recommendations
            else "没有角色达到最低觉醒要求的固定配队。"
        ),
        "missing_sections": [],
        "invalid_sections": [],
        "recommendation_count": len(recommendations),
        "counts": counts,
        "recommendations": recommendations,
    }


@action_info(
    name="resonance_pc.team_recommendations",
    public=True,
    read_only=True,
    description="Match cached characters and weapons against fixed BWIKI team guides.",
)
@requires_services(persistent_data="core/persistent_data")
def resonance_pc_team_recommendations(
    catalog_file: str | None = None,
    persistent_data: PersistentDataService | None = None,
) -> dict[str, Any]:
    if persistent_data is None:
        raise RuntimeError("persistent_data service is required")
    try:
        player_data = load_pc_user_info(persistent_data)
    except PlayerDataPersistenceError as exc:
        if exc.code == "player_data_incomplete":
            return _blocked(
                reason_code="player_data_incomplete",
                message="请先更新角色数据、武器数据。",
                missing_sections=("characters", "weapons"),
            )
        return _blocked(
            reason_code="player_data_invalid",
            message=f"用户数据格式无效：{exc}",
            invalid_sections=("characters", "weapons"),
        )
    catalog = load_team_recommendation_catalog(
        Path(catalog_file) if catalog_file else None
    )
    return copy.deepcopy(recommend_fixed_teams(player_data, catalog))


__all__ = [
    "TeamRecommendationDataError",
    "load_team_recommendation_catalog",
    "recommend_fixed_teams",
    "resonance_pc_team_recommendations",
]
