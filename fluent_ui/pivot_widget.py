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
        # stretch=1: the stacked monitor area takes all height the pivot
        # bar doesn't need, so this widget fills its splitter pane instead
        # of collapsing to the tab strip's height.
        self.vbox.addWidget(self.stacked_widget, 1)
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
        # None 守卫而不是 cast：QStackedWidget.widget() 的存根标注随 PySide6
        # 版本变化 —— 声明版本 6.8.2.1（vnpy/pyproject.toml:27）标非 Optional，
        # 6.11 起标 QWidget | None。cast 在前者被判冗余、在后者是必需，
        # 两个环境诊断相反；守卫在两边都成立且都不冗余。
        #
        # 这不只是为了让检查器闭嘴：C++ 侧 QStackedWidget::widget() 对越界索引
        # 返回 nullptr，6.8.2.1 的存根只是没写出来。守卫比 cast 更贴近运行时
        # 真相 —— cast 遇到 None 会在下一行抛 AttributeError 打断事件循环。
        widget = self.stacked_widget.widget(index)
        if widget is None:      # 索引越界；正常路径不会走到
            return
        self.pivot.setCurrentItem(widget.objectName())
