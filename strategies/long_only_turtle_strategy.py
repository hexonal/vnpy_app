"""Long-only Turtle for cash equities (HK/US).

Learned from the vnpy-forum Turtle deep-dive (topic 27), which shows the
built-in TurtleSignalStrategy is a *stripped* version — it drops the two
pieces that make the real Turtle robust:

  1. Capital-based unit sizing (the forum's TurtlePortfolio): each unit risks
     a fixed % of account capital per 1N (ATR) adverse move, instead of a
     hard-coded fixed_size=1. Rounded to the stock's board lot.
  2. The last-trade filter (the forum's "上一笔盈利则跳过入场"): after a WINNING
     trade, the ordinary 20-day breakout is ignored; re-entry then requires a
     stronger 55-day breakout (Turtle System 2, the always-takeable failsafe
     that prevents the naive "skip forever after a win" deadlock). This is the
     Turtle's main whipsaw suppressor.

Plus the cash-equity adaptation: LONG ONLY. The short side of the built-in
(short/cover, and reversing on a down-break) is removed — HK/US cash accounts
can't freely short. Exits flatten to cash, never reverse.

HONESTY (per this project's doctrine): this is a textbook public breakout
system — risk/discipline scaffolding (structural stop + add-only-to-winners +
mechanical exit), NOT a proven alpha engine. Donchian breakout edge on liquid
names has decayed; naive Turtle likely nets <=0 on ranging stocks after cost.
Treat as a BASELINE; it must clear a cost-inclusive walk-forward before it's
called anything more, and real edge has to come from filters layered on top.
"""

from vnpy_ctastrategy import (
    ArrayManager,
    BarData,
    BarGenerator,
    CtaTemplate,
    Direction,
    OrderData,
    StopOrder,
    TickData,
    TradeData,
)


