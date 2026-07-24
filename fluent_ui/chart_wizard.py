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

from qfluentwidgets import CalendarPicker, MessageBox, PushButton, SegmentedWidget
from tzlocal import get_localzone_name

from vnpy.chart import CandleItem, ChartWidget, VolumeItem
from vnpy.event import Event, EventEngine
from vnpy.trader.constant import Interval
from vnpy.trader.engine import MainEngine
from vnpy.trader.event import EVENT_CONTRACT, EVENT_TICK
from vnpy.trader.locale import _
from vnpy.trader.object import BarData, ContractData, SubscribeRequest, TickData
from vnpy.trader.ui import QtCore, QtWidgets
from vnpy.trader.utility import ZoneInfo
from vnpy_chartwizard.engine import APP_NAME, EVENT_CHART_HISTORY, ChartWizardEngine

# Broker-app-style period buttons: (label, vnpy Interval, default lookback
# days). "5天" is a 5-trading-day intraday minute chart (matching how
# futu/uSMART apps show it), not a 5-day-bar chart. All four map to
# intervals FutuGateway/query_history support (futu_mapping INTERVAL_VT2FUTU:
# K_1M/K_60M/K_DAY/K_WEEK). Order matches a broker app's tab strip.
_PERIODS: list[tuple[str, Interval, int]] = [
    ("5天", Interval.MINUTE, 5),
    ("时", Interval.HOUR, 30),
    ("日", Interval.DAILY, 365),
    ("周", Interval.WEEKLY, 365 * 3),
]
_PERIOD_BY_LABEL: dict[str, tuple[Interval, int]] = {
    label: (interval, lookback) for label, interval, lookback in _PERIODS
}

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

        self.charts: dict[str, ChartWidget] = {}
        # Per-chart period so tick-driven live bars aggregate at the same
        # interval the chart's history was loaded at.
        self.chart_intervals: dict[str, Interval] = {}
        # The current, still-forming period bar per symbol, updated on
        # every tick and pushed to the chart (BarManager.update_bar keys by
        # datetime, so re-pushing the same period-start datetime updates
        # the last bar in place — this is how the day bar "ticks live" like
        # a broker app, without needing a server-side realtime-K feed that
        # uSMART doesn't have).
        self.running_bars: dict[str, BarData] = {}
        # Last seen cumulative tick volume per symbol, for per-period
        # volume deltas (tick.volume is session-cumulative).
        self._last_tick_volume: dict[str, float] = {}
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

        # Broker-app-style period tab strip (5天/时/日/周) — replaces the
        # old interval dropdown. Each new chart uses whichever period is
        # selected here; the period also drives live-bar aggregation.
        self.period_pivot = SegmentedWidget()
        for label, _interval, _lookback in _PERIODS:
            self.period_pivot.addItem(routeKey=label, text=label)
        self.period_pivot.setCurrentItem("日")
        self._current_period = "日"
        self.period_pivot.currentItemChanged.connect(self._on_period_changed)

        # Optional custom date range — CalendarPickers, empty by default so
        # the selected period's own lookback is used unless the user picks
        # explicit dates (broker apps do the same: a period button plus an
        # optional custom range). isRestEnabled lets the user clear back to
        # "use period default".
        self.start_date = CalendarPicker()
        self.start_date.setResetEnabled(True)
        self.end_date = CalendarPicker()
        self.end_date.setResetEnabled(True)

        self.button = PushButton(_("新建图表"))
        self.button.clicked.connect(self.new_chart)

        hbox = QtWidgets.QHBoxLayout()
        hbox.addWidget(QtWidgets.QLabel(_("本地代码")))
        hbox.addWidget(self.symbol_line)
        hbox.addWidget(self.period_pivot)
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

    def _on_period_changed(self, route_key: str) -> None:
        self._current_period = route_key

    @staticmethod
    def _period_start(dt: datetime, interval: Interval) -> datetime:
        """Align a tick's datetime to the start of the period it belongs
        to — the key BarManager.update_bar uses to know whether a live
        bar updates the last chart bar or opens a new one."""
        if interval == Interval.HOUR:
            return dt.replace(minute=0, second=0, microsecond=0)
        if interval == Interval.DAILY:
            return dt.replace(hour=0, minute=0, second=0, microsecond=0)
        if interval == Interval.WEEKLY:
            monday = dt - timedelta(days=dt.weekday())
            return monday.replace(hour=0, minute=0, second=0, microsecond=0)
        # MINUTE (incl. the "5天" intraday chart)
        return dt.replace(second=0, microsecond=0)

    def close_tab(self, index: int) -> None:
        # Tab text is "vt_symbol · period"; charts/state are keyed by the
        # bare vt_symbol, so split it back out.
        vt_symbol = self.tab.tabText(index).split(" · ")[0]
        self.tab.removeTab(index)
        self.charts.pop(vt_symbol, None)
        self.chart_intervals.pop(vt_symbol, None)
        self.running_bars.pop(vt_symbol, None)
        self._last_tick_volume.pop(vt_symbol, None)

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

        interval, lookback = _PERIOD_BY_LABEL[self._current_period]

        # Date range: use the selected period's default lookback, unless
        # the user picked explicit custom dates in both pickers.
        tz = ZoneInfo(get_localzone_name())
        end = datetime.now(tz)
        start = end - timedelta(days=lookback)
        sd, ed = self.start_date.getDate(), self.end_date.getDate()
        if sd.isValid() and ed.isValid():
            start = datetime(sd.year(), sd.month(), sd.day(), tzinfo=tz)
            end = datetime(ed.year(), ed.month(), ed.day(), tzinfo=tz) + timedelta(days=1)
            if start >= end:
                box = MessageBox(_("日期区间无效"), _("起始日期必须早于结束日期。"), self.window())
                box.hideCancelButton()
                box.exec()
                return

        self.chart_intervals[vt_symbol] = interval

        chart = self.create_chart()
        self.charts[vt_symbol] = chart
        self.tab.addTab(chart, f"{vt_symbol} · {self._current_period}")
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
        interval = self.chart_intervals.get(tick.vt_symbol)
        if interval is None or not tick.last_price:
            return
        chart = self.charts.get(tick.vt_symbol)
        if chart is None:
            return

        # Aggregate the tick into the current period's still-forming bar.
        # datetime is the period start (see _period_start) so re-pushing it
        # to update_bar keeps updating the SAME last chart bar until the
        # period rolls over — the "day bar ticks live" behavior. (Assumes
        # tick.datetime and query_history bar.datetime share a timezone
        # convention within one gateway, which they do for FutuGateway;
        # flagged for live calibration like the uSMART timestamp assumption.)
        start = self._period_start(tick.datetime, interval)
        running = self.running_bars.get(tick.vt_symbol)
        prev_vol = self._last_tick_volume.get(tick.vt_symbol)

        if running is None or running.datetime != start:
            running = BarData(
                gateway_name=tick.gateway_name,
                symbol=tick.symbol,
                exchange=tick.exchange,
                datetime=start,
                interval=interval,
                open_price=tick.last_price,
                high_price=tick.last_price,
                low_price=tick.last_price,
                close_price=tick.last_price,
                volume=0,
            )
            self.running_bars[tick.vt_symbol] = running
        else:
            running.high_price = max(running.high_price, tick.last_price)
            running.low_price = min(running.low_price, tick.last_price)
            running.close_price = tick.last_price
            if prev_vol is not None:
                running.volume += max(tick.volume - prev_vol, 0)

        self._last_tick_volume[tick.vt_symbol] = tick.volume
        chart.update_bar(copy(running))

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
