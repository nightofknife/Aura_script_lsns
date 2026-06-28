from __future__ import annotations

import hashlib
import json
import os
import random
import shlex
import shutil
import subprocess
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import yaml

from tools._shared import ensure_directory, plan_path, repo_root, sanitize_filename


IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".webp"}
SAMPLE_STATUSES = {
    "unlabeled",
    "in_manual_session",
    "draft_generated",
    "in_review_session",
    "approved",
    "ignored",
}


class YoloProjectError(RuntimeError):
    """Project/workbench specific error."""


@dataclass(frozen=True)
class SampleRecord:
    sample_id: str
    image_relpath: str
    approved_label_relpath: str | None
    draft_label_relpath: str | None
    status: str
    source: str
    last_session_id: str | None
    last_model_run_id: str | None
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "SampleRecord":
        return cls(
            sample_id=str(payload["sample_id"]),
            image_relpath=str(payload["image_relpath"]),
            approved_label_relpath=_optional_text(payload.get("approved_label_relpath")),
            draft_label_relpath=_optional_text(payload.get("draft_label_relpath")),
            status=str(payload.get("status") or "unlabeled"),
            source=str(payload.get("source") or "imported"),
            last_session_id=_optional_text(payload.get("last_session_id")),
            last_model_run_id=_optional_text(payload.get("last_model_run_id")),
            updated_at=str(payload.get("updated_at") or utc_now()),
        )


@dataclass(frozen=True)
class SessionRecord:
    session_id: str
    session_type: str
    session_dir: str
    sample_ids: list[str]
    status: str
    created_at: str
    completed_at: str | None
    labelimg_command: list[str] | None
    labelimg_pid: int | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "SessionRecord":
        command_payload = payload.get("labelimg_command") or []
        return cls(
            session_id=str(payload["session_id"]),
            session_type=str(payload["session_type"]),
            session_dir=str(payload["session_dir"]),
            sample_ids=[str(item) for item in payload.get("sample_ids", [])],
            status=str(payload.get("status") or "active"),
            created_at=str(payload.get("created_at") or utc_now()),
            completed_at=_optional_text(payload.get("completed_at")),
            labelimg_command=[str(item) for item in command_payload] or None,
            labelimg_pid=int(payload["labelimg_pid"]) if payload.get("labelimg_pid") is not None else None,
        )


@dataclass(frozen=True)
class EnvCheckResult:
    ok: bool
    runtime_python: str | None
    labelimg_command: list[str] | None
    messages: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def project_root(plan_name: str, project_name: str) -> Path:
    return plan_path(plan_name) / "data" / "yolo_projects" / sanitize_filename(project_name, fallback="project")


def list_projects(plan_name: str) -> list[str]:
    root = plan_path(plan_name) / "data" / "yolo_projects"
    if not root.is_dir():
        return []
    names = []
    for child in sorted(root.iterdir()):
        if child.is_dir() and (child / "project.yaml").is_file():
            names.append(child.name)
    return names


def create_project(
    *,
    plan_name: str,
    project_name: str,
    class_names: Iterable[str],
) -> Path:
    root = project_root(plan_name, project_name)
    if root.exists():
        raise YoloProjectError(f"Project already exists: {root}")

    normalized_classes = _normalize_class_names(class_names)
    if not normalized_classes:
        raise YoloProjectError("At least one class name is required.")

    for relative in (
        "assets/images",
        "labels/approved",
        "labels/draft",
        "sessions/labelimg",
        "exports/dataset",
    ):
        ensure_directory(root / relative)

    classes_path = root / "classes.txt"
    classes_path.write_text("\n".join(normalized_classes) + "\n", encoding="utf-8")

    project_data = {
        "plan_name": plan_name,
        "project_name": sanitize_filename(project_name, fallback="project"),
        "task_type": "detection",
        "class_names": normalized_classes,
        "split": {
            "train_ratio": 0.8,
            "seed": 42,
        },
        "latest_export": None,
        "sessions": {
            "active_manual_session": None,
            "active_review_session": None,
            "history": [],
        },
        "created_at": utc_now(),
        "updated_at": utc_now(),
    }
    save_project_config(root, project_data)
    write_samples(root, [])
    return root


