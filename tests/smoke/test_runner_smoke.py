"""Exercise only the framework runner lifecycle and benchmark task."""

from __future__ import annotations

from packages.aura_game import EmbeddedGameRunner, SubprocessGameRunner


def test_embedded_runner_starts_and_stops() -> None:
    runner = EmbeddedGameRunner()
    try:
        assert runner.start()["ready"] is True
        assert runner.stop()["ready"] is False
    finally:
        runner.close()


def test_embedded_runner_discovers_required_games_and_tasks() -> None:
    runner = EmbeddedGameRunner()
    try:
        games = {row["game_name"] for row in runner.list_games()}
        assert {"aura_benchmark", "resonance", "resonance_pc"}.issubset(games)

        for game_name in ("resonance", "resonance_pc"):
            assert runner.list_tasks(game_name)
    finally:
        runner.close()


def test_embedded_runner_executes_framework_benchmark() -> None:
    runner = EmbeddedGameRunner()
    try:
        result = runner.run_task(
            game_name="aura_benchmark",
            task_ref="tasks:single_sleep.yaml",
            inputs={"duration_ms": 1, "scenario": "project_smoke"},
            wait=True,
            timeout_sec=60,
        )
        assert result["run"]["detail"]["status"] == "success"
    finally:
        runner.close()


def test_subprocess_runner_starts_and_stops() -> None:
    runner = SubprocessGameRunner()
    try:
        assert runner.start()["ready"] is True
        assert runner.stop()["ready"] is False
    finally:
        runner.close()


def test_subprocess_runner_discovers_games() -> None:
    runner = SubprocessGameRunner()
    try:
        games = {row["game_name"] for row in runner.list_games()}
        assert {"aura_benchmark", "resonance", "resonance_pc"}.issubset(games)
    finally:
        runner.close()
