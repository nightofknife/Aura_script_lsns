"""Profile fatigue uses two same-frame digit crops and fresh stable reads."""
from pathlib import Path
from types import SimpleNamespace as NS

import cv2
import pytest

from packages.aura_core.context.persistence.persistent_data_service import PersistentDataService
from plans.resonance_pc.src.actions import player_data_pc_actions as player


FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/profile_status_digits/fatigue_58_864.png"


@pytest.fixture
def clock(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(player.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(player.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))
    return now


def readers(observations):
    image = cv2.imread(str(FIXTURE))
    assert image is not None

    class App:
        captures = 0
        side = 0
        clicks = []

        def capture(self, *, rect):
            assert rect == player._PROFILE_FIELD_REGIONS["fatigue"]
            self.captures += 1
            self.side = 0
            return NS(success=True, image=image.copy())

        def click(self, **point):
            self.clicks.append(point)

    app = App()

    class Ocr:
        crops = []

        def recognize_all(self, *, source_image):
            value = observations[min(app.captures - 1, len(observations) - 1)]
            assert app.side < 2, "each frame must recognize only the two digit sides"
            self.crops.append(source_image.copy())
            text = str(value[app.side])
            confidence = value[2]
            app.side += 1
            return NS(results=[NS(text=text, confidence=confidence)])

    return app, Ocr()


@pytest.mark.parametrize(("observations", "captures", "expected"), [
    ([(58, 864, .99)], 2, {"current": 58, "max": 864}),
    ([(58, 864, .99), (59, 864, .99), (59, 864, .99)], 3, {"current": 59, "max": 864}),
    ([(58, 864, .99), (58, 864, .89), (58, 864, .99), (58, 864, .99)], 4,
     {"current": 58, "max": 864}),
    ([(1200, 864, .99)], 2, {"current": 1200, "max": 864}),
])
def test_profile_reads_digit_crops_and_confirms_fresh_pairs(clock, observations, captures, expected):
    app, ocr = readers(observations)
    updates = []
    assert player._read_profile_stage(app, ocr, ["fatigue"], on_updated=updates.append) == {"fatigue": expected}
    assert updates == ["fatigue"]
    assert app.captures == captures
    assert len(ocr.crops) >= captures
    assert all(crop.shape[1] < 98 * 3 for crop in ocr.crops)


def test_unconfirmed_profile_fatigue_does_not_mark_or_save(tmp_path, monkeypatch, clock):
    service = PersistentDataService(tmp_path / "install")
    old = {"status": {"fatigue": {"current": 40, "max": 864}},
           "metadata": {"profile_section_updated_at": {"fatigue": "old"}}}
    service.set(player.USER_INFO_FILE, [], old)
    app, ocr = readers([(58, 864, .89)])
    monkeypatch.setattr(player, "_wait_for_any_marker", lambda *args, **kwargs: [])
    monkeypatch.setattr(player, "_wait_for_profile_panel", lambda *args, **kwargs: None)
    monkeypatch.setattr(player, "_best_effort_return_to_main", lambda *args: None)
    with pytest.raises(player.StopTaskException, match="invalid fatigue digits"):
        player.resonance_pc_player_data_refresh(
            stages=["profile"], profile_sections=["fatigue"],
            app=app, ocr=ocr, vision=object(), persistent_data=service,
        )
    assert service.read(player.USER_INFO_FILE) == old


def test_slow_second_read_cannot_commit_profile_fatigue(clock, monkeypatch):
    app, ocr = readers([(58, 864, .99)])
    recognize = ocr.recognize_all

    def slow(*, source_image):
        result = recognize(source_image=source_image)
        if app.captures == 2:
            clock[0] += 3
        return result

    monkeypatch.setattr(ocr, "recognize_all", slow)
    updates = []
    with pytest.raises(player.StopTaskException, match="observation_timeout"):
        player._read_profile_stage(app, ocr, ["fatigue"], on_updated=updates.append)
    assert updates == []


def test_profile_fatigue_cancelled_before_capture(clock, monkeypatch):
    app, ocr = readers([(58, 864, .99)])
    monkeypatch.setattr(player, "check_cancelled", lambda: (_ for _ in ()).throw(player.StopTaskException("cancelled")))
    with pytest.raises(player.StopTaskException, match="cancelled"):
        player._read_profile_fatigue(app, ocr)
    assert app.captures == 0
