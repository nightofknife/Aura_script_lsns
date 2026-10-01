"""Batched geometry must preserve visibility and ambiguous-target decisions."""
import math

import cv2
import numpy as np

from plans.resonance_pc.src.actions import _deep_dive_layout_vision as layout


def individual_visibility(scanner):
    camera=-(scanner.rotation.T@scanner.tvec).ravel()
    rows=[]
    for index,cell in enumerate(scanner.cells):
        delta=camera-layout._point(cell)
        cosine=float(np.array(layout.BASES[cell['face']][0])@delta/np.linalg.norm(delta))
        if cosine<.12:continue
        quad=scanner.project(layout._quad(cell))
        area=abs(cv2.contourArea(np.float32(quad)))
        if area<230 or not np.isfinite(quad).all():continue
        rows.append(dict(index=index,quad=quad,area=area,cosine=cosine,
                         centre=scanner.project([layout._point(cell)])[0]))
    return rows


def individual_association(scanner,targets,visible):
    assigned={};uncertain=set()
    for target in targets:
        ranks=[]
        for item in visible:
            cell=scanner.cells[item['index']]
            normal=np.array(layout.BASES[cell['face']][0],float)
            corridor=scanner.project([layout._point(cell)+normal*h
                                     for h in np.linspace(*layout._anchor_heights(target),16)])
            error=float(np.min(np.linalg.norm(corridor-np.array(target['point']),axis=1)))/max(20.,math.sqrt(item['area']))
            ranks.append((error,item['index']))
        ranks.sort()
        if not ranks:continue
        uncertain.update(i for error,i in ranks if error<.5)
        margin=ranks[1][0]-ranks[0][0] if len(ranks)>1 else 1.
        if ranks[0][0]<.30 and margin>.12 and target['confidence']>=.6 and target.get('confirmable',True):
            index=ranks[0][1]
            entry=dict(target,association_confidence=max(.55,1-ranks[0][0]),cell_index=index)
            assigned[index]=({'kind':'unknown','confidence':0.,'association_confidence':0.}
                             if index in assigned and assigned[index]['kind']!=entry['kind'] else entry)
    return assigned,uncertain


def test_projection_and_target_candidates_match_individual_geometry(monkeypatch):
    scanner=layout.LayoutScanner();rng=np.random.default_rng(1021)
    # Isolate batched candidate geometry from the new semantic proof gate.
    # Current-frame/full-cube vetoes have their own independent regressions.
    monkeypatch.setattr(scanner, '_guard_entity_associations',
                        lambda assigned, uncertain: (assigned, uncertain))
    for _ in range(30):
        scanner.rotation=cv2.Rodrigues(rng.normal(size=3)*2.)[0]
        scanner.rvec=cv2.Rodrigues(scanner.rotation)[0]
        scanner.tvec=layout.SEED_T+rng.normal(size=(3,1))*.1
        expected=individual_visibility(scanner);actual=scanner.visible()
        assert [r['index'] for r in actual]==[r['index'] for r in expected]
        for a,b in zip(actual,expected):
            assert np.array_equal(a['quad'],b['quad'])
            assert np.array_equal(a['centre'],b['centre'])
            assert abs(a['cosine']-b['cosine'])<1e-15
            assert a['area']==b['area']
        targets=[]
        for item in actual[::4]:
            for kind,height in [('player',.5),('singularity',.6),('inspiration',.3)]:
                cell=scanner.cells[item['index']]
                normal=np.array(layout.BASES[cell['face']][0])
                point=scanner.project([layout._point(cell)+normal*height])[0]
                targets.append(dict(kind=kind,point=point.tolist(),confidence=.9))
        old,old_uncertain=individual_association(scanner,targets,expected)
        new,new_uncertain=scanner._associate_targets(targets,actual)
        assert new_uncertain==old_uncertain
        assert new.keys()==old.keys()
        for index in new:
            assert new[index]['kind']==old[index]['kind']
            assert new[index].get('cell_index')==old[index].get('cell_index')
            assert abs(new[index]['association_confidence']-old[index]['association_confidence'])<1e-14


def test_no_visible_cells_does_not_assign_targets():
    scanner=layout.LayoutScanner()
    assert scanner._associate_targets([dict(kind='player',point=[640,360])],[])==({},set())
