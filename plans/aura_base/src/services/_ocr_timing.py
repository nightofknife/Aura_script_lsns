"""Opt-in, request-local OCR wall-clock timings with non-overlapping stages."""
from __future__ import annotations

from contextlib import contextmanager, nullcontext
from time import perf_counter_ns


STAGE_NAMES = (
    "image_prepare", "det_preprocess", "det_inference", "det_postprocess", "crop",
    "ori_preprocess", "ori_inference", "ori_postprocess", "rec_preprocess",
    "rec_inference", "rec_decode", "result_pack", "result_parse",
)
_DISABLED = nullcontext()


class OcrTiming:
    """One call's cumulative stage times; no shared or pipeline-global state."""

    def __init__(self):
        self._started_ns = perf_counter_ns()
        self._stage_ns = dict.fromkeys(STAGE_NAMES, 0)
        self._stage_calls = dict.fromkeys(STAGE_NAMES, 0)
        self._recognition_batches: list[dict] = []

    def record_recognition_batch(self, input_shape, content_widths) -> None:
        self._recognition_batches.append({
            "input_shape": [int(value) for value in input_shape],
            "content_widths": [int(value) for value in content_widths],
        })

    @contextmanager
    def measure(self, stage: str):
        if stage not in self._stage_ns:
            raise ValueError(f"Unknown OCR timing stage: {stage}")
        started_ns = perf_counter_ns()
        try:
            yield
        finally:
            self._stage_ns[stage] += perf_counter_ns() - started_ns
            self._stage_calls[stage] += 1

    def snapshot(self, *, total_ms: float | None = None) -> dict:
        if total_ms is None:
            total_ms = (perf_counter_ns() - self._started_ns) / 1e6
        stages_ms = {stage: elapsed / 1e6 for stage, elapsed in self._stage_ns.items()}
        return {
            "total_ms": float(total_ms),
            "stages_ms": stages_ms,
            "other_ms": max(0.0, float(total_ms) - sum(stages_ms.values())),
            "stage_calls": dict(self._stage_calls),
            "recognition_batches": [
                {"input_shape": list(batch["input_shape"]), "content_widths": list(batch["content_widths"])}
                for batch in self._recognition_batches
            ],
        }


def timed(timing: OcrTiming | None, stage: str):
    # The default path reads no clocks and creates no profiling records.
    return _DISABLED if timing is None else timing.measure(stage)
