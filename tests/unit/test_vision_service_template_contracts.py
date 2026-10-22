"""Public template API contracts using small, deterministic OpenCV images."""
from __future__ import annotations

import asyncio
from collections import defaultdict
import threading

import cv2
import numpy as np
import pytest

from plans.aura_base.src.services import _vision_timing as timing_module
from plans.aura_base.src.services.vision_service import MatchResult, MultiMatchResult, VisionService


OPERATIONS = (
    "find_template",
    "find_all_templates",
    "find_templates_batch",
    "find_all_templates_batch",
)
BATCH_OPERATIONS = OPERATIONS[2:]


@pytest.fixture(autouse=True)
def isolated_timing(monkeypatch):
    # Do not depend on the developer's process-level sampling setting or counters.
    monkeypatch.setattr(timing_module, "_SAMPLE_EVERY", 0)
    monkeypatch.setattr(timing_module, "_sample_counts", defaultdict(int))


@pytest.fixture
def scene():
    source = np.random.default_rng(731).integers(20, 236, (28, 40, 3), dtype=np.uint8)
    positions = [(3, 2), (14, 2), (3, 15), (22, 17)]
    templates = [source[y:y + 5, x:x + 6].copy() for x, y in positions]
    return source, templates, positions


def _arguments(operation, scene):
    source, templates, _ = scene
    arguments = {
        "source_image": source,
        "threshold": 0.9999,
        "use_grayscale": False,
        "match_method": cv2.TM_CCOEFF_NORMED,
        "preprocess": "none",
    }
    if operation in BATCH_OPERATIONS:
        arguments["template_images"] = templates
    else:
        arguments["template_image"] = templates[0]
    return arguments


def _invoke(service, operation, execution, arguments):
    method = getattr(service, operation + ("_async" if execution == "async" else ""))
    if execution == "async":
        return asyncio.run(method(**arguments))
    return method(**arguments)


def _matches(operation, result):
    results = result if operation in BATCH_OPERATIONS else [result]
    if operation in ("find_all_templates", "find_all_templates_batch"):
        for item in results:
            assert isinstance(item, MultiMatchResult)
            assert item.count == len(item.matches) == 1
        return [item.matches[0] for item in results]
    return results


def _assert_hits(operation, result, scene):
    matches = _matches(operation, result)
    positions = scene[2] if operation in BATCH_OPERATIONS else scene[2][:1]
    assert len(matches) == len(positions)
    for match, (x, y) in zip(matches, positions):
        assert isinstance(match, MatchResult)
        assert match.found is True
        assert match.top_left == (x, y)
        assert match.center_point == (x + 3, y + 2)
        assert match.rect == (x, y, 6, 5)
        assert match.confidence == pytest.approx(1.0, abs=1e-5)


@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize("execution", ("sync", "async"))
def test_real_matches_keep_coordinates_and_inputs(operation, execution, scene):
    source_before = scene[0].copy()
    templates_before = [image.copy() for image in scene[1]]
    result = _invoke(VisionService(), operation, execution, _arguments(operation, scene))
    _assert_hits(operation, result, scene)
    np.testing.assert_array_equal(scene[0], source_before)
    for actual, expected in zip(scene[1], templates_before):
        np.testing.assert_array_equal(actual, expected)


@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize("execution", ("sync", "async"))
@pytest.mark.parametrize("error_type", (FileNotFoundError, TypeError, ValueError))
@pytest.mark.parametrize("failure_stage", ("prepare", "match"))
def test_handled_errors_keep_each_api_failure_shape(
    monkeypatch, operation, execution, error_type, failure_stage, scene,
):
    service = VisionService()

    def fail(*args, **kwargs):
        raise error_type("injected template failure")

    if failure_stage == "prepare":
        monkeypatch.setattr(service, "_prepare_image", fail)
    else:
        monkeypatch.setattr(cv2, "matchTemplate", fail)
    result = _invoke(service, operation, execution, _arguments(operation, scene))
    results = result if operation in BATCH_OPERATIONS else [result]
    assert len(results) == (len(scene[1]) if operation in BATCH_OPERATIONS else 1)
    for item in results:
        if operation in ("find_all_templates", "find_all_templates_batch"):
            assert item == MultiMatchResult()
        else:
            assert item == MatchResult(debug_info={"error": "injected template failure"})


@pytest.mark.parametrize("operation", BATCH_OPERATIONS)
@pytest.mark.parametrize("execution", ("sync", "async"))
def test_mask_count_error_escapes_before_image_work(monkeypatch, operation, execution, scene):
    service = VisionService()

    def unexpected_preparation(*args, **kwargs):
        raise AssertionError("invalid mask count must be rejected before preparation")

    monkeypatch.setattr(service, "_prepare_image", unexpected_preparation)
    arguments = _arguments(operation, scene)
    arguments["mask_images"] = [None] * (len(scene[1]) - 1)
    with pytest.raises(ValueError, match="mask_images length must match template_images length"):
        _invoke(service, operation, execution, arguments)


@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize("execution", ("sync", "async"))
@pytest.mark.parametrize("sample_every", (0, 1))
def test_opencv_error_propagates_unchanged(monkeypatch, operation, execution, sample_every, scene):
    error = cv2.error("injected OpenCV failure")
    monkeypatch.setattr(timing_module, "_SAMPLE_EVERY", sample_every)
    monkeypatch.setattr(timing_module, "log_event", lambda *args, **kwargs: None)

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(cv2, "matchTemplate", fail)
    with pytest.raises(cv2.error) as raised:
        _invoke(VisionService(), operation, execution, _arguments(operation, scene))
    assert raised.value is error


