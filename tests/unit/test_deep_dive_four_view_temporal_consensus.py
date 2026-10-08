from research.deep_dive_four_view_reading_check import temporal_consensus


def report(value,expected='orange_triple_eye'):
    return dict(records=[dict(face='panel',row=0,col=0,
        result=dict(reading=dict(icon_id=value)),expected=expected,scored=True)])


def test_two_confirmed_real_frames_survive_one_unknown():
    result=temporal_consensus([report('orange_triple_eye'),report('orange_triple_eye'),report(None)])
    assert result['passed'] and result['correct']==1


def test_conflicting_confirmed_class_is_never_ignored():
    result=temporal_consensus([report('orange_triple_eye'),report('orange_triple_eye'),report('red_single_eye')])
    assert not result['passed'] and result['unknown']==1


def test_one_confirmed_reading_is_insufficient():
    assert not temporal_consensus([report(None),report('orange_triple_eye'),report(None)])['passed']


def test_truth_does_not_select_class():
    result=temporal_consensus([report('red_single_eye'),report('red_single_eye'),report(None)])
    assert result['records'][0]['reading']['icon_id']=='red_single_eye'
    assert result['wrong']==1 and not result['passed']
