"""
Fluent-native replacement for vnpy_chartwizard's ChartWizardWidget — same
ChartWizardEngine (reused unmodified: it queries local-DB/gateway history
correctly, the bug was in the widget) and same BarGenerator-driven live
tick->bar aggregation, with one real bug fixed and one UX gap closed:

1. process_tick_event() (and process_spread_event(), which we drop — see
   below) did `bar = copy(bg.bar); bar.datetime = bar.datetime.replace(...)`
   with no None check. BarGenerator.update_tick() (vnpy/trader/utility.py)
   returns early without ever setting self.bar when a tick's last_price is
   falsy (`if not tick.last_price: return`) — the very first tick after
   subscribing can easily be one of these (e.g. a snapshot/quote-only push
   before the first real trade tick), so `bg.bar` stays None and
   `copy(None).datetime` throws AttributeError. This is exactly the
   traceback hit live with 700.SEHK. Fixed by skipping the update when
   bg.bar is still None instead of assuming update_tick() always
   populates it.

2. new_chart() silently does nothing if main_engine.get_contract(vt_symbol)
   returns None (unknown symbol, or gateway not connected yet — no
   contracts loaded) — no feedback, looks like a broken button. Now shows
   a warning explaining why.

Spread-trading support (process_spread_event, the vnpy_spreadtrading
import) is dropped entirely — this project never adds SpreadTradingApp,
so EVENT_SPREAD_DATA never fires; keeping the import just for symmetry
with the stock widget would add a dependency for genuinely dead code.
"""

from __future__ import annotations

from copy import copy
from datetime import datetime, timedelta

from qfluentwidgets import LineEdit, PushButton
from tzlocal import get_localzone_name

from vnpy.chart import CandleItem, ChartWidget, VolumeItem
from vnpy.event import Event, EventEngine
from vnpy.trader.constant import Interval
from vnpy.trader.engine import MainEngine
from vnpy.trader.event import EVENT_TICK
from vnpy.trader.locale import _
from vnpy.trader.object import BarData, ContractData, SubscribeRequest, TickData
from vnpy.trader.ui import QtCore, QtWidgets
from vnpy.trader.utility import BarGenerator, ZoneInfo
from vnpy_chartwizard.engine import APP_NAME, EVENT_CHART_HISTORY, ChartWizardEngine


class ChartWizardWidget(QtWidgets.QWidget):
    signal_tick: QtCore.Signal = QtCore.Signal(Event)
    signal_history: QtCore.Signal = QtCore.Signal(Event)

    def __init__(self, main_engine: MainEngine, event_engine: EventEngine) -> None:
        super().__init__()

        self.main_engine = main_engine
        self.event_engine = event_engine
        self.chart_engine: ChartWizardEngine = main_engine.get_engine(APP_NAME)

        self.bgs: dict[str, BarGenerator] = {}
        self.charts: dict[str, ChartWidget] = {}

        self.init_ui()
        self.register_event()

    def init_ui(self) -> None:
        self.setWindowTitle(_("K线图表"))

        self.tab = QtWidgets.QTabWidget()
        self.tab.setTabsClosable(True)
        self.tab.tabCloseRequested.connect(self.close_tab)

        self.symbol_line = LineEdit()
        self.symbol_line.setPlaceholderText(_("例如 700.SEHK"))

        self.button = PushButton(_("新建图表"))
        self.button.clicked.connect(self.new_chart)

        hbox = QtWidgets.QHBoxLayout()
        hbox.addWidget(QtWidgets.QLabel(_("本地代码")))
        hbox.addWidget(self.symbol_line)
        hbox.addWidget(self.button)
        hbox.addStretch()

        vbox = QtWidgets.QVBoxLayout()
        vbox.addLayout(hbox)
        vbox.addWidget(self.tab)
        self.setLayout(vbox)

    def create_chart(self) -> ChartWidget:
        chart = ChartWidget()
        chart.add_plot("candle", hide_x_axis=True)
        chart.add_plot("volume", maximum_height=200)
        chart.add_item(CandleItem, "candle", "candle")
        chart.add_item(VolumeItem, "volume", "volume")
        chart.add_cursor()
        return chart

    def close_tab(self, index: int) -> None:
        vt_symbol = self.tab.tabText(index)
        self.tab.removeTab(index)
        self.charts.pop(vt_symbol, None)
        self.bgs.pop(vt_symbol, None)

    def new_chart(self) -> None:
        vt_symbol = self.symbol_line.text()
        if not vt_symbol:
            return

        if vt_symbol in self.charts:
            return

        if "LOCAL" not in vt_symbol:
            contract: ContractData | None = self.main_engine.get_contract(vt_symbol)
            if not contract:
                QtWidgets.QMessageBox.warning(
                    self,
                    _("找不到合约"),
                    f"{vt_symbol}: 本地没有这个合约记录——先连接对应网关(合约查询会在连接成功后自动填充),再重试。",
                )
                return

        self.bgs[vt_symbol] = BarGenerator(self.on_bar)

        chart = self.create_chart()
        self.charts[vt_symbol] = chart
        self.tab.addTab(chart, vt_symbol)

        end = datetime.now(ZoneInfo(get_localzone_name()))
        start = end - timedelta(days=5)
        self.chart_engine.query_history(vt_symbol, Interval.MINUTE, start, end)

    def register_event(self) -> None:
        self.signal_tick.connect(self.process_tick_event)
        self.signal_history.connect(self.process_history_event)

        self.event_engine.register(EVENT_CHART_HISTORY, self.signal_history.emit)
        self.event_engine.register(EVENT_TICK, self.signal_tick.emit)

    def process_tick_event(self, event: Event) -> None:
        tick: TickData = event.data
        bg = self.bgs.get(tick.vt_symbol)
        if bg is None:
            return

        bg.update_tick(tick)
        if bg.bar is None:
            # See module docstring point 1 — update_tick() silently
            # no-ops on a zero/falsy last_price tick, most commonly the
            # very first tick right after subscribing.
            return

        chart = self.charts[tick.vt_symbol]
        bar: BarData = copy(bg.bar)
        bar.datetime = bar.datetime.replace(second=0, microsecond=0)
        chart.update_bar(bar)

    def process_history_event(self, event: Event) -> None:
        history: list[BarData] = event.data
        if not history:
            return

        bar = history[0]
        chart = self.charts.get(bar.vt_symbol)
        if chart is None:
            return
        chart.update_history(history)

        contract: ContractData | None = self.main_engine.get_contract(bar.vt_symbol)
        if contract:
            req = SubscribeRequest(contract.symbol, contract.exchange)
            self.main_engine.subscribe(req, contract.gateway_name)

    def on_bar(self, bar: BarData) -> None:
        chart = self.charts.get(bar.vt_symbol)
        if chart is not None:
            chart.update_bar(bar)
