from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest

from packages.aura_core.context.persistence.persistent_data_errors import (
    PersistentDataNotFoundError,
    PersistentDataValidationError,
    PersistentDataWriteError,
)
from packages.aura_core.context.persistence.persistent_data_service import PersistentDataService
from plans.aura_base.src.actions.persistent_data_actions import (
    persistent_data_batch,
    persistent_data_increment,
    persistent_data_read,
)


def _service(tmp_path: Path) -> PersistentDataService:
    return PersistentDataService(tmp_path)


def test_read_missing_default_and_deep_copy(tmp_path: Path) -> None:
    service = _service(tmp_path)
    with pytest.raises(PersistentDataNotFoundError) as exc_info:
        service.read("user-info.json")
    assert exc_info.value.code == "persistent_data_file_not_found"
    assert service.read("user-info.json", default={"items": []}) == {"items": []}

    source = {"profile": {"name": "测试"}}
    service.set("user-info.json", [], source)
    source["profile"]["name"] = "changed outside"
    loaded = service.read("user-info.json")
    loaded["profile"]["name"] = "changed copy"
    assert service.read("user-info.json", ["profile", "name"]) == "测试"


@pytest.mark.parametrize(
    "file",
    ["../config.json", "folder/../../escape.json", "absolute.txt", "C:/escape.json"],
)
def test_rejects_unsafe_or_non_json_paths(tmp_path: Path, file: str) -> None:
    service = _service(tmp_path)
    with pytest.raises(PersistentDataValidationError):
        service.read(file, default={})


