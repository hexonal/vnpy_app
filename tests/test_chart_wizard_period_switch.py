"""Unit tests for the broker-app period strip acting on the ACTIVE chart —
clicking 日/时/周 reloads the currently visible chart at that period, tab
switches re-highlight the strip, and programmatic syncs don't recurse into
a reload. This is the fix for "点击周期不生效" (the strip previously only
affected the next new_chart, never an already-open chart).

Qt-free: the tested methods only touch plain state + a few widget calls
(tab text / pivot / chart.clear_all / engine.query_history), all faked so
the real ChartWizardWidget methods run unchanged against captured state.
"""

from __future__ import annotations

import os
import sys
from types import MethodType, SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vnpy.trader.constant import Interval  # noqa: E402

from fluent_ui.chart_wizard import ChartWizardWidget  # noqa: E402


class _FakeChart:
    def __init__(self) -> None:
        self.cleared = 0

    def clear_all(self) -> None:
        self.cleared += 1


class _FakeTab:
    """Minimal QTabWidget stand-in: one active tab, mutable text."""

    def __init__(self, text: str) -> None:
        self._text = text
        self._index = 0

    def currentIndex(self) -> int:
        return self._index

    def tabText(self, index: int) -> str:
        return self._text

    def setTabText(self, index: int, text: str) -> None:
        self._text = text


class _FakePivot:
    def __init__(self) -> None:
        self.current = "日"
        self.set_calls: list[str] = []

    def setCurrentItem(self, route_key: str) -> None:
        self.current = route_key
        self.set_calls.append(route_key)


class _FakeEngine:
    def __init__(self) -> None:
        self.history_calls: list[tuple] = []

    def query_history(self, vt_symbol, interval, start, end) -> None:
        self.history_calls.append((vt_symbol, interval, start, end))


def _make_self(vt_symbol: str, interval: Interval, period_label: str):
    chart = _FakeChart()
    fake = SimpleNamespace(
        charts={vt_symbol: chart},
        chart_intervals={vt_symbol: interval},
        running_bars={vt_symbol: object()},
        _last_tick_volume={vt_symbol: 123.0},
        _last_tick_time={vt_symbol: object()},
        tab=_FakeTab(f"{vt_symbol} · {period_label}"),
        period_pivot=_FakePivot(),
        chart_engine=_FakeEngine(),
        _current_period=period_label,
        _syncing_period=False,
    )
    # Bind the real methods so the tested code path is the shipping one.
    for name in ("_active_vt_symbol", "_on_period_changed", "_on_tab_changed", "_reload_chart"):
        setattr(fake, name, MethodType(getattr(ChartWizardWidget, name), fake))
    return fake, chart


def _assert(name: str, cond: bool) -> None:
    print(f"  {'PASS' if cond else 'FAIL'}: {name}")
    if not cond:
        raise AssertionError(name)


def test_clicking_period_reloads_active_chart() -> None:
    fake, chart = _make_self("0700.SEHK", Interval.DAILY, "日")
    fake._on_period_changed("周")  # user clicks 周
    _assert("chart cleared once", chart.cleared == 1)
    _assert("interval switched to WEEKLY", fake.chart_intervals["0700.SEHK"] == Interval.WEEKLY)
    _assert("history re-queried at WEEKLY", fake.chart_engine.history_calls[-1][1] == Interval.WEEKLY)
    _assert("tab text updated to 周", fake.tab.tabText(0) == "0700.SEHK · 周")
    _assert("live-bar state reset", "0700.SEHK" not in fake.running_bars)
    _assert("current period is 周", fake._current_period == "周")


def test_programmatic_sync_does_not_reload() -> None:
    fake, chart = _make_self("0700.SEHK", Interval.DAILY, "日")
    fake._syncing_period = True
    fake._on_period_changed("周")  # simulated tab-switch sync, not a user click
    _assert("no reload during sync", chart.cleared == 0)
    _assert("no history query during sync", fake.chart_engine.history_calls == [])
    _assert("current period still recorded", fake._current_period == "周")


def test_tab_switch_syncs_period_strip() -> None:
    # Active chart is on WEEKLY but the strip currently shows 日.
    fake, _chart = _make_self("0700.SEHK", Interval.WEEKLY, "周")
    fake._current_period = "日"
    fake.period_pivot.current = "日"
    fake._on_tab_changed(0)
    _assert("strip set to the chart's period 周", fake.period_pivot.current == "周")
    _assert("current period synced to 周", fake._current_period == "周")
    _assert("sync flag reset after", fake._syncing_period is False)


def test_period_click_with_no_open_chart_is_safe() -> None:
    fake, _chart = _make_self("0700.SEHK", Interval.DAILY, "日")
    fake.charts = {}  # no open chart
    fake.tab = _FakeTab("")  # empty
    fake.tab._index = -1  # QTabWidget currentIndex() is -1 when empty
    fake._on_period_changed("周")
    _assert("no history query with no chart", fake.chart_engine.history_calls == [])
    _assert("period still recorded for next new_chart", fake._current_period == "周")


def main() -> None:
    tests = [
        test_clicking_period_reloads_active_chart,
        test_programmatic_sync_does_not_reload,
        test_tab_switch_syncs_period_strip,
        test_period_click_with_no_open_chart_is_safe,
    ]
    for t in tests:
        print(t.__name__)
        t()
        sys.stdout.flush()
    print(f"\nAll {len(tests)} tests passed.")


if __name__ == "__main__":
    main()
