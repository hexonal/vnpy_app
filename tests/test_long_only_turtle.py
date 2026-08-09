"""Tests for LongOnlyTurtleStrategy — the forum-learned additions over the
built-in TurtleSignalStrategy: capital-based unit sizing, the last-trade
filter (System-1 vs System-2 entry), round-trip PnL tracking, and the
long-only pyramiding. No real broker/engine — a fake cta_engine records the
orders the strategy would send.

Everything below the「港美股一等公民」banner is paired on purpose. The first
fifteen cases in this file all ran on 0700.SEHK with board_lot=100 and not one
of them mentioned SMART, which is exactly why three market assumptions —
board lot, the daily boundary, and the extended-hours tick gate — shipped
being right on one market and wrong on the other. Every behaviour added there
is asserted once on SEHK and once on SMART, and where the two markets
genuinely differ (HK has a lunch break and a closing auction, US has
continuous pre/post sessions) the pair asserts the same *rule* against each
market's own shape rather than the same clock reading.
"""

from __future__ import annotations

import os
import sys
from datetime import date, datetime, time, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vnpy.trader.constant import Direction, Exchange, Interval, Offset, Product
from vnpy.trader.object import BarData, ContractData, TickData, TradeData
from vnpy_gatewaykit.market_clock import market_tz
from vnpy_gatewaykit.sessions import SessionKind, day_close

from strategies.long_only_turtle_strategy import LongOnlyTurtleStrategy


class _StubMainEngine:
    """Answers get_contract from a dict; that is the whole MainEngine surface
    this strategy touches."""

    def __init__(self, contracts: dict[str, ContractData] | None = None) -> None:
        self._contracts: dict[str, ContractData] = contracts or {}

    def get_contract(self, vt_symbol: str) -> ContractData | None:
        return self._contracts.get(vt_symbol)


class _FakeCtaEngine:
    """Records send_order calls; no-ops the rest of the CtaEngine surface."""

    def __init__(self, main_engine: _StubMainEngine | None = None) -> None:
        self.orders: list = []
        self.cancel_all_calls: int = 0
        self.logs: list[str] = []
        self._id = 0
        # Only set when a test wants an OMS: a BacktestingEngine has no
        # main_engine at all, and the strategy must survive that.
        if main_engine is not None:
            self.main_engine = main_engine

    def send_order(self, strategy, direction, offset, price, volume, stop, lock, net) -> list:
        self._id += 1
        self.orders.append(
            {
                "direction": direction, "offset": offset, "price": price,
                "volume": volume, "stop": stop,
            }
        )
        return [f"fake.{self._id}"]

    def load_bar(self, *args, **kwargs) -> list:
        return []  # no preloaded history — tests feed bars via on_bar

    def cancel_all(self, strategy) -> None:
        self.cancel_all_calls += 1

    def put_strategy_event(self, strategy) -> None:
        pass

    def write_log(self, msg, strategy=None) -> None:
        self.logs.append(str(msg))

    def sync_strategy_data(self, strategy) -> None:
        pass


class _RecordingBarGenerator:
    """Stands in for BarGenerator so a test can see which ticks got through."""

    def __init__(self, daily_end: time) -> None:
        self.daily_end: time = daily_end
        self.ticks: list[TickData] = []

    def update_tick(self, tick: TickData) -> None:
        self.ticks.append(tick)


def _make_strategy(
    vt_symbol: str = "0700.SEHK",
    main_engine: _StubMainEngine | None = None,
    **setting,
) -> tuple[LongOnlyTurtleStrategy, _FakeCtaEngine]:
    engine = _FakeCtaEngine(main_engine)
    strat = LongOnlyTurtleStrategy(engine, "test", vt_symbol, setting)
    strat.on_init()
    strat.trading = True
    strat.inited = True
    return strat, engine


def _bar(
    dt: datetime, price: float, high: float | None = None, low: float | None = None
) -> BarData:
    return BarData(
        gateway_name="t", symbol="0700", exchange=Exchange.SEHK, datetime=dt,
        interval=Interval.DAILY, open_price=price,
        high_price=high if high is not None else price,
        low_price=low if low is not None else price,
        close_price=price, volume=1000,
    )


def _assert(name: str, cond: bool) -> None:
    print(f"  {'PASS' if cond else 'FAIL'}: {name}")
    if not cond:
        raise AssertionError(name)


