"""Read-only observation fixture; its click surface never connects to Windows."""
import asyncio
import json
from pathlib import Path

from plans.resonance_pc.src.actions import _eternal_scuffle_runtime as runtime

# Route the existing framework logger to this isolated test project, as normal
# runtime configuration does. Production Scuffle must not create its own files.
runtime.logger.setup(log_dir=str(Path(__file__).parent / "logs"), task_name="aura_session")

CATALOG = {
    "ranking_version": "offline-replay-v1",
    "characters": [{"id": i, "rank": i} for i in [1, 2, 3, 4, 5, 90, 91]],
    "equipment": [{"id": 201 + i, "rank": i + 1, "slot_type": slot}
                  for i, slot in enumerate(("attack", "defense", "support"))],
}


class ScriptedSurface:
    def __init__(self):
        self.scene = "home"
        self.round = self.pair = self.encounter = 0
        self.selected = None
        self.boxes = []
        self.clicks = []
        self.observations = 0
        self.assignments = []

    def get_window_size(self):
        return (1280, 720)

    def controls(self):
        names = {
            "home": ["play"], "coin": ["min", "plus", "max", "confirm"],
            "captain": ["confirm"], "stage": ["start", "abandon"],
            "victory": ["next"], "defeat": ["next"],
            "abandon_confirm": ["confirm"], "assign": ["confirm"],
            "settlement": ["back"] if not self.boxes else [],
        }.get(self.scene, [])
        return {name: {"center": [20 + i * 20, 20]} for i, name in enumerate(names)}

    def click(self, x, y, *args, **kwargs):
        old = self.scene
        control = next((k for k, v in self.controls().items() if v["center"] == [x, y]), None)
        self.clicks.append({"round": self.round, "scene": old, "control": control, "point": [x, y]})
        if old == "home" and control == "play":
            self.round += 1
            self.pair = self.encounter = 0
            self.scene = "coin"
        elif old == "coin" and control == "confirm":
            self.scene = "role_select"
        elif old == "role_select":
            assert [x, y] == [100, 200]
            self.scene = "initial_equipment"
        elif old == "initial_equipment":
            assert [x, y] == [100, 200]
            self.pair += 1
            self.scene = "captain" if self.pair == 5 else "role_select"
        elif old == "captain":
            self.scene = "stage"
        elif old == "stage" and control == "start":
            self.encounter += 1
            self.scene = "defeat" if self.round == 2 and self.encounter == 2 else "victory"
        elif old == "victory":
            self.scene = "loot_select" if self.encounter == 1 else "settlement"
            if self.scene == "settlement":
                self.boxes = list(range(7))
        elif old == "loot_select":
            assert [x, y] == [300, 200]  # defense wins over occupied attack slots
            self.scene = "assign"
            self.selected = 5
        elif old == "assign" and control == "confirm":
            assert self.selected == 1
            self.assignments.append({"round": self.round, "id": self.selected, "screen_index": 4})
            self.scene = "stage"
        elif old == "assign":
            self.selected = 5 - ((x - 100) // 100)
        elif old == "defeat":
            self.scene = "stage"
        elif old == "stage" and control == "abandon":
            self.scene = "abandon_confirm"
        elif old == "abandon_confirm":
            self.scene = "settlement"
            self.boxes = list(range(4))
        elif old == "settlement" and control == "back":
            assert not self.boxes
            self.scene = "home"
        elif old == "settlement":
            self.boxes.remove((x - 100) // 100)
        else:
            assert old == "coin" and control in {"min", "max", "plus"}

    def observe(self):
        self.observations += 1
        return {"valid": True, "scene": self.scene, "controls": self.controls(),
                "boxes": [{"center": [100 + i * 100, 400]} for i in self.boxes]}

    def read_candidates(self, frame, kind, **kwargs):
        if getattr(self, "pause_second_pair", False) and kind == "role" and self.pair == 1:
            (Path(__file__).parent / "cancellation_ready.json").write_text("{}", encoding="utf-8")
            return []
        if getattr(self, "fail_second_pair", False) and kind == "role" and self.pair == 1:
            raise RuntimeError("scripted candidate capture failure")
        ids = [self.pair + 1, 90, 91] if kind == "role" else [201, 202, 203]
        return [{"id": item, "index": i, "select_point": [100 + 200 * i, 200]}
                for i, item in enumerate(ids)]

    def read_team(self, frame, **kwargs):
        ids = [5, 4, 3, 2, 1] if self.scene == "assign" else [1, 2, 3, 4, 5]
        return [{"id": item, "screen_index": i, "center": [100 + i * 100, 300],
                 "selected": item == self.selected,
                 "slots": {"attack": "occupied", "defense": "empty", "support": "empty"},
                 "occupied_ids": {"attack": 201}}
                for i, item in enumerate(ids)]


    def read_selected_character(self, frame):
        selected = [row for row in self.read_team(frame) if row.get("selected")]
        return selected[0] if len(selected) == 1 else None


GAME = ScriptedSurface()
runtime.catalog_for = lambda engine=None: CATALOG
runtime.make_observer = lambda *args, **kwargs: GAME
_real_poll = runtime.poll_until


async def accelerated_poll(**kwargs):
    # Keep stable observations bounded while allowing CI worker-thread scheduling.
    kwargs["interval"] = 0.0001
    kwargs["timeout"] = min(kwargs["timeout"], 10 if getattr(GAME, "pause_second_pair", False) else 2.0)
    return await _real_poll(**kwargs)


async def accelerated_sleep(*args, **kwargs):
    await asyncio.sleep(0)


runtime.poll_until = accelerated_poll
runtime.aura_sleep = accelerated_sleep


def save_audit(engine, **extra):
    path = runtime.plan_root_for(engine).parents[1] / "replay_audit.json"
    path.write_text(json.dumps({"clicks": GAME.clicks, "observations": GAME.observations,
                               "assignments": GAME.assignments, **extra}, indent=2), encoding="utf-8")


async def replay_invoke(*args, **kwargs):
    try:
        return await runtime.invoke(*args, **kwargs)
    finally:
        save_audit(args[6])


async def replay_finish(session_key, state_store, event_bus, round_results=None):
    result = await runtime.finish(session_key, state_store, event_bus, round_results=round_results)
    path = Path(__file__).parent / "replay_audit.json"
    path.write_text(json.dumps({"clicks": GAME.clicks, "observations": GAME.observations,
                               "assignments": GAME.assignments,
                               "session_deleted": await state_store.get(session_key) is None,
                               "summary": result}, indent=2), encoding="utf-8")
    return result
