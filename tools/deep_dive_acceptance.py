"""Offline, persistent acceptance ledger. This tool never captures or controls a game.

The live harness measures time and supplies WGC provenance; the ledger checks
those declarations, hashes real files, and forbids reusing recognition evidence.
It does not independently attest that an image was captured through WGC.
"""
from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
from typing import Any

FACES = ("U", "R", "F", "D", "L", "B")
KINDS = ("player", "singularity", "inspiration")
FINGERPRINT_KEYS = ("code_sha256", "model_sha256", "config_sha256")


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode("utf-8")).hexdigest()


def _sha(path: Path) -> str:
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256")
    return digest.hexdigest()


def _fingerprint(value: dict) -> dict:
    if not isinstance(value, dict):
        raise ValueError("fingerprint must be an object")
    for key in FINGERPRINT_KEYS:
        text = value.get(key, "")
        if not isinstance(text, str) or len(text) != 64 or any(c not in "0123456789abcdef" for c in text):
            raise ValueError(f"invalid {key}")
    return deepcopy(value)


def fingerprint_files(repo_root: str | Path, code_paths: list[str], model_path: str,
                      runtime_config: dict) -> dict:
    """Hash actual code/model files and all relevant provider/thread/threshold config."""
    root = Path(repo_root).resolve()
    def local(name):
        path = (root / name).resolve()
        if not path.is_relative_to(root):
            raise ValueError("fingerprint files must stay in repository")
        return path
    code = {str(local(name).relative_to(root)).replace("\\", "/"): _sha(local(name))
            for name in sorted(set(code_paths))}
    if not code:
        raise ValueError("at least one production code file is required")
    return {"code_sha256": _digest(code), "model_sha256": _sha(local(model_path)),
            "model_file": str(local(model_path).relative_to(root)).replace("\\", "/"),
            "config_sha256": _digest(runtime_config), "code_files": code,
            "runtime_config": deepcopy(runtime_config)}


def _entities(rows: list[dict]) -> Counter:
    if not isinstance(rows, list):
        raise ValueError("entities must be a list")
    result = Counter()
    for row in rows:
        if not isinstance(row, dict) or row.get("kind") not in KINDS or row.get("face") not in FACES:
            raise ValueError("invalid entity kind/face")
        r, c = row.get("row"), row.get("col")
        if type(r) is not int or type(c) is not int or not 0 <= r <= 2 or not 0 <= c <= 2:
            raise ValueError("entity row/col must be integers in 0..2")
        result[(row["kind"], row["face"], r, c)] += 1
    return result


def _slot(key):
    _, face, row, col = key
    return FACES.index(face) * 9 + row * 3 + col


