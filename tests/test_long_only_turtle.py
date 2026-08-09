"""Tests for LongOnlyTurtleStrategy — the forum-learned additions over the
built-in TurtleSignalStrategy: capital-based unit sizing, the last-trade
filter (System-1 vs System-2 entry), round-trip PnL tracking, and the
long-only pyramiding. No real broker/engine — a fake cta_engine records the
orders the strategy would send.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vnpy.trader.constant import Direction, Exchange, Interval, Offset
from vnpy.trader.object import BarData, TradeData

from strategies.long_only_turtle_strategy import LongOnlyTurtleStrategy


class _FakeCtaEngine:
    """Records send_order calls; no-ops the rest of the CtaEngine surface."""

    def __init__(self) -> None:
        self.orders: list = []
        self._id = 0

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
        pass

    def put_strategy_event(self, strategy) -> None:
        pass

    def write_log(self, msg, strategy=None) -> None:
        pass

    def sync_strategy_data(self, strategy) -> None:
        pass


def _make_strategy(**setting) -> tuple[LongOnlyTurtleStrategy, _FakeCtaEngine]:
    engine = _FakeCtaEngine()
    strat = LongOnlyTurtleStrategy(engine, "test", "0700.SEHK", setting)
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
