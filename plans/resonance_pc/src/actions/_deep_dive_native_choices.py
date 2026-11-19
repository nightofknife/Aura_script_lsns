"""Translate the client's event choice panels into the existing event policies."""
from functools import lru_cache

from ._deep_dive_event_vision import catalog as healing_catalog
from ._deep_dive_node_rewards import ITEM_REPLACEMENT
from ._deep_dive_vortex_vision import catalog as vortex_catalog


@lru_cache(maxsize=2)
def _policy(event_type):
    catalog = {'healing': healing_catalog, 'vortex': vortex_catalog}.get(event_type)
    if catalog is None:
        return {}, {}
    data = catalog()
    return ({event['id']: set(event['options']) for event in data['events']},
            {option['id']: option for option in data['options']})


def observe_choice(observation, snapshot, event_type):
    kind = snapshot['kind']
    status = snapshot['status']
    if status == 'absent':
        return observation
    current = dict(observation, valid=True, scene='native_choice_wait', native_choice_kind=kind,
                   native_choice_reason=snapshot.get('reason'))
    current.pop('click', None)
    if status != 'ready':
        return current

    revision = snapshot['revision']
    subject_id = snapshot['subject_id']
    rows = snapshot['rows']
    controls = [dict(kind=kind, revision=revision, subject_id=subject_id,
                     index=row['index'], id=row['id']) for row in rows]
    if kind == 'replacement':
        return dict(current, scene=ITEM_REPLACEMENT, native=controls[0])
    if kind in {'battle_defeat', 'battle_victory'}:
        return dict(current, scene=kind, native=controls[0])
    if kind in {'formation_fill', 'formation_start', 'formation_leave'}:
        counts = {key: snapshot[key] for key in ('selected_count', 'available_count', 'required_count')
                  if key in snapshot}
        return dict(current, scene='battle_formation', native=controls[0], formation=counts)
    if kind in {'formation_quit', 'formation_finish'}:
        return dict(current, scene=kind, native=controls[0])
    if kind in {'rest_shortage', 'rest_finish'}:
        return dict(current, scene='rest_area' if kind == 'rest_shortage' else 'rest_finish', native=controls[0])
    if kind == 'cards':
        cards = [dict(native=control, selected=row['selected']) for row, control in zip(rows, controls)]
        return dict(current, scene='event_card_selection', cards=cards)

    events, options = _policy(event_type)
    allowed = events.get(subject_id, set())
    if not allowed or any(row['id'] not in allowed or row['id'] not in options for row in rows):
        return dict(current, native_choice_reason=f'unknown_native_event_options:{subject_id}')
    event_options = []
    for row, control in zip(rows, controls):
        state = 'disabled' if row['disabled'] else 'selected' if row['selected'] else 'normal'
        event_options.append(dict(options[row['id']], state=state, native=control))
    return dict(current, scene='event_options', event_options=event_options)