def load_project_config(project_dir: Path) -> dict[str, Any]:
    path = project_dir / "project.yaml"
    if not path.is_file():
        raise YoloProjectError(f"Missing project.yaml: {path}")
    with open(path, "r", encoding="utf-8") as handle:
        payload = yaml.safe_load(handle) or {}
    if not isinstance(payload, dict):
        raise YoloProjectError(f"Invalid project.yaml: {path}")
    return payload


def save_project_config(project_dir: Path, payload: dict[str, Any]) -> None:
    output = dict(payload)
    output["updated_at"] = utc_now()
    with open(project_dir / "project.yaml", "w", encoding="utf-8") as handle:
        yaml.safe_dump(output, handle, allow_unicode=True, sort_keys=False)


def load_samples(project_dir: Path) -> list[SampleRecord]:
    samples_path = project_dir / "samples.jsonl"
    if not samples_path.is_file():
        return []
    rows: list[SampleRecord] = []
    with open(samples_path, "r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line:
                continue
            rows.append(SampleRecord.from_dict(json.loads(line)))
    return rows


def write_samples(project_dir: Path, samples: Iterable[SampleRecord]) -> None:
    with open(project_dir / "samples.jsonl", "w", encoding="utf-8") as handle:
        for sample in samples:
            handle.write(json.dumps(sample.to_dict(), ensure_ascii=False) + "\n")


def sample_index(samples: Iterable[SampleRecord]) -> dict[str, SampleRecord]:
    return {sample.sample_id: sample for sample in samples}


def summarize_samples(samples: Iterable[SampleRecord]) -> dict[str, int]:
    counter = Counter(sample.status for sample in samples)
    return {status: int(counter.get(status, 0)) for status in sorted(SAMPLE_STATUSES)}


def import_images(project_dir: Path, image_paths: Iterable[Path | str]) -> dict[str, Any]:
    project = load_project_config(project_dir)
    existing = sample_index(load_samples(project_dir))
    assets_dir = ensure_directory(project_dir / "assets" / "images")
    imported: list[SampleRecord] = []
    skipped: list[str] = []

    for raw_path in image_paths:
        source_path = Path(raw_path).expanduser().resolve()
        if not source_path.is_file():
            skipped.append(f"missing:{source_path}")
            continue
        if source_path.suffix.lower() not in IMAGE_EXTENSIONS:
            skipped.append(f"unsupported:{source_path}")
            continue

        digest = sha1_file(source_path)[:12]
        sample_id = digest
        destination_name = f"{sanitize_filename(source_path.stem, fallback='image')}_{digest}{source_path.suffix.lower()}"
        destination = assets_dir / destination_name
        if not destination.exists():
            shutil.copy2(source_path, destination)

        if sample_id in existing:
            skipped.append(f"duplicate:{source_path}")
            continue

        record = SampleRecord(
            sample_id=sample_id,
            image_relpath=destination.relative_to(project_dir).as_posix(),
            approved_label_relpath=None,
            draft_label_relpath=None,
            status="unlabeled",
            source="imported",
            last_session_id=None,
            last_model_run_id=None,
            updated_at=utc_now(),
        )
        existing[sample_id] = record
        imported.append(record)

    ordered = sorted(existing.values(), key=lambda sample: sample.image_relpath)
    write_samples(project_dir, ordered)
    project["latest_import"] = {
        "count": len(imported),
        "skipped": skipped,
        "updated_at": utc_now(),
    }
    save_project_config(project_dir, project)
    return {
        "ok": True,
        "imported_count": len(imported),
        "skipped": skipped,
        "samples": [sample.to_dict() for sample in imported],
    }


def create_labelimg_session(project_dir: Path, *, session_type: str, batch_size: int = 50) -> SessionRecord:
    project = load_project_config(project_dir)
    samples = load_samples(project_dir)
    target_field = "active_manual_session" if session_type == "manual" else "active_review_session"
    active_session = ((project.get("sessions") or {}).get(target_field))
    if active_session:
        raise YoloProjectError(f"An unfinished {session_type} session already exists: {active_session}")

    eligible_status = "unlabeled" if session_type == "manual" else "draft_generated"
    eligible = [sample for sample in samples if sample.status == eligible_status]
    if not eligible:
        raise YoloProjectError(f"No samples available for {session_type} session.")

    batch = eligible[: max(int(batch_size), 1)]
    session_id = build_run_id(session_type)
    session_dir = ensure_directory(project_dir / "sessions" / "labelimg" / session_id)
    manifest = {
        "session_id": session_id,
        "session_type": session_type,
        "session_dir": session_dir.relative_to(project_dir).as_posix(),
        "status": "active",
        "sample_ids": [sample.sample_id for sample in batch],
        "created_at": utc_now(),
        "completed_at": None,
        "labelimg_command": None,
        "labelimg_pid": None,
        "items": [],
    }

    updated_samples: list[SampleRecord] = []
    selected = {sample.sample_id for sample in batch}
    for sample in samples:
        if sample.sample_id not in selected:
            updated_samples.append(sample)
            continue
        image_path = project_dir / sample.image_relpath
        session_image_name = f"{sample.sample_id}{image_path.suffix.lower()}"
        session_image_path = session_dir / session_image_name
        shutil.copy2(image_path, session_image_path)

        label_name = f"{sample.sample_id}.txt"
        session_label_path = session_dir / label_name
        if session_type == "review" and sample.draft_label_relpath:
            draft_path = project_dir / sample.draft_label_relpath
            if draft_path.is_file():
                shutil.copy2(draft_path, session_label_path)

        manifest["items"].append(
            {
                "sample_id": sample.sample_id,
                "session_image": session_image_name,
                "session_label": label_name,
                "image_relpath": sample.image_relpath,
            }
        )
        updated_samples.append(
            SampleRecord(
                sample_id=sample.sample_id,
                image_relpath=sample.image_relpath,
                approved_label_relpath=sample.approved_label_relpath,
                draft_label_relpath=sample.draft_label_relpath,
                status="in_manual_session" if session_type == "manual" else "in_review_session",
                source=sample.source,
                last_session_id=session_id,
                last_model_run_id=sample.last_model_run_id,
                updated_at=utc_now(),
            )
        )

    with open(session_dir / "session.yaml", "w", encoding="utf-8") as handle:
        yaml.safe_dump(manifest, handle, allow_unicode=True, sort_keys=False)

    write_samples(project_dir, updated_samples)
    sessions = project.setdefault("sessions", {})
    sessions[target_field] = session_id
    history = sessions.setdefault("history", [])
    history.append(
        {
            "session_id": session_id,
            "session_type": session_type,
            "status": "active",
            "sample_count": len(batch),
            "created_at": manifest["created_at"],
        }
    )
    save_project_config(project_dir, project)
    return SessionRecord.from_dict(manifest)


def complete_labelimg_session(project_dir: Path, *, session_type: str) -> dict[str, Any]:
    project = load_project_config(project_dir)
    samples = sample_index(load_samples(project_dir))
    target_field = "active_manual_session" if session_type == "manual" else "active_review_session"
    session_id = ((project.get("sessions") or {}).get(target_field))
    if not session_id:
        raise YoloProjectError(f"No active {session_type} session.")

    session_dir = project_dir / "sessions" / "labelimg" / str(session_id)
    manifest_path = session_dir / "session.yaml"
    if not manifest_path.is_file():
        raise YoloProjectError(f"Missing session manifest: {manifest_path}")
    session_data = _load_yaml(manifest_path)
    items = list(session_data.get("items") or [])

    approved_dir = ensure_directory(project_dir / "labels" / "approved")
    draft_dir = ensure_directory(project_dir / "labels" / "draft")
    approved_count = 0
    reverted_count = 0

    for item in items:
        sample_id = str(item["sample_id"])
        if sample_id not in samples:
            continue
        sample = samples[sample_id]
        label_path = session_dir / str(item["session_label"])
        approved_label_name = f"{sample_id}.txt"
        approved_label_path = approved_dir / approved_label_name
        draft_label_path = draft_dir / approved_label_name
        has_annotation_file = label_path.is_file()

        if has_annotation_file:
            shutil.copy2(label_path, approved_label_path)
            approved_count += 1
            if draft_label_path.exists():
                draft_label_path.unlink()
            samples[sample_id] = SampleRecord(
                sample_id=sample.sample_id,
                image_relpath=sample.image_relpath,
                approved_label_relpath=approved_label_path.relative_to(project_dir).as_posix(),
                draft_label_relpath=None,
                status="approved",
                source="manual",
                last_session_id=str(session_id),
                last_model_run_id=sample.last_model_run_id,
                updated_at=utc_now(),
            )
            continue

        reverted_count += 1
        next_status = "unlabeled" if session_type == "manual" else "draft_generated"
        samples[sample_id] = SampleRecord(
            sample_id=sample.sample_id,
            image_relpath=sample.image_relpath,
            approved_label_relpath=sample.approved_label_relpath,
            draft_label_relpath=sample.draft_label_relpath,
            status=next_status,
            source=sample.source,
            last_session_id=str(session_id),
            last_model_run_id=sample.last_model_run_id,
            updated_at=utc_now(),
        )

    write_samples(project_dir, sorted(samples.values(), key=lambda sample: sample.image_relpath))
    session_data["status"] = "completed"
    session_data["completed_at"] = utc_now()
    _write_yaml(manifest_path, session_data)

    sessions = project.setdefault("sessions", {})
    sessions[target_field] = None
    for item in sessions.get("history", []):
        if item.get("session_id") == session_id:
            item["status"] = "completed"
            item["completed_at"] = utc_now()
    save_project_config(project_dir, project)
    return {
        "ok": True,
        "session_id": session_id,
        "approved_count": approved_count,
        "reverted_count": reverted_count,
    }


def export_training_dataset(project_dir: Path) -> dict[str, Any]:
    project = load_project_config(project_dir)
    samples = [sample for sample in load_samples(project_dir) if sample.status == "approved"]
    if not samples:
        raise YoloProjectError("No approved samples are available for dataset export.")

    export_id = build_run_id("dataset")
    export_root = ensure_directory(project_dir / "exports" / "dataset" / export_id)
    train_images_dir = ensure_directory(export_root / "images" / "train")
    val_images_dir = ensure_directory(export_root / "images" / "val")
    train_labels_dir = ensure_directory(export_root / "labels" / "train")
    val_labels_dir = ensure_directory(export_root / "labels" / "val")

    split_cfg = project.get("split", {}) if isinstance(project.get("split"), dict) else {}
    train_ratio = float(split_cfg.get("train_ratio", 0.8))
    seed = int(split_cfg.get("seed", 42))
    train_ids, val_ids = _split_sample_ids([sample.sample_id for sample in samples], train_ratio=train_ratio, seed=seed)
    sample_by_id = {sample.sample_id: sample for sample in samples}

    for split_name, sample_ids in (("train", train_ids), ("val", val_ids)):
        image_dir = train_images_dir if split_name == "train" else val_images_dir
        label_dir = train_labels_dir if split_name == "train" else val_labels_dir
        for sample_id in sample_ids:
            sample = sample_by_id[sample_id]
            image_path = project_dir / sample.image_relpath
            label_path = project_dir / str(sample.approved_label_relpath or "")
            exported_image_path = image_dir / image_path.name
            exported_label_path = label_dir / f"{image_path.stem}.txt"
            shutil.copy2(image_path, exported_image_path)
            if label_path.is_file():
                shutil.copy2(label_path, exported_label_path)
            else:
                exported_label_path.write_text("", encoding="utf-8")

    dataset_yaml = {
        "path": export_root.as_posix(),
        "train": "images/train",
        "val": "images/val",
        "names": list(project.get("class_names") or []),
    }
    _write_yaml(export_root / "dataset.yaml", dataset_yaml)

    project["latest_export"] = {
        "export_id": export_id,
        "export_dir": export_root.relative_to(project_dir).as_posix(),
        "train_count": len(train_ids),
        "val_count": len(val_ids),
        "updated_at": utc_now(),
    }
    save_project_config(project_dir, project)
    return {
        "ok": True,
        "export_id": export_id,
        "export_dir": str(export_root),
        "dataset_yaml": str(export_root / "dataset.yaml"),
        "train_count": len(train_ids),
        "val_count": len(val_ids),
    }


def detect_runtime_python() -> Path | None:
    candidate = repo_root() / ".venv" / "Scripts" / "python.exe"
    if candidate.is_file():
        return candidate
    return None


def detect_labelimg_command() -> tuple[list[str] | None, str | None]:
    env_command = os.environ.get("AURA_YOLO_LABELIMG_COMMAND")
    if env_command:
        return shlex.split(env_command), "env:AURA_YOLO_LABELIMG_COMMAND"

    config_path = repo_root() / "config.yaml"
    if config_path.is_file():
        config_data = _load_yaml(config_path)
        yolo_cfg = config_data.get("yolo", {}) if isinstance(config_data.get("yolo"), dict) else {}
        configured = yolo_cfg.get("labelimg_command")
        if isinstance(configured, str) and configured.strip():
            return shlex.split(configured), "config:yolo.labelimg_command"

    runtime_python = detect_runtime_python()
    if runtime_python is not None:
        exe_candidates = [
            runtime_python.parent / "labelImg.exe",
            runtime_python.parent / "labelImg.bat",
        ]
        for candidate in exe_candidates:
            if candidate.is_file():
                return [str(candidate)], "venv-executable"
        try:
            probe = subprocess.run(
                [str(runtime_python), "-m", "labelImg", "--help"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
            )
            if probe.returncode == 0:
                return [str(runtime_python), "-m", "labelImg"], "venv-module"
        except Exception:
            pass

    which = shutil.which("labelImg")
    if which:
        return [which], "PATH"
    return None, None


def check_environment() -> EnvCheckResult:
    runtime_python = detect_runtime_python()
    labelimg_command, _source = detect_labelimg_command()
    messages: list[str] = []

    if runtime_python is None:
        messages.append("Missing repo runtime Python: .venv/Scripts/python.exe")
    if labelimg_command is None:
        messages.append("LabelImg executable/module was not found.")

    return EnvCheckResult(
        ok=labelimg_command is not None,
        runtime_python=str(runtime_python) if runtime_python is not None else None,
        labelimg_command=labelimg_command,
        messages=messages,
    )


def launch_labelimg(project_dir: Path, session: SessionRecord) -> dict[str, Any]:
    command, source = detect_labelimg_command()
    if command is None:
        raise YoloProjectError("LabelImg executable/module was not found. Configure yolo.labelimg_command or install LabelImg into .venv.")
    session_dir = project_dir / session.session_dir
    final_command = [*command, str(session_dir)]
    process = subprocess.Popen(final_command, cwd=str(session_dir))

    manifest_path = session_dir / "session.yaml"
    data = _load_yaml(manifest_path)
    data["labelimg_command"] = final_command
    data["labelimg_pid"] = int(process.pid)
    _write_yaml(manifest_path, data)
    return {
        "ok": True,
        "pid": int(process.pid),
        "command": final_command,
        "source": source,
    }


def project_display_payload(project_dir: Path) -> dict[str, Any]:
    project = load_project_config(project_dir)
    samples = load_samples(project_dir)
    return {
        "project_dir": str(project_dir),
        "project": project,
        "sample_summary": summarize_samples(samples),
        "sample_count": len(samples),
    }


def _split_sample_ids(sample_ids: list[str], *, train_ratio: float, seed: int) -> tuple[list[str], list[str]]:
    items = list(sample_ids)
    if not items:
        return [], []
    rng = random.Random(int(seed))
    rng.shuffle(items)

    if len(items) == 1:
        return items[:], items[:]

    desired_train = int(round(len(items) * float(train_ratio)))
    desired_train = max(1, min(desired_train, len(items) - 1))
    train_ids = items[:desired_train]
    val_ids = items[desired_train:]
    if not val_ids:
        val_ids = [train_ids[-1]]
    return train_ids, val_ids


def build_run_id(prefix: str) -> str:
    return f"{sanitize_filename(prefix, fallback='run')}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"


def sha1_file(path: Path) -> str:
    digest = hashlib.sha1()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _normalize_class_names(class_names: Iterable[str]) -> list[str]:
    normalized: list[str] = []
    for item in class_names:
        value = str(item).strip()
        if value and value not in normalized:
            normalized.append(value)
    return normalized


def _optional_text(value: Any) -> str | None:
    if value in (None, "", []):
        return None
    return str(value)


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    with open(path, "r", encoding="utf-8") as handle:
        payload = yaml.safe_load(handle) or {}
    return payload if isinstance(payload, dict) else {}


def _write_yaml(path: Path, payload: dict[str, Any]) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        yaml.safe_dump(payload, handle, allow_unicode=True, sort_keys=False)
