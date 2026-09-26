import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from types import SimpleNamespace
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from PySide6.QtWidgets import QApplication
from packages.resonance_gui.widgets.consciousness_deep_dive_panel import ConsciousnessDeepDivePanel
from packages.resonance_gui.main_window import ResonanceMainWindow
from packages.resonance_gui.logic import PC_CONSCIOUSNESS_DEEP_DIVE_LOOP_TASK_REF

APP=None


def test_panel_loop_inputs_progress_and_cancel_controls():
    global APP
    APP=QApplication.instance() or QApplication([])
    panel=ConsciousnessDeepDivePanel();sent=[]
    panel.runLoopRequested.connect(sent.append)
    panel.loop_count_spin.setValue(-1);panel.loop_button.click()
    assert sent==[{'loop_count':-1}]
    panel.loop_count_spin.setValue(0);panel.loop_button.click()
    assert len(sent)==1
    panel.begin_loop();panel.set_runner_busy(True)
    assert panel.cancel_button.isEnabled() and not panel.loop_button.isEnabled()
    panel.apply_loop_progress({'payload':{'completed_runs':2,'loop_count':-1,'run_index':3,'stage':'single'}})
    assert '2' in panel.summary_label.text() and '持续循环' in panel.summary_label.text()
    panel.show_loop_error('已取消')
    assert '2' in panel.summary_label.text() and '已取消' in panel.status_label.text()
    panel.close();panel.deleteLater();APP.processEvents()


def test_window_dispatches_loop_without_gui_timeout():
    sent=[];begun=[]
    fake=SimpleNamespace(_busy=False,_workflow_active=False,_commerce_active=False,_small_task_active_ref='',
        small_tasks_page=SimpleNamespace(consciousness_deep_dive_panel=SimpleNamespace(begin_loop=lambda:begun.append(True))),
        requestRunPcTask=SimpleNamespace(emit=lambda *args:sent.append(args)))
    ResonanceMainWindow._run_deep_dive_loop(fake,{'loop_count':-1})
    assert begun==[True]
    assert sent[0][0]==PC_CONSCIOUSNESS_DEEP_DIVE_LOOP_TASK_REF
    assert sent[0][1]=={'loop_count':-1} and sent[0][3]==0.
