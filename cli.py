# -*- coding: utf-8 -*-
"""Aura Game Framework launcher."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import click

from packages.aura_core.runtime.privilege import AdminPrivilegeRequiredError, ensure_admin_startup
from packages.aura_game import EmbeddedGameRunner, SubprocessGameRunner


def _make_runner(runner_mode: str):
    if runner_mode == "subprocess":
        return SubprocessGameRunner()
    return EmbeddedGameRunner()


def _dump(payload: Any) -> None:
    click.echo(json.dumps(payload, ensure_ascii=False, indent=2))


@click.group()
def aura() -> None:
    """Aura Game Framework."""
    try:
        ensure_admin_startup("Aura CLI")
    except AdminPrivilegeRequiredError as exc:
        raise click.ClickException(str(exc)) from exc


@aura.command("tui")
def tui_command() -> None:
    """Run interactive TUI mode."""
    try:
        from packages.aura_core.cli.tui_app import run_tui
    except ModuleNotFoundError as exc:
        raise click.ClickException(
            "TUI mode requires `prompt_toolkit`. Please install runtime dependencies first."
        ) from exc

    run_tui()


@aura.group("gui")
def gui_group() -> None:
    """Run desktop GUI frontends."""


@gui_group.command("resonance")
def gui_resonance_command() -> None:
    """Run the Resonance desktop control console."""
    try:
        from packages.resonance_gui.app import launch_resonance_gui
    except ModuleNotFoundError as exc:
        missing_name = str(getattr(exc, "name", "") or "")
        if missing_name.startswith("PySide6"):
            raise click.ClickException(
                "Resonance GUI requires PySide6. Please run: pip install -r requirements/gui.txt"
            ) from exc
        raise

    raise SystemExit(launch_resonance_gui())


@aura.command("games")
@click.option("--all/--no-all", "include_shared", default=False, help="Include shared modules without tasks.")
@click.option(
    "--runner",
    "runner_mode",
    type=click.Choice(["embedded", "subprocess"]),
    default="embedded",
    show_default=True,
    help="Execution backend for the local SDK.",
)
def games_command(include_shared: bool, runner_mode: str) -> None:
    """List available game modules."""
    runner = _make_runner(runner_mode)
    try:
        _dump(runner.list_games(include_shared=include_shared))
    finally:
        if hasattr(runner, "close"):
            runner.close()


@aura.command("tasks")
@click.argument("game_name")
@click.option(
    "--runner",
    "runner_mode",
    type=click.Choice(["embedded", "subprocess"]),
    default="embedded",
    show_default=True,
)
def tasks_command(game_name: str, runner_mode: str) -> None:
    """List tasks for one game module."""
    runner = _make_runner(runner_mode)
    try:
        _dump(runner.list_tasks(game_name))
    finally:
        if hasattr(runner, "close"):
            runner.close()


@aura.command("run")
@click.argument("game_name")
@click.argument("task_ref")
@click.option("--inputs", "inputs_json", default="{}", show_default=True, help="Task inputs as JSON object.")
@click.option("--inputs-file", type=click.Path(exists=True, dir_okay=False, path_type=Path), default=None, help="Read task inputs from a JSON file.")
@click.option("--wait/--no-wait", default=True, show_default=True, help="Wait for the run to finish.")
@click.option(
    "--timeout-sec",
    default=0.0,
    show_default=True,
    type=float,
    help="Wait timeout in seconds; <= 0 waits until the task reaches a terminal state.",
)
@click.option(
    "--runner",
    "runner_mode",
    type=click.Choice(["embedded", "subprocess"]),
    default="embedded",
    show_default=True,
)
def run_command(
    game_name: str,
    task_ref: str,
    inputs_json: str,
    inputs_file: Path | None,
    wait: bool,
    timeout_sec: float,
    runner_mode: str,
) -> None:
    """Dispatch one task."""
    if inputs_file is not None and inputs_json != "{}":
        raise click.ClickException("Use either --inputs or --inputs-file, not both.")

    if inputs_file is not None:
        inputs_source = inputs_file.read_text(encoding="utf-8-sig")
    else:
        inputs_source = inputs_json

    try:
        inputs = json.loads(inputs_source)
    except Exception as exc:  # noqa: BLE001
        source_label = f"--inputs-file {inputs_file}" if inputs_file is not None else "--inputs"
        raise click.ClickException(f"Invalid JSON for {source_label}: {exc}") from exc

    if not isinstance(inputs, dict):
        raise click.ClickException("--inputs must decode to a JSON object.")

    runner = _make_runner(runner_mode)
    try:
        result = runner.run_task(
            game_name=game_name,
            task_ref=task_ref,
            inputs=inputs,
            wait=wait,
            timeout_sec=timeout_sec,
        )
        _dump(result)
    finally:
        if hasattr(runner, "close"):
            runner.close()


@aura.command("runs")
@click.option("--limit", default=20, show_default=True, type=int)
@click.option("--game", "game_name", default=None, help="Filter by game module.")
@click.option("--task", "task_name", default=None, help="Filter by task_ref.")
@click.option("--status", default=None, help="Filter by status.")
@click.option(
    "--runner",
    "runner_mode",
    type=click.Choice(["embedded", "subprocess"]),
    default="embedded",
    show_default=True,
)
def runs_command(limit: int, game_name: str | None, task_name: str | None, status: str | None, runner_mode: str) -> None:
    """List recent runs."""
    runner = _make_runner(runner_mode)
    try:
        _dump(runner.list_runs(limit=limit, game_name=game_name, task_name=task_name, status=status))
    finally:
        if hasattr(runner, "close"):
            runner.close()


@aura.command("run-detail")
@click.argument("cid")
@click.option(
    "--runner",
    "runner_mode",
    type=click.Choice(["embedded", "subprocess"]),
    default="embedded",
    show_default=True,
)
def run_detail_command(cid: str, runner_mode: str) -> None:
    """Show one run detail."""
    runner = _make_runner(runner_mode)
    try:
        _dump(runner.get_run(cid))
    finally:
        if hasattr(runner, "close"):
            runner.close()


@aura.command("doctor")
@click.option("--all/--no-all", "include_shared", default=True, show_default=True)
@click.option("--ocr/--no-ocr", "check_ocr", default=False, show_default=True, help="Preload and test OCR.")
@click.option(
    "--ocr-provider",
    type=click.Choice(["cpu", "cuda"]),
    default=None,
    help="Require a specific OCR execution provider; implies --ocr.",
)
@click.option(
    "--runner",
    "runner_mode",
    type=click.Choice(["embedded", "subprocess"]),
    default="embedded",
    show_default=True,
)
def doctor_command(
    include_shared: bool,
    check_ocr: bool,
    ocr_provider: str | None,
    runner_mode: str,
) -> None:
    """Show an environment and module summary."""
    runner = _make_runner(runner_mode)
    try:
        result = runner.doctor(
            include_shared=include_shared,
            check_ocr=check_ocr or ocr_provider is not None,
            required_ocr_provider=ocr_provider,
        )
        _dump(result)
        if not bool(result.get("ok", True)):
            raise click.exceptions.Exit(1)
    finally:
        if hasattr(runner, "close"):
            runner.close()


if __name__ == "__main__":
    aura()
