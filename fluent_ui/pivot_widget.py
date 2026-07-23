"""
Small Pivot(segmented-tab) + QStackedWidget combo, copied from vnpy_evo's
widget.py PivotWidgdet (their spelling, kept as-is for anyone diffing
against upstream evo later) — lets HomeWidget pack several monitors into
one tabbed section instead of each getting its own full nav page.
"""

from __future__ import annotations

from qfluentwidgets import Pivot

from vnpy.trader.ui import QtCore, QtWidgets


class PivotWidgdet(QtWidgets.QWidget):
    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent=parent)

        self.pivot = Pivot(self)
        self.stacked_widget = QtWidgets.QStackedWidget(self)
        self.vbox = QtWidgets.QVBoxLayout(self)

        self.vbox.addWidget(self.pivot, 0, QtCore.Qt.AlignmentFlag.AlignLeft)
        self.vbox.addWidget(self.stacked_widget)
        self.vbox.setContentsMargins(0, 0, 0, 0)

        self.stacked_widget.currentChanged.connect(self.on_current_index_changed)

    def add_widget(self, widget: QtWidgets.QWidget, name: str) -> None:
        widget.setObjectName(name)

        self.stacked_widget.addWidget(widget)

        self.pivot.addItem(
            routeKey=name,
            text=name,
            onClick=lambda: self.stacked_widget.setCurrentWidget(widget),
        )

        if self.stacked_widget.count() == 1:
            self.stacked_widget.setCurrentWidget(widget)
            self.pivot.setCurrentItem(widget.objectName())

    def on_current_index_changed(self, index: int) -> None:
        widget = self.stacked_widget.widget(index)
        self.pivot.setCurrentItem(widget.objectName())
