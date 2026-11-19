"""Carry the identity of a client choice through selection and confirmation."""
import math

from ._deep_dive_node_rewards import ITEM_REPLACEMENT, result_changed


def native_key(choice):
    return ([choice[key] for key in ('revision', 'kind', 'subject_id', 'index', 'id')]
            if choice is not None else None)


def choice_signature(observation):
    return [observation['scene'],
            [[r['group'], r['state'], native_key(r.get('native'))]
             for r in observation.get('event_options', [])],
            [[native_key(r['native']) if 'native' in r else round(r['point'][0] / 10), r['selected']]
             for r in observation.get('cards', [])], native_key(observation.get('native'))]


def option_set(rows):
    return [[row['title'], row['group'], native_key(row['native'])] if 'native' in row
            else [row['title'], row['group']] for row in rows]


def choice_target(row, command, point):
    if 'native' in row:
        return dict(row['native'], command=command)
    return point


def target_fields(target):
    return {'native': dict(target)} if isinstance(target, dict) else {'point': list(target)}


def pending_target(pending):
    return dict(pending['native']) if 'native' in pending else list(pending['point'])


def can_retry(pending):
    native = pending.get('native')
    return native is None or native['command'] == 'select'


def same_choice(row, pending):
    native = pending.get('native')
    return native is None or native_key(native) == native_key(row.get('native'))


def ordered_cards(rows):
    if any('native' in row for row in rows):
        return rows if all('native' in row for row in rows) else []
    return sorted(rows, key=lambda row: row['point'][0])


def matching_card(rows, pending):
    if 'native' in pending:
        return next((row for row in rows if same_choice(row, pending)), None)
    point = pending.get('target')
    if point is not None:
        return next((row for row in rows if 'point' in row and math.dist(row['point'], point) < 20), None)


def new_card_panel(rows, pending):
    native = pending.get('native')
    return native is not None and any('native' in row and row['native']['revision'] != native['revision']
                                      for row in rows)


def result_progress(pending, observation):
    # A transition frame or a visual change does not acknowledge a discard if
    # the same full-bag dialog is still present.
    if observation['scene'] == ITEM_REPLACEMENT and pending.get('native', {}).get('kind') == 'replacement':
        current = observation.get('native')
        return current is not None and current['revision'] != pending['native']['revision']
    return result_changed(pending.get('result_scene'), pending.get('visual'), observation)