def test_compute_unit_capital_based_board_lot_rounded() -> None:
    strat, _ = _make_strategy(trading_capital=100000.0, risk_percent=1.0, board_lot=100)
    strat.atr_value = 2.0
    # risk budget = 100000 * 1% = 1000; raw = 1000/2 = 500 shares; board-lot
    # 100 → 500 (already a multiple).
    _assert("unit = 500 shares", strat._compute_unit() == 500)
    # Bigger ATR → fewer shares, rounded down to lots. raw = 1000/3 = 333.3 →
    # 300 (3 lots of 100).
    strat.atr_value = 3.0
    _assert("unit rounds down to 300", strat._compute_unit() == 300)
    # Capital too small for even one lot → 0 (no sub-lot orders).
    strat2, _ = _make_strategy(trading_capital=1000.0, risk_percent=1.0, board_lot=100)
    strat2.atr_value = 5.0  # raw = 10/5 = 2 shares < 100 lot → 0
    _assert("sub-lot capital yields 0 units", strat2._compute_unit() == 0)


def test_round_trip_pnl_tracks_for_filter() -> None:
    strat, _ = _make_strategy()
    strat.atr_value = 1.0
    # Two pyramided buys: 100@10, 100@11 → avg 10.5.
    strat.pos = 100
    strat.on_trade(TradeData(gateway_name="t", symbol="0700", exchange=Exchange.SEHK,
                             orderid="1", tradeid="1", direction=Direction.LONG,
                             offset=Offset.OPEN, price=10.0, volume=100,
                             datetime=datetime(2026, 7, 1)))
    strat.pos = 200
    strat.on_trade(TradeData(gateway_name="t", symbol="0700", exchange=Exchange.SEHK,
                             orderid="2", tradeid="2", direction=Direction.LONG,
                             offset=Offset.OPEN, price=11.0, volume=100,
                             datetime=datetime(2026, 7, 2)))
    # Close all 200 @ 13 → PnL = (13 - 10.5) * 200 = 500.
    strat.pos = 0
    strat.on_trade(TradeData(gateway_name="t", symbol="0700", exchange=Exchange.SEHK,
                             orderid="3", tradeid="3", direction=Direction.SHORT,
                             offset=Offset.CLOSE, price=13.0, volume=200,
                             datetime=datetime(2026, 7, 3)))
    _assert("round-trip PnL = 500 (winner)", strat.last_trade_pnl == 500.0)
    _assert("cost tracker reset after close", strat._entry_vol == 0)


def _feed_history(strat: LongOnlyTurtleStrategy, n: int, start: float, step: float) -> None:
    base = datetime(2026, 1, 1)
    for i in range(n):
        strat.on_bar(_bar(base + timedelta(days=i), start + step * i))


def test_last_trade_filter_switches_entry_channel() -> None:
    # A strictly rising series so the 20-day high (entry_up) < 55-day high is
    # false — need the 55-day high strictly ABOVE the 20-day high. Use a series
    # that rose long ago then flattened so the 55-day high > 20-day high.
    strat, engine = _make_strategy(trading_capital=100000.0, risk_percent=1.0, board_lot=100)
    base = datetime(2026, 1, 1)
    # 40 rising bars (to 140), then 25 flat-ish bars at 120 (below the old high)
    # → 55-day high ≈ 140 (from the early peak), 20-day high ≈ 120.
    prices = [100 + i for i in range(40)] + [120.0] * 25
    for i, p in enumerate(prices[:-1]):
        strat.on_bar(_bar(base + timedelta(days=i), p))

    strat.pos = 0
    engine.orders.clear()

    # Loss last trade → System 1 (20-day high ~120) breakout.
    strat.last_trade_pnl = -50
    strat.on_bar(_bar(base + timedelta(days=len(prices) - 1), 120.0))
    s1_prices = [o["price"] for o in engine.orders if o["direction"] == Direction.LONG]
    _assert("after a loss, first buy stop = 20-day high (entry_up)",
            bool(s1_prices) and abs(min(s1_prices) - strat.entry_up) < 1e-6)
    _assert("55-day high is strictly above 20-day high (test setup valid)",
            strat.breakout_up > strat.entry_up)

    strat.pos = 0
    engine.orders.clear()
    # Winner last trade → System 2 (55-day high) breakout instead.
    strat.last_trade_pnl = 200
    strat.on_bar(_bar(base + timedelta(days=len(prices)), 120.0))
    s2_prices = [o["price"] for o in engine.orders if o["direction"] == Direction.LONG]
    _assert("after a win, first buy stop = 55-day high (breakout_up)",
            bool(s2_prices) and abs(min(s2_prices) - strat.breakout_up) < 1e-6)


