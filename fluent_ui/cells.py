"""
Table cell classes for the Fluent monitors — copied verbatim from
vnpy.trader.ui.widget (they're plain QTableWidgetItem subclasses, nothing
Fluent-specific about them; they render fine inside qfluentwidgets'
TableWidget unchanged, since TableWidget IS a QTableWidget subclass).
Kept as a straight copy rather than importing from vnpy.trader.ui.widget so
this package has zero coupling to the fork's own widget.py — matches the
"new functionality lives in its own package" pattern used throughout
vnpy_app/vnpy_futu/vnpy_agentbridge.
"""

from __future__ import annotations

from tzlocal import get_localzone_name

from vnpy.trader.constant import Direction
from vnpy.trader.ui import QtCore, QtGui, QtWidgets
from vnpy.trader.utility import ZoneInfo

COLOR_LONG = QtGui.QColor("red")
COLOR_SHORT = QtGui.QColor("green")
COLOR_BID = QtGui.QColor(255, 174, 201)
COLOR_ASK = QtGui.QColor(160, 255, 160)
COLOR_BLACK = QtGui.QColor("black")


class BaseCell(QtWidgets.QTableWidgetItem):
    def __init__(self, content: object, data: object) -> None:
        super().__init__()
        self.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.set_content(content, data)

    def set_content(self, content: object, data: object) -> None:
        self.setText(str(content))
        self._data = data

    def get_data(self) -> object:
        return self._data


class EnumCell(BaseCell):
    def set_content(self, content: object, data: object) -> None:
        if content:
            super().set_content(content.value, data)


class DirectionCell(EnumCell):
    def set_content(self, content: object, data: object) -> None:
        super().set_content(content, data)
        if content is Direction.SHORT:
            self.setForeground(COLOR_SHORT)
        else:
            self.setForeground(COLOR_LONG)


class BidCell(BaseCell):
    def __init__(self, content: object, data: object) -> None:
        super().__init__(content, data)
        self.setForeground(COLOR_BID)


class AskCell(BaseCell):
    def __init__(self, content: object, data: object) -> None:
        super().__init__(content, data)
        self.setForeground(COLOR_ASK)


class PnlCell(BaseCell):
    def set_content(self, content: object, data: object) -> None:
        super().set_content(content, data)
        if str(content).startswith("-"):
            self.setForeground(COLOR_SHORT)
        else:
            self.setForeground(COLOR_LONG)


class TimeCell(BaseCell):
    local_tz = ZoneInfo(get_localzone_name())

    def set_content(self, content: object, data: object) -> None:
        if content is None:
            return

        content = content.astimezone(self.local_tz)
        timestamp: str = content.strftime("%H:%M:%S")

        millisecond: int = int(content.microsecond / 1000)
        timestamp = f"{timestamp}.{millisecond}" if millisecond else f"{timestamp}.000"

        self.setText(timestamp)
        self._data = data


class DateCell(BaseCell):
    def set_content(self, content: object, data: object) -> None:
        if content is None:
            return
        self.setText(content.strftime("%Y-%m-%d"))
        self._data = data


class MsgCell(BaseCell):
    def __init__(self, content: str, data: object) -> None:
        super().__init__(content, data)
        self.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter)