class AcceptanceLedger:
    def __init__(self, path: str | Path, repo_root: str | Path):
        self.root = Path(repo_root).resolve()
        self.path = Path(path).resolve()
        if not self.path.is_relative_to(self.root):
            raise ValueError("ledger must stay in repository")
        self.data = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else None
        if self.data is not None and self.data.get("schema_version") != 1:
            raise ValueError("unsupported ledger schema")

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".writing")
        temporary.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, self.path)

    def initialize(self, fingerprint: dict, *, initial_environment_sec: float | None = None):
        if self.data is not None:
            raise ValueError("existing ledger cannot be reinitialized; use a new campaign path")
        if initial_environment_sec is not None and (not math.isfinite(initial_environment_sec) or initial_environment_sec < 0):
            raise ValueError("invalid initial_environment_sec")
        self.data = {"schema_version": 1, "fingerprint": _fingerprint(fingerprint),
                     "created_at": datetime.now(timezone.utc).isoformat(),
                     "limit_sec": 90.0, "required_consecutive": 20,
                     "initial_environment_sec": initial_environment_sec,
                     "truths": {}, "runs": [], "consecutive_passes": 0}
        self._save()

    def _require(self):
        if self.data is None:
            raise ValueError("initialize ledger first")

    def _image(self, image: dict) -> tuple[str, str]:
        path = (self.root / image["path"]).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("evidence file must stay in repository")
        actual = _sha(path)
        if image.get("sha256") != actual:
            raise ValueError("evidence image hash mismatch")
        return str(path.relative_to(self.root)).replace("\\", "/"), actual

    def register_truth(self, truth: dict):
        self._require()
        truth = deepcopy(truth)
        for key in ("truth_id", "board_id", "state_id", "reviewer", "coordinate_frame"):
            if not isinstance(truth.get(key), str) or not truth[key].strip():
                raise ValueError(f"truth requires {key}")
        if truth["truth_id"] in self.data["truths"]:
            raise ValueError("truth is immutable; register a new truth_id")
        method = truth.get("method")
        if method not in ("manual_multiview", "manual_video", "confirmed_transition"):
            raise ValueError("truth requires an independent annotation method")
        if truth.get("independent_of_tested_output") is not True or truth.get("ambiguous") is not False:
            raise ValueError("truth must be independent and unambiguous")
        entities = _entities(truth.get("entities"))
        if any(n != 1 for n in entities.values()):
            raise ValueError("duplicate truth entity")
        counts = Counter(key[0] for key in entities)
        if counts["player"] != 1 or counts["singularity"] != 1:
            raise ValueError("truth must include exactly one player and singularity")
        if len({_slot(key) for key in entities}) != len(entities):
            raise ValueError("truth entities cannot share a cell")
        images = truth.get("images", [])
        verified = [self._image(image) for image in images]
        if len(set(sha for _, sha in verified)) < 2:
            raise ValueError("truth requires at least two different actual source images")
        if method == "confirmed_transition":
            parent = self.data["truths"].get(truth.get("parent_truth_id"))
            mapping = truth.get("mapping")
            if parent is None or not isinstance(mapping, list) or len(mapping) != 54 or sorted(mapping) != list(range(54)):
                raise ValueError("transition requires registered parent and bijective 54-cell mapping")
            if truth.get("action_confirmed") is not True or not truth.get("transition_review"):
                raise ValueError("transition requires independently reviewed actual action")
            expected = Counter()
            for key, count in _entities(parent["entities"]).items():
                destination = mapping[_slot(key)]
                expected[(key[0], FACES[destination // 9], destination % 9 // 3, destination % 3)] += count
            if entities != expected or truth["board_id"] != parent["board_id"]:
                raise ValueError("transition truth disagrees with independent mapping")
        truth["images"] = [{"path": path, "sha256": sha} for path, sha in verified]
        truth["registered_before_run_index"] = len(self.data["runs"]) + 1
        truth["registered_at"] = datetime.now(timezone.utc).isoformat()
        truth["truth_sha256"] = _digest(truth)
        self.data["truths"][truth["truth_id"]] = truth
        self._save()
        return deepcopy(truth)

    def record_run(self, run: dict) -> dict:
        self._require()
        record = deepcopy(run)
        failures = []
        truth = self.data["truths"].get(run.get("truth_id"))
        if truth is None:
            failures.append("unregistered_truth")
        else:
            try:
                for image in truth["images"]:
                    self._image(image)
            except (OSError, KeyError, TypeError, ValueError):
                failures.append("registered_truth_source_changed")
        if not isinstance(run.get("run_id"), str) or not run["run_id"].strip():
            failures.append("missing_run_id")
        elif any(row.get("run_id") == run["run_id"] for row in self.data["runs"]):
            failures.append("reused_run_id")
        try:
            if _fingerprint(run.get("fingerprint")) != self.data["fingerprint"]:
                failures.append("frozen_version_changed")
        except (TypeError, ValueError):
            failures.append("invalid_fingerprint")
        frozen = self.data["fingerprint"]
        try:
            for name, sha in frozen.get("code_files", {}).items():
                path = (self.root / name).resolve()
                if not path.is_relative_to(self.root) or _sha(path) != sha:
                    failures.append("actual_code_file_changed")
            if frozen.get("model_file"):
                path = (self.root / frozen["model_file"]).resolve()
                if not path.is_relative_to(self.root) or _sha(path) != frozen["model_sha256"]:
                    failures.append("actual_model_file_changed")
        except (OSError, TypeError, ValueError):
            failures.append("frozen_file_unavailable")
        status = run.get("business_status")
        recognition_ready = (status == "targets_ready" and run.get("scan_success") is True
                             and run.get("targets_readiness") is True)
        if run.get("business_ready") is not True or not (status in ("ready", "completed") or recognition_ready):
            failures.append("business_not_ready")
        if run.get("state_unchanged") is not True:
            failures.append("state_change_unresolved")
        for key in ("dispatch_wall_sec", "invocation_elapsed_sec"):
            duration = run.get(key)
            if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration < 0:
                failures.append(f"invalid_{key}")
            elif duration > self.data["limit_sec"]:
                failures.append(f"over_limit_{key}")
        actual = None
        try:
            actual = _entities(run.get("entities"))
            if truth and actual != _entities(truth["entities"]):
                failures.append("entity_set_mismatch")
        except (KeyError, TypeError, ValueError):
            failures.append("invalid_entities")
        used_ids, used_hashes, last_times = set(), set(), {}
        for previous in self.data["runs"]:
            for source in previous.get("verified_sources", []):
                used_ids.add((source["session_id"], source["generation"]))
                used_hashes.add(source["sha256"])
                prior = last_times.get(source["session_id"], (-1, float("-inf")))
                last_times[source["session_id"]] = max(prior, (source["generation"], source["frame_time"]))
        verified_sources = []
        sources = run.get("sources", [])
        if not isinstance(sources, list) or not sources:
            failures.append("missing_sources")
            sources = []
        for source in sources:
            try:
                if source.get("backend") != "wgc" or source.get("role") not in ("positive", "negative", "geometry"):
                    raise ValueError("invalid_capture_backend_or_role")
                session, generation = source.get("session_id"), source.get("generation")
                if type(session) is int and session >= 0:
                    session = str(session)
                if not isinstance(session, str) or not session.strip() or type(generation) is not int or generation < 0:
                    raise ValueError("invalid_capture_identity")
                frame_time = source.get("frame_time")
                if isinstance(frame_time, bool) or not isinstance(frame_time, (int, float)) or not math.isfinite(frame_time):
                    raise ValueError("invalid_frame_time")
                if type(source.get("map_revision")) is not int or source["map_revision"] < 0:
                    raise ValueError("invalid_map_revision")
                path, sha = self._image(source)
                if (session, generation) in used_ids:
                    raise ValueError("reused_capture_identity")
                if sha in used_hashes:
                    raise ValueError("reused_image_evidence")
                if session in last_times and (generation <= last_times[session][0] or frame_time <= last_times[session][1]):
                    raise ValueError("non_increasing_capture_source")
                last_times[session] = generation, frame_time
                verified = dict(source, path=path, sha256=sha, session_id=session)
                verified_sources.append(verified)
                used_ids.add((session, generation))
                used_hashes.add(sha)
            except (OSError, KeyError, TypeError, ValueError) as exc:
                failures.append(str(exc))
        if sources and not any(source.get("role") == "positive" for source in verified_sources):
            failures.append("missing_positive_evidence")
        metrics = None
        if actual is not None and truth is not None:
            expected = _entities(truth["entities"])
            correct = actual & expected
            metrics = {}
            for kind in KINDS:
                tp = sum(count for key, count in correct.items() if key[0] == kind)
                fp = sum(count for key, count in (actual - expected).items() if key[0] == kind)
                fn = sum(count for key, count in (expected - actual).items() if key[0] == kind)
                metrics[kind] = {"tp": tp, "fp": fp, "fn": fn,
                                 "precision": tp / (tp + fp) if tp + fp else 1.0,
                                 "recall": tp / (tp + fn) if tp + fn else 1.0}
        record.update(attempt_index=len(self.data["runs"]) + 1,
                      recorded_at=datetime.now(timezone.utc).isoformat(),
                      truth_sha256=truth.get("truth_sha256") if truth else None,
                      verified_sources=verified_sources, entity_metrics=metrics,
                      failures=failures, passed=not failures)
        self.data["consecutive_passes"] = self.data["consecutive_passes"] + 1 if not failures else 0
        record["consecutive_passes"] = self.data["consecutive_passes"]
        self.data["runs"].append(record)
        self._save()
        return deepcopy(record)

    def summary(self) -> dict:
        self._require()
        streak = self.data["consecutive_passes"]
        rows = self.data["runs"][-streak:] if streak else []
        truths = [self.data["truths"][row["truth_id"]] for row in rows]
        boards = sorted({truth["board_id"] for truth in truths})
        all_durations = [row["dispatch_wall_sec"] for row in self.data["runs"]
                         if isinstance(row.get("dispatch_wall_sec"), (int, float))
                         and not isinstance(row["dispatch_wall_sec"], bool)
                         and math.isfinite(row["dispatch_wall_sec"]) and row["dispatch_wall_sec"] >= 0]
        return {"attempts": len(self.data["runs"]), "consecutive_passes": streak,
                "required_consecutive": self.data["required_consecutive"],
                "campaign_passed": streak >= self.data["required_consecutive"],
                "current_streak_boards": boards,
                "current_streak_states": sorted({(truth["board_id"], truth["state_id"]) for truth in truths}),
                "same_board_only": bool(rows) and len(boards) == 1,
                "generalization_established": False,
                "scope": "Empirical frozen-version runs only; WGC provenance is harness-supplied.",
                "initial_environment_sec": self.data["initial_environment_sec"],
                "all_attempts_max_dispatch_wall_sec": max(all_durations, default=None),
                "all_attempts_mean_dispatch_wall_sec": sum(all_durations) / len(all_durations) if all_durations else None,
                "max_dispatch_wall_sec": max((row["dispatch_wall_sec"] for row in rows), default=None),
                "mean_dispatch_wall_sec": sum(row["dispatch_wall_sec"] for row in rows) / streak if streak else None}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("init", "truth", "run", "summary"))
    parser.add_argument("--ledger", required=True)
    parser.add_argument("--repo-root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--input", help="JSON fingerprint/truth/run payload")
    args = parser.parse_args()
    ledger = AcceptanceLedger(args.ledger, args.repo_root)
    payload = json.loads(Path(args.input).read_text(encoding="utf-8")) if args.input else None
    if args.command == "init":
        ledger.initialize(payload)
        result = ledger.summary()
    elif args.command == "truth":
        result = ledger.register_truth(payload)
    elif args.command == "run":
        result = ledger.record_run(payload)
    else:
        result = ledger.summary()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if args.command == "run" and not result["passed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