def test_no_orders_when_unit_size_zero() -> None:
    # Tiny capital → unit_size 0 → strategy must place no buy orders (no
    # sub-lot trading), even on a valid breakout.
    strat, engine = _make_strategy(trading_capital=500.0, risk_percent=1.0, board_lot=100)
    _feed_history(strat, 70, 100.0, 1.0)  # strong uptrend, breakout every bar
    buys = [o for o in engine.orders if o["direction"] == Direction.LONG]
    _assert("no buy orders when capital can't cover a lot", buys == [])


def main() -> None:
    tests = [
        test_compute_unit_capital_based_board_lot_rounded,
        test_round_trip_pnl_tracks_for_filter,
        test_last_trade_filter_switches_entry_channel,
        test_no_orders_when_unit_size_zero,
    ]
    for t in tests:
        print(t.__name__)
        t()
        sys.stdout.flush()
    print(f"\nAll {len(tests)} tests passed.")


if __name__ == "__main__":
    main()


def test_pyramid_base_stays_persisted_and_out_of_the_gui() -> None:
    """The ladder anchor must survive a restart, and must not be an operator field.

    A history replay cannot recover it: on_init would re-derive the *current*
    Donchian high, not the breakout price frozen at entry, so a restarted
    process would hang its ladder off a different price than the one it
    replaced. The gap-past-the-ladder disaster this value can cause is blocked
    in send_buy_orders instead — see the test below.
    """
    from strategies.long_only_turtle_strategy import LongOnlyTurtleStrategy

    assert "_pyramid_base" in LongOnlyTurtleStrategy.internal_vars
    assert "_pyramid_base" not in LongOnlyTurtleStrategy.display_vars
    assert "_pyramid_base" not in LongOnlyTurtleStrategy.derived_vars


def test_ladder_skips_rungs_already_below_the_market() -> None:
    """A rung below the market is not a breakout order — it fills at market.

    This is the actual fix for the gap case. Measured before it: holding one
    unit entered at base=101 with N=2, price at 146 after a restart, the rungs
    came out at 102/103/104 — all below market — and the local stop engine
    (tick.last_price >= stop.price) filled all three at 146.05 on the next
    tick: 1500 shares, 219k notional against 100k capital. The same thing is
    reachable without any restart, whenever a session gaps past the ladder,
    which is why the guard lives here and not in the persistence layer.
    """
    strat, engine = _make_strategy(trading_capital=100000.0, risk_percent=1.0, board_lot=100)
    strat.unit_size = 100
    strat.atr_value = 2.0
    strat.max_units = 4
    strat.pos = 0
    engine.orders.clear()

    # Anchor at 101 while the market is 146: every rung (101/102/103/104) is
    # under water and must be dropped rather than sent.
    strat.send_buy_orders(101.0, market_price=146.0)
    assert [o for o in engine.orders if o["direction"] == Direction.LONG] == []

    # Same anchor with the market at the anchor: ordinary breakout follow-through,
    # the at-market rung is legitimate and must still go out.
    engine.orders.clear()
    strat.send_buy_orders(101.0, market_price=101.0)
    sent = [o["price"] for o in engine.orders if o["direction"] == Direction.LONG]
    assert sent and abs(min(sent) - 101.0) < 1e-9


def test_strategy_runs_on_daily_bars_not_minutes() -> None:
    """Every window here is a day count, and load_bar's first argument is days —
    so 65 only means "65 bars" when the interval is daily.

    Running on the BarGenerator default (1-minute) made ATR(20) on 700.HK 0.29
    instead of 17.88, sizing a unit at 3400 shares = 14.8x capital, 59x at
    max_units=4. It also meant the backtest (daily) and live (minutes) were not
    the same strategy.
    """
    import inspect

    from strategies.long_only_turtle_strategy import LongOnlyTurtleStrategy

    source = inspect.getsource(LongOnlyTurtleStrategy.on_init)
    assert "Interval.DAILY" in source, "on_init 必须显式声明日线周期"
    assert "interval=Interval.DAILY" in source, "load_bar 必须按日线加载"

    # on_bar routes both engine-fed daily bars and BarGenerator minute bars into
    # the same daily code path, so backtest and live share one timeframe.
    routing = inspect.getsource(LongOnlyTurtleStrategy.on_bar)
    assert "Interval.DAILY" in routing and "on_daily_bar" in routing


# ---------------------------------------------------------------------------
# Stop declared to the risk gate
# ---------------------------------------------------------------------------


