"""Optional passenger parameters attached to an unchanged freight route."""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QWidget, QVBoxLayout, QFormLayout, QLabel, QSpinBox, QDoubleSpinBox, QComboBox

from packages.resonance_shared.passenger_parameters import (
    DEFAULT_PASSENGER_OPTIONS, normalize_passenger_options,
)
from .toggle_button import ToggleButton


class FreightPassengerPanel(QWidget):
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._restoring = False
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.enabled = ToggleButton("附加客运", self)
        self.enabled.setToolTip("仅不定线路使用；沿货运路线整批上下客，允许跨站。达到传单目标后停止新揽客，最后一车允许超出。")
        outer.addWidget(self.enabled)
        self.notice = QLabel("固定线路暂不支持附加客运。", self)
        self.notice.setProperty("caption", True)
        outer.addWidget(self.notice)
        self.fields = QWidget(self)
        form = QFormLayout(self.fields)
        form.setContentsMargins(0, 0, 0, 0)
        self.flyer_target = self._spin(0, 2147483647)
        self.flyer_target.setSpecialValueText("自动按客运等级计算")
        self.transport_level = self._spin(1, 100)
        self.seat_capacity = self._spin(0, 100000)
        self.seat_capacity.setSpecialValueText("请填写实际座位数")
        self.seat_level = self._spin(1, 5)
        self.furniture_bonus_bps = self._percent(10000)
        self.furniture_bonus_bps.setToolTip("有效平均家具加成；不计角色或车皮，默认0%。")
        self.cleanliness_bps = self._percent(100)
        self.cleanliness_bps.setToolTip("仅用于估算，低于30%游戏会拒绝揽客。")
        for label, field in (("传单消耗目标", self.flyer_target), ("客运等级", self.transport_level),
                             ("实际座位数（必填）", self.seat_capacity), ("硬座等级", self.seat_level),
                             ("家具收益加成", self.furniture_bonus_bps), ("清洁度", self.cleanliness_bps)):
            form.addRow(label, field)
        self.scores = {}
        for key, label in (("comfort", "舒适度"), ("foodScores", "美味评分"), ("playScores", "娱乐评分"),
                           ("petScores", "宠物评分"), ("fishScores", "水族评分"),
                           ("plantScores", "绿植评分"), ("medicalScores", "医疗评分")):
            field = self._spin(0, 2147483647)
            self.scores[key] = field
            form.addRow(label, field)
        self.news = QComboBox(self)
        self.news.addItem("无（首版）", "none")
        self.news.setEnabled(False)
        form.addRow("当周新闻", self.news)
        hint = QLabel("参考估算：每10张传单一次招募、人数区间中点；不代表服务端精确公式。实际一键最大派发，按前后库存差记账。", self)
        hint.setWordWrap(True)
        hint.setProperty("caption", True)
        form.addRow(hint)
        outer.addWidget(self.fields)
        self.enabled.toggled.connect(self._changed)
        for field in (self.flyer_target, self.transport_level, self.seat_capacity, self.seat_level,
                      self.furniture_bonus_bps, self.cleanliness_bps, *self.scores.values()):
            field.valueChanged.connect(self._changed)
        self.set_options(False, DEFAULT_PASSENGER_OPTIONS)
        self.set_available(True)

    def _spin(self, minimum, maximum):
        field = QSpinBox(self)
        field.setRange(minimum, maximum)
        return field

    def _percent(self, maximum):
        field = QDoubleSpinBox(self)
        field.setRange(0, maximum)
        field.setDecimals(2)
        field.setSuffix(" %")
        return field

    def _changed(self, *_):
        self.fields.setVisible(self.enabled.isChecked())
        if not self._restoring:
            self.changed.emit()

    def set_available(self, available: bool, *, busy: bool = False):
        self.enabled.setEnabled(available and not busy)
        self.notice.setVisible(not available)
        self.fields.setVisible(self.enabled.isChecked())
        self.fields.setEnabled(available and self.enabled.isChecked() and not busy)

    def set_options(self, enabled: bool, options: Mapping[str, Any] | None):
        values = normalize_passenger_options(deepcopy(dict(options or {})), enabled=False)
        self._restoring = True
        try:
            self.enabled.setChecked(bool(enabled))
            for key in ("flyer_target", "transport_level", "seat_capacity", "seat_level", "furniture_bonus_bps", "cleanliness_bps"):
                getattr(self, key).setValue((values[key] or 0) / 100 if key.endswith("_bps") else (values[key] or 0))
            for key, field in self.scores.items():
                field.setValue(values["scores"][key])
        finally:
            self._restoring = False
        self.fields.setVisible(self.enabled.isChecked())

    def collect_options(self, *, validate: bool = True) -> dict[str, Any]:
        values = {key: getattr(self, key).value() for key in
                  ("flyer_target", "transport_level", "seat_capacity", "seat_level", "furniture_bonus_bps", "cleanliness_bps")}
        for key in ("furniture_bonus_bps", "cleanliness_bps"):
            values[key] = round(values[key] * 100)
        values["seat_capacity"] = values["seat_capacity"] or None
        values.update(scores={key: field.value() for key, field in self.scores.items()}, news="none")
        if validate and values["seat_capacity"] is None:
            raise ValueError("请填写附加客运的实际座位数。")
        return normalize_passenger_options(values, enabled=validate)
