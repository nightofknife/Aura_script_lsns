from __future__ import annotations

import json
from pathlib import Path

import pytest

from plans.resonance.src.actions import city_shop_actions
from plans.resonance.src.actions.city_shop_actions import (
    _CITY_ALIAS_TO_KEY,
    CityShopResolveError,
    resonance_resolve_city_shop_point,
)


def _write_location(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def test_resolve_city_shop_point_success(tmp_path: Path) -> None:
    location_file = tmp_path / "location.json"
    _write_location(
        location_file,
        {
            "city": {
                "confluence_tower": {
                    "exchange": [500, 470],
                    "rest": [320, 180],
                }
            }
        },
    )

    result = resonance_resolve_city_shop_point(
        ocr_city_text="当前城市：汇流塔",
        shop_type="exchange",
        location_file_path=str(location_file),
    )

    assert result["city_key"] == "confluence_tower"
    assert result["city_name"] == "汇流塔"
    assert result["shop_type"] == "exchange"
    assert result["x"] == 500
    assert result["y"] == 470


def test_resolve_city_shop_point_relative_path_from_plan_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    location_file = tmp_path / "data" / "meta" / "location.json"
    _write_location(
        location_file,
        {"city": {"confluence_tower": {"exchange": [500, 470]}}},
    )
    monkeypatch.setattr(city_shop_actions, "_PLAN_ROOT", tmp_path)

    result = resonance_resolve_city_shop_point(
        ocr_city_text="汇流塔",
        shop_type="exchange",
        location_file_path="data/meta/location.json",
    )

    assert result["city_key"] == "confluence_tower"
    assert result["x"] == 500
    assert result["y"] == 470


def test_resolve_city_shop_point_city_not_resolved(tmp_path: Path) -> None:
    location_file = tmp_path / "location.json"
    _write_location(
        location_file,
        {"city": {"confluence_tower": {"exchange": [500, 470]}}},
    )

    with pytest.raises(CityShopResolveError) as exc:
        resonance_resolve_city_shop_point(
            ocr_city_text="当前城市：未知区域",
            shop_type="exchange",
            location_file_path=str(location_file),
        )

    assert exc.value.code == "city_not_resolved_from_ocr"


def test_resolve_city_shop_point_shop_not_found(tmp_path: Path) -> None:
    location_file = tmp_path / "location.json"
    _write_location(
        location_file,
        {"city": {"confluence_tower": {"exchange": [500, 470]}}},
    )

    with pytest.raises(CityShopResolveError) as exc:
        resonance_resolve_city_shop_point(
            ocr_city_text="汇流塔",
            shop_type="commerce",
            location_file_path=str(location_file),
        )

    assert exc.value.code == "shop_type_not_found_in_city"


def test_resolve_city_shop_point_city_not_found_in_location(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    location_file = tmp_path / "location.json"
    _write_location(
        location_file,
        {"city": {"confluence_tower": {"exchange": [500, 470]}}},
    )
    alias_map = dict(_CITY_ALIAS_TO_KEY)
    alias_map["测试缺失城市"] = "missing_city"
    monkeypatch.setattr(city_shop_actions, "_CITY_ALIAS_TO_KEY", alias_map)

    with pytest.raises(CityShopResolveError) as exc:
        resonance_resolve_city_shop_point(
            ocr_city_text="当前城市：测试缺失城市",
            shop_type="exchange",
            location_file_path=str(location_file),
        )

    assert exc.value.code == "city_not_found_in_location"


def test_resolve_city_shop_point_invalid_json(tmp_path: Path) -> None:
    location_file = tmp_path / "location.json"
    location_file.write_text("{ city: [", encoding="utf-8")

    with pytest.raises(CityShopResolveError) as exc:
        resonance_resolve_city_shop_point(
            ocr_city_text="汇流塔",
            shop_type="exchange",
            location_file_path=str(location_file),
        )

    assert exc.value.code == "location_json_invalid"


def test_resolve_city_shop_point_file_not_found(tmp_path: Path) -> None:
    missing_file = tmp_path / "missing.json"

    with pytest.raises(CityShopResolveError) as exc:
        resonance_resolve_city_shop_point(
            ocr_city_text="汇流塔",
            shop_type="exchange",
            location_file_path=str(missing_file),
        )

    assert exc.value.code == "location_file_not_found"