def test_declared_stop_is_the_atr_leg_off_the_quoted_price() -> None:
    """The gate needs the stop before the order leaves; on_trade only has it after."""
    strat, _ = _make_strategy(atr_stop=2.0)
    strat.atr_value = 2.05
    declared = strat.get_stop_price("0700.SEHK", Direction.LONG, 103.75)
    assert declared == pytest.approx(103.75 - 2.0 * 2.05)


def test_declared_stop_equals_what_on_trade_will_write_into_long_stop() -> None:
    """Same formula, one step early — they must not be allowed to drift apart."""
    strat, _ = _make_strategy(atr_stop=2.0)
    strat.atr_value = 2.05
    quote: float = 103.75
    declared = strat.get_stop_price("0700.SEHK", Direction.LONG, quote)

    strat.on_trade(
        TradeData(
            gateway_name="t", symbol="0700", exchange=Exchange.SEHK,
            orderid="1", tradeid="1", direction=Direction.LONG,
            price=quote, volume=100, datetime=datetime(2026, 1, 5, 9, 30),
        )
    )
    assert declared == pytest.approx(strat.long_stop)


def test_declared_stop_ignores_the_exit_channel_so_risk_is_never_understated() -> None:
    """max(long_stop, exit_down) is the resting level; declaring it would flatter.

    Measured on this fixture: the armed sell sits at 100.50 (3.25/share) while
    the ATR leg is 99.65 (4.10/share). The gate must see the floor.
    """
    strat, _ = _make_strategy(atr_stop=2.0)
    strat.atr_value = 2.05
    strat.exit_down = 100.50
    declared = strat.get_stop_price("0700.SEHK", Direction.LONG, 103.75)
    assert declared == pytest.approx(99.65)
    assert declared < strat.exit_down


def test_a_reducing_order_declares_no_stop() -> None:
    """increases_exposure exempts a covered sell; a level below the quote would
    be refused by check_stop_side and would wall off the exit."""
    strat, _ = _make_strategy()
    strat.atr_value = 2.05
    assert strat.get_stop_price("0700.SEHK", Direction.SHORT, 103.75) is None


def test_no_stop_is_declared_before_atr_is_ready() -> None:
    strat, _ = _make_strategy()
    for atr in (0.0, -1.0, float("nan")):
        strat.atr_value = atr
        assert strat.get_stop_price("0700.SEHK", Direction.LONG, 103.75) is None


def test_no_stop_is_declared_for_a_zero_quote() -> None:
    """check_stop_order hands over 0.0 whenever limit_up and ask_price_5 are both
    empty; price - 2N would then be negative and pretend to be a stop."""
    strat, _ = _make_strategy()
    strat.atr_value = 5.0
    for quote in (0.0, -1.0, float("nan")):
        assert strat.get_stop_price("0700.SEHK", Direction.LONG, quote) is None


def test_no_stop_is_declared_when_the_atr_leg_would_not_protect() -> None:
    """A stop at or above the entry is not a stop. Reachable with a huge N on a
    cheap name — say it out loud rather than declare a level that cannot hold."""
    strat, _ = _make_strategy(atr_stop=2.0)
    strat.atr_value = 60.0
    assert strat.get_stop_price("0700.SEHK", Direction.LONG, 103.75) is None


def test_pyramid_adds_declare_off_their_own_rung_not_the_first_fill() -> None:
    """atr_value is frozen while a position is open, so every rung declares the
    same 2N distance measured from its own quote."""
    strat, _ = _make_strategy(atr_stop=2.0)
    strat.atr_value = 2.0
    quotes = (100.0, 101.0, 102.0, 103.0)
    declared = [strat.get_stop_price("0700.SEHK", Direction.LONG, q) for q in quotes]
    assert declared == [pytest.approx(q - 4.0) for q in quotes]


# ---------------------------------------------------------------------------
# 港美股一等公民：市场夹具
# ---------------------------------------------------------------------------

# A fixed Monday. WeekdayCalendar — the default sessions.py calendar — calls it
# a trading day for both markets, and no US DST boundary is anywhere near it,
# so every local wall-clock time below means exactly one instant.
_MONDAY: date = date(2026, 1, 5)

# (vt_symbol, exchange, 每手股数, 连续盘内, 收市之后, 【非连续盘但未到收市】)
#
# 最后一列刻意取两个市场各自的形状而不是同一个钟点：港股撞的是 12:00-13:00 午休，
# 美股撞的是 04:00-09:30 盘前。同一条规则，两堵不同的墙——这正是上一版只在
# 0700.SEHK 上试过、于是盘前那条永远没被行使过的地方。
_MARKETS: tuple[tuple[str, Exchange, int, time, time, time], ...] = (
    ("0700.SEHK", Exchange.SEHK, 100, time(10, 30), time(16, 5), time(12, 30)),
    ("NBIS.SMART", Exchange.SMART, 1, time(10, 30), time(16, 5), time(4, 12)),
)

