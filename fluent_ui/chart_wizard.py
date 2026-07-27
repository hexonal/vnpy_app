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

import pyqtgraph as pg
from qfluentwidgets import CalendarPicker, ComboBox, MessageBox, PushButton, SegmentedWidget
from tzlocal import get_localzone_name

from vnpy.chart import ChartWidget, VolumeItem
from vnpy.event import Event, EventEngine
from vnpy.trader.constant import Exchange, Interval
from vnpy.trader.engine import MainEngine
from vnpy.trader.event import EVENT_CONTRACT, EVENT_TICK
from vnpy.trader.locale import _
from vnpy_gatewaykit.market_clock import market_tz
from vnpy.trader.object import BarData, ContractData, SubscribeRequest, TickData
from vnpy.trader.ui import QtCore, QtWidgets
from vnpy.trader.utility import ZoneInfo
from vnpy_chartwizard.engine import APP_NAME, EVENT_CHART_HISTORY, ChartWizardEngine

# Broker-app-style period buttons: (label, vnpy Interval, default lookback
# days). Order matches a broker app's tab strip: real-time first, then
# widening windows.
#   实时 = today's real-time intraday, 1-minute bars over the last ~24h,
#          INCLUDING the extended/night session (盘前/盘后 for US); the
#          night bars are drawn + marked distinctly (see SessionCandleItem
#          + _mark_extended_sessions).
#   5天  = 5-trading-day intraday minute chart (futu/uSMART "5日").
#   时   = hourly bars.  日 = daily bars (one per day).  周 = weekly bars.
# All map to intervals FutuGateway/query_history support (futu_mapping
# INTERVAL_VT2FUTU: K_1M/K_60M/K_DAY/K_WEEK). 实时 and 5天 share the 1-minute
# interval and differ only in lookback, so charts are tracked by period
# LABEL (chart_periods), not by interval.
_PERIODS: list[tuple[str, Interval, int]] = [
    ("实时", Interval.MINUTE, 1),
    ("5天", Interval.MINUTE, 5),
    ("时", Interval.HOUR, 30),
    ("日", Interval.DAILY, 365),
    ("周", Interval.WEEKLY, 365 * 3),
]
_PERIOD_BY_LABEL: dict[str, tuple[Interval, int]] = {
    label: (interval, lookback) for label, interval, lookback in _PERIODS
}


def _query_tz(vt_symbol: str) -> ZoneInfo:
    """查询窗口的时区 —— 取标的所在市场，不是这台机器所在地。

    这两者在本项目里从来不相等：机器在 US Pacific/Eastern，标的是港股。
    用 get_localzone_name() 取窗口，查港股「最近 1 天」实际取到的是港股时间
    的另一段，边界那根 K 线必然错位。GUI 日志里留下过现场：

        查询K线 -> FUTU: HistoryRequest(symbol='1', exchange=SEHK,
            start=... tzinfo=ZoneInfo(key='America/New_York'), ...)

    查 SEHK 却带着 New_York 的墙钟。market_tz 是本项目的单一真相源，
    与网关写 bar 用的是同一张表；未知交易所它自己会给出合理回退。
    """
    try:
        _, exchange_str = vt_symbol.rsplit(".", 1)
        return market_tz(Exchange(exchange_str))
    except (ValueError, KeyError):
        # vt_symbol 不合法或交易所未收录：退回机器时区并非最优，但比抛异常
        # 让整个图表打不开要好 —— 图表是只读视图，不是下单路径。
        return ZoneInfo(get_localzone_name())
