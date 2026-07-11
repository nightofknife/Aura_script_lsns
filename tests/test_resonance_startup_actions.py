from __future__ import annotations

from types import SimpleNamespace

import numpy as np

from packages.aura_core.utils.exceptions import StopTaskException
from plans.aura_base.src.services.ocr_service import MultiOcrResult, OcrResult
from plans.resonance.src.actions import startup_actions
from plans.resonance.src.actions.startup_actions import resonance_close_game, resonance_detect_startup_state, resonance_enter_main


class _FakeApp:
    def __init__(self):
        self.clicks = []
        self.launches = []
        self.force_stops = []
        self.keys = []

    def capture(self, rect=None):
        return SimpleNamespace(success=True, image=np.zeros((720, 1280, 3), dtype=np.uint8), rect=rect)

    def click(self, x=None, y=None, **_kwargs):
        self.clicks.append((x, y))

    def launch_app(self, package_name, **kwargs):
        self.launches.append((package_name, kwargs))
        return {"launched": True, "package": package_name}

    def force_stop_app(self, package_name, **kwargs):
        self.force_stops.append((package_name, kwargs))
        return {"stopped": True, "method": "am_force_stop", "package": package_name}

    def press_key(self, key, presses=1, interval=None):
        self.keys.append((key, presses, interval))


class _FakeOcr:
    def __init__(self, texts):
        self.texts = texts

    def recognize_all(self, source_image):
        texts = self.texts
        if self.texts and isinstance(self.texts[0], list):
            texts = self.texts.pop(0)
        return MultiOcrResult(
            count=len(texts),
            results=[
                OcrResult(found=True, text=text, center_point=(100 + index, 200 + index), confidence=0.95)
                for index, text in enumerate(texts)
            ],
        )


def _detect(texts):
    return resonance_detect_startup_state(app=_FakeApp(), ocr=_FakeOcr(texts))


def test_detect_startup_state_main_screen():
    result = _detect(["任务", "访问城市", "启程", "资产"])

    assert result["state"] == "main"
    assert result["main"] is True
    assert result["matched"]["main"][0]["marker"] == "访问城市"


def test_detect_startup_state_title_screen():
    result = _detect(["点击屏幕进入游戏", "173****9178"])

    assert result["state"] == "title"
    assert result["title"] is True
    assert result["main"] is False


def test_detect_startup_state_health_warning_title_screen():
    result = _detect(["SOLSTICESTUDIO", "健康游戏忠告"])

    assert result["state"] == "title"
    assert result["title"] is True
    assert result["main"] is False


def test_detect_startup_state_train_screen():
    result = _detect(["列车电力", "电力等级", "引擎核心"])

    assert result["state"] == "train"
    assert result["train"] is True
    assert result["main"] is False


def test_detect_startup_state_login_required():
    result = _detect(["手机号", "验证码", "账号登录"])

    assert result["state"] == "login_required"
    assert result["login_required"] is True
    assert result["main"] is False


def test_detect_startup_state_info_panel():
    result = _detect(["资讯", "公告", "触碰空白区域退出"])

    assert result["state"] == "info_panel"
    assert result["info_panel"] is True
    assert result["main"] is False


def test_detect_startup_state_external_web():
    result = _detect(["返回", "雷索纳斯", "微博认证：雷索纳斯官方微博", "精选", "超话", "相册"])

    assert result["state"] == "external_web"
    assert result["external_web"] is True
    assert result["main"] is False


def test_detect_startup_state_update():
    result = _detect(["资源更新", "下载中", "128MB"])

    assert result["state"] == "update"
    assert result["update"] is True
    assert result["main"] is False


def test_detect_startup_state_download_complete_is_title():
    result = _detect(["下载已经完成，点击任意位置进入游戏"])

    assert result["state"] == "title"
    assert result["title"] is True
    assert result["update"] is False
    assert result["main"] is False


