import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QSpinBox
from packages.resonance_gui.widgets.compact_parameter_grid import CompactParameterGrid


def test_compact_grid_falls_back_to_one_column_and_preserves_editor_instances():
    app = QApplication.instance() or QApplication([])
    grid = CompactParameterGrid()
    fields = [QSpinBox() for _ in range(4)]
    for index, field in enumerate(fields):
        field.setValue(index + 7)
        grid.add_field(f"参数 {index}", field)
    grid.show()
    for width, columns in [(520, 1), (800, 2), (1300, 3), (520, 1)]:
        grid.resize(width, 400)
        app.processEvents()
        assert grid.column_count == columns
        assert [field.value() for field in fields] == [7, 8, 9, 10]
    grid.set_field_visible(fields[1], False)
    app.processEvents()
    assert fields[1].parentWidget().isHidden()
    grid.set_field_visible(fields[1], True)
    app.processEvents()
    assert fields[1].isVisible()
    assert fields[1].value() == 8
    grid.close()