# Intervals whose _period_start key provably matches futu's history-bar
# time_key, so a live tick updates the last history bar IN PLACE rather than
# appending a phantom duplicate. Verified against real futu data:
#   MINUTE  → time_key at minute start (HH:MM:00)  ✓ matches our floor
#   DAILY   → time_key at date 00:00               ✓ matches our midnight floor
#   WEEKLY  → time_key at Monday 00:00             ✓ matches our Monday floor
# HOUR is deliberately EXCLUDED: futu stamps hour bars session-aligned at the
# bar END (HK 10:30/11:30/12:00/14:00/15:00/16:00 with the lunch break, US
# 10:30…15:30 from the 09:30 open), which our calendar-hour floor (:00) never
# equals — live aggregation there would append a growing row of phantom :00
# bars. So the 时 chart shows history only; its last bar refreshes on reload,
# not intra-hour. (Reproducing futu's session-aware hour stamping in
# _period_start is fragile; a static hour chart is correct, a phantom-bar one
# is not.)
_LIVE_ALIGNED_INTERVALS: frozenset[Interval] = frozenset(
    {Interval.MINUTE, Interval.DAILY, Interval.WEEKLY}
)

from .market_session import is_extended
from .searchable_combo_box import SearchableComboBox
from .session_candle import SessionCandleItem

