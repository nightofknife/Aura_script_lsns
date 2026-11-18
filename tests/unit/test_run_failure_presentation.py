import json

import pytest

from packages.aura_core.utils.exceptions import StopTaskException
from packages.aura_core.utils.task_errors import SubtaskFailed, recognition_failure, task_error_details
from packages.resonance_gui.run_failure import present_run_failure


@pytest.mark.parametrize(("kind", "outcome", "expected"), [
    ("template", "not_found", "当前画面没有识别到"),
    ("template", "timeout", "等待 8 秒后仍未识别到"),
    ("text", "not_found", "当前画面没有读到"),
    ("text", "unconfirmed", "未能确认"),
])
def test_terminal_child_failure_keeps_the_recognition_target(kind, outcome, expected):
    context = recognition_failure("确认揽客结果", "招募成功提示", kind, outcome, timeout_sec=8)
    child = task_error_details(
        StopTaskException("private diagnostic", success=False, details=context),
        task="resonance_pc/passenger/recruit_passengers_by_flyer",
        node="recruit",
    )
    wrapped = task_error_details(
        SubtaskFailed("tasks:passenger.yaml:recruit_passengers_by_flyer", child),
        task="resonance_pc/auto_cycle_trade_pc/auto_cycle_trade_pc",
        node="run",
    )
    row = {
        "task_name": "tasks:auto_cycle_trade_pc.yaml:auto_cycle_trade_pc", "status": "failed",
        "final_result_json": json.dumps({"status": "FAILED", "error": wrapped}),
    }
    failure = present_run_failure(row)
    assert failure.title == "PC 自动跑商 › 传单揽客 · 确认揽客结果"
    assert expected + "「招募成功提示」" in failure.text
    assert "private diagnostic" not in failure.text


def test_optional_recovered_observation_does_not_become_a_terminal_failure():
    evidence = [{"evidence_id": "earlier", "error_code": "input_integrity_mismatch"}]
    row = {"status": "success", "final_result": {
        "status": "SUCCESS", "error": None, "failure_evidence": evidence,
        "user_data": {"success": True},
    }}
    assert present_run_failure(row) is None


def test_business_timeout_names_the_text_that_was_expected():
    row = {"status": "success", "task_name": "enter_main", "user_data": {
        "status": "timeout", **recognition_failure("确认启动后的主界面", "STARTENGINE", "text", "timeout"),
    }}
    assert present_run_failure(row).text == (
        "进入主界面 · 确认启动后的主界面：等待超时，未读到「STARTENGINE」。"
    )


def test_legacy_evidence_has_the_permission_code_without_exposing_its_stack():
    row = {"status": "failed", "task_name": "tasks:player_data_pc.yaml:player_data_refresh",
           "final_result": {"error": {"message": "native error", "traceback": "private stack",
                                     "failure_evidence": [{"error_code": "input_integrity_mismatch"}]}}}
    assert present_run_failure(row).text == (
        "货运前数据刷新 · 操作游戏窗口：Aura 无法操作游戏窗口。请用相同权限重新启动 Aura 和游戏。"
    )
