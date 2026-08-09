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

HK AND US ARE BOTH FIRST-CLASS
------------------------------
This strategy shipped into the GUI dropdown (fe9cfb5) carrying three market
assumptions that had never been exercised, each of them right on exactly one
of the two markets it claims to trade. All three now read the same shared
sources the rest of this fork reads, so neither market is the special case:

  * **Board lot comes from the contract.** `board_lot` was a parameter
    defaulting to 1 — correct for US, wrong for every HK line, and the true
    value was already sitting in the OMS (both gateways write
    ``size=1, min_volume=<每手股数>``; vnpy_usmart/gateway.py:748 and
    vnpy_futu/futu_mapping.py:326). The parameter survives only as the
    fallback for when no contract has been pushed — a backtest, or a live
    session where the gateway has not finished querying instruments.
  * **The daily boundary comes from the session table.** It used to be a
    two-row dict copied into this file, with `.get(exchange, 16:00)` — so an
    exchange nobody had mapped got a silent, invented close. It is now
    derived from `vnpy_gatewaykit.sessions`, which is the single source of
    truth market_clock/query_window/tick_filter all already consume, and an
    unmapped exchange is refused rather than defaulted. That is the same
    policy market_tz has held since it was written: 宁可拒绝也不让错误的默认值
    静默生效。
  * **Only continuous-session ticks reach the strategy.** See the next
    section — this is the one that could lose money today.

EXTENDED HOURS (the US-only landmine)
-------------------------------------
`on_tick` used to hand every tick straight to the BarGenerator. HK has no
continuous pre/post session, so the defect could not show on 0700.SEHK — the
only symbol this file was ever tested against. US does: futu subscribes SMART
with `extended_time=True`, which delivers 04:00–09:30 and 16:00–20:00 ET
prints. Two distinct consequences, and they need two distinct fixes because
they happen in two different processes' worth of code:

  (a) **Bar contamination.** A thin 04:12 print becomes the day's open, and a
      single after-hours cross becomes the high. Donchian channels and ATR
      then describe prices at which the regular session never traded. Fixed
      here: `on_tick` admits a tick only when
      `sessions.is_open(exchange, tick.datetime, kinds={REGULAR})`.
  (b) **Local stop orders firing on an extended print.** This one is NOT
      reachable from `on_tick`: `CtaEngine.check_stop_order` (engine.py:230)
      walks `self.stop_orders` on *every* tick event, before any strategy
      callback, and triggers on `tick.last_price >= stop.price`. Filtering
      our own `on_tick` does nothing about it — measured shape of the
      disaster: four pyramid rungs meant for 102/103/104 all filling on one
      04:12 print. The only lever a strategy has is to **not leave orders
      resting outside the session**, so the first tick at or after the day's
      regular close cancels everything and sets `_rearm_pending`; the first
      regular tick of the next day re-arms the protective sell, and the
      following daily bar rebuilds the ladder.

