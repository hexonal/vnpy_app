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
            {"direction": direction, "offset": offset, "price": price, "volume": volume, "stop": stop}
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


def _bar(dt: datetime, price: float, high: float | None = None, low: float | None = None) -> BarData:
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
                             offset=Offset.OPEN, price=10.0, volume=100, datetime=datetime(2026, 7, 1)))
    strat.pos = 200
    strat.on_trade(TradeData(gateway_name="t", symbol="0700", exchange=Exchange.SEHK,
                             orderid="2", tradeid="2", direction=Direction.LONG,
                             offset=Offset.OPEN, price=11.0, volume=100, datetime=datetime(2026, 7, 2)))
    # Close all 200 @ 13 → PnL = (13 - 10.5) * 200 = 500.
    strat.pos = 0
    strat.on_trade(TradeData(gateway_name="t", symbol="0700", exchange=Exchange.SEHK,
                             orderid="3", tradeid="3", direction=Direction.SHORT,
                             offset=Offset.CLOSE, price=13.0, volume=200, datetime=datetime(2026, 7, 3)))
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