_REGULAR: frozenset[SessionKind] = frozenset({SessionKind.REGULAR})


def _at(exchange: Exchange, moment: time) -> datetime:
    """That local wall-clock reading on _MONDAY, in the market's own zone."""
    return datetime.combine(_MONDAY, moment, tzinfo=market_tz(exchange))


def _tick(
    vt_symbol: str, exchange: Exchange, when: datetime, price: float = 100.0
) -> TickData:
    return TickData(
        gateway_name="t", symbol=vt_symbol.split(".")[0], exchange=exchange,
        datetime=when, last_price=price,
    )


def _contract(vt_symbol: str, exchange: Exchange, lot: int) -> ContractData:
    """size=1 / min_volume=每手股数 — the shape both live gateways actually write
    (vnpy_usmart/gateway.py:748, vnpy_futu/futu_mapping.py:325-326)."""
    return ContractData(
        gateway_name="t", symbol=vt_symbol.split(".")[0], exchange=exchange,
        name=vt_symbol, product=Product.EQUITY, size=1, pricetick=0.01,
        min_volume=float(lot),
    )


def _with_recorder(
    strat: LongOnlyTurtleStrategy,
) -> _RecordingBarGenerator:
    """Swap the BarGenerator for one that only records, so a tick-gate test
    asserts on the gate and not on bar aggregation side effects."""
    recorder = _RecordingBarGenerator(strat.bg.daily_end)
    strat.bg = recorder
    return recorder


# ---------------------------------------------------------------------------
# 每手股数：合约优先、参数兜底
# ---------------------------------------------------------------------------


