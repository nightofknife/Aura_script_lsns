"""Multi-region capture boundaries through public observation APIs."""
import asyncio
from types import SimpleNamespace
from uuid import uuid4

import numpy as np
import pytest

from packages.aura_core.scheduler.operation_scope import OperationScope, OperationTimeout
from plans.aura_base.src.services.observation_service import CaptureError, ObservationService
from plans.aura_base.src.services.vision_service import MatchResult


REGIONS = ((10, 20, 4, 3), (18, 25, 2, 2))
ENCLOSING = (10, 20, 10, 7)


class CaptureApp:
    def __init__(self):
        self.events = []
        self.on_capture = lambda: None
        self.transform = lambda result: result

    def capture(self, rect=None):
        self.events.append(rect)
        x, y, width, height = rect
        rows, columns = np.indices((height, width))
        image = np.empty((height, width, 3), dtype=np.uint8)
        image[:, :, 0] = x + columns
        image[:, :, 1] = y + rows
        image[:, :, 2] = len(self.events)
        result = SimpleNamespace(success=True, image=image, relative_rect=rect,
                                 error_message="backend unavailable")
        self.on_capture()
        return self.transform(result)


class CropMatcher:
    def __init__(self):
        self.sources = []

    def find_template(self, *, source_image, **options):
        self.sources.append(source_image.copy())
        return MatchResult(found=True, center_point=(1, 1), confidence=.99)


def capture(service, regions=REGIONS, *, asynchronous=False, scope=None):
    if asynchronous:
        return asyncio.run(service.capture_regions_async(regions, scope=scope))
    return service.capture_regions(regions, scope=scope)


@pytest.mark.parametrize("asynchronous", [False, True])
def test_regions_match_absolute_crops_from_exactly_one_capture(asynchronous):
    app, vision = CaptureApp(), CropMatcher()
    service = ObservationService(app, vision=vision)
    frame = capture(service, asynchronous=asynchronous)
    template = np.ones((1, 1, 3), dtype=np.uint8)
    results = [service.match_template(frame, template, roi=roi) for roi in REGIONS]

    assert app.events == [ENCLOSING]
    assert frame.roi == ENCLOSING
    assert [result.frame_id for result in results] == [frame.id, frame.id]
    assert [result.client_center for result in results] == [(11, 21), (19, 26)]
    assert [source.shape for source in vision.sources] == [(3, 4, 3), (2, 2, 3)]
    assert [source[0, 0].tolist() for source in vision.sources] == [[10, 20, 1], [18, 25, 1]]
    assert all(frame.crop(roi).timestamp == frame.timestamp for roi in REGIONS)


@pytest.mark.parametrize("asynchronous", [False, True])
def test_separate_rounds_always_capture_new_pixels_and_frame_ids(asynchronous):
    app = CaptureApp()
    service = ObservationService(app)
    first = capture(service, asynchronous=asynchronous)
    second = capture(service, asynchronous=asynchronous)
    assert app.events == [ENCLOSING, ENCLOSING]
    assert first.id != second.id
    assert first.crop(REGIONS[0]).image[0, 0, 2] == 1
    assert second.crop(REGIONS[0]).image[0, 0, 2] == 2


@pytest.mark.parametrize("regions", [
    (), ((-1, 0, 1, 1),), ((0, 0, 0, 1),), ((0, 0, 1, -1),),
    ((0, 0, 1),), ((0, 0, 1.5, 1),), ((True, 0, 1, 1),),
    ((0, 0, float("inf"), 1),), ((0, 0, float("nan"), 1),),
])
def test_empty_or_invalid_regions_fail_before_capture(regions):
    app = CaptureApp()
    with pytest.raises(ValueError):
        ObservationService(app).capture_regions(regions)
    assert app.events == []


@pytest.mark.parametrize("failure,reason", [
    ("reported", "capture_failed"), ("exception", "capture_failed"),
    ("size", "capture_size_invalid"), ("clipped", "capture_size_invalid"),
    ("shifted", "capture_region_mismatch"),
])
@pytest.mark.parametrize("asynchronous", [False, True])
def test_capture_failures_keep_reason_and_requested_enclosing_roi(failure, reason, asynchronous):
    app = CaptureApp()

    def transform(result):
        if failure == "reported":
            result.success = False
        elif failure == "exception":
            raise RuntimeError("capture backend failed")
        elif failure == "size":
            result.image = result.image[:-1]
        elif failure == "clipped":
            result.image = result.image[:-1]
            result.relative_rect = (10, 20, 10, 6)
        elif failure == "shifted":
            result.relative_rect = (11, 20, 10, 7)
        return result

    app.transform = transform
    with pytest.raises(CaptureError) as error:
        capture(ObservationService(app), asynchronous=asynchronous)
    assert error.value.reason == reason
    assert error.value.roi == ENCLOSING
    assert app.events == [ENCLOSING]


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("stop", ["cancel", "timeout"])
@pytest.mark.parametrize("during_capture", [False, True])
def test_stopped_round_never_returns_frame_or_starts_later_capture(asynchronous, stop, during_capture):
    app = CaptureApp()
    now = [10.]
    scope = OperationScope(1., cid=uuid4().hex, monotonic=lambda: now[0])

    def interrupt():
        if stop == "cancel":
            scope.cancel()
        else:
            now[0] = 12.

    if during_capture:
        app.on_capture = interrupt
    else:
        interrupt()
    exception = asyncio.CancelledError if stop == "cancel" else OperationTimeout
    service = ObservationService(app)
    with pytest.raises(exception):
        capture(service, asynchronous=asynchronous, scope=scope)
    expected = [ENCLOSING] if during_capture else []
    assert app.events == expected
    with pytest.raises(exception):
        capture(service, asynchronous=asynchronous, scope=scope)
    assert app.events == expected