class LongOnlyTurtleStrategy(CtaTemplate):
    """Long-only Turtle with capital-based sizing + the last-trade filter."""

    author = "hexonal fork"

    # --- parameters ---
    entry_window: int = 20        # System 1 Donchian entry (bars)
    breakout_window: int = 55     # System 2 failsafe breakout (always takeable)
    exit_window: int = 10         # Donchian exit (bars)
    atr_window: int = 20          # N = ATR window
    trading_capital: float = 100000.0   # account capital used for unit sizing
    risk_percent: float = 1.0     # % of capital risked per unit per 1N move
    board_lot: int = 1            # shares per lot (HK: 100/500/...; US: 1)
    max_units: int = 4            # pyramiding cap
    atr_stop: float = 2.0         # stop = entry - atr_stop * N

    # --- variables (shown in the GUI) ---
    entry_up: float = 0           # 20-day high (System 1)
    breakout_up: float = 0        # 55-day high (System 2)
    exit_down: float = 0          # 10-day low (exit)
    atr_value: float = 0
    unit_size: int = 0            # shares per unit (board-lot rounded)
    long_stop: float = 0
    last_trade_pnl: float = 0     # PnL of the last completed round-trip

    parameters = [
        "entry_window", "breakout_window", "exit_window", "atr_window",
        "trading_capital", "risk_percent", "board_lot", "max_units", "atr_stop",
    ]
    variables = [
        "entry_up", "breakout_up", "exit_down", "atr_value", "unit_size",
        "long_stop", "last_trade_pnl",
    ]

    def on_init(self) -> None:
        self.write_log("策略初始化")
        self.bg: BarGenerator = BarGenerator(self.on_bar)
        # ArrayManager must hold enough bars for the widest window (55-day).
        self.am: ArrayManager = ArrayManager(size=self.breakout_window + 10)

        # Frozen breakout level to pyramid on while a position is open, and
        # the open-position cost tracker for round-trip PnL (the filter input).
        self._pyramid_base: float = 0
        self._entry_cost: float = 0.0   # Σ price*volume of open long fills
        self._entry_vol: float = 0.0    # Σ volume of open long fills
        self._round_pnl: float = 0.0    # realized PnL accumulating this round-trip

        self.load_bar(self.breakout_window + 10)

    def on_start(self) -> None:
        self.write_log("策略启动")

    def on_stop(self) -> None:
        self.write_log("策略停止")

    def on_tick(self, tick: TickData) -> None:
        self.bg.update_tick(tick)

    def on_bar(self, bar: BarData) -> None:
        self.cancel_all()

        self.am.update_bar(bar)
        if not self.am.inited:
            return

        # Entry channels are frozen while flat (recomputed only with no pos).
        if not self.pos:
            self.entry_up, _ = self.am.donchian(self.entry_window)
            self.breakout_up, _ = self.am.donchian(self.breakout_window)

        _, self.exit_down = self.am.donchian(self.exit_window)

        if not self.pos:
            self.atr_value = self.am.atr(self.atr_window)
            self.unit_size = self._compute_unit()
            self.long_stop = 0

            # Last-trade filter: after a winner, only a stronger 55-day
            # breakout re-enters (System 2 failsafe); otherwise the 20-day
            # breakout (System 1). System 2 is always takeable, so this never
            # deadlocks.
            entry_price: float = self.breakout_up if self.last_trade_pnl > 0 else self.entry_up
            self._pyramid_base = entry_price
            self.send_buy_orders(entry_price)

        elif self.pos > 0:
            # Pyramid more units on the same breakout base, and protect with a
            # sell stop at the tighter of the ATR stop / 10-day exit channel.
            self.send_buy_orders(self._pyramid_base)
            sell_price: float = max(self.long_stop, self.exit_down)
            self.sell(sell_price, abs(self.pos), True)

        self.put_event()

    def on_trade(self, trade: TradeData) -> None:
        if trade.direction == Direction.LONG:
            # A new unit filled — advance the ATR stop off the latest entry and
            # accumulate the open-position cost for round-trip PnL.
            self.long_stop = trade.price - self.atr_stop * self.atr_value
            self._entry_cost += trade.price * trade.volume
            self._entry_vol += trade.volume
        else:
            # A close (sell) fill — realize PnL on the closed shares.
            if self._entry_vol > 0:
                avg_entry: float = self._entry_cost / self._entry_vol
                self._round_pnl += (trade.price - avg_entry) * trade.volume
                self._entry_cost -= avg_entry * trade.volume
                self._entry_vol -= trade.volume

            if not self.pos:  # round-trip fully closed → feed the filter
                self.last_trade_pnl = self._round_pnl
                self._round_pnl = 0.0
                self._entry_cost = 0.0
                self._entry_vol = 0.0

    def on_order(self, order: OrderData) -> None:
        pass

    def on_stop_order(self, stop_order: StopOrder) -> None:
        pass

    def _compute_unit(self) -> int:
        """Turtle unit size in shares: risk risk_percent of capital per 1N
        move, rounded down to whole board lots. For a stock, $ volatility per
        share is N (1 price point = $1/share). Returns 0 when capital can't
        cover even one lot — the strategy then simply doesn't enter (no
        sub-lot orders)."""
        if self.atr_value <= 0 or self.board_lot <= 0:
            return 0
        risk_budget: float = self.trading_capital * self.risk_percent / 100.0
        raw_shares: float = risk_budget / self.atr_value
        lots: int = int(raw_shares // self.board_lot)
        return lots * self.board_lot

    def send_buy_orders(self, price: float) -> None:
        """Place up to max_units buy stops, staggered by 0.5N, each one unit —
        classic Turtle pyramiding, generalized from the built-in's hard-coded
        four levels to max_units units of unit_size shares."""
        if self.unit_size <= 0:
            return

        t: float = self.pos / self.unit_size
        for i in range(self.max_units):
            if t < i + 1:
                self.buy(price + self.atr_value * 0.5 * i, self.unit_size, True)