def test_enter_main_launches_clicks_and_stops_when_main_reached():
    app = _FakeApp()
    ocr = _FakeOcr([["点击屏幕进入游戏"], ["点击屏幕进入游戏"], ["访问城市"]])

    result = resonance_enter_main(app=app, ocr=ocr, round_interval_sec=0, main_stable_sec=0, max_settle_rounds=20)

    assert result["reached_main"] is True
    assert result["rounds"] == 2
    assert app.launches == [("com.hermes.goda", {"timeout_sec": 10.0})]
    assert app.clicks == [(640, 560)]


def test_enter_main_clicks_overlay_text_before_fixed_point():
    app = _FakeApp()
    ocr = _FakeOcr([["公告", "确定"], ["公告", "确定"], ["访问城市"]])

    result = resonance_enter_main(app=app, ocr=ocr, round_interval_sec=0, main_stable_sec=0, max_settle_rounds=20)

    assert result["reached_main"] is True
    assert app.clicks == [(101, 201)]
    assert result["history"][1]["action"] == "click_overlay"


def test_enter_main_closes_info_panel_with_blank_area():
    app = _FakeApp()
    ocr = _FakeOcr([["点击屏幕进入游戏"], ["资讯", "公告", "触碰空白区域退出"], ["访问城市"]])

    result = resonance_enter_main(app=app, ocr=ocr, round_interval_sec=0, main_stable_sec=0, max_settle_rounds=20)

    assert result["reached_main"] is True
    assert app.clicks == [(640, 675)]
    assert result["history"][1]["action"] == "close_info_panel"


def test_enter_main_presses_back_from_external_web():
    app = _FakeApp()
    ocr = _FakeOcr([["点击屏幕进入游戏"], ["返回", "微博认证：雷索纳斯官方微博", "精选", "超话"], ["访问城市"]])

    result = resonance_enter_main(app=app, ocr=ocr, round_interval_sec=0, main_stable_sec=0, max_settle_rounds=20)

    assert result["reached_main"] is True
    assert app.clicks == []
    assert app.keys == [("back", 1, None)]
    assert result["history"][1]["action"] == "press_back_external_web"


def test_enter_main_waits_on_update_without_fixed_click():
    app = _FakeApp()
    ocr = _FakeOcr([["点击屏幕进入游戏"], ["资源更新", "下载中", "128MB"], ["访问城市"]])

    result = resonance_enter_main(app=app, ocr=ocr, round_interval_sec=0, main_stable_sec=0, max_settle_rounds=20)

    assert result["reached_main"] is True
    assert app.clicks == []
    assert result["history"][1]["action"] == "wait_update"


def test_enter_main_requires_continuous_main_stability(monkeypatch):
    app = _FakeApp()
    ocr = _FakeOcr(
        [
            ["点击屏幕进入游戏"],
            ["访问城市"],
            ["访问城市"],
            ["任务"],
            ["访问城市"],
            ["访问城市"],
            ["访问城市"],
        ]
    )
    clock = {"now": 0.0}

    def fake_monotonic():
        return clock["now"]

    def fake_sleep(seconds):
        clock["now"] += float(seconds)

    monkeypatch.setattr(startup_actions.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(startup_actions.time, "sleep", fake_sleep)

    result = resonance_enter_main(app=app, ocr=ocr, round_interval_sec=0.5, main_stable_sec=1.0, max_settle_rounds=20)

    assert result["reached_main"] is True
    assert result["main_stable_sec"] >= 1.0
    assert app.clicks == [(640, 560)]
    assert any(row["action"] == "observe_main" for row in result["history"])


def test_enter_main_fails_on_login_required():
    app = _FakeApp()
    ocr = _FakeOcr([["手机号", "验证码", "账号登录"]])

    try:
        resonance_enter_main(app=app, ocr=ocr, round_interval_sec=0, max_settle_rounds=20)
    except StopTaskException as exc:
        assert exc.success is False
        assert "manual login" in str(exc)
    else:
        raise AssertionError("expected StopTaskException")


def test_close_game_force_stops_default_package():
    app = _FakeApp()

    result = resonance_close_game(app=app)

    assert result["ok"] is True
    assert result["stopped"] is True
    assert result["package"] == "com.hermes.goda"
    assert app.force_stops == [("com.hermes.goda", {"timeout_sec": 10.0})]
