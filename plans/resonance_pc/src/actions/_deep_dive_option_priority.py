"""Rank eligible Deep Dive choices by their configured effects."""

from collections.abc import Mapping, Sequence
from typing import Any


_FREE_BENEFIT, _FREE_EXIT, _PAID_NONCOMBAT, _BATTLE, _UNCLASSIFIED = range(5)
_ITEM_COSTS = {'LoseEquip': 'dice', 'LoseBuff': 'thrust'}
_BENEFITS = {'Reward', 'Intensify', 'Recover'}

Option = Mapping[str, Any]
Candidate = tuple[int, Option]
Cost = tuple[str, float]


def _costs(effect: Option) -> list[Cost]:
    kind = effect['orderType']
    if kind == 'Recover' and effect['param'] < 0:
        return [('hp', -effect['param'])]
    if kind in _ITEM_COSTS:
        return [(_ITEM_COSTS[kind], effect['param'])]
    if kind == 'Discard':
        return [(f"item:{item['id']}", item['num']) for item in effect['discardList']]
    return []


def _classify(option: Option) -> tuple[int, Cost | None]:
    effects = option.get('effects')
    if effects is None:
        return _UNCLASSIFIED, None
    if not effects:
        return _FREE_EXIT, None
    if any(effect['orderType'] == 'Level' for effect in effects):
        return _BATTLE, None

    costs = []
    followup = False
    for effect in effects:
        kind = effect['orderType']
        if kind == 'Event' and option.get('next_event_id') == effect['eventId']:
            # Only catalogued continuations whose reachable choices avoid battle are eligible here.
            followup = True
        elif kind not in _BENEFITS and kind not in _ITEM_COSTS and kind != 'Discard':
            return _UNCLASSIFIED, None
        costs.extend(_costs(effect))

    if not costs and not followup:
        return _FREE_BENEFIT, None
    resources = {resource for resource, _ in costs}
    if followup or len(resources) != 1:
        return _PAID_NONCOMBAT, None
    return _PAID_NONCOMBAT, (costs[0][0], sum(amount for _, amount in costs))


def choose_event_option(candidates: Sequence[Candidate]) -> Candidate:
    ranked = [(index, option, *_classify(option)) for index, option in candidates]
    best = min(ranked, key=lambda row: (
        row[2], row[1].get('priority', 10) if row[2] in (_FREE_BENEFIT, _UNCLASSIFIED) else 0, row[0],
    ))
    index, option, tier, cost = best
    if cost is None:
        return index, option

    # Compare quantities only within the same resource. UI order resolves incomparable costs.
    comparable = [row for row in ranked if row[2] == tier and row[3] is not None and row[3][0] == cost[0]]
    index, option, _, _ = min(comparable, key=lambda row: (row[3][1], row[0]))
    return index, option
