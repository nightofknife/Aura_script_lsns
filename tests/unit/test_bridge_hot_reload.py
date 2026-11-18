from types import SimpleNamespace

import pytest

from plans.aura_base.src.platform.contracts import TargetRuntimeError
from plans.resonance_pc.src.bridge.client import BridgeClient
from plans.resonance_pc.src.bridge.native_bundle import BOOTSTRAP_DLL, HOST_DLL, stage_bootstrap, stage_host
from plans.resonance_pc.src.services import input_bridge_service


def test_native_assets_stage_by_content(tmp_path, monkeypatch):
    assets = tmp_path / "assets"
    profiles = assets / "profiles"
    profiles.mkdir(parents=True)
    (assets / BOOTSTRAP_DLL).write_bytes(b"bootstrap")
    profile = profiles / "profile.json"
    profile.write_text("{}", encoding="utf-8")
    host = assets / HOST_DLL
    host.write_bytes(b"version A")
    cache = tmp_path / "cache"
    monkeypatch.setenv("AURA_INPUT_BRIDGE_CACHE", str(cache))

    bootstrap = stage_bootstrap(assets)
    assert bootstrap.read_bytes() == b"bootstrap"
    assert (bootstrap.parent / "profiles" / profile.name).read_text(encoding="utf-8") == "{}"
    profile.write_text('{"version": 2}', encoding="utf-8")
    assert stage_bootstrap(assets).parent != bootstrap.parent
    a = stage_host(assets, str(cache))
    host.write_bytes(b"version B")
    b = stage_host(assets, str(cache))
    host.write_bytes(b"version A")
    assert stage_host(assets, str(cache)) == a
    assert a != b
    assert (cache / "host" / a / HOST_DLL).read_bytes() == b"version A"
    assert (cache / "host" / b / HOST_DLL).read_bytes() == b"version B"


def test_bridge_waits_for_host_swap_before_hello(monkeypatch, tmp_path):
    service = input_bridge_service.ResonancePcInputBridgeService(config={})
    monkeypatch.setattr(service, "_bridge_target", lambda: (222, 333))
    monkeypatch.setattr(input_bridge_service.win32gui, "IsIconic", lambda _: False)
    monkeypatch.setattr(input_bridge_service, "stage_host", lambda assets, cache: "B" * 64)
    monkeypatch.setattr(input_bridge_service.time, "sleep", lambda _: None)
    calls = []
    installed = False
    client = SimpleNamespace(pid=222, uncertain=False, host_instance=None)

    def request(op, args=None, **_):
        nonlocal installed
        calls.append(op)
        if op == "status":
            return {"result": {"bootstrap_abi": 1, "host_state": "loaded", "state": "ready",
                               "cache_root": str(tmp_path), "host_instance": 2 if installed else 1,
                               "capabilities": {"frame_stream": True}}}
        if op == "prepare_host":
            assert args == {"host_hash": "B" * 64}
            if calls.count(op) == 1:
                raise TargetRuntimeError("bridge_transition", "unloading")
            installed = True
            return {"result": {"host_instance": 2}}
        if op == "hello":
            assert client.host_instance == 2
            return {"result": {"capabilities": {"frame_stream": True}}}
        if op == "unload_host":
            assert args == {"host_instance": 2}
        return {"result": {}}

    client.request = request
    monkeypatch.setattr(input_bridge_service, "BridgeClient", lambda _: client)
    assert service.ensure_ready() == {"frame_stream": True}
    assert calls == ["status", "prepare_host", "status", "prepare_host", "status", "hello"]
    service.close()
    assert calls[-1] == "close"
    service.close(unload=True)
    assert calls[-1] == "unload_host"


def test_old_host_instance_rejection_does_not_mark_input_uncertain():
    requests = []

    def exchange(pipe, request, timeout):
        requests.append(request)
        return {"version": 1, "sequence": request["sequence"], "session": request["session"],
                "pid": 222, "process_created": "100", "status": "rejected",
                "error": {"code": "session_invalid", "message": "The bridge instance changed"}}

    client = BridgeClient(222, transport=SimpleNamespace(exchange=exchange))
    client.host_instance = 1
    with pytest.raises(TargetRuntimeError, match="bridge instance changed"):
        client.request("drag")
    assert requests[0]["host_instance"] == 1
    assert not client.uncertain
