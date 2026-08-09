"""Fluent-native replacement for vnpy_paperaccount's PaperManager settings widget.

Why the stock widget looks broken inside FluentWindow
-----------------------------------------------------

Upstream's PaperManager (66 lines, site-packages/vnpy_paperaccount/ui/widget.py)
calls ``setFixedWidth(500)`` and ``setFixedHeight(200)`` on itself. That is a
reasonable thing to do for a free-floating child window under stock vnpy's
QMainWindow + menu-bar layout, which is what it was written for. Inside
``FluentWindow.addSubInterface()`` the widget is handed a full page of a
QStackedWidget instead, and a fixed size means it refuses that page: three rows
of controls pinned to the top-left of a 1600x1000 window, everything else empty
background. That — not theming, not the dark palette — is the whole content of
the "控件挤在左上角、大片空白" screenshot. The uneven label column in the same
shot is QFormLayout's default right-alignment meeting Chinese labels of unequal
length; nothing here uses QFormLayout, so it cannot recur.

The engine is reused unmodified. Every setting written here goes through the
same ``set_trade_slippage`` / ``set_timer_interval`` / ``set_instant_trade``
calls the stock widget used, so ``paper_account_setting.json`` keeps exactly the
same three keys and an install can be rolled back to the stock widget without
touching a file on disk.

Clearing positions is a destructive action wearing an ordinary button
--------------------------------------------------------------------

``PaperEngine.clear_position()`` zeroes volume/frozen/price on every position,
pushes EVENT_POSITION for each, and then calls ``save_data()`` — the JSON on
disk is overwritten before the click finishes. There is no undo and no second
copy. Upstream wires it straight to ``clicked`` and the only guard is that the
button is drawn at double height, while the two spin boxes it sits beside cost
nothing to mis-click. A misfire on one of those changes a number you can read
and set back; a misfire on this one ends the account. Hence the MessageBox
confirmation, with the position count in the text so the dialog says what is
actually about to be destroyed rather than asking an abstract "确认？".

"跳" is only as trustworthy as the gateway that filled in pricetick
------------------------------------------------------------------

``engine.py:369`` fills market and stop orders at
``tick.ask_price_1 + trade_slippage * contract.pricetick``. The unit of this
setting is therefore whatever the quote gateway wrote into ``pricetick``, and
the two gateways in this workspace mean different things by it:

* **uSMART** backfills it from the live quote, so on US names it is the real
  USD 0.01 grid and the slippage setting does what it says.
* **Futu** writes ``spread_table.finest_tick(exchange)`` — deliberately the
  *floor* of the market's spread table (SEHK 0.001, SMART 0.0001), because a
  contract is built before any quote exists and the floor is the only scalar
  that never calls a legal price illegal. It is a lower bound, not the tick at
  the current price. 700.SEHK near HKD 400 trades on the 0.200 grid, so two
  "跳" of simulated slippage come to HKD 0.002 against a real minimum move of
  HKD 0.2 — one percent of one tick, i.e. no slippage at all. US names quoted
  by Futu are understated by the same factor of a hundred (0.0001 against the
  penny grid).

Neither market is the exception here, which is why the panel reports both
side by side and computes the real band from ``spread_table.price_tick``
instead of naming HK in a comment and leaving the US case to the reader.
Adding a third market means adding it to ``REPORTED_MARKETS`` below *and* to
spread_table; a market absent from the table raises rather than guessing, so a
missing entry shows up as a "无价位表" line in the panel, not as a plausible
wrong number.

What this panel deliberately does not do
----------------------------------------

It does not subscribe to EVENT_CONTRACT or EVENT_TICK to keep the readings
live. Futu pushes on the order of ten thousand contracts at connect, and an
EventEngine handler that rescans every contract per event would burn the event
thread for a cosmetic label — and a handler that raises kills that thread
silently for the whole process (``EventEngine._run`` only catches Empty). The
readings are recomputed in ``showEvent`` instead: navigating to this page is
already the moment a trader wants them, and it costs one pass over the contract
dict. The cost is that a page left open while a gateway connects keeps showing
"尚未加载合约" until it is navigated away from and back.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

from qfluentwidgets import (
    BodyLabel,
    IndicatorPosition,
    MessageBox,
    PushSettingCard,
    SettingCard,
    SettingCardGroup,
    SimpleCardWidget,
    SingleDirectionScrollArea,
    SpinBox,
    StrongBodyLabel,
    SwitchButton,
)
from qfluentwidgets import FluentIcon as FIF
from vnpy.event import EventEngine
from vnpy.trader.constant import Exchange
from vnpy.trader.engine import MainEngine
from vnpy.trader.object import ContractData, PositionData, TickData
from vnpy.trader.ui import QtCore, QtGui, QtWidgets
from vnpy_gatewaykit.spread_table import price_tick
from vnpy_paperaccount.engine import APP_NAME, PaperEngine

# Markets reported by the slippage readout. Both are traded here — HK through
# Futu quotes, US through uSMART — and the whole point of the readout is that
# the two disagree about what pricetick means, so neither may be dropped for
# brevity. SSE/SZSE are left out because this fork is read-only on them (see
# vnpy_futu's stop_supported note): a simulated fill on a market with no trading
# channel is not a rehearsal of anything.
REPORTED_MARKETS: tuple[Exchange, ...] = (Exchange.SEHK, Exchange.SMART)

_MARKET_LABELS: dict[Exchange, str] = {
    Exchange.SEHK: "港股",
    Exchange.SMART: "美股",
}

_MARKET_CURRENCIES: dict[Exchange, str] = {
    Exchange.SEHK: "港元",
    Exchange.SMART: "美元",
}

SLIPPAGE_HINT: str = "1 跳 = 该合约的 pricetick，值多少钱按网关与市场各不相同 —— 见下方核对"

INTERVAL_HINT: str = "每隔这么多秒重算一次浮动盈亏并推送持仓事件，不影响撮合"

INSTANT_HINT: str = "开启后委托一进来就用当前盘口撮合，不等下一笔 tick"

CLEAR_HINT: str = "把每个持仓的数量、冻结与均价归零并立刻写盘，不可撤销 —— 点下后会先确认"


# ---------------------------------------------------------------------------
# Slippage readings
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SlippageReading:
    """What ``slippage`` 跳 is actually worth on one market, right now.

    ``engine_tick`` is what the paper engine will multiply (``contract.pricetick``
    as the quote gateway wrote it); ``market_tick`` is what the exchange's spread
    table says the minimum move really is at ``reference_price``. Keeping both
    is the entire value of this object — a single number cannot show that the
    two disagree, and it is the disagreement that makes the setting inert.

    Any of the three optional fields may be None, and each None means something
    different: no contract loaded for this market at all, a contract but no
    quote yet, or a price the spread table refuses to answer for. They are
    rendered as three different sentences rather than collapsed into one
    "unavailable", because "connect the gateway" and "this price is outside the
    published table" call for different actions from the reader.
    """

    exchange: Exchange
    slippage: int
    vt_symbol: str | None
    engine_tick: float | None
    reference_price: float | None
    market_tick: float | None

    def describe(self) -> str:
        label: str = _MARKET_LABELS.get(self.exchange, self.exchange.value)
        currency: str = _MARKET_CURRENCIES.get(self.exchange, "")
        head: str = f"{label}（{self.exchange.value}）"

        if self.vt_symbol is None or self.engine_tick is None:
            return f"{head}：尚未加载合约 —— 连接对应网关后这一行才有读数。"

        # isfinite before any comparison: a NaN pricetick makes `<= 0` False and
        # would sail through a plain sign check into a division below, printing a
        # confident nan% ratio. A gateway that publishes NaN/inf/0 here is broken
        # in a way that silently multiplies into every simulated fill price, so
        # it gets its own sentence instead of a formatted number.
        if not math.isfinite(self.engine_tick) or self.engine_tick <= 0:
            return (
                f"{head} {self.vt_symbol}：pricetick={self.engine_tick!r} 不是正有限数，"
                f"滑点乘上去等于把成交价推向未定义 —— 这是网关缺陷，先修网关。"
            )

        cost: float = self.slippage * self.engine_tick
        line: str = (
            f"{head} {self.vt_symbol}：引擎按 pricetick={_fmt(self.engine_tick)} 折算，"
            f"{self.slippage} 跳 = {_fmt(cost)} {currency}"
        )

        if self.reference_price is None:
            return f"{line}；还没有报价，真实价位档无从核对。"

        if self.market_tick is None:
            return (
                f"{line}；但 {_fmt(self.reference_price)} 这个价位不在价位表里，"
                f"真实档位拒绝外推。"
            )

        ratio: float = self.engine_tick / self.market_tick
        if ratio >= 1:
            return (
                f"{line}；{_fmt(self.reference_price)} 上的真实档位是 "
                f"{_fmt(self.market_tick)}，读数可信。"
            )

        return (
            f"{line}；而 {_fmt(self.reference_price)} 上的真实档位是 "
            f"{_fmt(self.market_tick)}，一跳只有真实一档的 {ratio:.1%} —— "
            f"滑点在这个市场上形同虚设。"
        )


def _fmt(value: float) -> str:
    """Prices and ticks in this file span 0.0001 to a few thousand; %g keeps
    both ends readable without printing 0.20000000000000001 or 1e-04."""
    return f"{value:g}"


def _reference_price(tick: TickData | None) -> float | None:
    """The price to look a spread-table band up with.

    last_price first, then the offer, then the bid: a symbol that has not traded
    today still has a book, and either side of it lands in the same band as any
    price a fill could occur at. An unpopulated TickData field is 0.0, so the
    fallback chain is just a sign test — except that the isfinite guard earns
    its place on inf rather than on NaN. NaN fails `> 0` on its own and drops
    through harmlessly; `inf > 0` is True, and inf would be printed to the
    trader as a reference price and then handed to price_tick, which refuses it
    — leaving a reading that shows a price and no band with no way to tell that
    from a genuinely off-table price.
    """
    if tick is None:
        return None
    for candidate in (tick.last_price, tick.ask_price_1, tick.bid_price_1):
        if math.isfinite(candidate) and candidate > 0:
            return float(candidate)
    return None


def read_slippage(
    slippage: int,
    contracts: Iterable[ContractData],
    quote: Callable[[str], TickData | None],
    markets: Sequence[Exchange] = REPORTED_MARKETS,
) -> tuple[SlippageReading, ...]:
    """One reading per market, in the order given, whether or not it has data.

    Kept free of Qt and of MainEngine so the arithmetic can be tested against
    hand-built contracts — ``quote`` is exactly ``main_engine.get_tick`` in
    production. Contracts are sorted by vt_symbol and a quoted one wins over an
    unquoted one, so the panel names the same symbol on every refresh instead of
    following dict insertion order and appearing to jump between symbols.
    """
    by_market: dict[Exchange, list[ContractData]] = {market: [] for market in markets}
    for contract in contracts:
        bucket: list[ContractData] | None = by_market.get(contract.exchange)
        if bucket is not None:
            bucket.append(contract)

    readings: list[SlippageReading] = []
    for market in markets:
        candidates: list[ContractData] = sorted(by_market[market], key=lambda c: c.vt_symbol)
        chosen: ContractData | None = None
        price: float | None = None

        for contract in candidates:
            found: float | None = _reference_price(quote(contract.vt_symbol))
            if chosen is None:
                chosen = contract
            if found is not None:
                chosen = contract
                price = found
                break

        readings.append(
            SlippageReading(
                exchange=market,
                slippage=slippage,
                vt_symbol=None if chosen is None else chosen.vt_symbol,
                engine_tick=None if chosen is None else chosen.pricetick,
                reference_price=price,
                market_tick=None if price is None else _market_tick(market, price),
            )
        )

    return tuple(readings)


def _market_tick(exchange: Exchange, price: float) -> float | None:
    """spread_table refuses rather than extrapolating — an unmapped exchange
    raises KeyError and a price past HKEX's published ceiling raises ValueError.
    Both mean "no authoritative answer", which the reading renders as its own
    sentence; swallowing them into a guessed tick would put a number the
    exchange never published next to one it did."""
    try:
        return price_tick(exchange, price)
    except (KeyError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Clearing positions
# ---------------------------------------------------------------------------


def count_open_positions(positions: Iterable[PositionData]) -> int:
    """Positions ``clear_position()`` would actually change.

    ``PaperEngine.positions`` is keyed (vt_symbol, direction) and holds a
    zero-volume entry for every symbol ever touched — ``get_position`` creates
    one on first lookup and nothing ever removes it. Counting the raw dict would
    tell a trader who holds two positions that eleven are about to be destroyed,
    which trains them to ignore the number. isfinite first for the same reason
    as everywhere else: a NaN volume is not zero, and `!= 0` on NaN is True by
    accident rather than by meaning.
    """
    return sum(1 for p in positions if math.isfinite(p.volume) and p.volume != 0)


def describe_clear_impact(count: int, data_filename: str) -> str:
    """The confirmation body. Names the count and the file because those are the
    two facts a trader needs to decide, and states the irreversibility in the
    dialog rather than trusting the button label — 「清空所有持仓」 reads like a
    view filter to anyone who has not read the engine."""
    if count == 0:
        return (
            f"当前没有非零持仓。执行后只会把空持仓重写进 {data_filename}，账面不变。"
        )
    return (
        f"将把 {count} 个非零持仓的数量、冻结与均价全部归零，并立刻写盘到 "
        f"{data_filename}。\n\n"
        f"这一步不可撤销：模拟账户没有第二份持仓副本，写盘之后连上一秒的状态都取不回来。"
    )


# ---------------------------------------------------------------------------
# The panel
# ---------------------------------------------------------------------------


class PaperAccountWidget(QtWidgets.QWidget):
    """Registered in mainwindow's APP_WIDGET_OVERRIDES under "PaperAccount"."""

    def __init__(self, main_engine: MainEngine, event_engine: EventEngine) -> None:
        super().__init__()

        self.main_engine: MainEngine = main_engine
        self.event_engine: EventEngine = event_engine

        engine: PaperEngine | None = main_engine.get_engine(APP_NAME)
        if engine is None:
            # Raising here surfaces as mainwindow's "界面加载失败，已跳过" log and
            # one missing nav item. A widget that carried on with engine=None
            # would instead paint a full settings page whose switches write
            # nowhere — the failure mode where the trader believes the account
            # is configured.
            raise RuntimeError(
                f"模拟账户引擎未加载 —— main_engine 里找不到 {APP_NAME},"
                f"界面无从读写设置"
            )
        self.paper_engine: PaperEngine = engine

        self.init_ui()

    def init_ui(self) -> None:
        self.setWindowTitle("模拟交易")

        self.slippage_spin: SpinBox = SpinBox()
        self.slippage_spin.setMinimum(0)
        self.slippage_spin.setSuffix(" 跳")
        self.slippage_spin.setValue(self.paper_engine.get_trade_slippage())

        self.interval_spin: SpinBox = SpinBox()
        self.interval_spin.setMinimum(1)
        self.interval_spin.setSuffix(" 秒")
        self.interval_spin.setValue(self.paper_engine.get_timer_interval())

        self.instant_switch: SwitchButton = SwitchButton(
            parent=self, indicatorPos=IndicatorPosition.RIGHT
        )
        self.instant_switch.setOnText("开")
        self.instant_switch.setOffText("关")
        self.instant_switch.setChecked(self.paper_engine.get_instant_trade())

        # Signals connected only after the initial values are in place. Every
        # setter calls save_setting() → save_json(), so connecting first would
        # rewrite paper_account_setting.json with its own contents on each open —
        # harmless in effect, but it puts a disk write with no user intent behind
        # it into the middle of window construction.
        self.slippage_spin.valueChanged.connect(self._on_slippage_changed)
        self.interval_spin.valueChanged.connect(self._on_interval_changed)
        self.instant_switch.checkedChanged.connect(self._on_instant_changed)

        slippage_card = SettingCard(FIF.SPEED_HIGH, "市价与停止委托的成交滑点", SLIPPAGE_HINT)
        _attach(slippage_card, self.slippage_spin)

        interval_card = SettingCard(FIF.STOP_WATCH, "持仓盈亏的计算频率", INTERVAL_HINT)
        _attach(interval_card, self.interval_spin)

        instant_card = SettingCard(FIF.SEND, "下单后立即用当前盘口撮合", INSTANT_HINT)
        _attach(instant_card, self.instant_switch)

        match_group = SettingCardGroup("撮合", self)
        match_group.addSettingCard(slippage_card)
        match_group.addSettingCard(interval_card)
        match_group.addSettingCard(instant_card)

        self.clear_card: PushSettingCard = PushSettingCard(
            "清空所有持仓", FIF.DELETE, "清空所有持仓", CLEAR_HINT
        )
        self.clear_card.clicked.connect(self._on_clear_clicked)

        danger_group = SettingCardGroup("危险操作", self)
        danger_group.addSettingCard(self.clear_card)

        body_layout = QtWidgets.QVBoxLayout()
        body_layout.setContentsMargins(36, 24, 36, 24)
        body_layout.setSpacing(20)
        body_layout.addWidget(match_group)
        body_layout.addWidget(self._build_reading_card())
        body_layout.addWidget(danger_group)
        body_layout.addStretch(1)

        body = QtWidgets.QWidget()
        body.setLayout(body_layout)
        body.setObjectName("paperAccountBody")

        # setWidgetResizable(True) plus no setFixedWidth/Height anywhere is the
        # whole fix for the screenshot: the page grows with the window and the
        # cards stretch across it instead of leaving the right two thirds empty.
        scroll = SingleDirectionScrollArea(self, orient=QtCore.Qt.Orientation.Vertical)
        scroll.setWidgetResizable(True)
        scroll.setWidget(body)
        scroll.enableTransparentBackground()
        scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)

        layout = QtWidgets.QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(scroll)
        self.setLayout(layout)

        self.refresh_readings()

    def _build_reading_card(self) -> SimpleCardWidget:
        """The market-by-market slippage readout.

        A SimpleCardWidget rather than another SettingCard: SettingCard is a
        fixed 70px single-line row (see its __init__), and this text is a
        wrapped paragraph plus one line per market that would be clipped there.
        """
        title = StrongBodyLabel("一跳到底是多少 —— 港股与美股分开核对")

        explain = BodyLabel(
            "撮合价是「盘口 ± 跳数 × contract.pricetick」。pricetick 由报价网关写入，"
            "两个网关的口径并不一致：uSMART 从行情回填，美股读数就是真实的 0.01；"
            "Futu 写的是该市场的地板价位（港股 0.001、美股 0.0001），那是价位表的下界"
            "而不是当前价位的档 —— 港股 700 在 400 港元附近真实档位是 0.2，"
            "按 0.001 折算的滑点只有真实一档的百分之几，这个设置在那一侧形同虚设。"
        )
        explain.setWordWrap(True)

        self.reading_label: BodyLabel = BodyLabel("")
        self.reading_label.setWordWrap(True)

        layout = QtWidgets.QVBoxLayout()
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(8)
        layout.addWidget(title)
        layout.addWidget(explain)
        layout.addWidget(self.reading_label)

        card = SimpleCardWidget(self)
        card.setLayout(layout)
        return card

    def refresh_readings(self) -> None:
        readings: tuple[SlippageReading, ...] = read_slippage(
            self.slippage_spin.value(),
            self.main_engine.get_all_contracts(),
            self.main_engine.get_tick,
        )
        self.reading_label.setText("\n".join(reading.describe() for reading in readings))

    def showEvent(self, event: QtGui.QShowEvent) -> None:
        super().showEvent(event)
        self.refresh_readings()

    def _on_slippage_changed(self, value: int) -> None:
        self.paper_engine.set_trade_slippage(value)
        self.refresh_readings()

    def _on_interval_changed(self, value: int) -> None:
        self.paper_engine.set_timer_interval(value)

    def _on_instant_changed(self, checked: bool) -> None:
        self.paper_engine.set_instant_trade(checked)

    def _on_clear_clicked(self) -> None:
        count: int = count_open_positions(self.paper_engine.positions.values())

        box = MessageBox(
            "清空所有模拟持仓",
            describe_clear_impact(count, self.paper_engine.data_filename),
            self,
        )
        box.yesButton.setText("确认清空")
        box.cancelButton.setText("取消")

        if not box.exec():
            return

        self.paper_engine.clear_position()
        self.main_engine.write_log(f"模拟账户已清空 {count} 个非零持仓并写盘")


def _attach(card: SettingCard, widget: QtWidgets.QWidget) -> None:
    """Right-align a control inside a SettingCard, matching the 16px trailing
    margin PushSettingCard uses for its own button so the whole column lines up."""
    card.hBoxLayout.addWidget(widget, 0, QtCore.Qt.AlignmentFlag.AlignRight)
    card.hBoxLayout.addSpacing(16)