`vnpy_gatewaykit.tick_filter.TickFilterMixin` was read before writing this and
deliberately not used. It is gateway-side by construction ("Mix in BEFORE
BaseGateway … replace every `self.on_tick(tick)` call site with
`self.push_tick(...)`"), because it needs the broker status fields that exist
only where the tick is produced. Three consequences make it the wrong tool
here: a strategy cannot reach those call sites; its default mode is OBSERVE,
which drops nothing, so adopting it without also writing an enforcement policy
would be decoration; and enforcing PHASE_NOT_ALLOWED at the gateway would
remove extended ticks from *every* consumer on the bus, including the recorder
that is supposed to store them. What this strategy needs is a pure function of
(exchange, instant), and `sessions.active_session` is exactly that — the same
function `TickFilter._clock_phase` itself calls. So the truth source is
reused; the enforcement engine is not.

Cancelling at the close is a real behaviour change with a real cost, stated
plainly: between 16:00 and the next day's first regular tick this strategy
holds a position with no resting protective order. That is deliberate. The
protective sell is a *local* stop — it lives in this process, not at the
broker — and firing it on a 04:12 print means selling into the thinnest book
of the day at a price the regular session never confirmed. Re-arming on the
first regular tick restores protection within one tick of the open, and a
genuine overnight gap triggers the re-armed stop immediately anyway.

DAILY BAR COMPLETION MOVED
--------------------------
Dropping the post-close ticks also drops what used to complete the daily bar:
`BarGenerator.update_bar_daily_window` flushes on `bar.datetime.time() >=
daily_end`, and with 16:00 as the boundary the flush was being driven by the
very extended-hours prints (US) and closing-auction prints (HK) that are now
refused. Completion therefore falls through to the fork's date-rollover flush
(vnpy/trader/utility.py:467-482), which fires on the first minute bar of the
next session — measured effect: `on_daily_bar` runs at about 09:31 local
instead of 16:00 local. This is not a new code path being invented for the
occasion; that rollover flush was added precisely because "any session that
never produced a bar stamped exactly daily_end" was merging two days into one,
and a thin name with no after-hours print already took it. The cost is that
the ladder is rebuilt one minute into the session rather than the evening
before, so a breakout inside the opening minute is missed; the orders resting
through that minute are the previous day's, which is what would have been
resting at 09:30 under the old scheme too.

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
import math
from datetime import date, datetime
from datetime import time as _time

from vnpy.trader.constant import Exchange, Interval
from vnpy.trader.object import ContractData
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
from vnpy_gatewaykit.market_clock import market_tz
from vnpy_gatewaykit.sessions import SessionKind, day_close, is_open

import strategy_state

# The only session kind this strategy will look at. Declared once because the
# three places that ask (the tick gate, the close detector, the daily boundary)
# must not be able to drift apart — that drift is how the strategy would end up
# building bars out of one set of windows and closing its day on another.
_REGULAR_ONLY: frozenset[SessionKind] = frozenset({SessionKind.REGULAR})

# A fixed Monday, used for one question only: "what local wall-clock time does
# the continuous session end". A date is needed because sessions.day_close
# resolves windows per calendar day, and the answer must not depend on when the
# strategy happens to be constructed (a Sunday would answer None). Local close
# times do not move with DST — 16:00 ET is 16:00 ET in both halves of the year,
# which is the whole reason sessions.py stores local times rather than offsets —
# so any trading day gives the same answer and a constant is honest here.
_CLOSE_PROBE_DAY: date = date(2026, 1, 5)


def _resolve_exchange(vt_symbol: str) -> Exchange:
    """Exchange out of the vt_symbol suffix, refusing anything unrecognised.

    Refusing rather than guessing matters more than it looks: every downstream
    market fact this strategy needs — timezone, session windows, the daily
    boundary — is keyed on this value, and a wrong key produces plausible
    numbers rather than an error.
    """
    suffix: str = vt_symbol.split(".")[-1]
    try:
        return Exchange(suffix)
    except ValueError as exc:
        raise ValueError(
            f"无法从 vt_symbol 解析交易所，本策略拒绝按默认交易所继续，收到 {vt_symbol!r}"
        ) from exc


def _regular_close(exchange: Exchange) -> _time:
    """Local time at which the exchange's continuous session ends.

    HK answers 16:00 HKT (the 16:00–16:10 closing auction is a separate
    AUCTION window and is not part of continuous trading), US answers 16:00
    ET. The two agreeing is a coincidence of these two markets, not a licence
    to hardcode it — the previous version of this file hardcoded exactly that
    agreement plus a `.get(exchange, 16:00)` default, and the default is what
    would have shipped a wrong daily boundary the day someone added a third
    market. An unmapped exchange raises instead.

    The KeyError caught below can come from either of the two maps — sessions
    has no window row, or market_clock has no timezone (sessions.windows asks
    market_tz first). Both are the same operator action, so they get one
    message that names both files rather than two that differ by which map was
    reached first.
    """
    try:
        close: datetime | None = day_close(exchange, _CLOSE_PROBE_DAY, kinds=_REGULAR_ONLY)
    except KeyError as exc:
        raise ValueError(
            f"该交易所没有交易时段定义，本策略拒绝按 16:00 兜底运行；请先在 "
            f"vnpy_gatewaykit 的 sessions._SESSIONS 与 market_clock._MARKET_TZ_NAME "
            f"里补齐，收到 {exchange.value!r}"
        ) from exc
    if close is None:
        raise ValueError(
            f"该交易所没有连续竞价时段，本策略只在连续盘交易，收到 {exchange.value!r}"
        )
    return close.time()


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
    board_lot: int = 1            # FALLBACK lot only; the contract wins (_resolve_board_lot)
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
    effective_lot: int = 0        # board lot actually in force (contract, else param)

    # --- state: persisted, hidden from the GUI ---
    _pyramid_base: float = 0.0    # breakout price the add ladder hangs off
    _entry_cost: float = 0.0      # Σ price*volume of open long fills
    _entry_vol: float = 0.0       # Σ volume of open long fills
    _round_pnl: float = 0.0       # realized PnL accumulating this round-trip

    # --- transient: rebuilt every process start, never persisted ---
    _rearm_pending: bool = False  # re-place the protective stop after a restart
    # Resolved once in on_init, and the readiness sentinel for everything that
    # needs a market clock. It is assigned LAST, after bg/am exist, so that
    # `_exchange is not None` means "this object is fully built" — which matters
    # because CtaEngine._init_strategy sets `inited = True` unconditionally
    # (engine.py:763) even when call_strategy_func has just swallowed an
    # exception out of on_init. Without the sentinel, a strategy whose exchange
    # was refused would still be fed ticks and would die on a missing self.bg.
    _exchange: Exchange | None = None
    _naive_tick_logged: bool = False  # one line per process, not one per tick
    _cleared_date: str = ""       # ISO local date whose close already cancelled
    _lot_source: str = ""         # last logged provenance of the board lot
    # "a protective sell is believed to be resting". Written by the two places
    # that place one and cleared by the two that cancel, so `on_ready` can tell
    # a genuine restart (nothing resting) from its own late arrival.
    _stop_armed: bool = False

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
    # effective_lot joins it for the same reason from the other direction: it is
    # a fact about the *contract*, not about this strategy's history, and the
    # contract can legitimately change under us (an HK lot-size revision, or a
    # restart that resolves the symbol on a different gateway). Persisting it
    # would let a stale lot outlive the contract that produced it.
    derived_vars = ["exit_down", "effective_lot"]
    transient_vars = [
        "am", "bg", "_rearm_pending",
        "_exchange", "_naive_tick_logged", "_cleared_date", "_lot_source",
        "_stop_armed",
    ]

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
        #
        # The daily boundary is asked of the session table rather than declared
        # here. Both mapped markets answer 16:00 local, but the value now comes
        # from the same rows the tick gate below filters on, so "which windows
        # feed the bar" and "when does the bar close" cannot disagree.
        exchange: Exchange = _resolve_exchange(self.vt_symbol)
        daily_end: _time = _regular_close(exchange)
        self.bg: BarGenerator = BarGenerator(
            self.on_minute_bar,
            1,
            self.on_daily_bar,
            Interval.DAILY,
            daily_end=daily_end,
        )
        # ArrayManager must hold enough bars for the widest window (55-day).
        self.am: ArrayManager = ArrayManager(size=self.breakout_window + 10)
        self._rearm_pending = False
        self._stop_armed = False
        self._naive_tick_logged = False
        self._cleared_date = ""
        self._lot_source = ""
        self._exchange = exchange     # assigned last: it is the readiness sentinel

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
        #
        # `_stop_armed` guards against this callback arriving LATE. It is driven
        # by StatefulCtaTemplate._ensure_ready, which fires on the first
        # send_order or the first get_data of the process, whichever comes
        # first — and once the close-time cancel became a second producer of
        # `_rearm_pending`, "first send_order" could be the re-armed protective
        # sell itself. Setting the flag again from inside that very send makes
        # the next tick place a SECOND stop for the same shares; both trigger
        # together and the position flips short. Measured in
        # test_the_protective_stop_is_rearmed_on_the_next_sessions_first_regular_tick,
        # which failed on exactly this before the guard existed.
        if self._stop_armed:
            return
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
        self._stop_armed = False

    def on_start(self) -> None:
        self.write_log("策略启动")

    def on_stop(self) -> None:
        self.write_log("策略停止")

    def on_tick(self, tick: TickData) -> None:
        """Continuous-session ticks build bars; everything else is refused.

        The refusal is not "ignore and move on" for one case: the first tick at
        or after the day's continuous close takes the orders down, because a
        resting local stop is triggered by CtaEngine, not by this method, and
        CtaEngine does not know what a session is.
        """
        exchange: Exchange | None = self._exchange
        if exchange is None:
            return

        local: datetime | None = self._local_moment(tick, exchange)
        if local is None:
            return

        if is_open(exchange, local, kinds=_REGULAR_ONLY):
            self._rearm_protective_stop()
            self.bg.update_tick(tick)
            return

        self._clear_orders_after_regular_close(exchange, local)

    def _local_moment(self, tick: TickData, exchange: Exchange) -> datetime | None:
        """The tick's instant in the exchange's own wall clock, or None to drop.

        None means refuse, never default. A naive timestamp has no instant at
        all: `sessions._local` raises on one, and letting that raise here would
        be worse than dropping, because `call_strategy_func` would flip
        `inited`/`trading` off on the first such tick and take the position's
        protection with it. Dropping is logged exactly once per process — a
        feed that produces one naive tick produces every tick that way, and a
        per-tick line would bury the log rather than inform it.
        """
        if tick.datetime.tzinfo is None:
            if not self._naive_tick_logged:
                self._naive_tick_logged = True
                self.write_log(
                    f"丢弃裸时间戳行情：无法判定所处交易时段，收到 {tick.datetime!r}"
                )
            return None
        return tick.datetime.astimezone(market_tz(exchange))

    def _clear_orders_after_regular_close(
        self, exchange: Exchange, local: datetime
    ) -> None:
        """Take every resting order down once the continuous session is over.

        Deliberately keyed on the *day's* close and not on "not REGULAR". The
        naive version breaks HK and only HK: 12:00–13:00 is a genuine
        non-REGULAR hole, so cancelling on it would strip the ladder and the
        protective stop for the whole afternoon session and not rebuild them
        until the next day's bar. Asking `day_close` instead means both markets
        are handled by their own 16:00 and the HK lunch hour, the HK pre-open
        auction and the US pre-market all fall on the same "leave it alone"
        branch.

        `_rearm_pending` is reused rather than reinvented: the restart path
        already means exactly "a position exists and its protective stop does
        not", which is precisely the state a close-time cancel creates.
        """
        day_key: str = local.date().isoformat()
        if self._cleared_date == day_key:
            return

        close: datetime | None = day_close(exchange, local.date(), kinds=_REGULAR_ONLY)
        if close is None or local < close:
            # A non-trading day (nothing was armed), or still before the close —
            # the lunch break and the pre-market both land here.
            return

        self._cleared_date = day_key
        self.cancel_all()
        self._stop_armed = False
        if self.pos > 0:
            self._rearm_pending = True
            tail: str = f"持仓 {self.pos:.0f} 股的保护性止损将在下一个连续盘首笔行情补挂"
        else:
            tail = "当前无持仓"
        self.write_log(f"连续盘已收市 ({close:%Y-%m-%d %H:%M %Z})，撤走全部挂单；{tail}")

    def _rearm_protective_stop(self) -> None:
        """Re-place the exit stop that a restart — or the close-time cancel — dropped.

        Two producers now set `_rearm_pending`, and they want the same repair:
        a position is open and nothing is protecting it. Both are resolved on
        the first continuous-session tick rather than on the next daily bar,
        which on a daily strategy could otherwise be a whole session away.
        """
        if not self._rearm_pending:
            return
        if not self.trading or self.pos <= 0:
            return

        sell_price: float = max(self.long_stop, self.exit_down)
        if sell_price <= 0:
            return  # nothing trustworthy to arm with; on_bar will rebuild it

        # Both flags move BEFORE the send, not after: send_order is what runs
        # _ensure_ready, so on_ready can observe them mid-call. Recording the
        # intent early makes a failed send look like "armed but rejected",
        # which is what `_rearm_pending = False` already meant here.
        self._rearm_pending = False
        self._stop_armed = True
        self.sell(sell_price, abs(self.pos), True)
        self.write_log(f"补挂保护性止损 {sell_price:.3f} x {abs(self.pos):.0f}")

    def on_bar(self, bar: BarData) -> None:
        """Whatever the engine feeds us, funnel it to one daily code path.

        Backtesting with interval=DAILY calls this with daily bars directly and
        never touches BarGenerator; live trading calls it with the 1-minute bars
        BarGenerator built from ticks. Routing both here is what keeps backtest
        and live on the SAME timeframe — previously the backtest ran daily while
        live ran on minutes, so what was validated was not what would trade.

        The session gate that `on_tick` applies is deliberately NOT applied to
        the minute branch, and the reason is bar labelling rather than
        squeamishness. A tick timestamp is an instant, so "is this inside the
        continuous session" has one answer. A bar timestamp is a convention:
        `VNPY_BAR_LABEL_NORMALIZE` is off by default, so what is in the
        database is close-labelled, and a bar stamped 16:00 is the 15:59–16:00
        bar — the last regular minute, and also the one that trips
        `daily_end`. Gating on the label would delete the close and the day's
        completion together. The cost of leaving it ungated is that a
        minute-interval backtest of a US name still ingests extended-hours
        bars; the honest answer to that is that every window on this strategy
        is a day count and the daily interval is the validated path, so a
        minute backtest is measuring something this strategy does not do.
        Re-examine once bar labels are normalised across the history.
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
        self._stop_armed = False

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
            self._stop_armed = True
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

    def get_stop_price(
        self, vt_symbol: str, direction: Direction, price: float
    ) -> float | None:
        """Stop declared to the risk gate for one order: ``price - atr_stop * N``.

        This is the SAME number ``on_trade`` will write into ``long_stop`` once
        the order fills (``trade.price - self.atr_stop * self.atr_value``),
        computed one step early off the price about to be quoted.  The three
        in-house gates sit on ``MainEngine.send_order`` and need the stop
        *before* the order leaves, while ``on_trade`` only has it afterwards.
        ``atr_value`` is frozen for as long as a position is open — ``on_bar``
        recomputes it only in the flat branch — so the two agree to the last
        digit whenever the fill lands on the quote, for the first unit and for
        every pyramid add alike.

        Without this override the base class returns ``None``, no ``|stop=``
        suffix reaches the request, and 强制止损检查 refuses every entry.
        Measured end to end before this existed: four armed rungs, four
        refusals, zero orders at the gateway, ``pos`` stuck at 0 forever.

        Why the exit channel is deliberately not folded in
        --------------------------------------------------

        ``on_bar`` arms the protective sell at ``max(long_stop, exit_down)``, so
        the level actually resting in the market is often *above* this one —
        measured on a synthetic breakout: fill 103.75, N 2.05, this method
        declares 99.65 while the armed sell sits at 100.50, which is 3.25 versus
        4.10 of risk per share.  Declaring the ``max`` would match the resting
        order exactly and **understate** the risk, and that is the one direction
        this number must not err in:

        * ``max(long_stop, exit_down) >= long_stop`` always holds, so the ATR
          leg is the floor of every exit this strategy can take.  The gate asks
          what this order can lose; the honest answer is bounded by the floor,
          not by wherever the 10-day low happens to sit today.
        * Between a fill and the next daily close the channel leg is not in the
          market **at all** — ``on_bar`` re-arms the sell before the new unit
          exists, and ``_rearm_pending`` only covers a process restart, not this
          window.  For that stretch the ATR leg is the whole protection.
        * ``exit_down`` is a rolling minimum recomputed every bar and is in
          ``derived_vars`` precisely because it is not persisted; ``long_stop``
          is frozen at the fill.  Declaring off a drifting number would let the
          same order clear the gate today and fail it tomorrow.

        The cost is stated plainly: while the channel leg is the binding exit,
        the declared risk is **conservative**, and 单笔风险上限 will refuse some
        orders whose true risk was smaller.  That is the recoverable direction.

        A reducing order declares nothing.  ``increases_exposure`` already
        exempts a sell that is covered by the OMS net position, and handing a
        SHORT leg a level *below* the quote would be refused by
        ``check_stop_side`` as offering no protection — which would wall off the
        exit rather than protect it.  When the net position does not cover the
        sell the gate is right to refuse: that is a naked short, and a decorative
        stop must not be the thing that lets it through.
        """
        if direction != Direction.LONG:
            return None
        # math.isfinite rather than a bare comparison: a NaN atr_value makes
        # every comparison False, so `if atr <= 0: refuse` would invert into
        # allow and declare a NaN stop.
        if not math.isfinite(self.atr_value) or self.atr_value <= 0.0:
            return None
        if not math.isfinite(price) or price <= 0.0:
            return None
        stop: float = price - self.atr_stop * self.atr_value
        if not math.isfinite(stop) or stop <= 0.0 or stop >= price:
            # No log here on purpose. CtaEngine already writes one line per
            # refusal via explain_rejection, and check_stop_order retries on
            # *every* tick — a second line from here would multiply the spam
            # rather than add information.
            return None
        return stop

    def _contract(self) -> ContractData | None:
        """The OMS record for this symbol, or None when there is no OMS.

        `cta_engine` is a BacktestingEngine in a backtest and a CtaEngine live;
        only the second one owns a `main_engine`, and only a live MainEngine
        has `get_contract` (OmsEngine assigns it onto MainEngine in
        `init_engines`, so it is an attribute rather than a method and a
        `hasattr` check is the honest way to ask). Everything here therefore
        has to work with None, which is also the state of a live session in the
        seconds before the gateway finishes querying instruments.
        """
        main_engine = getattr(self.cta_engine, "main_engine", None)
        if main_engine is None:
            return None
        getter = getattr(main_engine, "get_contract", None)
        if getter is None:
            return None
        contract: ContractData | None = getter(self.vt_symbol)
        return contract

    def _resolve_board_lot(self) -> tuple[int, str]:
        """Shares per lot, and where the number came from.

        The contract wins. Both gateways already publish the real figure — HK
        lines come back with `min_volume` 100/500/1000/2000 and US lines with
        1 — and a parameter that the operator has to remember to change per
        market is the shape of bug that only shows up on the market nobody
        tested. `size` is deliberately not consulted: it is 1 on both gateways
        by design (see vnpy_futu/futu_mapping.py's `size` vs `min_volume`
        note), and the risk gates multiply by it.

        `min_volume` is a float on ContractData, so it is validated with
        `math.isfinite` before rounding: a NaN would make `lot <= 0` False and
        the refusal would inverse into "size the position off a NaN lot".

        The cost of contract-wins, stated: ReplayGateway builds ContractData
        without touching `min_volume` (vnpy_replay/gateway.py:242-251), so it
        reports the ContractData default of 1 and an HK symbol under replay
        will be sized in single shares. That is left alone rather than papered
        over with `max(contract_lot, board_lot)` — the max would silently let a
        stale HK parameter override a genuine US contract, trading a
        read-only-path artifact for a live-path one. The replay gateway is the
        place to fix it, once it has a lot-size source to fix it from.
        """
        contract: ContractData | None = self._contract()
        if contract is not None:
            lot: float = float(contract.min_volume)
            if math.isfinite(lot) and lot >= 1.0:
                return int(lot), f"合约 {self.vt_symbol}"
        return int(self.board_lot), "参数 board_lot（未取到合约）"

    def _board_lot(self) -> int:
        """The lot in force, logging once whenever its provenance changes.

        One line at startup, and one more only if the answer actually moves —
        the operator needs to be able to see "港股每手 100 来自合约" versus
        "每手 1 来自参数" without reading four hundred identical lines.
        """
        lot, source = self._resolve_board_lot()
        if source != self._lot_source:
            self._lot_source = source
            self.write_log(f"每手股数 {lot}，来源：{source}")
        return lot

    def _compute_unit(self) -> int:
        """Turtle unit size in shares: risk risk_percent of capital per 1N
        move, rounded down to whole board lots. For a stock, $ volatility per
        share is N (1 price point = $1/share). Returns 0 when capital can't
        cover even one lot — the strategy then simply doesn't enter (no
        sub-lot orders).

        The lot comes from the contract when there is one, so the same
        parameters size 300 shares on an HK line and 333 on a US line without
        the operator touching anything. `effective_lot` is written here rather
        than in on_bar because this is the only place that needs it, and it is
        a derived var so the GUI shows which lot actually applied.

        `atr_value` is checked with `math.isfinite` and not with a bare
        comparison. NaN makes every comparison False, so the old `if
        atr_value <= 0: return 0` fell through on a NaN and then raised
        ValueError out of `int(nan // lot)` — inside on_daily_bar, i.e. it took
        the whole bar handler (and with it `cancel_all` and the protective
        sell) down rather than declining to size.
        """
        lot: int = self._board_lot()
        self.effective_lot = lot
        if lot <= 0:
            return 0
        if not math.isfinite(self.atr_value) or self.atr_value <= 0.0:
            return 0
        risk_budget: float = self.trading_capital * self.risk_percent / 100.0
        raw_shares: float = risk_budget / self.atr_value
        lots: int = int(raw_shares // lot)
        return lots * lot

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