# Exchange filter for the symbol picker: (label, vt_symbol suffix). A None
# suffix is 全部 (no filter). The suffixes are the vt_symbol exchange parts
# FutuGateway serves (see FutuGateway.exchanges: SEHK/SMART/SSE/SZSE), so
# the numeric HK codes and alphabetic US tickers can be browsed separately
# instead of drowning in one ~22k-symbol list.
_MARKETS: list[tuple[str, str | None]] = [
    ("全部", None),
    ("港股", "SEHK"),
    ("美股", "SMART"),
    ("沪市", "SSE"),
    ("深市", "SZSE"),
]


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
        # Per-chart period LABEL (实时/5天/时/日/周). Tracked separately from
        # the interval because 实时 and 5天 share the 1-minute interval, so the
        # interval alone can't say which period tab a chart is on.
        self.chart_periods: dict[str, str] = {}
        # Grey background bands (pg.LinearRegionItem) marking extended/night
        # sessions per chart, so they can be cleared on reload/close.
        self._session_bands: dict[str, list[pg.LinearRegionItem]] = {}
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
        # Last seen tick datetime per symbol, to detect a session rollover
        # (date change) — on which tick.volume resets and the raw delta
        # would go hugely negative. Mirrors BarGenerator.update_tick.
        self._last_tick_time: dict[str, datetime] = {}
        # O(1) dedupe alongside symbol_line's item list: findText() is a
        # Python-level linear scan (qfluentwidgets combo_box.py:244-250),
        # and _add_symbol_if_new runs once per EVENT_CONTRACT — with a
        # FutuGateway pushing ~22k contracts on connect, findText-based
        # dedupe is Σi ≈ n²/2 ≈ 2×10⁸ comparisons during the burst.
        self._known_symbols: set[str] = set()
        # Current exchange filter for the symbol picker (a vt_symbol suffix,
        # or None for 全部). Only symbols matching it are shown in the combo.
        self._market_filter: str | None = None

        self.init_ui()
        self.register_event()

    def init_ui(self) -> None:
        self.setWindowTitle(_("K线图表"))

        self.tab = QtWidgets.QTabWidget()
        self.tab.setTabsClosable(True)
        self.tab.tabCloseRequested.connect(self.close_tab)
        # Switching tabs re-syncs the period strip to that chart's period.
        self.tab.currentChanged.connect(self._on_tab_changed)

        # Market/exchange filter — narrows the symbol picker to one exchange
        # so US tickers (alphabetic) aren't buried under ~17k HK/CN numeric
        # codes. 全部 shows everything (default).
        self.market_combo = ComboBox()
        for label, _suffix in _MARKETS:
            self.market_combo.addItem(label)
        self.market_combo.setCurrentIndex(0)
        self.market_combo.currentIndexChanged.connect(self._on_market_changed)

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
        # Guards the period strip while we set it programmatically (on tab
        # switch) so that sync doesn't recurse into a chart reload.
        self._syncing_period = False
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
        hbox.addWidget(QtWidgets.QLabel(_("市场")))
        hbox.addWidget(self.market_combo)
        hbox.addWidget(QtWidgets.QLabel(_("代码")))
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
        # SessionCandleItem draws 盘前/盘后 (夜盘) bars in a muted palette and
        # reports the session in the cursor legend — one of the three night-
        # session markings (with the background band + cursor 时段 line).
        chart.add_item(SessionCandleItem, "candle", "candle")
        chart.add_item(VolumeItem, "volume", "volume")
        chart.add_cursor()
        return chart

    def _active_vt_symbol(self) -> str | None:
        """vt_symbol of the currently visible chart tab, or None if no tab
        is open. Tab text is 'vt_symbol · period'."""
        index = self.tab.currentIndex()
        if index < 0:
            return None
        return self.tab.tabText(index).split(" · ")[0]

    def _on_period_changed(self, route_key: str) -> None:
        self._current_period = route_key
        # Programmatic sync (tab switch) — just record it, don't reload.
        if self._syncing_period:
            return
        # User clicked a period tab: apply it to the active chart, like a
        # broker app (click 日 → the current chart reloads as a daily chart).
        # With no chart open, it just sets the period for the next new_chart.
        vt_symbol = self._active_vt_symbol()
        if vt_symbol is not None and vt_symbol in self.charts:
            self._reload_chart(vt_symbol, route_key)

    def _on_tab_changed(self, index: int) -> None:
        """Re-highlight the period strip to match the chart the user just
        switched to, without triggering a reload."""
        vt_symbol = self._active_vt_symbol()
        if vt_symbol is None:
            return
        label = self.chart_periods.get(vt_symbol)
        if label is None or label == self._current_period:
            return
        self._syncing_period = True
        try:
            self.period_pivot.setCurrentItem(label)
            self._current_period = label
        finally:
            self._syncing_period = False

    def _reload_chart(self, vt_symbol: str, period_label: str) -> None:
        """Re-query and redraw an already-open chart at a new period, in
        place (same tab). Clears the old bars and live-bar state so the new
        interval's history and live aggregation start clean."""
        interval, lookback = _PERIOD_BY_LABEL[period_label]
        chart = self.charts.get(vt_symbol)
        if chart is None:
            return

        chart.clear_all()
        self._clear_session_bands(vt_symbol)
        self.chart_intervals[vt_symbol] = interval
        self.chart_periods[vt_symbol] = period_label
        # Drop the running live bar + volume baseline so the new interval
        # doesn't inherit the previous period's partial bar/cumulative.
        self.running_bars.pop(vt_symbol, None)
        self._last_tick_volume.pop(vt_symbol, None)
        self._last_tick_time.pop(vt_symbol, None)

        # Reflect the new period in the tab label.
        index = self.tab.currentIndex()
        if index >= 0 and self.tab.tabText(index).split(" · ")[0] == vt_symbol:
            self.tab.setTabText(index, f"{vt_symbol} · {period_label}")

        # Period switch uses the period's own lookback (custom date pickers
        # only apply when creating a chart, matching broker-app behavior).
        tz = _query_tz(vt_symbol)
        end = datetime.now(tz)
        start = end - timedelta(days=lookback)
        self.chart_engine.query_history(vt_symbol, interval, start, end)

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
        self._clear_session_bands(vt_symbol)
        self._session_bands.pop(vt_symbol, None)
        self.charts.pop(vt_symbol, None)
        self.chart_intervals.pop(vt_symbol, None)
        self.chart_periods.pop(vt_symbol, None)
        self.running_bars.pop(vt_symbol, None)
        self._last_tick_volume.pop(vt_symbol, None)
        self._last_tick_time.pop(vt_symbol, None)

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
        tz = _query_tz(vt_symbol)
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
        self.chart_periods[vt_symbol] = self._current_period

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

    @staticmethod
    def _vt_exchange(vt_symbol: str) -> str:
        """The exchange suffix of a vt_symbol ('AAPL.SMART' -> 'SMART')."""
        return vt_symbol.rsplit(".", 1)[-1]

    def _matches_market(self, vt_symbol: str) -> bool:
        """Whether a symbol belongs in the picker under the current filter."""
        return self._market_filter is None or self._vt_exchange(vt_symbol) == self._market_filter

    def _add_symbol_if_new(self, vt_symbol: str) -> None:
        # EVENT_CONTRACT fires once per contract per query — a gateway
        # reconnect (or a second connect() to a different market) re-fires
        # it for symbols already in the list, so this must dedupe. Set
        # membership, not symbol_line.findText(): see _known_symbols'
        # init comment for why the linear scan was an O(n²) burst.
        if vt_symbol not in self._known_symbols:
            self._known_symbols.add(vt_symbol)
            # Only show it if it matches the current market filter; it's
            # still recorded in _known_symbols so switching the filter later
            # surfaces it (via _rebuild_symbol_list).
            if self._matches_market(vt_symbol):
                self.symbol_line.addItem(vt_symbol)

    def _on_market_changed(self, index: int) -> None:
        """Repopulate the symbol picker with only the chosen exchange's
        contracts (全部 = no filter)."""
        if 0 <= index < len(_MARKETS):
            self._market_filter = _MARKETS[index][1]
        self._rebuild_symbol_list()

    def _rebuild_symbol_list(self) -> None:
        """Rebuild the symbol combo's items from _known_symbols under the
        current market filter. Sorted so HK numeric codes and US alphabetic
        tickers each read in order."""
        items = sorted(s for s in self._known_symbols if self._matches_market(s))
        self.symbol_line.clear()
        if items:
            self.symbol_line.addItems(items)

    def process_tick_event(self, event: Event) -> None:
        tick: TickData = event.data
        interval = self.chart_intervals.get(tick.vt_symbol)
        # Drop non-positive prices: `not tick.last_price` catches 0/None; the
        # `< 0` catches a negative (which `not` does NOT — it would corrupt
        # low_price). A book-only tick before the first quote also lands here
        # (last_price 0). No stock has a negative price, so this is pure
        # garbage-in protection.
        if interval is None or not tick.last_price or tick.last_price < 0:
            return
        # Only aggregate live for intervals whose period key matches futu's
        # history stamping (see _LIVE_ALIGNED_INTERVALS) — HOUR would spawn
        # phantom :00 bars, so its chart stays history-only.
        if interval not in _LIVE_ALIGNED_INTERVALS:
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
        prev_time = self._last_tick_time.get(tick.vt_symbol)

        # Stale / out-of-order tick guard. Within one session futu's cumulative
        # `volume` is monotonic non-decreasing, so a SAME-SESSION tick whose
        # volume went DOWN is a stale re-push or out-of-order frame (common
        # right after an OpenD reconnect snapshot). Drop it entirely: if we
        # processed it, the delta would floor to 0 (harmless) BUT the baseline
        # would still advance to the lower value (poisoning the next real
        # tick's delta into a phantom overcount), and its possibly-stale
        # extreme price would paint a permanent fake wick on high/low. A date
        # change is a legitimate session reset, not staleness — let it fall
        # through to the rollover branch below. (Guard on volume, never on
        # datetime: futu is second-precision, so a `tick.datetime > last`
        # guard would wrongly drop the 2nd tick of the same second.)
        if (
            prev_vol is not None
            and prev_time is not None
            and tick.datetime.date() == prev_time.date()
            and tick.volume < prev_vol
        ):
            return

        # This tick's volume contribution. tick.volume is session-cumulative,
        # so normally it's the delta against the previous tick; on a session
        # rollover (date change) it resets, and the new day's cumulative-so-
        # far IS the contribution (raw delta would be hugely negative and the
        # max(,0) floor would drop it). Mirrors BarGenerator.update_tick.
        if prev_vol is None:
            vol_delta = 0.0
        elif prev_time is not None and tick.datetime.date() != prev_time.date():
            vol_delta = tick.volume
        else:
            vol_delta = max(tick.volume - prev_vol, 0)

        if running is None or running.datetime != start:
            # New period bar. Seed its volume with this tick's contribution
            # rather than 0 — the boundary tick's delta belongs to the period
            # it falls in (the new one), so dropping it would undercount every
            # period by one tick at its open.
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
                volume=vol_delta,
            )
            self.running_bars[tick.vt_symbol] = running
        else:
            running.close_price = tick.last_price
            # Only extend high/low on a real trade (vol_delta > 0). last_price
            # is the last TRADED price, so it can only move when a trade prints,
            # which increments cumulative volume — a last_price change carrying
            # NO volume delta is a glitch/restatement (bad OpenD relay, parse
            # artifact, away-market print). Gating the extremes on vol_delta > 0
            # stops such a glitch from painting a permanent fake wick on the
            # bar (high/low never self-heal within a period; close does, on the
            # next real tick). No arbitrary %-band needed — the volume delta is
            # the signal. (The forum's tick-cleaning posts name 异常价格 but give
            # no threshold; this is the robust volume-gated form.)
            if vol_delta > 0:
                running.high_price = max(running.high_price, tick.last_price)
                running.low_price = min(running.low_price, tick.last_price)
                running.volume += vol_delta

        self._last_tick_volume[tick.vt_symbol] = tick.volume
        self._last_tick_time[tick.vt_symbol] = tick.datetime
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
        # Shade the extended/night-session (盘前/盘后) stretches so they're
        # marked at a glance, on top of SessionCandleItem's muted coloring.
        self._mark_extended_sessions(bar.vt_symbol, history)

        contract: ContractData | None = self.main_engine.get_contract(bar.vt_symbol)
        if contract:
            req = SubscribeRequest(contract.symbol, contract.exchange)
            self.main_engine.subscribe(req, contract.gateway_name)

    def _clear_session_bands(self, vt_symbol: str) -> None:
        """Remove any night-session background bands drawn for a chart."""
        chart = self.charts.get(vt_symbol)
        bands = self._session_bands.get(vt_symbol)
        if not bands:
            return
        candle_plot = chart.get_plot("candle") if chart else None
        if candle_plot is not None:
            for band in bands:
                candle_plot.removeItem(band)
        self._session_bands[vt_symbol] = []

    def _mark_extended_sessions(self, vt_symbol: str, bars: list[BarData]) -> None:
        """Draw a translucent grey band behind each contiguous run of
        extended-hours (盘前/盘后) bars, so the night session is obvious at a
        glance. Bar index == position in the chart's manager (history is the
        full initial set). No-op for markets/periods with no extended bars
        (HK/CN stocks, or the daily/weekly views). Defensive: any drawing
        error is swallowed — a missing band must never break the chart."""
        self._clear_session_bands(vt_symbol)
        chart = self.charts.get(vt_symbol)
        if chart is None:
            return
        candle_plot = chart.get_plot("candle")
        if candle_plot is None:
            return

        # Collect [start_ix, end_ix] index ranges of consecutive 夜盘 bars.
        ranges: list[tuple[int, int]] = []
        run_start: int | None = None
        for ix, bar in enumerate(bars):
            if is_extended(bar):
                if run_start is None:
                    run_start = ix
            elif run_start is not None:
                ranges.append((run_start, ix - 1))
                run_start = None
        if run_start is not None:
            ranges.append((run_start, len(bars) - 1))

        if not ranges:
            return

        bands: list[pg.LinearRegionItem] = []
        brush = pg.mkBrush(255, 255, 255, 18)  # faint grey, behind candles
        try:
            for start_ix, end_ix in ranges:
                band = pg.LinearRegionItem(
                    values=(start_ix - 0.5, end_ix + 0.5),
                    orientation="vertical",
                    brush=brush,
                    movable=False,
                )
                band.setZValue(-1)  # behind the candles
                # Hide the draggable edge lines — this is a static marker.
                for line in band.lines:
                    line.setPen(pg.mkPen(color=(255, 255, 255, 0)))
                candle_plot.addItem(band)
                bands.append(band)
        except Exception as exc:  # noqa: BLE001 — a band is decoration, never fatal
            print(f"[chart_wizard] 夜盘背景带绘制失败(不影响K线): {exc}")

        self._session_bands[vt_symbol] = bands
