"""Battle shutdown keeps its cleanup lock and the unified display subscription."""
from pathlib import Path
from types import SimpleNamespace
import threading

import pytest
from PySide6.QtCore import Qt

from packages.aura_core.observability.logging import coordinator, stream_capture
from packages.resonance_gui.bridge import RunnerBridge
from packages.resonance_gui.logic import PC_BATTLE_TASK_REF


@pytest.mark.parametrize("native_stopped", [True, False])
def test_soft_battle_close_retains_logs_until_native_stop_is_confirmed(tmp_path, monkeypatch, native_stopped):
    coordinator.close_session()
    monkeypatch.delenv("AURA_LOG_DESCRIPTOR", raising=False)
    monkeypatch.delenv("AURA_LOG_SESSION_ID", raising=False)
    monkeypatch.setattr(stream_capture, "install_capture", lambda **kwargs: None)
    monkeypatch.setattr(stream_capture, "uninstall_capture", lambda: None)
    descriptor = coordinator.start_session(tmp_path / "logs", descriptor={}, role="test_gui")
    bridge = RunnerBridge()
    closed, stopped, failures, messages = [], [], [], []
    status_seen = threading.Event()
    soft_message = "已请求作战软停止，等待原生自动关闭与页面收尾；不会启动后续工作流程。"

    def observe(message):
        messages.append(message)
        if message == soft_message:
            status_seen.set()

    bridge.logMessage.connect(observe, Qt.ConnectionType.DirectConnection)
    bridge.closeFailed.connect(failures.append)
    bridge._runner = SimpleNamespace(
        request_battle_stop=lambda cid: stopped.append(cid) or {
            "status": "success", "soft_stop_requested": True,
        },
        close=lambda: closed.append(True),
    )
    bridge._current_cid = "battle-logging"
    bridge._current_item = {"task_ref": PC_BATTLE_TASK_REF, "label": "今日作战"}
    bridge._busy = True
    try:
        bridge.close()
        assert stopped == ["battle-logging"]
        assert bridge.busy and not bridge._closed and bridge._log_unsubscribe is not None
        assert status_seen.wait(5)
        assert messages.count(soft_message) == 1
        assert coordinator.flush()
        text = Path(descriptor["log_path"]).read_text(encoding="utf-8")
        assert text.count("[GuiStatus] " + soft_message) == 1

        bridge._finish_current({
            "cid": "battle-logging", "status": "success", "execution_pending": False,
            "final_result": {"user_data": {
                "schema": "resonance_pc.battle_plan_result.v1", "status": "cancelled",
                "structural": {"schema": "resonance_pc.structural_native_result.v1",
                               "mode": "native_auto", "native_stopped": native_stopped},
            }},
        })
        if native_stopped:
            assert bridge._closed and bridge._log_unsubscribe is None
            assert closed == [True] and not failures
        else:
            assert not bridge._closed and bridge.busy and bridge.current_cid == "battle-logging"
            assert bridge._log_unsubscribe is not None
            assert failures and not closed
    finally:
        bridge._reset_current()
        bridge.close()
        coordinator.close_session()
