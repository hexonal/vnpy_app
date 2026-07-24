"""Unit tests for ChartWizardWidget.process_tick_event live-bar volume
aggregation — the tick→period-bar synthesis that gives the K-line its
broker-app "day bar ticks live" behavior.

Focus: the session-rollover / period-rollover volume accounting a code
review flagged. tick.volume is session-cumulative, so per-tick volume is a
delta; the boundary tick between two periods must land in the NEW period
bar (not be dropped), and a session reset (new trading day) must not push a
huge negative delta through the max(,0) floor and silently zero the volume.

Qt-free: process_tick_event only touches plain dict state, the static
_period_start, and chart.update_bar(). We call the real (unbound) method
against a lightweight fake `self` so the tested code path is the shipping
one, not a copy.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime
from types import SimpleNamespace

# Headless Qt — chart_wizard imports PySide6/qfluentwidgets at module load.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vnpy.trader.constant import Exchange, Interval  # noqa: E402
from vnpy.trader.object import TickData  # noqa: E402

from fluent_ui.chart_wizard import ChartWizardWidget  # noqa: E402


class _FakeChart:
    """Captures every bar pushed via update_bar so a test can inspect the
    latest live bar state."""

    def __init__(self) -> None:
        self.bars: list = []

    def update_bar(self, bar) -> None:
        self.bars.append(bar)

    @property
    def last(self):
        return self.bars[-1]


def _make_self(vt_symbol: str, interval: Interval):
    chart = _FakeChart()
    fake = SimpleNamespace(
        chart_intervals={vt_symbol: interval},
        charts={vt_symbol: chart},
        running_bars={},
        _last_tick_volume={},
        _last_tick_time={},
        # Bind the real staticmethod so the tested path is unchanged.
        _period_start=ChartWizardWidget._period_start,
    )
    return fake, chart


def _tick(symbol: str, dt: datetime, price: float, volume: float) -> TickData:
    return TickData(
        gateway_name="TEST",
        symbol=symbol,
        exchange=Exchange.SEHK,
        datetime=dt,
        last_price=price,
        volume=volume,
    )


def _feed(fake, tick: TickData) -> None:
    ChartWizardWidget.process_tick_event(fake, SimpleNamespace(data=tick))


def _assert(name: str, cond: bool) -> None:
    print(f"  {'PASS' if cond else 'FAIL'}: {name}")
    if not cond:
        raise AssertionError(name)


def test_same_period_accumulates() -> None:
    """Within one minute bar, volume is the running delta of cumulative
    tick volume."""
    fake, chart = _make_self("0700.SEHK", Interval.MINUTE)
    _feed(fake, _tick("0700", datetime(2026, 7, 24, 9, 30, 1), 100.0, 1000))
    _feed(fake, _tick("0700", datetime(2026, 7, 24, 9, 30, 20), 101.0, 1500))
    _feed(fake, _tick("0700", datetime(2026, 7, 24, 9, 30, 45), 100.5, 1800))
    bar = chart.last
    # First tick seeds a fresh bar (no prior baseline) with delta 0; then
    # +500 and +300. Total = 800. High/low/close track.
    _assert("same-period volume = 800", bar.volume == 800)
    _assert("same-period high = 101", bar.high_price == 101.0)
    _assert("same-period low = 100", bar.low_price == 100.0)
    _assert("same-period close = 100.5", bar.close_price == 100.5)


def test_intraperiod_rollover_keeps_boundary_delta() -> None:
    """The boundary tick that opens a new minute must contribute its delta
    to the NEW bar, not be silently dropped."""
    fake, chart = _make_self("0700.SEHK", Interval.MINUTE)
    _feed(fake, _tick("0700", datetime(2026, 7, 24, 9, 30, 10), 100.0, 1000))
    _feed(fake, _tick("0700", datetime(2026, 7, 24, 9, 30, 50), 100.2, 1200))  # +200 into 09:30
    # New minute 09:31 — cumulative jumps to 1500 (=+300 at the boundary).
    _feed(fake, _tick("0700", datetime(2026, 7, 24, 9, 31, 5), 100.4, 1500))
    _feed(fake, _tick("0700", datetime(2026, 7, 24, 9, 31, 40), 100.6, 1650))  # +150
    minute_0931 = chart.last
    _assert(
        "09:31 bar keeps boundary +300 then +150 = 450",
        minute_0931.volume == 450,
    )
    _assert("09:31 bar is a new datetime", minute_0931.datetime.minute == 31)
    # Cross-check: total captured volume never loses the boundary tick.
    # 09:30 bar was 200 (0 seed + 200), 09:31 bar is 450 → 650 = full 1650-1000.
    _assert("no boundary volume lost (200 + 450 = 650)", 200 + minute_0931.volume == 650)


def test_session_rollover_uses_cumulative_not_negative_delta() -> None:
    """Across a trading-day boundary tick.volume RESETS. The naive delta
    (today_small - yesterday_large) is hugely negative; the new day bar must
    take the reset cumulative as its volume, not floor to ~0."""
    fake, chart = _make_self("0700.SEHK", Interval.DAILY)
    # Day 1 builds up a big cumulative total.
    _feed(fake, _tick("0700", datetime(2026, 7, 24, 9, 30, 0), 100.0, 5000))
    _feed(fake, _tick("0700", datetime(2026, 7, 24, 15, 0, 0), 102.0, 900000))
    # Day 2 open — cumulative resets to a small value.
    _feed(fake, _tick("0700", datetime(2026, 7, 25, 9, 30, 0), 103.0, 4000))
    day2 = chart.last
    _assert("day-2 bar is a new day", day2.datetime.day == 25)
    _assert("day-2 open volume = reset cumulative 4000 (not 0)", day2.volume == 4000)
    _feed(fake, _tick("0700", datetime(2026, 7, 25, 10, 0, 0), 104.0, 7000))  # +3000
    # chart.update_bar receives copy() snapshots, so re-read the latest one
    # rather than the earlier snapshot (which confirms snapshots are stable).
    _assert("day-2 volume accumulates to 7000", chart.last.volume == 7000)


def test_first_tick_seeds_zero_no_baseline() -> None:
    """The very first tick has no prior cumulative baseline, so it seeds 0
    (same as BarGenerator's first-tick behavior) rather than treating the
    whole cumulative as one bar's delta."""
    fake, chart = _make_self("0700.SEHK", Interval.HOUR)
    _feed(fake, _tick("0700", datetime(2026, 7, 24, 9, 30, 0), 100.0, 12345))
    _assert("first-tick bar seeds volume 0", chart.last.volume == 0)


def main() -> None:
    tests = [
        test_same_period_accumulates,
        test_intraperiod_rollover_keeps_boundary_delta,
        test_session_rollover_uses_cumulative_not_negative_delta,
        test_first_tick_seeds_zero_no_baseline,
    ]
    for t in tests:
        print(t.__name__)
        t()
        sys.stdout.flush()
    print(f"\nAll {len(tests)} tests passed.")


if __name__ == "__main__":
    main()
