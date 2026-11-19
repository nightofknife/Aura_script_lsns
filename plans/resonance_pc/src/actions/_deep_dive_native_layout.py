"""Convert a bridge snapshot to the layout used by reports and the planner."""
from ._deep_dive_planner_rules import (
    BASES, FACES, cell_to_slot, coord_dict, geometry, rotation_dict, validate_cube_size,
)

# Native prefab faces 1–6 are up, back, right, forward, left, down.
# The first native grid index runs along the planner's column axis. The
# second runs upwards on five faces and along the row axis on the bottom.
NATIVE_FACES = 'UFRBLD'
NODE_INFO = {
    'empty': ('empty', 'white_diamond'),
    'battle': ('battle', 'red_single_eye'),
    'boss_battle': ('boss_battle', 'orange_triple_eye'),
    'elite_battle': ('elite_battle', 'orange_triple_eye'),
    'shop': ('shop', 'blue_scales'),
    'special_shop': ('shop', 'blue_scales'),
    'healing': ('healing', 'green_burst'),
    'adventure': ('vortex', 'purple_ring'),
    'reward': ('item', 'yellow_hex'),
}


def _coordinate(face, row, col, size):
    if (type(face) is not int or not 1 <= face <= 6 or type(row) is not int
            or type(col) is not int or not (1 <= row <= size and 1 <= col <= size)):
        raise ValueError('invalid_native_board_coordinate')
    return NATIVE_FACES[face - 1], (col - 1 if face == 6 else size - col), row - 1


def _position(key):
    face, row, col = key
    return dict(face=face, row=row, col=col)


def native_cell_for_slot(slot, cube_size=3):
    cell = coord_dict(slot, cube_size=cube_size)
    face = NATIVE_FACES.index(cell['face']) + 1
    return dict(face=face, row=cell['col'] + 1,
                col=cell['row'] + 1 if face == 6 else cube_size - cell['row'])


def layout_from_board(board):
    size = board['size']
    if (board.get('schema') != 'resonance_pc.deep_dive_board.v1' or board.get('coordinate_base') != 1
            or type(size) is not int or not 1 <= size <= 9):
        raise ValueError('invalid_native_board_schema')
    cells = {}
    for face in board['faces']:
        if len(face['rows']) != size:
            raise ValueError('incomplete_native_board_face')
        for row, nodes in enumerate(face['rows'], 1):
            if len(nodes) != size:
                raise ValueError('incomplete_native_board_row')
            for col, node in enumerate(nodes, 1):
                key = _coordinate(face['face'], row, col, size)
                if key in cells:
                    raise ValueError('duplicate_native_board_coordinate')
                kind, icon = NODE_INFO.get(node['type'], (None, None))
                cells[key] = dict(_position(key), occupant='none', occupant_status='confirmed',
                                  node_status='known' if kind else 'unknown', node_kind=kind,
                                  icon_id=icon, native_node=dict(node))
    if len(cells) != 6 * size**2:
        raise ValueError('incomplete_native_board_coordinates')

    targets = {kind: [] for kind in ('player', 'singularity', 'inspiration')}
    for obj in board['objects']:
        key = _coordinate(obj['face'], obj['row'], obj['col'], size)
        cells[key].setdefault('objects', []).append(dict(obj))
        if obj['type'] in targets:
            targets[obj['type']].append(key)
            cell = cells[key]
            if cell['occupant'] == 'none':
                cell['occupant'] = obj['type']
            else:
                cell.update(occupant='unknown', occupant_status='conflict')
    if len(targets['player']) != 1 or len(targets['singularity']) > 1:
        raise ValueError('invalid_native_board_actors')
    known = sum(cell['node_status'] == 'known' for cell in cells.values())
    complete = known == len(cells) and all(cell['occupant_status'] == 'confirmed' for cell in cells.values())
    layout = dict(schema='resonance_pc.deep_dive_layout.v1', coordinate_frame='native_board', size=size,
                status='completed' if complete else 'partial', success=complete, layout_complete=complete,
                reason='bridge_snapshot' if complete else 'unknown_or_overlapping_nodes', map_valid=True,
                recognition_goal='full', recognition_backend='bridge', known_cells=known,
                faces_observed=6, cells=[cells[key] for key in sorted(cells, key=lambda k: (FACES.index(k[0]), *k[1:]))],
                faces=[dict(face_id=face, normal=axes[0], column_axis=axes[1], row_axis=axes[2])
                       for face, axes in BASES.items()],
                player_cell=_position(targets['player'][0]),
                singularity_cell=_position(targets['singularity'][0]) if targets['singularity'] else None,
                inspiration_cells=[_position(key) for key in targets['inspiration']],
                native_board=board)
    move_distance = (board.get('turn') or {}).get('move_distance')
    if type(move_distance) is int:
        layout['move_distance'] = move_distance
    return layout


