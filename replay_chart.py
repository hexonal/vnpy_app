"""
Multi-period playback/复盘 chart tool.

Same idea as the vnpy forum post "利用vnpy图表实现多周期复盘的代码"
(https://www.vnpy.com/forum/topic/30419-...): replay a stored 1-minute bar
series bar-by-bar while multiple synchronized panes at different periods
(1m/5m/30m/4h) build their candles live, so you can watch how a session
"actually happened" across timeframes instead of just looking at the
finished chart.

Deliberately NOT the forum post's multi-process/queue architecture — that
was a workaround for keeping many separate OS-level chart windows painting
smoothly; four ChartWidget panes inside one QSplitter in a single process
is simpler, has no IPC to debug, and vnpy.chart's PlotItem.setDownsampling
already keeps single-process repaints cheap at this scale. If four panes
ever isn't enough (e.g. tick-level replay, dozens of panes), revisit with
the multi-process approach.

Data source: reads 1-minute bars straight out of vnpy's local SQLite
database (vnpy.trader.database.get_database()) — the same DB that
vnpy_datamanager's "下载" button (backed by vnpy_futu's query_history, see
vnpy_futu/README.md) populates. Download the 1m history for a symbol in
DataManager first; this tool only reads, it never calls out to Futu/OpenD
itself.
"""

from __future__ import annotations

import os

os.environ.setdefault("LANGUAGE", "zh_CN")

from datetime import datetime

from vnpy.chart import CandleItem, ChartWidget, VolumeItem
from vnpy.trader.constant import Exchange, Interval
from vnpy.trader.database import get_database
from vnpy.trader.object import BarData
from vnpy.trader.ui import QtCore, QtWidgets, create_qapp

# (label, minutes-per-bar) — 1m is always the base/driving series.
PERIODS: list[tuple[str, int]] = [
    ("1分钟", 1),
    ("5分钟", 5),
    ("30分钟", 30),
    ("4小时", 240),
]

SUPPORTED_EXCHANGES = [Exchange.SEHK, Exchange.SMART, Exchange.SSE, Exchange.SZSE]


