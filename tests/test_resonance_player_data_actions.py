from __future__ import annotations

import json
from pathlib import Path

from plans.resonance.src.actions.player_data_actions import (
    _extract_count_int,
    _extract_nickname,
    _extract_ratio,
    _extract_uid,
    _load_latest,
    _parse_city_name,
    _parse_recovery_option,
    _persist_latest,
)


def test_extract_uid_ratio_and_city_name():
    assert _extract_uid("UID: 8820206170") == "8820206170"
    assert _extract_uid("Q：88202061700") == "8820206170"
    assert _extract_nickname("1 面包猫南北") == "面包猫南北"
    assert _extract_ratio("292/292 +") == {"current": 292, "max": 292}
    assert _extract_ratio("疲劳值 0 / 824") == {"current": 0, "max": 824}
    assert _parse_city_name([{"text": "N"}, {"text": "修格里城"}, {"text": "访问城市"}]) == "修格里城"


def test_parse_clarity_recovery_options():
    unavailable = _parse_recovery_option(
        name="仙人掌能量棒棒糖",
        delta=40,
        slot_text="获取途径 澄明度 +40",
        count_text="0",
    )
    assert unavailable == {
        "name": "仙人掌能量棒棒糖",
        "delta": 40,
        "count": 0,
        "available": False,
    }

    stocked = _parse_recovery_option(
        name="仙人掌跳跳卷",
        delta=60,
        slot_text="仙人掌跳跳卷 澄明度 +60",
        count_text="37",
    )
    assert stocked == {
        "name": "仙人掌跳跳卷",
        "delta": 60,
        "count": 37,
        "available": True,
    }

    limited = _parse_recovery_option(
        name="桦石",
        delta=100,
        slot_text="桦石 澄明度 +100",
        limit_text="每日限购8/8",
    )
    assert limited == {
        "name": "桦石",
        "delta": 100,
        "daily_limit": "8/8",
        "available": True,
    }


def test_parse_fatigue_recovery_options():
    assert _extract_count_int("T") == 1
    assert _extract_count_int("市") == 1

    stocked = _parse_recovery_option(
        name="仙人掌提神跳糖",
        delta=-900,
        slot_text="仙人掌提神跳糖 疲劳值 -900",
        count_text="1",
    )
    assert stocked == {
        "name": "仙人掌提神跳糖",
        "delta": -900,
        "count": 1,
        "available": True,
    }

    limited = _parse_recovery_option(
        name="桦石",
        delta=-150,
        slot_text="桦石 疲劳值 -150",
        limit_text="每日限购8/8",
    )
    assert limited["daily_limit"] == "8/8"
    assert limited["available"] is True


def test_persist_and_load_latest(tmp_path: Path):
    cache_file = tmp_path / "player" / "latest.json"
    payload = {
        "profile": {"uid": "8820206170", "nickname": "面包猫南北", "level": 71},
        "location": {"current_city": "修格里城"},
        "currencies": {"iron_coins": 9132364, "birch_stone": 615},
        "status": {
            "clarity": {"current": 292, "max": 292, "recovery_options": []},
            "fatigue": {"current": 0, "max": 824, "recovery_options": []},
            "cargo": {"current": 96, "max": 708},
        },
        "metadata": {"refreshed_at": "2026-07-01T00:00:00+00:00", "source": "ocr"},
    }

    _persist_latest(payload, cache_file=cache_file)

    assert json.loads(cache_file.read_text(encoding="utf-8")) == payload
    assert _load_latest(cache_file=cache_file) == payload
