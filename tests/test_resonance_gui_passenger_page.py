from __future__ import annotations

import os
import json

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

from packages.resonance_gui.bridge import RunnerBridge
from packages.resonance_gui.config_repository import ResonanceConfigRepository
from packages.resonance_gui.logic import (
    PASSENGER_PROGRESS_EVENT,
    PASSENGER_PROGRESS_SCHEMA,
    PC_GAME_NAME,
    PC_PASSENGER_TASK_REF,
)
from packages.resonance_gui.widgets.passenger_page import PassengerPage


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _page(tmp_path) -> PassengerPage:
    _app()
    settings = QSettings(str(tmp_path / "passenger-page.ini"), QSettings.Format.IniFormat)
    page = PassengerPage(ResonanceConfigRepository(settings=settings))
    page.resize(1112, 760)
    page.show()
    QApplication.processEvents()
    return page


def test_passenger_page_collects_and_persists_inputs(tmp_path):
    page = _page(tmp_path)
    try:
        page.city_a.setCurrentIndex(page.city_a.findData("2"))
        page.city_b.setCurrentIndex(page.city_b.findData("3"))
        page.trip_count.setValue(3)
        page.trade_during_trip.setChecked(True)
        page.auto_reposition.setChecked(False)

        inputs = page.collect_inputs()

        assert inputs == {
            "passenger_city_a_id": "2",
            "passenger_city_b_id": "3",
            "trip_count": 3,
            "trade_during_trip": True,
            "reposition_to_route": False,
        }
        assert page.trade_during_trip.isEnabled()
        assert page.auto_reposition.isEnabled()
        assert page.trade_during_trip.isChecked()
        assert page.start_button.objectName() == "primaryButton"
        assert "93" in page.expected_fatigue.text()
        assert "3 次 × 单次疲劳 31" in page.expected_fatigue.text()

        restored = _page(tmp_path)
        try:
            assert restored.trip_count.value() == 3
            assert restored.city_a.currentData() == "2"
            assert restored.city_b.currentData() == "3"
            assert restored.trade_during_trip.isChecked()
            assert not restored.auto_reposition.isChecked()
        finally:
            restored.close()
    finally:
        page.close()


def test_legacy_round_trip_setting_migrates_to_two_single_trips(tmp_path):
    settings = QSettings(str(tmp_path / "legacy-passenger.ini"), QSettings.Format.IniFormat)
    settings.setValue(
        "passenger/inputs_json",
        json.dumps(
            {
                "round_trips": 3,
                "passenger_from_city_id": "2",
                "passenger_to_city_id": "3",
            },
            ensure_ascii=False,
        ),
    )
    repository = ResonanceConfigRepository(settings=settings)

    loaded = repository.load_passenger_inputs()

    assert loaded["trip_count"] == 6
    assert loaded["passenger_city_a_id"] == "2"
    assert loaded["passenger_city_b_id"] == "3"
    assert "round_trips" not in loaded


def test_passenger_page_reduces_progress_and_renders_blocked_result(tmp_path):
    page = _page(tmp_path)
    try:
        page.set_target_status({"ok": True, "target": {"visible": True, "title": "雷索纳斯"}})
        page.begin_run({"cid": "passenger-cid"})
        page.apply_progress(
            {
                "name": PASSENGER_PROGRESS_EVENT,
                "payload": {
                    "schema": PASSENGER_PROGRESS_SCHEMA,
                    "cid": "passenger-cid",
                    "sequence": 1,
                    "stage": "travel",
                    "state": "started",
                    "trip_index": 1,
                    "leg_index": 1,
                    "leg_count": 2,
                    "source_city": "海角城",
                    "destination_city": "岚心城",
                    "recruited_count": 35,
                    "seat_capacity": 64,
                    "expected_fatigue_used": 0,
                    "expected_fatigue_total": 152,
                },
            }
        )

        assert page.route_value.text() == "海角城 → 岚心城"
        assert page.leg_value.text() == "1 / 2"
        assert page.passenger_value.text() == "35 / 64"
        assert page.fatigue_value.text() == "0 / 152"

        page.finish_run(
            {
                "status": "success",
                "final_result": {
                    "user_data": {
                        "success": False,
                        "status": "blocked",
                        "reason": "fatigue_recovery_required",
                        "requested_trips": 1,
                        "completed_legs": [],
                        "expected_fatigue_used": 0,
                        "total_revenue": 0,
                        "requires_manual_completion": True,
                    }
                },
            }
        )

        assert page.run_status_value.text() == "已阻塞"
        assert page.manual_value.text() == "是"
        assert not hasattr(page, "revenue_value")
        assert not page.is_busy()
    finally:
        page.close()


class _Runner:
    def run_task(self, **kwargs):
        self.kwargs = kwargs
        return {"cid": "passenger-cid", "status": "queued"}

    def close(self):
        return None


def test_bridge_dispatches_passenger_to_dedicated_pc_task():
    _app()
    runner = _Runner()
    bridge = RunnerBridge(runner_factory=lambda: runner)
    dispatched: list[dict] = []
    bridge.taskDispatched.connect(dispatched.append)

    bridge.run_pc_passenger({"trip_count": 2}, 0.0)

    assert runner.kwargs["game_name"] == PC_GAME_NAME
    assert runner.kwargs["task_ref"] == PC_PASSENGER_TASK_REF
    assert runner.kwargs["inputs"] == {"trip_count": 2}
    assert dispatched[0]["item"]["kind"] == "passenger_run"
