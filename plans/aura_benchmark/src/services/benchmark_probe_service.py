"""Runtime benchmark metrics service."""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional

from packages.aura_core.api import service_info


@service_info(
    alias="benchmark_probe",
    public=True,
    singleton=True,
    description="Collects runtime concurrency and latency metrics for benchmark tasks.",
)
class BenchmarkProbeService:
    def __init__(self):
        self._lock = threading.RLock()
        self._scenarios: Dict[str, Dict[str, Any]] = {}

    def reset(self, scenario: str) -> Dict[str, Any]:
        with self._lock:
            self._scenarios[scenario] = self._new_state(scenario)
            return self.snapshot(scenario)

    def begin(self, scenario: str, label: Optional[str] = None, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        now_ns = time.perf_counter_ns()
        label_value = label or f"run-{now_ns}"
        with self._lock:
            state = self._scenarios.setdefault(scenario, self._new_state(scenario))
            state["started_count"] += 1
            state["active_count"] += 1
            state["peak_active_count"] = max(state["peak_active_count"], state["active_count"])
            state["last_started_label"] = label_value
            if payload is not None:
                state["last_payload"] = payload
            return {
                "scenario": scenario,
                "label": label_value,
                "started_at_ns": now_ns,
            }

    def end(
        self,
        ticket: Dict[str, Any],
        *,
        success: bool,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        finished_ns = time.perf_counter_ns()
        started_ns = int(ticket.get("started_at_ns") or finished_ns)
        duration_ms = round((finished_ns - started_ns) / 1_000_000, 3)
        scenario = str(ticket.get("scenario") or "default")
        label = str(ticket.get("label") or "unknown")

        with self._lock:
            state = self._scenarios.setdefault(scenario, self._new_state(scenario))
            state["active_count"] = max(0, int(state["active_count"]) - 1)
            state["completed_count"] += 1
            if not success:
                state["failure_count"] += 1
            state["durations_ms"].append(duration_ms)
            state["last_completed_label"] = label

            record = {
                "scenario": scenario,
                "label": label,
                "success": success,
                "duration_ms": duration_ms,
            }
            if extra:
                record.update(extra)

            recent_records: List[Dict[str, Any]] = state["recent_records"]
            recent_records.append(record)
            if len(recent_records) > 25:
                del recent_records[0 : len(recent_records) - 25]

            return record

    def snapshot(self, scenario: str) -> Dict[str, Any]:
        with self._lock:
            state = self._scenarios.setdefault(scenario, self._new_state(scenario))
            durations = list(state["durations_ms"])
            completed = int(state["completed_count"])
            total_duration = round(sum(durations), 3) if durations else 0.0
            return {
                "scenario": scenario,
                "started_count": int(state["started_count"]),
                "completed_count": completed,
                "failure_count": int(state["failure_count"]),
                "active_count": int(state["active_count"]),
                "peak_active_count": int(state["peak_active_count"]),
                "avg_duration_ms": round(total_duration / completed, 3) if completed else 0.0,
                "max_duration_ms": max(durations) if durations else 0.0,
                "min_duration_ms": min(durations) if durations else 0.0,
                "total_duration_ms": total_duration,
                "last_started_label": state.get("last_started_label"),
                "last_completed_label": state.get("last_completed_label"),
                "recent_records": list(state["recent_records"]),
            }

    @staticmethod
    def _new_state(scenario: str) -> Dict[str, Any]:
        return {
            "scenario": scenario,
            "started_count": 0,
            "completed_count": 0,
            "failure_count": 0,
            "active_count": 0,
            "peak_active_count": 0,
            "durations_ms": [],
            "recent_records": [],
            "last_started_label": None,
            "last_completed_label": None,
        }