@pytest.mark.parametrize("operation", BATCH_OPERATIONS)
@pytest.mark.parametrize("execution", ("sync", "async"))
def test_parallel_batch_returns_input_order_when_workers_finish_in_reverse(
    monkeypatch, operation, execution, scene,
):
    service = VisionService()
    method_name = (
        "_match_all_templates_prepared"
        if operation == "find_all_templates_batch" else "_match_template_prepared"
    )
    original = getattr(service, method_name)
    template_indices = {id(image): index for index, image in enumerate(scene[1])}
    barrier = threading.Barrier(len(scene[1]), timeout=5)
    finished = [threading.Event() for _ in scene[1]]
    completion_order = []
    completion_lock = threading.Lock()

    def reverse_completion(source, template, *args, **kwargs):
        index = template_indices[id(template)]
        result = original(source, template, *args, **kwargs)
        # A serial implementation cannot pass this barrier. Events enforce order
        # without relying on short sleeps or the operating system's scheduling.
        barrier.wait()
        if index < len(finished) - 1:
            assert finished[index + 1].wait(timeout=5), "later template worker did not finish"
        with completion_lock:
            completion_order.append(index)
        finished[index].set()
        return result

    monkeypatch.setattr(service, method_name, reverse_completion)
    result = _invoke(service, operation, execution, _arguments(operation, scene))
    assert completion_order == [3, 2, 1, 0]
    _assert_hits(operation, result, scene)


@pytest.mark.parametrize("operation", OPERATIONS)
def test_sync_call_rejects_scheduler_loop_before_preparation(monkeypatch, operation, scene):
    service = VisionService()

    def unexpected_preparation(*args, **kwargs):
        raise AssertionError("loop-thread misuse must be rejected before preparation")

    monkeypatch.setattr(service, "_prepare_image", unexpected_preparation)

    async def call_from_scheduler_loop():
        service._loop = asyncio.get_running_loop()
        with pytest.raises(RuntimeError, match="sync API called from event loop thread"):
            getattr(service, operation)(**_arguments(operation, scene))

    asyncio.run(call_from_scheduler_loop())


@pytest.mark.parametrize("operation", OPERATIONS)
def test_sync_guard_retains_other_loop_allowance(monkeypatch, operation, scene):
    service = VisionService()
    # The existing guard rejects the scheduler loop specifically, not every
    # thread with an event loop. Stub its identity without creating a second loop.
    monkeypatch.setattr(service, "_get_running_loop", lambda: object())

    async def call_from_other_loop():
        return getattr(service, operation)(**_arguments(operation, scene))

    _assert_hits(operation, asyncio.run(call_from_other_loop()), scene)


@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize("execution", ("sync", "async"))
def test_preparation_runs_in_the_expected_thread(monkeypatch, operation, execution, scene):
    service = VisionService()
    original = service._prepare_image
    caller_thread = threading.get_ident()
    preparation_threads = []

    def record_preparation(*args, **kwargs):
        preparation_threads.append(threading.get_ident())
        return original(*args, **kwargs)

    monkeypatch.setattr(service, "_prepare_image", record_preparation)
    result = _invoke(service, operation, execution, _arguments(operation, scene))
    _assert_hits(operation, result, scene)
    assert preparation_threads
    if execution == "async":
        assert all(thread != caller_thread for thread in preparation_threads)
    else:
        assert all(thread == caller_thread for thread in preparation_threads)


@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize("execution", ("sync", "async"))
def test_sampling_does_not_change_matches_or_debug_data(monkeypatch, operation, execution, scene):
    service = VisionService()
    events = []
    monkeypatch.setattr(timing_module, "log_event", lambda event, **fields: events.append((event, fields)))
    arguments = _arguments(operation, scene)
    baseline = _invoke(service, operation, execution, arguments)
    assert events == []

    monkeypatch.setattr(timing_module, "_SAMPLE_EVERY", 1)
    sampled = _invoke(service, operation, execution, arguments)
    _assert_hits(operation, sampled, scene)
    for before, after in zip(_matches(operation, baseline), _matches(operation, sampled)):
        assert after.found == before.found
        assert after.top_left == before.top_left
        assert after.center_point == before.center_point
        assert after.rect == before.rect
        assert after.confidence == pytest.approx(before.confidence, abs=1e-6)
        assert after.debug_info == before.debug_info
    assert len(events) == 1
    event, fields = events[0]
    assert event == "vision.timing"
    assert fields["operation"] == operation
    assert fields["status"] == "completed"
    assert fields["stage_calls"]["match_sum"] == (4 if operation in BATCH_OPERATIONS else 1)


@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize("execution", ("sync", "async"))
def test_timing_sink_failure_cannot_replace_results(monkeypatch, operation, execution, scene):
    monkeypatch.setattr(timing_module, "_SAMPLE_EVERY", 1)

    def fail_to_log(*args, **kwargs):
        raise RuntimeError("timing sink unavailable")

    monkeypatch.setattr(timing_module, "log_event", fail_to_log)
    result = _invoke(VisionService(), operation, execution, _arguments(operation, scene))
    _assert_hits(operation, result, scene)
