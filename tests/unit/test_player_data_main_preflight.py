"""The PC player-data preflight uses the existing stable main-screen template."""

from types import SimpleNamespace

import numpy as np
import pytest

from packages.aura_core.context.persistence.persistent_data_service import PersistentDataService
from plans.resonance_pc.src.actions import player_data_pc_actions as player_data


def test_main_preflight_confirms_template_without_running_full_screen_ocr(monkeypatch):
    calls = []

    def confirm(app, vision, *, timeout_sec):
        calls.append((app, vision, timeout_sec))
        return {"confirmed": True, "match": {"confidence": 0.99}}

    monkeypatch.setattr(player_data, "_wait_main_stable", confirm)
    app = object()
    vision = object()
    ocr = SimpleNamespace(recognize_all=lambda **_kwargs: pytest.fail("full-screen OCR was called"))

    result = player_data._wait_for_any_marker(
        app,
        ocr,
        markers=player_data._MAIN_PAGE_MARKERS,
        region=player_data._MAIN_PAGE_REGION,
        label="main page before player data refresh",
        timeout_sec=8.0,
        vision=vision,
        prefer_main_template=True,
    )

    assert result == []
    assert calls == [(app, vision, 8.0)]


def test_main_preflight_rejects_unconfirmed_template_without_ocr(monkeypatch):
    monkeypatch.setattr(
        player_data,
        "_wait_main_stable",
        lambda *_args, **_kwargs: {"confirmed": False, "match": {"confidence": 0.64}},
    )
    ocr = SimpleNamespace(recognize_all=lambda **_kwargs: pytest.fail("full-screen OCR was called"))

    with pytest.raises(player_data.StopTaskException, match="Last confidence: 0.640"):
        player_data._wait_for_any_marker(
            object(),
            ocr,
            markers=player_data._MAIN_PAGE_MARKERS,
            region=player_data._MAIN_PAGE_REGION,
            vision=object(),
            prefer_main_template=True,
        )


def test_main_preflight_keeps_ocr_fallback_without_vision():
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    app = SimpleNamespace(capture=lambda **_kwargs: SimpleNamespace(success=True, image=frame))
    ocr = SimpleNamespace(
        recognize_all=lambda **_kwargs: SimpleNamespace(
            results=[SimpleNamespace(text="访问城市", center_point=(10, 10), rect=(0, 0, 20, 20), confidence=0.99)]
        )
    )

    result = player_data._wait_for_any_marker(
        app,
        ocr,
        markers=player_data._MAIN_PAGE_MARKERS,
        region=player_data._MAIN_PAGE_REGION,
        prefer_main_template=True,
    )

    assert result[0]["text"] == "访问城市"


def test_location_refresh_uses_template_preflight_and_ocr_only_for_city(monkeypatch, tmp_path):
    monkeypatch.setattr(
        player_data,
        "_wait_main_stable",
        lambda *_args, **_kwargs: {"confirmed": True, "match": {"confidence": 0.99}},
    )
    captures = []

    def capture(*, rect):
        captures.append(rect)
        assert rect == player_data._MAIN_CITY_REGION
        return SimpleNamespace(success=True, image=np.zeros((rect[3], rect[2], 3), dtype=np.uint8))

    app = SimpleNamespace(capture=capture)
    ocr = SimpleNamespace(
        recognize_all=lambda **_kwargs: SimpleNamespace(
            results=[SimpleNamespace(text="修格里城", center_point=(10, 10), rect=(0, 0, 20, 20), confidence=0.99)]
        )
    )

    result = player_data.resonance_pc_player_data_refresh(
        stages=["location"],
        app=app,
        ocr=ocr,
        vision=object(),
        persistent_data=PersistentDataService(tmp_path),
    )

    assert result["location"]["current_city"] == "修格里城"
    assert captures == [player_data._MAIN_CITY_REGION]