def controls_from_board(data):
    """Map the native actor, destinations and adjacent faces to solver actions."""
    cube_size = validate_cube_size(data['size'])

    def slot(row):
        return cell_to_slot(_coordinate(row['face'], row['row'], row['col'], cube_size), cube_size=cube_size)

    def point(control):
        x, y = control['point']
        if type(x) is not int or type(y) is not int or not (0 <= x < data['width'] and 0 <= y < data['height']):
            raise ValueError('invalid_native_control_point')
        return [x, y]

    actor = slot(data['player'])
    move_distance = data['turn'].get('move_distance', 1)
    geo = geometry(cube_size=cube_size, move_distance=move_distance)
    by_face = {coord_dict(geo.rotations[rotation, actor], cube_size=cube_size)['face']: int(rotation)
               for rotation in geo.actor_rotations[actor]}

    def rotation_for(face):
        if type(face) is not int or not 1 <= face <= 6 or NATIVE_FACES[face - 1] not in by_face:
            raise ValueError('invalid_native_rotation_face')
        return by_face[NATIVE_FACES[face - 1]]

    moves = [dict(destination_slot=slot(row), native_cell={k: row[k] for k in ('face', 'row', 'col')},
                  point=point(row)) for row in data['moves']]
    rotations = [dict(rotation_dict(rotation_for(row['next_face']), actor, cube_size=cube_size), name=row['name'],
                      next_face=row['next_face'], point=point(row)) for row in data['rotations']]
    if (len({row['destination_slot'] for row in moves}) != len(moves)
            or len({row['rotation_id'] for row in rotations}) != len(rotations)):
        raise ValueError('ambiguous_native_controls')
    preview = rotation_for(data['preview_face']) if 'preview_face' in data else None
    selected = slot(data['selected']) if 'selected' in data else None
    return dict(status='ready', source='bridge', size=cube_size, move_distance=move_distance,
                actor_slot=actor, frame=data['frame'],
                turn=data['turn'], moves=moves, rotations=rotations, preview_rotation_id=preview, selected_slot=selected)


def selected_native_destination(controls, action):
    return (controls is not None and controls['actor_slot'] == action['expected_actor_slot']
            and controls['selected_slot'] == action['destination_slot'])


def native_operation_scene(observation, controls):
    """Recognize an open mode even when a projected arrow misses the visual ROI."""
    if (not controls or not observation.get('valid') or not observation.get('player_turn')
            or observation.get('scene') not in {'board', 'choose_move', 'choose_rotate'}):
        return observation
    if controls['rotations']:
        return dict(observation, scene='choose_rotate')
    if controls['moves']:
        return dict(observation, scene='choose_move')
    return observation


def bind_native_control(controls, action):
    if not controls or controls['actor_slot'] != action['expected_actor_slot']:
        return dict(status='waiting', reason='native_actor_unconfirmed')
    if controls['preview_rotation_id'] is not None:
        return dict(status='waiting', reason='native_preview_still_pending')
    if action['kind'] == 'move':
        geo = geometry(cube_size=controls.get('size', 3), move_distance=controls.get('move_distance', 1))
        neighbors = geo.moves[controls['actor_slot']]
        if any(row['destination_slot'] not in neighbors for row in controls['moves']):
            return dict(status='blocked', reason='unmodelled_move_candidates')
        rows, key = controls['moves'], 'destination_slot'
    else:
        rows, key = controls['rotations'], 'rotation_id'
    matches = [row for row in rows if row[key] == action[key]]
    if len(matches) != 1:
        return dict(status='waiting', reason='native_target_not_visible')
    return dict(matches[0], status='ready', source='bridge')


def verify_native_preview(controls, action):
    if not controls or controls['actor_slot'] != action['expected_actor_slot']:
        return dict(status='unknown', reason='native_actor_unconfirmed')
    observed = controls['preview_rotation_id']
    if observed is None:
        return dict(status='unknown', reason='native_preview_unconfirmed')
    return dict(status='matched' if observed == action['rotation_id'] else 'mismatch',
                reason='native_pending_rotation', observed_rotation_id=observed, frame=controls['frame'])