class PeriodAggregator:
    """
    Accumulates a stream of 1-minute bars into the in-progress BarData for
    one coarser period. update() returns the SAME BarData object (mutated
    in place) while a period is still forming, and a fresh one once a new
    period starts — ChartWidget.update_bar() keys off bar.datetime, so
    repeated calls with an unchanged datetime redraw the candle in place
    (see vnpy/chart/manager.py BarManager.update_bar), which is what makes
    the coarser panes visibly "grow" bar by bar instead of only appearing
    once complete.

    Period boundaries are floored on the bar's own stored wall-clock
    datetime (e.g. 30m buckets at :00/:30, 4h buckets at 00/04/08...) —
    not exchange-session-aligned. Good enough for visual replay; do not
    treat bucket edges as authoritative session boundaries.
    """

    def __init__(self, interval: Interval, minutes: int, gateway_name: str) -> None:
        self.minutes = minutes
        self.interval = interval
        self.gateway_name = gateway_name
        self.bar: BarData | None = None

    def update(self, bar_1m: BarData) -> BarData:
        period_start = self._period_start(bar_1m.datetime)

        if self.bar is None or self.bar.datetime != period_start:
            self.bar = BarData(
                symbol=bar_1m.symbol,
                exchange=bar_1m.exchange,
                datetime=period_start,
                interval=self.interval,
                open_price=bar_1m.open_price,
                high_price=bar_1m.high_price,
                low_price=bar_1m.low_price,
                close_price=bar_1m.close_price,
                volume=bar_1m.volume,
                turnover=bar_1m.turnover,
                gateway_name=self.gateway_name,
            )
        else:
            self.bar.high_price = max(self.bar.high_price, bar_1m.high_price)
            self.bar.low_price = min(self.bar.low_price, bar_1m.low_price)
            self.bar.close_price = bar_1m.close_price
            self.bar.volume += bar_1m.volume
            self.bar.turnover += bar_1m.turnover

        return self.bar

    def _period_start(self, dt: datetime) -> datetime:
        if self.minutes < 60:
            floor_minute = (dt.minute // self.minutes) * self.minutes
            return dt.replace(minute=floor_minute, second=0, microsecond=0)

        hours = self.minutes // 60
        floor_hour = (dt.hour // hours) * hours
        return dt.replace(hour=floor_hour, minute=0, second=0, microsecond=0)


class ReplayChartWidget(QtWidgets.QWidget):
    def __init__(self) -> None:
        super().__init__()

        self.database = get_database()
        self.bars: list[BarData] = []
        self.cursor: int = 0
        self.aggregators: dict[str, PeriodAggregator] = {}
        self.panes: dict[str, ChartWidget] = {}

        self.timer = QtCore.QTimer()
        self.timer.timeout.connect(self._step)

        self._init_ui()

    def _init_ui(self) -> None:
        self.setWindowTitle("多周期复盘")
        self.resize(1400, 900)

        self.symbol_edit = QtWidgets.QLineEdit()
        self.symbol_edit.setPlaceholderText("例如 700")

        self.exchange_combo = QtWidgets.QComboBox()
        for exchange in SUPPORTED_EXCHANGES:
            self.exchange_combo.addItem(exchange.value, exchange)

        today = datetime.now()
        self.start_edit = QtWidgets.QDateEdit(QtCore.QDate(today.year - 1, today.month, today.day))
        self.start_edit.setCalendarPopup(True)
        self.end_edit = QtWidgets.QDateEdit(QtCore.QDate(today.year, today.month, today.day))
        self.end_edit.setCalendarPopup(True)

        self.load_button = QtWidgets.QPushButton("加载")
        self.load_button.clicked.connect(self._load)

        self.play_button = QtWidgets.QPushButton("播放")
        self.play_button.clicked.connect(self._toggle_play)
        self.play_button.setEnabled(False)

        self.speed_spin = QtWidgets.QSpinBox()
        self.speed_spin.setRange(1, 200)
        self.speed_spin.setValue(1)
        self.speed_spin.setSuffix(" 根/tick")

        self.status_label = QtWidgets.QLabel("未加载")

        top_bar = QtWidgets.QHBoxLayout()
        top_bar.addWidget(QtWidgets.QLabel("代码"))
        top_bar.addWidget(self.symbol_edit)
        top_bar.addWidget(QtWidgets.QLabel("交易所"))
        top_bar.addWidget(self.exchange_combo)
        top_bar.addWidget(QtWidgets.QLabel("开始"))
        top_bar.addWidget(self.start_edit)
        top_bar.addWidget(QtWidgets.QLabel("结束"))
        top_bar.addWidget(self.end_edit)
        top_bar.addWidget(self.load_button)
        top_bar.addWidget(self.play_button)
        top_bar.addWidget(self.speed_spin)
        top_bar.addWidget(self.status_label)
        top_bar.addStretch()

        splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical)
        for label, minutes in PERIODS:
            pane = ChartWidget()
            if minutes == 1:
                pane.add_plot("candle", hide_x_axis=True)
                pane.add_plot("volume", maximum_height=150)
                pane.add_item(CandleItem, "candle", "candle")
                pane.add_item(VolumeItem, "volume", "volume")
            else:
                pane.add_plot("candle")
                pane.add_item(CandleItem, "candle", "candle")
            pane.add_cursor()

            container = QtWidgets.QWidget()
            v = QtWidgets.QVBoxLayout(container)
            v.setContentsMargins(0, 0, 0, 0)
            v.addWidget(QtWidgets.QLabel(label))
            v.addWidget(pane)
            splitter.addWidget(container)

            self.panes[label] = pane

        layout = QtWidgets.QVBoxLayout()
        layout.addLayout(top_bar)
        layout.addWidget(splitter)
        self.setLayout(layout)

    def _load(self) -> None:
        self.timer.stop()
        self.play_button.setText("播放")

        symbol = self.symbol_edit.text().strip()
        if not symbol:
            QtWidgets.QMessageBox.warning(self, "提示", "请输入代码")
            return

        exchange: Exchange = self.exchange_combo.currentData()
        start = datetime(
            self.start_edit.date().year(), self.start_edit.date().month(), self.start_edit.date().day()
        )
        end = datetime(
            self.end_edit.date().year(), self.end_edit.date().month(), self.end_edit.date().day()
        )

        self.bars = self.database.load_bar_data(
            symbol, exchange, interval=Interval.MINUTE, start=start, end=end
        )

        if not self.bars:
            QtWidgets.QMessageBox.warning(
                self,
                "没有数据",
                f"本地数据库没有 {symbol}.{exchange.value} 这段时间的 1 分钟数据——"
                f"先在数据管理里下载 MINUTE 周期的历史数据,再来复盘。",
            )
            self.play_button.setEnabled(False)
            return

        self.cursor = 0
        self.aggregators = {
            label: PeriodAggregator(Interval.MINUTE if minutes == 1 else Interval.HOUR, minutes, "REPLAY")
            for label, minutes in PERIODS
        }
        # clear_all() resets each pane's BarManager and tells its existing
        # CandleItem/VolumeItem to redraw empty — items stay registered, so
        # re-adding them here would stack duplicate items on the same plot.
        for pane in self.panes.values():
            pane.clear_all()

        self.play_button.setEnabled(True)
        self._update_status()

    def _toggle_play(self) -> None:
        if self.timer.isActive():
            self.timer.stop()
            self.play_button.setText("播放")
        else:
            self.timer.start(100)
            self.play_button.setText("暂停")

    def _step(self) -> None:
        for _ in range(self.speed_spin.value()):
            if self.cursor >= len(self.bars):
                self.timer.stop()
                self.play_button.setText("播放")
                self.status_label.setText(f"复盘结束,共 {len(self.bars)} 根 1 分钟K线")
                return

            bar_1m = self.bars[self.cursor]
            self.cursor += 1

            for label, minutes in PERIODS:
                pane = self.panes[label]
                if minutes == 1:
                    pane.update_bar(bar_1m)
                else:
                    aggregated = self.aggregators[label].update(bar_1m)
                    pane.update_bar(aggregated)

        self._update_status()

    def _update_status(self) -> None:
        self.status_label.setText(f"第 {self.cursor} / 共 {len(self.bars)} 根")


def main() -> None:
    qapp = create_qapp()
    widget = ReplayChartWidget()
    widget.show()
    qapp.exec()


if __name__ == "__main__":
    main()