def test_board_lot_comes_from_the_contract_on_both_markets() -> None:
    """board_lot=7 is wrong for both markets on purpose — the contract must win.

    Budget 1000 at N=3 is 333.33 shares: HK quantises to 3 lots of 100, US to
    333 single shares. Same parameters, two correct answers, nothing for the
    operator to remember.
    """
    for vt_symbol, exchange, lot, *_ in _MARKETS:
        oms = _StubMainEngine({vt_symbol: _contract(vt_symbol, exchange, lot)})
        strat, _ = _make_strategy(
            vt_symbol, oms, board_lot=7, trading_capital=100000.0, risk_percent=1.0
        )
        strat.atr_value = 3.0
        assert strat._compute_unit() == (333 // lot) * lot
        assert strat.effective_lot == lot
        assert strat._lot_source.startswith("合约")


def test_board_lot_falls_back_to_the_parameter_when_there_is_no_oms_on_both_markets() -> None:
    """A BacktestingEngine has no main_engine at all, and a live session has no
    contract for the first seconds after connect. Both must size, not crash."""
    for vt_symbol, _exchange, lot, *_ in _MARKETS:
        strat, _ = _make_strategy(
            vt_symbol, None, board_lot=lot, trading_capital=100000.0, risk_percent=1.0
        )
        strat.atr_value = 3.0
        assert strat._compute_unit() == (333 // lot) * lot
        assert strat.effective_lot == lot
        assert strat._lot_source.startswith("参数")


def test_a_contract_lot_beats_a_parameter_left_over_from_the_other_market() -> None:
    """The concrete failure the parameter caused: one number, two markets.

    Carried from HK to US it quantises 333 shares down to 300 — 10% of the
    intended risk silently discarded. Carried from US to HK it emits a 333-share
    odd lot, which SEHK's board-lot market will not fill at all.
    """
    hk = _StubMainEngine({"0700.SEHK": _contract("0700.SEHK", Exchange.SEHK, 100)})
    us = _StubMainEngine({"NBIS.SMART": _contract("NBIS.SMART", Exchange.SMART, 1)})

    hk_strat, _ = _make_strategy("0700.SEHK", hk, board_lot=1)
    hk_strat.atr_value = 3.0
    assert hk_strat._compute_unit() == 300

    us_strat, _ = _make_strategy("NBIS.SMART", us, board_lot=100)
    us_strat.atr_value = 3.0
    assert us_strat._compute_unit() == 333


def test_a_non_finite_contract_lot_falls_back_instead_of_sizing_off_nan() -> None:
    """min_volume is a float on ContractData, so NaN is representable. A bare
    `lot <= 0` check would read False on it and hand NaN to the divider."""
    for vt_symbol, exchange, lot, *_ in _MARKETS:
        broken = _contract(vt_symbol, exchange, lot)
        broken.min_volume = float("nan")
        strat, _ = _make_strategy(
            vt_symbol, _StubMainEngine({vt_symbol: broken}), board_lot=lot
        )
        strat.atr_value = 3.0
        assert strat._compute_unit() == (333 // lot) * lot
        assert strat.effective_lot == lot


def test_a_nan_atr_declines_to_size_rather_than_raising_out_of_the_bar_handler() -> None:
    """`int(nan // lot)` raises ValueError, and _compute_unit runs inside
    on_daily_bar — so the old comparison took cancel_all and the protective
    sell down with it instead of simply not entering."""
    for vt_symbol, exchange, lot, *_ in _MARKETS:
        oms = _StubMainEngine({vt_symbol: _contract(vt_symbol, exchange, lot)})
        strat, _ = _make_strategy(vt_symbol, oms)
        strat.atr_value = float("nan")
        assert strat._compute_unit() == 0


def test_effective_lot_is_a_derived_var_and_is_never_persisted() -> None:
    """It is a fact about the contract, not about this strategy's history: an
    HK lot-size revision, or a restart that resolves the symbol on a different
    gateway, must not be outlived by a stored copy."""
    assert "effective_lot" in LongOnlyTurtleStrategy.derived_vars
    assert "effective_lot" not in LongOnlyTurtleStrategy.internal_vars
    assert "effective_lot" not in LongOnlyTurtleStrategy.display_vars


# ---------------------------------------------------------------------------
# 日线边界来自 sessions.py，未映射的交易所被拒绝
# ---------------------------------------------------------------------------


def test_daily_end_is_each_markets_own_continuous_close() -> None:
    """Asserted against sessions.py rather than against a literal: the point of
    the change is that the two cannot drift, so the test has to move when the
    table moves."""
    for vt_symbol, exchange, *_ in _MARKETS:
        strat, _ = _make_strategy(vt_symbol)
        expected = day_close(exchange, _MONDAY, kinds=_REGULAR)
        assert expected is not None
        assert strat.bg.daily_end == expected.time()


def test_hk_daily_end_is_the_continuous_close_not_the_closing_auction_end() -> None:
    """Unfiltered, day_close answers 16:10 for HK — the 收市竞价 window's end.

    Taking that would hold the day open across an auction whose ticks the gate
    below refuses, so the daily bar would wait for the date rollover it did not
    need to wait for.
    """
    strat, _ = _make_strategy("0700.SEHK")
    all_kinds = day_close(Exchange.SEHK, _MONDAY)
    assert all_kinds is not None and all_kinds.time() == time(16, 10)
    assert strat.bg.daily_end == time(16, 0)


def test_us_daily_end_is_the_continuous_close_not_the_after_hours_end() -> None:
    """Same assertion, the other market's shape: unfiltered US answers 20:00."""
    strat, _ = _make_strategy("NBIS.SMART")
    all_kinds = day_close(Exchange.SMART, _MONDAY)
    assert all_kinds is not None and all_kinds.time() == time(20, 0)
    assert strat.bg.daily_end == time(16, 0)


def test_an_unmapped_exchange_is_refused_instead_of_defaulting_to_1600() -> None:
    """SSE is a real Exchange with no session row, and Shanghai closes at 15:00.

    The old `.get(exchange, time(16, 0))` handed it 16:00 without a word, which
    is the failure mode market_tz has refused since it was written.
    """
    with pytest.raises(ValueError, match="没有交易时段定义"):
        _make_strategy("600000.SSE")


def test_an_unparseable_vt_symbol_suffix_is_refused() -> None:
    with pytest.raises(ValueError, match="无法从 vt_symbol 解析交易所"):
        _make_strategy("AAPL.NOSUCHVENUE")


def test_the_local_close_table_is_no_longer_copied_into_this_file() -> None:
    """A copy is how two components end up disagreeing about when the market
    closed — sessions.py's own module docstring says so, and this file held the
    copy it was warning about."""
    import strategies.long_only_turtle_strategy as module

    assert not hasattr(module, "_MARKET_CLOSE")


# ---------------------------------------------------------------------------
# 只放行连续盘的行情
# ---------------------------------------------------------------------------


def test_only_continuous_session_ticks_reach_the_bar_generator_on_both_markets() -> None:
    for vt_symbol, exchange, _lot, inside, after_close, outside in _MARKETS:
        strat, _ = _make_strategy(vt_symbol)
        recorder = _with_recorder(strat)

        strat.on_tick(_tick(vt_symbol, exchange, _at(exchange, inside)))
        assert len(recorder.ticks) == 1

        strat.on_tick(_tick(vt_symbol, exchange, _at(exchange, outside)))
        strat.on_tick(_tick(vt_symbol, exchange, _at(exchange, after_close)))
        assert len(recorder.ticks) == 1


def test_us_extended_hours_prints_never_shape_the_daily_bar() -> None:
    """The defect 0700.SEHK structurally could not catch: HK has no continuous
    pre/post session, so an 04:12 print is a US-only object, and futu subscribes
    SMART with extended_time=True. A thin pre-market print became the day's open
    and a single after-hours cross became its high, so the Donchian channel and
    the ATR described prices the regular session never traded at."""
    strat, _ = _make_strategy("NBIS.SMART")
    recorder = _with_recorder(strat)

    for moment in (time(4, 12), time(9, 29), time(16, 0), time(19, 30)):
        strat.on_tick(_tick("NBIS.SMART", Exchange.SMART, _at(Exchange.SMART, moment)))
    assert recorder.ticks == []

    strat.on_tick(_tick("NBIS.SMART", Exchange.SMART, _at(Exchange.SMART, time(9, 30))))
    strat.on_tick(_tick("NBIS.SMART", Exchange.SMART, _at(Exchange.SMART, time(15, 59))))
    assert len(recorder.ticks) == 2


def test_hk_auction_and_lunch_prints_never_shape_the_daily_bar() -> None:
    """HK's shape of the same rule: the 09:00-09:30 开市前竞价 and the
    16:00-16:10 收市竞价 are AUCTION windows, and 12:00-13:00 is a real hole."""
    strat, _ = _make_strategy("0700.SEHK")
    recorder = _with_recorder(strat)

    for moment in (time(9, 15), time(12, 30), time(16, 5)):
        strat.on_tick(_tick("0700.SEHK", Exchange.SEHK, _at(Exchange.SEHK, moment)))
    assert recorder.ticks == []

    for moment in (time(9, 30), time(11, 59), time(13, 0), time(15, 59)):
        strat.on_tick(_tick("0700.SEHK", Exchange.SEHK, _at(Exchange.SEHK, moment)))
    assert len(recorder.ticks) == 4


def test_a_naive_tick_is_dropped_and_logged_exactly_once_on_both_markets() -> None:
    """A naive timestamp has no instant to place on a market clock. Raising
    would be worse than dropping: call_strategy_func would flip inited/trading
    off on the first such tick and take the position's protection with it."""
    for vt_symbol, exchange, *_ in _MARKETS:
        strat, engine = _make_strategy(vt_symbol)
        recorder = _with_recorder(strat)
        engine.logs.clear()

        for minute in range(3):
            naive = datetime(2026, 1, 5, 10, 30) + timedelta(minutes=minute)
            strat.on_tick(_tick(vt_symbol, exchange, naive))

        assert recorder.ticks == []
        assert sum("裸时间戳" in line for line in engine.logs) == 1


# ---------------------------------------------------------------------------
# 收市清场，次日首笔连续盘行情补挂
# ---------------------------------------------------------------------------


def test_the_first_tick_after_the_continuous_close_cancels_everything_on_both_markets() -> None:
    """CtaEngine.check_stop_order walks every stop order on every tick event,
    before any strategy callback and with no idea what a session is — so the
    only lever a strategy has against an extended-hours trigger is to leave
    nothing resting."""
    for vt_symbol, exchange, _lot, _inside, after_close, _outside in _MARKETS:
        strat, engine = _make_strategy(vt_symbol)
        _with_recorder(strat)
        strat.pos = 300

        strat.on_tick(_tick(vt_symbol, exchange, _at(exchange, after_close)))
        assert engine.cancel_all_calls == 1
        assert strat._rearm_pending is True


def test_the_close_time_cancel_fires_once_per_day_on_both_markets() -> None:
    for vt_symbol, exchange, _lot, _inside, after_close, _outside in _MARKETS:
        strat, engine = _make_strategy(vt_symbol)
        _with_recorder(strat)
        strat.pos = 300

        for minute in range(3):
            when = _at(exchange, after_close) + timedelta(minutes=minute)
            strat.on_tick(_tick(vt_symbol, exchange, when))
        assert engine.cancel_all_calls == 1


def test_a_tick_outside_the_session_but_before_the_close_cancels_nothing() -> None:
    """Keying the cancel on 「非连续盘」 instead of 「过了当日收市」 breaks HK and
    only HK: it would strip the ladder and the protective stop at 12:00 HKT and
    not rebuild them until the next daily bar, leaving the entire afternoon
    session unguarded. The US pre-market lands on the same branch for the same
    reason."""
    for vt_symbol, exchange, _lot, _inside, _after, outside in _MARKETS:
        strat, engine = _make_strategy(vt_symbol)
        _with_recorder(strat)
        strat.pos = 300

        strat.on_tick(_tick(vt_symbol, exchange, _at(exchange, outside)))
        assert engine.cancel_all_calls == 0
        assert strat._rearm_pending is False


def test_the_close_time_cancel_does_not_arm_a_rearm_when_flat_on_both_markets() -> None:
    for vt_symbol, exchange, _lot, _inside, after_close, _outside in _MARKETS:
        strat, engine = _make_strategy(vt_symbol)
        _with_recorder(strat)
        strat.pos = 0

        strat.on_tick(_tick(vt_symbol, exchange, _at(exchange, after_close)))
        assert engine.cancel_all_calls == 1
        assert strat._rearm_pending is False


def test_the_protective_stop_is_rearmed_on_the_next_sessions_first_regular_tick() -> None:
    """Cost of the close-time cancel, and its repayment: protection is absent
    overnight and restored within one tick of the next continuous open, rather
    than resting through the night where an extended print could fire it into
    the thinnest book of the day."""
    for vt_symbol, exchange, _lot, inside, after_close, _outside in _MARKETS:
        strat, engine = _make_strategy(vt_symbol)
        _with_recorder(strat)
        strat.pos = 300
        strat.long_stop = 95.0
        strat.exit_down = 90.0

        strat.on_tick(_tick(vt_symbol, exchange, _at(exchange, after_close)))
        engine.orders.clear()

        strat.on_tick(
            _tick(vt_symbol, exchange, _at(exchange, inside) + timedelta(days=1))
        )
        stops = [o for o in engine.orders if o["direction"] == Direction.SHORT]
        assert len(stops) == 1
        assert stops[0]["stop"] is True
        assert stops[0]["price"] == pytest.approx(95.0)
        assert stops[0]["volume"] == 300
        assert strat._rearm_pending is False


def test_no_order_is_rearmed_by_a_tick_outside_the_continuous_session() -> None:
    """The re-arm must not be reachable from the very window it exists to avoid
    holding orders through — an 04:12 US print or a 16:05 HK auction print."""
    for vt_symbol, exchange, _lot, _inside, after_close, outside in _MARKETS:
        strat, engine = _make_strategy(vt_symbol)
        _with_recorder(strat)
        strat.pos = 300
        strat.long_stop = 95.0
        strat._rearm_pending = True

        for moment in (outside, after_close):
            strat.on_tick(_tick(vt_symbol, exchange, _at(exchange, moment)))
        assert engine.orders == []
        assert strat._rearm_pending is True


def test_only_one_protective_stop_is_placed_when_on_ready_arrives_late() -> None:
    """StatefulCtaTemplate._ensure_ready fires on the first send_order of the
    process, so once the close-time cancel became a second producer of
    `_rearm_pending`, the re-armed sell itself could be that first order — and
    on_ready would then set the flag again from inside it. The next tick puts
    out a second stop for the same shares; both trigger together and the
    position flips short."""
    for vt_symbol, exchange, _lot, inside, after_close, _outside in _MARKETS:
        strat, engine = _make_strategy(vt_symbol)
        _with_recorder(strat)
        strat.pos = 300
        strat.long_stop = 95.0

        strat.on_tick(_tick(vt_symbol, exchange, _at(exchange, after_close)))
        engine.orders.clear()

        for extra in range(3):
            when = _at(exchange, inside) + timedelta(days=1, minutes=extra)
            strat.on_tick(_tick(vt_symbol, exchange, when))
        stops = [o for o in engine.orders if o["direction"] == Direction.SHORT]
        assert len(stops) == 1


def test_a_restart_while_holding_still_arms_the_protective_stop_on_both_markets() -> None:
    """The guard above must not have disabled the case it guards: a fresh
    process has nothing resting, so on_ready still has to ask for a re-arm."""
    for vt_symbol, exchange, _lot, inside, *_ in _MARKETS:
        strat, engine = _make_strategy(vt_symbol)
        _with_recorder(strat)
        strat.pos = 300
        strat.long_stop = 95.0

        strat.on_ready(True)
        assert strat._rearm_pending is True

        strat.on_tick(_tick(vt_symbol, exchange, _at(exchange, inside)))
        stops = [o for o in engine.orders if o["direction"] == Direction.SHORT]
        assert len(stops) == 1
        assert stops[0]["price"] == pytest.approx(95.0)
