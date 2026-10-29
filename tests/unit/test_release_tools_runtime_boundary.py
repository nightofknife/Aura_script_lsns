"""Release metadata and smoke commands describe the supported PC runtime."""
import json
from pathlib import Path

from scripts.release import validate_release, write_build_info
from scripts.release.release_contract import parse_hashed_lock


ROOT = Path(__file__).resolve().parents[2]


def test_build_information_uses_only_current_asset_fingerprints(tmp_path, monkeypatch):
    lock = tmp_path / "runtime.lock"
    lock.write_text("numpy==2.0.0 --hash=sha256:" + "a" * 64 + "\n", encoding="utf-8")
    ocr = tmp_path / "ocr"
    ocr.mkdir()
    (ocr / "model.onnx").write_bytes(b"fixture model")
    monkeypatch.setattr(write_build_info, "_installed_version", lambda name: "fixture")
    information = write_build_info.build_information(
        profile="cpu", label="v1.2.3", source_commit="a" * 40, source_dirty=False,
        contract_path=ROOT / "packaging/release-contract.json", lock_path=lock, ocr_root=ocr,
    )
    assert information["ocr_fingerprint"]
    assert information["dependencies"] == {"numpy": "2.0.0"}
    assert "mumu_lock_sha256" not in information
    path = write_build_info.write_information(tmp_path / "release", information)
    assert json.loads(path.read_text("utf-8")) == information


def test_release_smoke_queries_pc_without_executing_game_operations(tmp_path, monkeypatch):
    commands = []
    monkeypatch.setattr(validate_release, "_resolve_powershell_executable", lambda: "pwsh")
    def invoke(command, **kwargs):
        commands.append(command)
        return '[{"game_name": "resonance_pc"}]' if "games" in command else "ok"
    monkeypatch.setattr(validate_release, "_run_checked", invoke)
    validate_release.run_runtime_smoke(tmp_path / "release")
    task_queries = [command for command in commands if "tasks" in command]
    assert len(task_queries) == 1
    assert task_queries[0][-1] == "resonance_pc"
    assert [str((tmp_path / "release/AuraResonanceGui.exe").resolve()), "--runtime-self-check"] in commands
    assert all("run" not in command for command in commands)
    assert not any("aura_benchmark" in command or "resonance" in command for command in commands)


def test_release_locks_drop_retired_video_decoder_consistently():
    cpu = parse_hashed_lock(ROOT / "requirements/release-cpu.lock.txt")
    gpu = parse_hashed_lock(ROOT / "requirements/release-gpu.lock.txt")
    assert "av" not in cpu
    assert "av" not in gpu
    cpu.pop("onnxruntime")
    gpu.pop("onnxruntime-gpu")
    assert cpu == gpu
