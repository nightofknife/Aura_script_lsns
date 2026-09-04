"""Smoke checks for the Resonance PC Auto Book GUI contract."""

from __future__ import annotations

import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication, QLabel

from packages.resonance_gui.config_repository import (
    DEFAULT_TRADE_INPUTS,
    ResonanceConfigRepository,
)
from packages.resonance_gui.logic import (
    normalize_trade_task_inputs,
    trade_result_summary,
)
from packages.resonance_gui.main_window import ResonanceMainWindow


def _application() -> QApplication:
    return QApplication.instance() or QApplication(["aura-auto-book-gui-smoke"])


def _repository(path) -> ResonanceConfigRepository:
    return ResonanceConfigRepository(
        QSettings(str(path), QSettings.Format.IniFormat)
    )


def test_auto_book_defaults_and_existing_trade_values_are_not_migrated(tmp_path) -> None:
    assert DEFAULT_TRADE_INPUTS["auto_book"] is False
    assert DEFAULT_TRADE_INPUTS["book_profit_threshold"] == 500000

    fresh = _repository(tmp_path / "fresh.ini").load_trade_inputs()
    assert fresh["auto_book"] is False
    assert fresh["book_profit_threshold"] == 500000

    settings = QSettings(str(tmp_path / "existing.ini"), QSettings.Format.IniFormat)
    settings.setValue(
        "trade/inputs_json",
        json.dumps({"book_budget": 37, "book_profit_threshold": 15000}),
    )
    settings.sync()
    repository = ResonanceConfigRepository(settings)

    loaded = repository.load_trade_inputs()
    assert loaded["auto_book"] is False
    assert loaded["book_budget"] == 37
    assert loaded["book_profit_threshold"] == 15000

    loaded["auto_book"] = True
    repository.save_trade_inputs(loaded)
    persisted = repository.load_trade_inputs()
    assert persisted["auto_book"] is True
    assert persisted["book_budget"] == 37


def test_auto_book_task_normalization_and_trade_result_summary() -> None:
    saved = {"auto_book": True, "book_budget": 29, "fatigue_budget": 700}
    task = normalize_trade_task_inputs(saved)

    assert task == {"auto_book": True, "fatigue_budget": 700}
    assert saved["book_budget"] == 29
    assert normalize_trade_task_inputs(
        {"auto_book": False, "book_budget": 29}
    )["book_budget"] == 29

    summary = trade_result_summary(
        {
            "final_result": {
                "books_used": 7,
                "book_incremental_profit": 4480000,
                "book_incremental_profit_exact": "4480000",
                "average_book_profit": 640000,
                "average_book_profit_exact": "640000",
            }
        }
    )
    assert summary["book_incremental_profit"] == 4480000
    assert summary["book_incremental_profit_exact"] == "4480000"
    assert summary["average_book_profit"] == 640000
    assert summary["average_book_profit_exact"] == "640000"


def test_auto_book_controls_sync_preserve_budgets_and_survive_busy_state(tmp_path) -> None:
    _application()
    window = ResonanceMainWindow(
        settings=_repository(tmp_path / "controls.ini"),
        initialize_on_startup=False,
        update_checker=lambda: "",
    )
    try:
        trade = window.trade_page
        workflow = window.workflow_page
        checks = (trade.auto_book, workflow.trade_auto_book)

        labels = [
            label.text()
            for label in window.findChildren(QLabel)
            if label.text() == "Auto Book 模式"
        ]
        assert len(labels) == 2
        for check in checks:
            assert check.text() == ""
            assert check.accessibleName() == "Auto Book 模式"
            assert "收益阈值" in check.toolTip()

        trade.book_budget.setValue(19)
        workflow.trade_books.setValue(23)
        trade.auto_book.setChecked(True)
        assert workflow.trade_auto_book.isChecked()
        assert trade.book_budget.value() == 19
        assert workflow.trade_books.value() == 23
        assert not trade.book_budget.isEnabled()
        assert not workflow.trade_books.isEnabled()

        trade.set_busy(True)
        trade.set_busy(False)
        assert not trade.book_budget.isEnabled()

        workflow.begin_workflow(["commerce"], ["trade"], {"auto_book": True})
        workflow.finish_workflow(success=True, message="done")
        assert not workflow.trade_books.isEnabled()

        workflow.trade_auto_book.setChecked(False)
        assert not trade.auto_book.isChecked()
        assert trade.book_budget.value() == 19
        assert workflow.trade_books.value() == 23
        assert trade.book_budget.isEnabled()
        assert workflow.trade_books.isEnabled()
    finally:
        window.close()


