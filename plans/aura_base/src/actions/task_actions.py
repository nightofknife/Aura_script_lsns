from __future__ import annotations

from packages.aura_core.api import action_info


@action_info(name="aura.run_task", public=True)
def run_task(engine, task_ref: str, inputs: dict | None = None):
    # 这个 action 的真实执行在 ActionInjector 内部处理，这里只保留注册占位符。
    return None
