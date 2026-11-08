"""Independent entries for reminder-gated daily mission and battle-pass rewards."""
from packages.aura_core.api import action_info, requires_services

from ._daily_reward_flow import DailyRewardFlow
from ._battle_pass_reward_flow import BattlePassRewardFlow
from .runtime_preflight_pc_actions import resonance_pc_require_client_resolution


@action_info(name="resonance_pc.collect_daily_mission_rewards", public=True, read_only=False,
             description="Collect daily mission/activity rewards only when the main-screen reminder is visible, then confirm return to main.")
@requires_services(app="plans/aura_base/app", observation="plans/aura_base/observation",
                   interaction="plans/aura_base/interaction")
def resonance_pc_collect_daily_mission_rewards(app=None, observation=None, interaction=None) -> dict:
    resonance_pc_require_client_resolution(app=app)
    return DailyRewardFlow(app, observation, interaction).run()


@action_info(name="resonance_pc.collect_battle_pass_rewards", public=True, read_only=False,
             description="Collect completed tasks and unlocked rewards for every pass when its main-screen star has a reminder, then return to main.")
@requires_services(app="plans/aura_base/app", observation="plans/aura_base/observation",
                   interaction="plans/aura_base/interaction")
def resonance_pc_collect_battle_pass_rewards(app=None, observation=None, interaction=None) -> dict:
    resonance_pc_require_client_resolution(app=app)
    return BattlePassRewardFlow(app, observation, interaction).run()
