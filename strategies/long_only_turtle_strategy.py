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

STATE LAYERING (forum topic 32750, item 3)
------------------------------------------
The first version of this file kept `_pyramid_base`, `_entry_cost`,
`_entry_vol` and `_round_pnl` as plain attributes created in `on_init`. They
were therefore absent from `variables`, and CtaEngine only restores names
listed there (engine.py:689) — so every restart silently reset them:

  * `_pyramid_base` was re-derived from the *current* 20-day Donchian high
    instead of the frozen entry breakout, moving every pyramid add level.
  * `_entry_cost` / `_entry_vol` went to zero, so `on_trade` skipped the PnL
    block on the closing fill and recorded `last_trade_pnl = 0` for a trade
    that actually won — silently disabling the whipsaw filter for the next
    entry.

They are now declared in `internal_vars`: persisted like everything else, but
kept out of the GUI. Operator-facing values stay in `display_vars`.

HONESTY (per this project's doctrine): this is a textbook public breakout
system — risk/discipline scaffolding (structural stop + add-only-to-winners +
mechanical exit), NOT a proven alpha engine. Donchian breakout edge on liquid
names has decayed; naive Turtle likely nets <=0 on ranging stocks after cost.
Treat as a BASELINE; it must clear a cost-inclusive walk-forward before it's
called anything more, and real edge has to come from filters layered on top.
"""

# Imported as a module, not `from ... import StatefulCtaTemplate`: CtaEngine's
# class scanner registers every CtaTemplate subclass it finds in dir(module),
# so an imported base class would show up in the "add strategy" dropdown.
from datetime import time as _time

import strategy_state

from vnpy.trader.constant import Exchange, Interval
from vnpy_ctastrategy import (
    ArrayManager,
    BarData,
    BarGenerator,
    Direction,
    OrderData,
    StopOrder,
    TickData,
    TradeData,
)

# Local close, used to tell BarGenerator where a trading day ends when it
# aggregates minute bars into daily ones. Both markets close at 16:00 local
# (HKEX 16:00 HKT after the closing auction, US 16:00 ET regular session).
_MARKET_CLOSE: dict[Exchange, _time] = {
    Exchange.SEHK: _time(16, 0),
    Exchange.SMART: _time(16, 0),
}


class LongOnlyTurtleStrategy(strategy_state.StatefulCtaTemplate):
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

    # --- state: persisted AND shown in the GUI (frozen values, unrecoverable) ---
    entry_up: float = 0.0         # 20-day high (System 1), frozen while in a position
    breakout_up: float = 0.0      # 55-day high (System 2), frozen while in a position
    atr_value: float = 0.0        # N, frozen at entry
    unit_size: int = 0            # shares per unit (board-lot rounded)
    long_stop: float = 0.0        # ATR stop off the most recent unit's fill
    last_trade_pnl: float = 0.0   # PnL of the last completed round-trip

    # --- derived: shown, recomputed every bar, deliberately NOT persisted ---
    exit_down: float = 0.0        # 10-day low (exit channel)

    # --- state: persisted, hidden from the GUI ---
    _pyramid_base: float = 0.0    # breakout price the add ladder hangs off
    _entry_cost: float = 0.0      # Σ price*volume of open long fills
    _entry_vol: float = 0.0       # Σ volume of open long fills
    _round_pnl: float = 0.0       # realized PnL accumulating this round-trip

    # --- transient: rebuilt every process start, never persisted ---
    _rearm_pending: bool = False  # re-place the protective stop after a restart

    parameters = [
        "entry_window", "breakout_window", "exit_window", "atr_window",
        "trading_capital", "risk_percent", "board_lot", "max_units", "atr_stop",
    ]
    display_vars = [
        "entry_up", "breakout_up", "atr_value", "unit_size",
        "long_stop", "last_trade_pnl",
    ]
    # _pyramid_base IS persisted, on purpose. It is the breakout price frozen at
    # entry, and a history replay cannot recover it — on_init would re-derive the
    # *current* Donchian high instead, so a restarted process would hang its
    # ladder off a different price than the process it replaced. That breaks the
    # invariant this whole state kit exists for: restart while holding must
    # reproduce the same orders.
    #
    # The danger the frozen value creates — a gap past base + 1.5N while the
    # process was down leaves every rung under the market, and the local stop
    # engine (tick.last_price >= stop.price) fires them ALL on the first tick
    # (measured: three rungs meant for 102/103/104 filling at 146.05) — is
    # handled where it actually belongs, in send_buy_orders, which drops any
    # rung already below the market. Dropping persistence instead would trade
    # this bug for restart non-determinism and leave the same disaster reachable
    # in a continuous session that gaps.
    internal_vars = [
        "_pyramid_base", "_entry_cost", "_entry_vol", "_round_pnl",
    ]
    # exit_down is recomputed unconditionally on every bar, so replaying history
    # through on_init rebuilds it exactly. Persisting it would restore whatever
    # it was at the last fill (possibly weeks of bars ago) on top of the correct
    # fresh value — and that stale number is what the post-restart protective
    # stop would be armed with.
    derived_vars = ["exit_down"]
    transient_vars = ["am", "bg", "_rearm_pending"]

    state_version = 1

    def on_init(self) -> None:
        self.write_log("策略初始化")
        # DAILY, not the default MINUTE. Every window on this strategy is a day
        # count — entry_window=20 means a 20-DAY Donchian, and load_bar's first
        # argument is days, so 65 only means "65 bars" when the interval is
        # daily. With the default BarGenerator(self.on_bar) the whole thing ran
        # on 1-minute bars: measured on 700.HK, ATR(20) was 0.29 (0.068% of
        # price) instead of 17.88 (4.1%), which sized a unit at 3400 shares —
        # 14.8x capital per unit, 59x at max_units=4. On daily bars the same
        # parameters size a unit at 0 shares (HKD 100k cannot buy one lot at
        # that N) and the strategy correctly declines to enter.
        exchange: Exchange = Exchange(self.vt_symbol.split(".")[-1])
        self.bg: BarGenerator = BarGenerator(
            self.on_minute_bar,
            1,
            self.on_daily_bar,
            Interval.DAILY,
            daily_end=_MARKET_CLOSE.get(exchange, _time(16, 0)),
        )
        # ArrayManager must hold enough bars for the widest window (55-day).
        self.am: ArrayManager = ArrayManager(size=self.breakout_window + 10)
        self._rearm_pending = False

        # NOTE: the persisted fields are deliberately NOT initialised here.
        # Replaying history through on_bar below runs with pos == 0 (the engine
        # restores pos only after on_init returns), which would clobber them;
        # the engine's restore loop and the state sidecar put the real values
        # back immediately afterwards.
        self.load_bar(self.breakout_window + 10, interval=Interval.DAILY)

    def on_ready(self, restored: bool) -> None:
        """Post-restore checkpoint — runs before this process sends any order."""
        if not restored:
            return

        self.write_log(
            f"策略状态已恢复: pos={self.pos} 入场基准={self._pyramid_base:.3f} "
            f"持仓成本={self._entry_cost:.2f}/{self._entry_vol:.0f}股 "
            f"N={self.atr_value:.3f} 止损={self.long_stop:.3f}"
        )

        if not self.pos:
            return

        # A restart leaves no orders in the engine, so the protective sell stop
        # is gone. on_bar re-places it, but on a daily strategy that can be a
        # whole session away — re-arm on the first tick after start instead.
        self._rearm_pending = True

        broker_pos = self.broker_net_position()
        if broker_pos is not None and abs(broker_pos - self.pos) > 1e-9:
            self.write_log(
                f"[风控] 持仓不一致: 策略 {self.pos} vs 券商 {broker_pos}。"
                f"先人工核对再启动交易。"
            )

    def on_reset(self, reason: str) -> None:
        self.write_log(f"策略状态已重置 ({reason})")
        self._rearm_pending = False

    def on_start(self) -> None:
        self.write_log("策略启动")

    def on_stop(self) -> None:
        self.write_log("策略停止")

    def on_tick(self, tick: TickData) -> None:
        self._rearm_protective_stop()
        self.bg.update_tick(tick)

    def _rearm_protective_stop(self) -> None:
        """Re-place the exit stop that the restart dropped, before the next bar."""
        if not self._rearm_pending:
            return
        if not self.trading or self.pos <= 0:
            return

        sell_price: float = max(self.long_stop, self.exit_down)
        if sell_price <= 0:
            return  # nothing trustworthy to arm with; on_bar will rebuild it

        self._rearm_pending = False
        self.sell(sell_price, abs(self.pos), True)
        self.write_log(f"重启后补挂保护性止损 {sell_price:.3f} x {abs(self.pos):.0f}")

    def on_bar(self, bar: BarData) -> None:
        """Whatever the engine feeds us, funnel it to one daily code path.

        Backtesting with interval=DAILY calls this with daily bars directly and
        never touches BarGenerator; live trading calls it with the 1-minute bars
        BarGenerator built from ticks. Routing both here is what keeps backtest
        and live on the SAME timeframe — previously the backtest ran daily while
        live ran on minutes, so what was validated was not what would trade.
        """
        if bar.interval is Interval.DAILY:
            self.on_daily_bar(bar)
        else:
            self.bg.update_bar(bar)

    def on_minute_bar(self, bar: BarData) -> None:
        """1-minute bars from ticks; only useful as input to the daily window."""
        self.bg.update_bar(bar)

    def on_daily_bar(self, bar: BarData) -> None:
        self._rearm_pending = False   # about to rebuild the full order set anyway
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
            self.long_stop = 0.0

            # Last-trade filter: after a winner, only a stronger 55-day
            # breakout re-enters (System 2 failsafe); otherwise the 20-day
            # breakout (System 1). System 2 is always takeable, so this never
            # deadlocks.
            entry_price: float = self.breakout_up if self.last_trade_pnl > 0 else self.entry_up
            self._pyramid_base = entry_price
            self.send_buy_orders(entry_price, bar.close_price)

        elif self.pos > 0:
            # Pyramid more units on the same breakout base, and protect with a
            # sell stop at the tighter of the ATR stop / 10-day exit channel.
            self.send_buy_orders(self._pyramid_base, bar.close_price)
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

    def send_buy_orders(self, price: float, market_price: float = 0.0) -> None:
        """Place up to max_units buy stops, staggered by 0.5N, each one unit —
        classic Turtle pyramiding, generalized from the built-in's hard-coded
        four levels to max_units units of unit_size shares.

        `market_price` is the latest close. A rung sitting BELOW the market is
        not a breakout order at all: the local stop engine triggers on
        tick.last_price >= stop.price, so it fills at market on the very next
        tick. Measured case — a restart across a gap left the ladder anchored at
        101 while price was 146, and all three rungs (102/103/104) filled at
        146.05 on one tick, 1500 shares against 100k capital.

        A rung exactly AT the market is left alone: that is an ordinary breakout
        follow-through (price has just reached the Donchian high) and filling it
        is the intended behaviour. Only strictly-below rungs are dropped.
        """
        if self.unit_size <= 0:
            return

        t: float = self.pos / self.unit_size
        for i in range(self.max_units):
            if t >= i + 1:
                continue
            stop_price: float = price + self.atr_value * 0.5 * i
            if market_price > 0 and stop_price < market_price - 1e-9:
                self.write_log(
                    f"跳过第 {i + 1} 档加仓 {stop_price:.3f}：已在市价 {market_price:.3f} "
                    f"之下，挂出去会立刻以市价成交而不是突破跟进"
                )
                continue
            self.buy(stop_price, self.unit_size, True)
