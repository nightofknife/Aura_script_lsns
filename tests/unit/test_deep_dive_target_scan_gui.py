"""Distinguish confirmed target scans from complete node-pattern acceptance."""
import os

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication

from packages.resonance_gui.widgets.consciousness_deep_dive_panel import ConsciousnessDeepDivePanel


@pytest.fixture
def panel():
    app = QApplication.instance() or QApplication(['deep-dive-target-result-test'])
    widget = ConsciousnessDeepDivePanel()
    yield widget
    widget.close()
    widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


def target_summary(**changes):
    return dict(dict(status='targets_ready', success=True, targets_ready=True,
                     recognition_goal='targets', layout_complete=False,
                     known_cells=50, faces_observed=6), **changes)


def test_confirmed_target_scan_is_success_without_claiming_complete_patterns(panel):
    panel.begin_scan()
    panel.apply_scan_result(target_summary())
    assert panel.status_label.property('resultState') == 'success'
    assert '目标识别完成' in panel.status_label.text()
    assert '布局扫描完成' not in panel.status_label.text()
    assert '节点图案未作为完整布局验收' in panel.summary_label.text()


@pytest.mark.parametrize('changes', [dict(success=False), dict(targets_ready=False),
                                    dict(status='partial'), dict(status='blocked'),
                                    dict(recognition_goal='full')])
def test_flags_or_partial_target_summary_never_turn_green(panel, changes):
    panel.apply_scan_result(target_summary(**changes))
    assert panel.status_label.property('resultState') == 'error'
    assert '目标识别完成' not in panel.status_label.text()


def test_complete_legacy_scan_keeps_full_layout_success(panel):
    panel.apply_scan_result(dict(status='completed', success=True, layout_complete=True,
                                known_cells=54, faces_observed=6))
    assert panel.status_label.property('resultState') == 'success'
    assert '布局扫描完成' in panel.status_label.text()


def test_planned_progress_exposes_ordered_view_and_registration_phase(panel):
    panel.begin_planned_run()
    panel.apply_planned_run_progress(dict(payload=dict(phase='scan_turn', scan_progress=dict(view_index=3))))
    assert '视角 3/4' in panel.status_label.text()
    assert '视角 3/4' in panel.summary_label.text()
    panel.apply_planned_run_progress(dict(payload=dict(phase='read_registration_anchors')))
    assert '补读定位图案' in panel.status_label.text()
    assert '视角 3/4' not in panel.status_label.text()
