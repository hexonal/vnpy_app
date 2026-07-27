"""A CandleItem that draws extended-hours (夜盘: 盘前/盘后) bars in muted
colors so they're visually distinct from the vivid regular-session candles,
and reports the session in the cursor legend.

Kept in the app layer (not the vnpy fork) because the session boundaries are
market-specific (see market_session) — the fork's CandleItem stays generic.
The muted palette keeps the up/down direction readable (dim red / dim green,
same hollow-up / filled-down convention as the regular candles) while making
it obvious at a glance which bars are off-hours; a shaded background band
(added by the chart widget) plus this coloring plus the cursor 时段 line give
three independent, unmistakable markings of the night session.
"""

from __future__ import annotations

import pyqtgraph as pg
from vnpy.chart.base import BAR_WIDTH, PEN_WIDTH
from vnpy.chart.item import CandleItem
from vnpy.chart.manager import BarManager
from vnpy.trader.object import BarData
from vnpy.trader.ui import QtCore, QtGui

from .market_session import is_extended, market_session

# Muted vs the regular vivid red (255,75,75) / green (55,200,120): dimmed so
# a night bar reads as "same direction, off-hours".
_EXT_UP_COLOR = (150, 66, 66)
_EXT_DOWN_COLOR = (45, 120, 82)


class SessionCandleItem(CandleItem):
    """CandleItem that renders 盘前/盘后 bars in a muted palette and appends
    the session to the cursor info text."""

    def __init__(self, manager: BarManager) -> None:
        super().__init__(manager)

        self._ext_up_pen: QtGui.QPen = pg.mkPen(color=_EXT_UP_COLOR, width=PEN_WIDTH)
        self._ext_down_pen: QtGui.QPen = pg.mkPen(color=_EXT_DOWN_COLOR, width=PEN_WIDTH)
        self._ext_down_brush: QtGui.QBrush = pg.mkBrush(color=_EXT_DOWN_COLOR)

    def _draw_bar_picture(self, ix: int, bar: BarData) -> QtGui.QPicture:
        # Regular-session bars: unchanged vivid rendering from the base class.
        if not is_extended(bar):
            return super()._draw_bar_picture(ix, bar)

        # Extended-hours bar: same geometry as the base CandleItem, muted
        # pens/brushes. Up stays hollow (black fill outlined), down filled —
        # mirroring the regular convention so direction is still readable.
        candle_picture: QtGui.QPicture = QtGui.QPicture()
        painter: QtGui.QPainter = QtGui.QPainter(candle_picture)

        if bar.close_price >= bar.open_price:
            painter.setPen(self._ext_up_pen)
            painter.setBrush(self._black_brush)
        else:
            painter.setPen(self._ext_down_pen)
            painter.setBrush(self._ext_down_brush)

        if bar.high_price > bar.low_price:
            painter.drawLine(
                QtCore.QPointF(ix, bar.high_price),
                QtCore.QPointF(ix, bar.low_price),
            )

        if bar.open_price == bar.close_price:
            painter.drawLine(
                QtCore.QPointF(ix - BAR_WIDTH, bar.open_price),
                QtCore.QPointF(ix + BAR_WIDTH, bar.open_price),
            )
        else:
            rect: QtCore.QRectF = QtCore.QRectF(
                ix - BAR_WIDTH,
                bar.open_price,
                BAR_WIDTH * 2,
                bar.close_price - bar.open_price,
            )
            painter.drawRect(rect)

        painter.end()
        return candle_picture

    def get_info_text(self, ix: int) -> str:
        text: str = super().get_info_text(ix)
        bar: BarData | None = self._manager.get_bar(ix)
        if bar:
            text += f"\n\n时段\n{market_session(bar)}"
        return text