def test_rejects_symlink_escape(tmp_path: Path) -> None:
    service = _service(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    service.root.mkdir()
    link = service.root / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Directory symlinks are not available for this Windows test user")
    with pytest.raises(PersistentDataValidationError) as exc_info:
        service.set("linked/escape.json", [], {})
    assert exc_info.value.code == "persistent_data_path_escape"


def test_mutation_operations_and_noop_write(tmp_path: Path) -> None:
    service = _service(tmp_path)
    first = service.set("user-info.json", ["profile"], {"name": "A"})
    assert first.changed is True
    target = service.root / "user-info.json"
    original_mtime = target.stat().st_mtime_ns

    same = service.set("user-info.json", ["profile"], {"name": "A"})
    assert same.changed is False
    assert target.stat().st_mtime_ns == original_mtime

    service.merge("user-info.json", ["profile"], {"level": 10})
    service.increment("user-info.json", ["daily", "used"], delta=2, maximum=6)
    capped = service.increment("user-info.json", ["daily", "used"], delta=10, maximum=6)
    service.append("user-info.json", ["history"], {"city": "海角城"})
    deleted = service.delete("user-info.json", ["profile", "level"])
    missing_delete = service.delete("user-info.json", ["profile", "missing"])

    assert capped.new_value == 6
    assert deleted.changed is True
    assert missing_delete.changed is False
    assert service.read("user-info.json", ["profile"]) == {"name": "A"}
    assert service.read("user-info.json", ["history"]) == [{"city": "海角城"}]


def test_increment_rejects_boolean_and_invalid_bounds(tmp_path: Path) -> None:
    service = _service(tmp_path)
    with pytest.raises(PersistentDataValidationError):
        service.increment("user-info.json", ["used"], delta=True)
    with pytest.raises(PersistentDataValidationError):
        service.increment("user-info.json", ["used"], minimum=7, maximum=6)
    service.set("user-info.json", ["used"], False)
    with pytest.raises(PersistentDataValidationError):
        service.increment("user-info.json", ["used"])


def test_batch_is_atomic_and_preserves_unrelated_sections(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.set("user-info.json", [], {"profile": {"name": "A"}, "daily": {"reward": True}})
    result = service.batch(
        "user-info.json",
        [
            {"operation": "merge", "path": ["profile"], "value": {"level": 20}},
            {"operation": "increment", "path": ["daily", "drink_used"], "delta": 1},
        ],
    )
    assert result.changed is True
    assert service.read("user-info.json") == {
        "profile": {"name": "A", "level": 20},
        "daily": {"reward": True, "drink_used": 1},
    }

    before = (service.root / "user-info.json").read_bytes()
    with pytest.raises(PersistentDataValidationError):
        service.batch(
            "user-info.json",
            [
                {"operation": "set", "path": ["profile", "name"], "value": "B"},
                {"operation": "append", "path": ["profile"], "value": "invalid"},
            ],
        )
    assert (service.root / "user-info.json").read_bytes() == before


def test_update_is_atomic_and_requires_object_result(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.set("user-info.json", [], {"daily": {"used": 1}})
    result = service.update(
        "user-info.json",
        lambda data: {**data, "daily": {"used": data["daily"]["used"] + 1}},
    )
    assert result.new_value["daily"]["used"] == 2
    before = (service.root / "user-info.json").read_bytes()
    with pytest.raises(PersistentDataValidationError):
        service.update("user-info.json", lambda _data: [])  # type: ignore[return-value]
    assert (service.root / "user-info.json").read_bytes() == before


def test_write_failure_keeps_original_and_cleans_temp(tmp_path: Path, monkeypatch) -> None:
    service = _service(tmp_path)
    service.set("user-info.json", [], {"value": 1})
    target = service.root / "user-info.json"
    before = target.read_bytes()

    def fail_replace(_source, _target):
        raise OSError("replace failed")

    monkeypatch.setattr("packages.aura_core.context.persistence.persistent_data_service.os.replace", fail_replace)
    with pytest.raises(PersistentDataWriteError):
        service.set("user-info.json", ["value"], 2)
    assert target.read_bytes() == before
    assert list(service.root.glob(".user-info.json.*.tmp")) == []


def test_fsync_failure_keeps_original_and_cleans_temp(tmp_path: Path, monkeypatch) -> None:
    service = _service(tmp_path)
    service.set("user-info.json", [], {"value": 1})
    target = service.root / "user-info.json"
    before = target.read_bytes()

    monkeypatch.setattr(
        "packages.aura_core.context.persistence.persistent_data_service.os.fsync",
        lambda _fd: (_ for _ in ()).throw(OSError("fsync failed")),
    )
    with pytest.raises(PersistentDataWriteError):
        service.set("user-info.json", ["value"], 2)
    assert target.read_bytes() == before
    assert list(service.root.glob(".user-info.json.*.tmp")) == []


def test_unserializable_value_keeps_original(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.set("user-info.json", [], {"value": 1})
    target = service.root / "user-info.json"
    before = target.read_bytes()
    with pytest.raises(PersistentDataValidationError) as exc_info:
        service.set("user-info.json", ["bad"], object())
    assert exc_info.value.code == "persistent_data_not_serializable"
    assert target.read_bytes() == before
    with pytest.raises(PersistentDataValidationError):
        service.set("user-info.json", ["bad"], float("nan"))
    assert target.read_bytes() == before


def test_invalid_intermediate_path_does_not_change_file(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.set("user-info.json", [], {"profile": "not-an-object"})
    target = service.root / "user-info.json"
    before = target.read_bytes()
    with pytest.raises(PersistentDataValidationError) as exc_info:
        service.delete("user-info.json", ["profile", "name"])
    assert exc_info.value.code == "persistent_data_path_not_object"
    assert target.read_bytes() == before


def test_invalid_json_preserves_one_hashed_backup(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.root.mkdir()
    target = service.root / "user-info.json"
    raw = b'{"broken":'
    target.write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()[:12]
    backup = service.root / f"user-info.corrupt-{digest}.json"

    for _ in range(2):
        with pytest.raises(PersistentDataValidationError):
            service.read("user-info.json")
    assert target.read_bytes() == raw
    assert backup.read_bytes() == raw
    assert len(list(service.root.glob("user-info.corrupt-*.json"))) == 1


def test_sequential_tasks_share_one_file_and_one_task_uses_multiple_files(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.set("user-info.json", ["profile"], {"name": "A"})
    service.set("user-info.json", ["daily", "used"], 3)
    assert service.read("user-info.json") == {"profile": {"name": "A"}, "daily": {"used": 3}}

    service.set("trade-history.json", ["last"], {"profit": 100})
    assert service.read("trade-history.json", ["last", "profit"]) == 100
    assert service.read("user-info.json", ["daily", "used"]) == 3


def test_async_wrappers_and_yaml_actions(tmp_path: Path) -> None:
    service = _service(tmp_path)

    async def exercise_async() -> None:
        await service.set_async("user-info.json", ["daily", "used"], 1)
        assert await service.read_async("user-info.json", ["daily", "used"]) == 1
        assert await service.exists_async("user-info.json", ["daily"]) is True

    asyncio.run(exercise_async())
    assert persistent_data_read(
        file="missing.json",
        required=False,
        default={"ok": True},
        persistent_data=service,
    ) == {"ok": True}
    incremented = persistent_data_increment(
        file="user-info.json",
        path=["daily", "used"],
        maximum=6,
        persistent_data=service,
    )
    assert incremented["new_value"] == 2
    batched = persistent_data_batch(
        file="user-info.json",
        operations=[{"operation": "set", "path": ["location"], "value": "海角城"}],
        persistent_data=service,
    )
    assert batched["operation"] == "batch"


def test_inspect_does_not_create_missing_file(tmp_path: Path) -> None:
    service = _service(tmp_path)
    missing = service.inspect("user-info.json")
    assert missing["exists"] is False
    assert not service.root.exists()
    service.set("user-info.json", [], {"ok": True})
    present = service.inspect("user-info.json")
    assert present["exists"] is True
    assert present["valid"] is True
    assert json.loads((service.root / "user-info.json").read_text(encoding="utf-8")) == {"ok": True}