def test_all_gui_trade_dispatches_drop_only_the_auto_book_budget(tmp_path) -> None:
    _application()
    repository = _repository(tmp_path / "dispatch.ini")
    window = ResonanceMainWindow(
        settings=repository,
        initialize_on_startup=False,
        update_checker=lambda: "",
    )
    try:
        trade = window.trade_page
        workflow = window.workflow_page
        trade.book_budget.setValue(19)
        workflow.trade_books.setValue(23)
        trade.auto_book.setChecked(True)
        trade.start_city.setCurrentIndex(trade.start_city.findData("1"))

        window.requestRunPcTrade.disconnect()
        window.requestPreviewPcTrade.disconnect()
        run_payloads: list[dict] = []
        preview_payloads: list[dict] = []
        window.requestRunPcTrade.connect(
            lambda inputs, _timeout: run_payloads.append(dict(inputs))
        )
        window.requestPreviewPcTrade.connect(
            lambda inputs, _timeout: preview_payloads.append(dict(inputs))
        )

        trade._request_start()
        trade._request_preview()
        window._preview_workflow_trade()
        assert len(run_payloads) == 1
        assert len(preview_payloads) == 2
        assert all(payload["auto_book"] is True for payload in run_payloads + preview_payloads)
        assert all("book_budget" not in payload for payload in run_payloads + preview_payloads)
        assert repository.load_trade_inputs()["book_budget"] == 19

        window._start_commerce_sequence(True, False)
        assert run_payloads[-1]["auto_book"] is True
        assert "book_budget" not in run_payloads[-1]
        assert repository.load_trade_inputs()["book_budget"] == 19
        window._finish_commerce_sequence()

        for check in workflow._task_checks.values():
            check.setChecked(False)
        workflow._task_checks["commerce"].setChecked(True)
        workflow._commerce_checks["trade"].setChecked(True)
        workflow._commerce_checks["passenger"].setChecked(False)
        window._start_workflow()
        assert run_payloads[-1]["auto_book"] is True
        assert "book_budget" not in run_payloads[-1]
        assert repository.load_trade_inputs()["book_budget"] == 23
        window._finish_workflow(True, "done")

        passenger = window.passenger_page.collect_inputs()
        saved_trade = trade.collect_inputs()
        for order in ("trade_first", "passenger_first"):
            combined = window._combined_commerce_inputs(
                order=order,
                trade=saved_trade,
                passenger=passenger,
            )
            assert combined["order"] == order
            assert combined["trade_inputs"]["auto_book"] is True
            assert "book_budget" not in combined["trade_inputs"]
    finally:
        window.close()


def test_average_book_profit_overview_is_shown_only_for_used_books(tmp_path) -> None:
    _application()
    window = ResonanceMainWindow(
        settings=_repository(tmp_path / "result.ini"),
        initialize_on_startup=False,
        update_checker=lambda: "",
    )
    try:
        page = window.trade_page
        caption = page.result_captions["average_book_profit"]
        value = page.result_values["average_book_profit"]

        page._render_overview(
            {"books_used": 7, "average_book_profit": 640000}, route=[]
        )
        assert not caption.isHidden()
        assert not value.isHidden()
        assert value.text() == "640,000"

        page._render_overview(
            {"books_used": 3, "average_book_profit": 1234.5}, route=[]
        )
        assert value.text() == "1,234.5"

        page._render_overview(
            {"books_used": 0, "average_book_profit": 640000}, route=[]
        )
        assert caption.isHidden()
        assert value.isHidden()

        page._render_overview({"books_used": 7}, route=[])
        assert caption.isHidden()
        assert value.isHidden()
    finally:
        window.close()
