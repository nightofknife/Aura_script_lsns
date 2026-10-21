"""Repository-only entrypoints for diagnostics, asset production and research."""
from __future__ import annotations

import argparse
import runpy
import sys
from pathlib import Path


DIAGNOSTICS = {
    "runtime": "tools.runtime_probe",
    "capture": "tools.capture_probe",
    "input": "tools.input_profile_debugger",
    "gpu": "tools.gpu_runtime_diagnostics",
    "window": "tools.window_visibility_monitor",
    "logs": "tools.log_analyzer",
    "task-graph": "tools.task_graph_viewer",
    "state-map": "tools.state_map_inspector",
    "plan": "tools.plan_doctor",
    "deep-dive-plan": "tools.plan_deep_dive_layout",
}
ASSETS = {
    # One route for each existing production algorithm. Registry generation
    # only inspects local filenames; listing help never imports a model/runtime.
    path.stem.replace("_", "-"): f"tools.{path.stem}"
    for path in sorted(Path(__file__).parent.glob("*.py"))
    if path.stem.startswith(("build_", "generate_", "calibrate_", "review_"))
}
ASSETS.update({
    "yolo": "tools.yolo_workbench",
    "video-frames": "tools.extract_video_frames_every_n",
    "ocr-export": "tools.export_paddleocr_onnx",
    "team-recommendations": "tools.sync_team_recommendations",
    "template-capture": "tools.quick_template_capture",
})
RESEARCH = {
    "saved-view": "research.read_saved_four_view",
    "reading-check": "research.deep_dive_four_view_reading_check",
    "servo": "tools.deep_dive_four_view_servo_experiment",
    "capture-broker": "tools.deep_dive_vertical_capture_broker",
    "command": "tools.deep_dive_vertical_command",
}
GROUPS = {"diagnostics": DIAGNOSTICS, "assets": ASSETS, "research": RESEARCH}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    groups = parser.add_subparsers(dest="group", required=True)
    for name, commands in GROUPS.items():
        group = groups.add_parser(name)
        group.add_argument("command", choices=sorted(commands))
        group.add_argument("arguments", nargs=argparse.REMAINDER,
                           help="Arguments passed to the selected tool; use COMMAND --help")
    args = parser.parse_args(argv)
    module = GROUPS[args.group][args.command]
    previous_argv = sys.argv
    try:
        sys.argv = [module, *args.arguments]
        runpy.run_module(module, run_name="__main__")
    finally:
        sys.argv = previous_argv


if __name__ == "__main__":
    main()
