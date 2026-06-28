"""Actions used by runtime benchmark tasks."""

from __future__ import annotations

import time
from typing import Any, Dict, Optional

from packages.aura_core.api import action_info, requires_services
from ..services.benchmark_probe_service import BenchmarkProbeService


@action_info(name="benchmark_reset", public=True, read_only=False, description="Reset metrics for one benchmark scenario.")
@requires_services(benchmark_probe="benchmark_probe")
def benchmark_reset(scenario: str = "default", benchmark_probe: BenchmarkProbeService | None = None) -> Dict[str, Any]:
    if benchmark_probe is None:
        raise RuntimeError("benchmark_probe service is not available.")
    return benchmark_probe.reset(scenario)


@action_info(name="benchmark_sleep", public=True, read_only=False, description="Sleep for N ms and record concurrency metrics.")
@requires_services(benchmark_probe="benchmark_probe")
def benchmark_sleep(
    duration_ms: int = 100,
    scenario: str = "default",
    label: Optional[str] = None,
    payload: Optional[Dict[str, Any]] = None,
    benchmark_probe: BenchmarkProbeService | None = None,
) -> Dict[str, Any]:
    if benchmark_probe is None:
        raise RuntimeError("benchmark_probe service is not available.")

    ticket = benchmark_probe.begin(scenario=scenario, label=label, payload=payload)
    try:
        time.sleep(max(float(duration_ms), 0.0) / 1000.0)
        return benchmark_probe.end(
            ticket,
            success=True,
            extra={
                "requested_duration_ms": float(duration_ms),
                "payload": payload,
            },
        )
    except Exception as exc:
        benchmark_probe.end(ticket, success=False, extra={"error": str(exc), "payload": payload})
        raise


@action_info(name="benchmark_snapshot", public=True, read_only=True, description="Return metrics snapshot for one benchmark scenario.")
@requires_services(benchmark_probe="benchmark_probe")
def benchmark_snapshot(
    scenario: str = "default",
    benchmark_probe: BenchmarkProbeService | None = None,
) -> Dict[str, Any]:
    if benchmark_probe is None:
        raise RuntimeError("benchmark_probe service is not available.")
    return benchmark_probe.snapshot(scenario)
