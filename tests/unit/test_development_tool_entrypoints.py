"""Unified development routes dispatch without loading game or model services."""
import sys

import pytest

from tools import __main__ as tools_cli


@pytest.mark.parametrize("argv,module,arguments", [
    (["diagnostics", "runtime", "--help"], "tools.runtime_probe", ["--help"]),
    (["assets", "build-city-identity-templates", "--source", "frame.png"],
     "tools.build_city_identity_templates", ["--source", "frame.png"]),
    (["research", "saved-view", "frame.png", "--view", "1"],
     "research.read_saved_four_view", ["frame.png", "--view", "1"]),
])
def test_routes_preserve_tool_arguments_and_process_argv(monkeypatch, argv, module, arguments):
    calls = []
    original = sys.argv
    monkeypatch.setattr(tools_cli.runpy, "run_module",
                        lambda name, **kwargs: calls.append((name, kwargs, list(sys.argv))))
    tools_cli.main(argv)
    assert calls == [(module, {"run_name": "__main__"}, [module, *arguments])]
    assert sys.argv is original


def test_tool_failure_restores_callers_argv(monkeypatch):
    original = sys.argv
    def fail(*args, **kwargs):
        raise RuntimeError("tool failure")
    monkeypatch.setattr(tools_cli.runpy, "run_module", fail)
    with pytest.raises(RuntimeError, match="tool failure"):
        tools_cli.main(["diagnostics", "runtime"])
    assert sys.argv is original
