"""Optional sampled timing for template matching; never stores image pixels."""
from __future__ import annotations

from collections import defaultdict
import os
import threading
import time

from packages.aura_core.observability.runtime_trace import log_event


def _sample_every() -> int:
    try:
        return max(int(os.environ.get("AURA_VISION_TIMING_SAMPLE_EVERY", "0")), 0)
    except ValueError:
        return 0


# Read once at process startup. Zero disables both clocks and trace emission.
_SAMPLE_EVERY = _sample_every()
_sample_lock = threading.Lock()
_sample_counts: dict[str, int] = defaultdict(int)


class VisionTiming:
    """One sampled request, including worker stages which may overlap in a batch."""

    def __init__(self, operation: str, metadata: dict):
        self.operation = operation
        self.metadata = metadata
        self.submitted_at = time.perf_counter()
        self.started_at: float | None = None
        self.stage_ms: dict[str, float] = defaultdict(float)
        self.stage_calls: dict[str, int] = defaultdict(int)
        self.candidate_count = 0
        self.error_type: str | None = None
        self._lock = threading.Lock()

    def start_work(self) -> None:
        self.started_at = time.perf_counter()
        if self.metadata.get("execution") != "async":
            self.submitted_at = self.started_at

    def handled_error(self, error: Exception) -> None:
        self.error_type = type(error).__name__

    def record(self, stage: str, started_at: float) -> None:
        elapsed_ms = (time.perf_counter() - started_at) * 1000
        with self._lock:
            self.stage_ms[stage] += elapsed_ms
            self.stage_calls[stage] += 1

    def add_candidates(self, count: int) -> None:
        with self._lock:
            self.candidate_count += count

    def finish(self, status: str = "completed") -> None:
        # Optional diagnostics must not change recognition results or exceptions.
        try:
            finished_at = time.perf_counter()
            started_at = self.started_at if self.started_at is not None else self.submitted_at
            if status == "completed" and self.error_type is not None:
                status = "handled_error"
            log_event(
                "vision.timing",
                operation=self.operation,
                status=status,
                queue_ms=round((started_at - self.submitted_at) * 1000, 3),
                work_ms=round((finished_at - started_at) * 1000, 3),
                total_ms=round((finished_at - self.submitted_at) * 1000, 3),
                stage_ms={key: round(value, 3) for key, value in self.stage_ms.items()},
                stage_calls=dict(self.stage_calls),
                candidate_count=self.candidate_count,
                error_type=self.error_type,
                **self.metadata,
            )
        except Exception:
            pass


def start_vision_timing(operation: str, **metadata) -> VisionTiming | None:
    if _SAMPLE_EVERY <= 0:
        return None
    with _sample_lock:
        _sample_counts[operation] += 1
        if (_sample_counts[operation] - 1) % _SAMPLE_EVERY:
            return None
    return VisionTiming(operation, metadata)
