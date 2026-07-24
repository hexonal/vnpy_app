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

3. That warning ("本地没有这个合约记录——先连接对应网关...") kept firing even
   with a gateway genuinely connected and its contracts already loaded,
   because "本地代码" was a bare LineEdit — nothing stopped a user from
   typing just the bare symbol ("700") instead of the real vt_symbol
   ("700.SEHK") get_contract() actually looks up by. The placeholder text
   ("例如 700.SEHK") was the only hint, easy to miss/forget once you start
   typing. Now a SearchableComboBox (see searchable_combo_box.py)
   populated straight from main_engine.get_all_contracts() — search by
   typing part of a real symbol, pick the exact vt_symbol, done; free-text
   entry (e.g. a "LOCAL" synthetic feed with no real contract, see
   new_chart()'s own `if "LOCAL" not in vt_symbol` branch below) still
   works exactly as before.

Spread-trading support (process_spread_event, the vnpy_spreadtrading
import) is dropped entirely — this project never adds SpreadTradingApp,
so EVENT_SPREAD_DATA never fires; keeping the import just for symmetry
with the stock widget would add a dependency for genuinely dead code.
"""

from __future__ import annotations

from copy import copy
from datetime import datetime, timedelta

from qfluentwidgets import CalendarPicker, MessageBox, PushButton
from tzlocal import get_localzone_name

from vnpy.chart import CandleItem, ChartWidget, VolumeItem
from vnpy.event import Event, EventEngine
from vnpy.trader.constant import Interval
from vnpy.trader.engine import MainEngine
from vnpy.trader.event import EVENT_CONTRACT, EVENT_TICK
from vnpy.trader.locale import _
from vnpy.trader.object import BarData, ContractData, SubscribeRequest, TickData
from vnpy.trader.ui import QtCore, QtWidgets
from vnpy.trader.utility import BarGenerator, ZoneInfo
from vnpy_chartwizard.engine import APP_NAME, EVENT_CHART_HISTORY, ChartWizardEngine

from .searchable_combo_box import SearchableComboBox


class ChartWizardWidget(QtWidgets.QWidget):
    signal_tick: QtCore.Signal = QtCore.Signal(Event)
    signal_history: QtCore.Signal = QtCore.Signal(Event)
    signal_contract: QtCore.Signal = QtCore.Signal(Event)

    def __init__(self, main_engine: MainEngine, event_engine: EventEngine) -> None:
        super().__init__()

        self.main_engine = main_engine
        self.event_engine = event_engine
        self.chart_engine: ChartWizardEngine = main_engine.get_engine(APP_NAME)

        self.bgs: dict[str, BarGenerator] = {}
        self.charts: dict[str, ChartWidget] = {}
        # O(1) dedupe alongside symbol_line's item list: findText() is a
        # Python-level linear scan (qfluentwidgets combo_box.py:244-250),
        # and _add_symbol_if_new runs once per EVENT_CONTRACT — with a
        # FutuGateway pushing ~22k contracts on connect, findText-based
        # dedupe is Σi ≈ n²/2 ≈ 2×10⁸ comparisons during the burst.
        self._known_symbols: set[str] = set()

        self.init_ui()
        self.register_event()

    def init_ui(self) -> None:
        self.setWindowTitle(_("K线图表"))

        self.tab = QtWidgets.QTabWidget()
        self.tab.setTabsClosable(True)
        self.tab.tabCloseRequested.connect(self.close_tab)

        self.symbol_line = SearchableComboBox()
        self.symbol_line.setPlaceholderText(_("输入代码搜索本地已知合约，或直接输入新代码"))
        # Seeded from whatever main_engine already knows *right now* — but
        # ChartWizardWidget is constructed at app startup (init_widgets(),
        # before the user has connected any gateway), so this is normally
        # empty at construction time. process_contract_event() below is
        # what actually keeps it populated as contracts stream in after a
        # real connect() — this seed loop only matters for the case where
        # the widget somehow gets built after contracts already exist
        # (e.g. a future reconnect-without-rebuilding-the-widget path).
        for contract in self.main_engine.get_all_contracts():
            self._add_symbol_if_new(contract.vt_symbol)

        # K-line period selector — the chart used to be hardcoded to
        # Interval.MINUTE, so "K线无法选择年月日" was really "no interval
        # AND no date-range control at all". Both are added here.
        self.interval_combo = SearchableComboBox()
        for interval in (Interval.MINUTE, Interval.HOUR, Interval.DAILY, Interval.WEEKLY):
            self.interval_combo.addItem(interval.value, userData=interval)

        # Start/end date pickers (CalendarPicker = Fluent-native, click to
        # open a calendar flyout — replaces the previous fixed
        # "last 5 days" window). Default to a 30-day lookback so there's a
        # sensible range pre-filled.
        end_default = datetime.now()
        start_default = end_default - timedelta(days=30)
        self.start_date = CalendarPicker()
        self.start_date.setDate(QtCore.QDate(start_default.year, start_default.month, start_default.day))
        self.end_date = CalendarPicker()
        self.end_date.setDate(QtCore.QDate(end_default.year, end_default.month, end_default.day))

        self.button = PushButton(_("新建图表"))
        self.button.clicked.connect(self.new_chart)

        hbox = QtWidgets.QHBoxLayout()
        hbox.addWidget(QtWidgets.QLabel(_("本地代码")))
        hbox.addWidget(self.symbol_line)
        hbox.addWidget(QtWidgets.QLabel(_("周期")))
        hbox.addWidget(self.interval_combo)
        hbox.addWidget(QtWidgets.QLabel(_("起")))
        hbox.addWidget(self.start_date)
        hbox.addWidget(QtWidgets.QLabel(_("止")))
        hbox.addWidget(self.end_date)
        hbox.addWidget(self.button)
        hbox.addStretch()

        vbox = QtWidgets.QVBoxLayout()
        vbox.addLayout(hbox)
        # stretch=1 so the chart tab area fills the page height instead of
        # sizing to sizeHint (same family as the home-widget layout bug).
        vbox.addWidget(self.tab, 1)
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
                box = MessageBox(
                    _("找不到合约"),
                    f"{vt_symbol}: 本地没有这个合约记录——先连接对应网关(合约查询会在连接成功后自动填充),再重试。",
                    self.window(),
                )
                box.hideCancelButton()
                box.exec()
                return

        interval: Interval = self.interval_combo.currentData() or Interval.MINUTE

        # Read the picked date range; end is inclusive of that whole day.
        tz = ZoneInfo(get_localzone_name())
        sd = self.start_date.getDate()
        ed = self.end_date.getDate()
        start = datetime(sd.year(), sd.month(), sd.day(), tzinfo=tz)
        end = datetime(ed.year(), ed.month(), ed.day(), tzinfo=tz) + timedelta(days=1)
        if start >= end:
            box = MessageBox(_("日期区间无效"), _("起始日期必须早于结束日期。"), self.window())
            box.hideCancelButton()
            box.exec()
            return

        self.bgs[vt_symbol] = BarGenerator(self.on_bar)

        chart = self.create_chart()
        self.charts[vt_symbol] = chart
        self.tab.addTab(chart, vt_symbol)
        self.tab.setCurrentWidget(chart)

        self.chart_engine.query_history(vt_symbol, interval, start, end)

    def register_event(self) -> None:
        self.signal_tick.connect(self.process_tick_event)
        self.signal_history.connect(self.process_history_event)
        self.signal_contract.connect(self.process_contract_event)

        self.event_engine.register(EVENT_CHART_HISTORY, self.signal_history.emit)
        self.event_engine.register(EVENT_TICK, self.signal_tick.emit)
        self.event_engine.register(EVENT_CONTRACT, self.signal_contract.emit)

    def process_contract_event(self, event: Event) -> None:
        contract: ContractData = event.data
        self._add_symbol_if_new(contract.vt_symbol)

    def _add_symbol_if_new(self, vt_symbol: str) -> None:
        # EVENT_CONTRACT fires once per contract per query — a gateway
        # reconnect (or a second connect() to a different market) re-fires
        # it for symbols already in the list, so this must dedupe. Set
        # membership, not symbol_line.findText(): see _known_symbols'
        # init comment for why the linear scan was an O(n²) burst.
        if vt_symbol not in self._known_symbols:
            self._known_symbols.add(vt_symbol)
            self.symbol_line.addItem(vt_symbol)

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
