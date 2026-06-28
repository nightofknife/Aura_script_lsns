# -*- coding: utf-8 -*-
"""Durable run state store backed by SQLite (WAL)."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional


_TERMINAL_STATUSES = {"success", "error", "failed", "timeout", "cancelled"}
_ALLOWED_TRANSITIONS = {
    None: {"queued", "running", *sorted(_TERMINAL_STATUSES)},
    "queued": {"running", *sorted(_TERMINAL_STATUSES)},
    "running": set(_TERMINAL_STATUSES),
}


class RunStore:
    """Authoritative run timeline store."""

    def __init__(self, db_path: Path):
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self):
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL;")
            cur.execute("PRAGMA synchronous=NORMAL;")
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    cid TEXT PRIMARY KEY,
                    trace_id TEXT,
                    trace_label TEXT,
                    source TEXT,
                    parent_cid TEXT,
                    plan_name TEXT,
                    task_name TEXT,
                    status TEXT,
                    started_at_ms INTEGER,
                    finished_at_ms INTEGER,
                    queue_wait_ms REAL,
                    duration_ms INTEGER,
                    updated_at_ms INTEGER,
                    final_result_json TEXT
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS node_terminal_events (
                    cid TEXT NOT NULL,
                    node_id TEXT NOT NULL,
                    node_name TEXT,
                    status TEXT,
                    start_ms INTEGER,
                    end_ms INTEGER,
                    duration_ms REAL,
                    retry_count INTEGER,
                    exception_type TEXT,
                    exception_message TEXT,
                    loop_index INTEGER DEFAULT 0,
                    loop_item_json TEXT,
                    source_event TEXT NOT NULL,
                    updated_at_ms INTEGER,
                    PRIMARY KEY (cid, node_id, loop_index)
                )
                """
            )
            cur.execute("CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_runs_plan_task ON runs(plan_name, task_name)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_runs_updated ON runs(updated_at_ms DESC)")
            self._conn.commit()

    def apply_event(self, name: str, payload: Dict[str, Any], timestamp_ms: int):
        cid = payload.get("cid")
        if not cid:
            return
        lowered = (name or "").lower()
        with self._lock:
            if lowered == "queue.enqueued":
                self._upsert_queued(cid, payload, timestamp_ms)
            elif lowered == "task.started":
                self._upsert_started(cid, payload, timestamp_ms)
            elif lowered == "task.finished":
                self._upsert_finished(cid, payload, timestamp_ms)
            elif lowered in {"node.finished", "node.failed"}:
                self._upsert_node_terminal(cid, lowered, payload, timestamp_ms)
            self._conn.commit()

    def _get_current_status(self, cid: str) -> Optional[str]:
        row = self._conn.execute("SELECT status FROM runs WHERE cid = ?", (cid,)).fetchone()
        return (row["status"] if row else None)

    @staticmethod
    def _normalize_status(raw: Any) -> str:
        value = str(raw or "").strip().lower()
        if not value:
            return "unknown"
        return value

    def _assert_transition(self, cid: str, next_status: str):
        current = self._get_current_status(cid)
        if current in _TERMINAL_STATUSES:
            raise ValueError(f"Illegal run status transition for {cid}: {current} -> {next_status}")
        allowed = _ALLOWED_TRANSITIONS.get(current, set())
        if next_status not in allowed and next_status not in _TERMINAL_STATUSES:
            raise ValueError(f"Illegal run status transition for {cid}: {current} -> {next_status}")

    def _upsert_queued(self, cid: str, p: Dict[str, Any], ts_ms: int):
        current = self._get_current_status(cid)
        if current in _TERMINAL_STATUSES:
            return
        self._conn.execute(
            """
            INSERT INTO runs (cid, trace_id, trace_label, source, parent_cid, plan_name, task_name, status, updated_at_ms)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', ?)
            ON CONFLICT(cid) DO UPDATE SET
                trace_id=excluded.trace_id,
                trace_label=excluded.trace_label,
                source=excluded.source,
                parent_cid=excluded.parent_cid,
                plan_name=excluded.plan_name,
                task_name=excluded.task_name,
                status=CASE WHEN runs.status IN ('success','error','failed','timeout','cancelled') THEN runs.status ELSE 'queued' END,
                updated_at_ms=excluded.updated_at_ms
            """,
            (
                cid,
                p.get("trace_id"),
                p.get("trace_label"),
                p.get("source"),
                p.get("parent_cid"),
                p.get("plan_name"),
                p.get("task_name"),
                ts_ms,
            ),
        )

    def _upsert_started(self, cid: str, p: Dict[str, Any], ts_ms: int):
        self._assert_transition(cid, "running")
        start_time = p.get("start_time")
        if isinstance(start_time, (int, float)) and start_time < 1e12:
            start_ms = int(start_time * 1000)
        elif isinstance(start_time, (int, float)):
            start_ms = int(start_time)
        else:
            start_ms = ts_ms

        self._conn.execute(
            """
            INSERT INTO runs (cid, trace_id, trace_label, source, parent_cid, plan_name, task_name, status, started_at_ms, queue_wait_ms, updated_at_ms)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'running', ?, ?, ?)
            ON CONFLICT(cid) DO UPDATE SET
                trace_id=excluded.trace_id,
                trace_label=excluded.trace_label,
                source=excluded.source,
                parent_cid=excluded.parent_cid,
                plan_name=excluded.plan_name,
                task_name=excluded.task_name,
                status='running',
                started_at_ms=COALESCE(runs.started_at_ms, excluded.started_at_ms),
                queue_wait_ms=COALESCE(excluded.queue_wait_ms, runs.queue_wait_ms),
                updated_at_ms=excluded.updated_at_ms
            """,
            (
                cid,
                p.get("trace_id"),
                p.get("trace_label"),
                p.get("source"),
                p.get("parent_cid"),
                p.get("plan_name"),
                p.get("task_name"),
                start_ms,
                p.get("queue_wait_ms"),
                ts_ms,
            ),
        )

    def _upsert_finished(self, cid: str, p: Dict[str, Any], ts_ms: int):
        next_status = self._normalize_status(p.get("final_status") or p.get("status"))
        if next_status not in _TERMINAL_STATUSES:
            next_status = "error"
        self._assert_transition(cid, next_status)

        end_time = p.get("end_time")
        if isinstance(end_time, (int, float)) and end_time < 1e12:
            end_ms = int(end_time * 1000)
        elif isinstance(end_time, (int, float)):
            end_ms = int(end_time)
        else:
            end_ms = ts_ms

        duration_ms = p.get("duration_ms")
        if duration_ms is None and p.get("duration") is not None:
            try:
                duration_ms = int(float(p.get("duration")) * 1000)
            except Exception:
                duration_ms = None

        final_result_json = None
        if p.get("final_result") is not None:
            try:
                final_result_json = json.dumps(p.get("final_result"), ensure_ascii=False)
            except Exception:
                final_result_json = json.dumps({"raw": str(p.get("final_result"))}, ensure_ascii=False)

        self._conn.execute(
            """
            INSERT INTO runs (cid, trace_id, trace_label, source, parent_cid, plan_name, task_name, status, finished_at_ms, duration_ms, queue_wait_ms, updated_at_ms, final_result_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(cid) DO UPDATE SET
                trace_id=COALESCE(excluded.trace_id, runs.trace_id),
                trace_label=COALESCE(excluded.trace_label, runs.trace_label),
                source=COALESCE(excluded.source, runs.source),
                parent_cid=COALESCE(excluded.parent_cid, runs.parent_cid),
                plan_name=COALESCE(excluded.plan_name, runs.plan_name),
                task_name=COALESCE(excluded.task_name, runs.task_name),
                status=excluded.status,
                finished_at_ms=excluded.finished_at_ms,
                duration_ms=COALESCE(excluded.duration_ms, runs.duration_ms),
                queue_wait_ms=COALESCE(excluded.queue_wait_ms, runs.queue_wait_ms),
                updated_at_ms=excluded.updated_at_ms,
                final_result_json=COALESCE(excluded.final_result_json, runs.final_result_json)
            """,
            (
                cid,
                p.get("trace_id"),
                p.get("trace_label"),
                p.get("source"),
                p.get("parent_cid"),
                p.get("plan_name"),
                p.get("task_name"),
                next_status,
                end_ms,
                duration_ms,
                p.get("queue_wait_ms"),
                ts_ms,
                final_result_json,
            ),
        )

    def _upsert_node_terminal(self, cid: str, event_name: str, p: Dict[str, Any], ts_ms: int):
        node_id = p.get("node_id") or p.get("step_name") or "node"
        loop_index = int(p.get("loop_index") or 0)

        end_time = p.get("end_time")
        if isinstance(end_time, (int, float)) and end_time < 1e12:
            end_ms = int(end_time * 1000)
        elif isinstance(end_time, (int, float)):
            end_ms = int(end_time)
        else:
            end_ms = ts_ms

        start_time = p.get("start_time")
        if isinstance(start_time, (int, float)) and start_time < 1e12:
            start_ms = int(start_time * 1000)
        elif isinstance(start_time, (int, float)):
            start_ms = int(start_time)
        else:
            start_ms = end_ms

        status = self._normalize_status(p.get("status") or ("failed" if event_name == "node.failed" else "success"))

        loop_item_json = None
        if p.get("loop_item") is not None:
            try:
                loop_item_json = json.dumps(p.get("loop_item"), ensure_ascii=False)
            except Exception:
                loop_item_json = json.dumps(str(p.get("loop_item")), ensure_ascii=False)

        self._conn.execute(
            """
            INSERT INTO node_terminal_events (
                cid, node_id, node_name, status, start_ms, end_ms, duration_ms, retry_count,
                exception_type, exception_message, loop_index, loop_item_json, source_event, updated_at_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(cid, node_id, loop_index) DO UPDATE SET
                node_name=COALESCE(excluded.node_name, node_terminal_events.node_name),
                status=excluded.status,
                start_ms=COALESCE(node_terminal_events.start_ms, excluded.start_ms),
                end_ms=excluded.end_ms,
                duration_ms=COALESCE(excluded.duration_ms, node_terminal_events.duration_ms),
                retry_count=COALESCE(excluded.retry_count, node_terminal_events.retry_count),
                exception_type=COALESCE(excluded.exception_type, node_terminal_events.exception_type),
                exception_message=COALESCE(excluded.exception_message, node_terminal_events.exception_message),
                loop_item_json=COALESCE(excluded.loop_item_json, node_terminal_events.loop_item_json),
                source_event=excluded.source_event,
                updated_at_ms=excluded.updated_at_ms
            """,
            (
                cid,
                node_id,
                p.get("node_name"),
                status,
                start_ms,
                end_ms,
                p.get("duration_ms"),
                p.get("retry_count"),
                p.get("exception_type"),
                p.get("exception_message"),
                loop_index,
                loop_item_json,
                event_name,
                ts_ms,
            ),
        )

    def get_run(self, cid: str) -> Dict[str, Any]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM runs WHERE cid = ?", (cid,)).fetchone()
            if not row:
                return {}
            run = dict(row)
            final_result_raw = run.pop("final_result_json", None)
            if final_result_raw:
                try:
                    run["final_result"] = json.loads(final_result_raw)
                except Exception:
                    run["final_result"] = {"raw": final_result_raw}
            nodes = self._conn.execute(
                """
                SELECT * FROM node_terminal_events
                WHERE cid = ?
                ORDER BY updated_at_ms ASC
                """,
                (cid,),
            ).fetchall()
            run["nodes"] = [self._row_to_node(dict(r)) for r in nodes]
            return run

    def close(self) -> None:
        with self._lock:
            conn = getattr(self, "_conn", None)
            if conn is None:
                return
            self._conn = None
            try:
                conn.close()
            except Exception:
                pass

    def list_runs(
        self,
        limit: int = 50,
        plan_name: Optional[str] = None,
        task_name: Optional[str] = None,
        status: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        with self._lock:
            clauses: List[str] = []
            params: List[Any] = []
            if plan_name:
                clauses.append("plan_name = ?")
                params.append(plan_name)
            if task_name:
                clauses.append("task_name = ?")
                params.append(task_name)
            if status:
                clauses.append("status = ?")
                params.append(str(status).lower())
            where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
            query = f"SELECT * FROM runs {where} ORDER BY updated_at_ms DESC LIMIT ?"
            params.append(max(1, int(limit)))
            rows = self._conn.execute(query, params).fetchall()
            return [dict(r) for r in rows]

    def get_metrics_snapshot(self, running_tasks: int = 0) -> Dict[str, Any]:
        with self._lock:
            out = {
                "tasks_started": 0,
                "tasks_finished": 0,
                "tasks_success": 0,
                "tasks_error": 0,
                "tasks_failed": 0,
                "tasks_timeout": 0,
                "tasks_cancelled": 0,
                "tasks_running": int(running_tasks),
                "nodes_total": 0,
                "nodes_succeeded": 0,
                "nodes_failed": 0,
                "nodes_duration_ms_sum": 0.0,
                "nodes_duration_ms_avg": 0.0,
                "updated_at": time.time(),
            }

            total_started = self._conn.execute(
                "SELECT COUNT(*) AS cnt FROM runs WHERE started_at_ms IS NOT NULL"
            ).fetchone()["cnt"]
            out["tasks_started"] = int(total_started or 0)

            terminal_rows = self._conn.execute(
                "SELECT status, COUNT(*) AS cnt FROM runs WHERE status IN ('success','error','failed','timeout','cancelled') GROUP BY status"
            ).fetchall()
            finished = 0
            for row in terminal_rows:
                status = row["status"]
                cnt = int(row["cnt"] or 0)
                finished += cnt
                out[f"tasks_{status}"] = cnt
            out["tasks_finished"] = finished

            node_rows = self._conn.execute(
                """
                SELECT status, COUNT(*) AS cnt, COALESCE(SUM(duration_ms), 0.0) AS dur
                FROM node_terminal_events
                WHERE source_event = 'node.finished'
                GROUP BY status
                """
            ).fetchall()
            total_nodes = 0
            duration_sum = 0.0
            for row in node_rows:
                cnt = int(row["cnt"] or 0)
                total_nodes += cnt
                duration_sum += float(row["dur"] or 0.0)
                status = str(row["status"] or "").lower()
                if status == "success":
                    out["nodes_succeeded"] += cnt
                elif status in {"failed", "error"}:
                    out["nodes_failed"] += cnt
            out["nodes_total"] = total_nodes
            out["nodes_duration_ms_sum"] = duration_sum
            out["nodes_duration_ms_avg"] = (duration_sum / total_nodes) if total_nodes > 0 else 0.0
            return out

    @staticmethod
    def _row_to_node(row: Dict[str, Any]) -> Dict[str, Any]:
        loop_item_raw = row.get("loop_item_json")
        loop_item = None
        if loop_item_raw is not None:
            try:
                loop_item = json.loads(loop_item_raw)
            except Exception:
                loop_item = loop_item_raw
        return {
            "node_id": row.get("node_id"),
            "node_name": row.get("node_name"),
            "status": row.get("status"),
            "startMs": row.get("start_ms"),
            "endMs": row.get("end_ms"),
            "duration_ms": row.get("duration_ms"),
            "retry_count": row.get("retry_count"),
            "exception_type": row.get("exception_type"),
            "exception_message": row.get("exception_message"),
            "loop_index": row.get("loop_index"),
            "loop_item": loop_item,
            "source_event": row.get("source_event"),
        }
