"""回测开跑前要说清楚本地有没有这段数据。

用户点开始回测，得到十一行加载进度，然后"数据量：0 / 策略回测失败，历史数据
为空"。而当时库里 NBIS.SMART 有 6643 根 1h、391 根 d、82 根 w —— 只是没有
1m，而周期框选的正是 1m。

报错没说错，但它没说手上有什么。差这一句，人就得自己去数据管理面板逐个周期
翻，或者干脆以为"这个标的没数据"。
"""

from __future__ import annotations

import os
import sys
import types
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vnpy.trader.constant import Exchange, Interval

import fluent_ui.backtester_data_check as check
from fluent_ui.backtester_data_check import describe_gap

TZ = ZoneInfo("Asia/Shanghai")


def _overview(
    symbol: str,
    exchange: Exchange,
    interval: Interval | None,
    count: int,
    start: str,
    end: str,
) -> types.SimpleNamespace:
    """一条 K 线账目。interval/start/end 都可为空 —— 库里确实有残缺行。"""
    return types.SimpleNamespace(
        symbol=symbol,
        exchange=exchange,
        interval=interval,
        count=count,
        start=datetime.fromisoformat(start).replace(tzinfo=TZ) if start else None,
        end=datetime.fromisoformat(end).replace(tzinfo=TZ) if end else None,
    )


# 照抄用户机器上的真实分布：NBIS 有 1h/d/w，没有 1m。
NBIS_ROWS = [
    _overview("NBIS", Exchange.SMART, Interval.HOUR, 6643, "2025-01-02", "2026-07-28"),
    _overview("NBIS", Exchange.SMART, Interval.DAILY, 391, "2025-01-02", "2026-07-27"),
    _overview("NBIS", Exchange.SMART, Interval.WEEKLY, 82, "2025-01-06", "2026-07-27"),
    _overview("700", Exchange.SEHK, Interval.MINUTE, 99801, "2023-07-24", "2024-10-16"),
]


@pytest.fixture(autouse=True)
def _fake_db(monkeypatch: pytest.MonkeyPatch) -> None:
    """用假账目替掉真实数据库 —— 断言不能取决于这台机器下过什么。"""
    db = types.SimpleNamespace(get_bar_overview=lambda: list(NBIS_ROWS))
    monkeypatch.setattr(check, "get_database", lambda: db)


def _gap(
    symbol: str,
    exchange: Exchange,
    interval: Interval,
    start: str = "2023-07-23",
    end: str = "2026-07-28",
) -> str:
    return describe_gap(
        symbol,
        exchange,
        interval,
        datetime.fromisoformat(start),
        datetime.fromisoformat(end),
    )


# ── 三种接不上，各自说清该做什么 ─────────────────────────────────────

def test_missing_interval_lists_what_is_available() -> None:
    """用户报的那一个：选了 1m，而本地只有 1h/d/w。"""
    message = _gap("NBIS", Exchange.SMART, Interval.MINUTE)

    assert "没有 NBIS.SMART 的 1m" in message
    assert "1h（6643 根" in message, "得说清有什么、有多少、覆盖到哪天"
    assert "改选已有的周期" in message, "只说缺什么不够，得说该怎么办"


def test_unknown_symbol_says_download_first() -> None:
    message = _gap("ZZZZ", Exchange.SMART, Interval.DAILY)
    assert "任何 K 线" in message and "下载数据" in message


def test_non_overlapping_dates_name_the_available_range() -> None:
    """周期对但时间段错开 —— 该改的是日期，不是周期，所以要分开讲。"""
    message = _gap("700", Exchange.SEHK, Interval.MINUTE, "2025-01-01", "2026-07-28")

    assert "2023-07-24~2024-10-16" in message
    assert "不重叠" in message and "改一下日期" in message


# ── 接得上就一个字不说 ──────────────────────────────────────────────

def test_available_data_produces_no_noise() -> None:
    """每次回测都刷一行"数据没问题"等于没说，还会把真提示淹掉。"""
    assert _gap("NBIS", Exchange.SMART, Interval.HOUR) == ""
    assert _gap("NBIS", Exchange.SMART, Interval.DAILY) == ""


def test_partial_overlap_is_fine() -> None:
    """只覆盖到区间的一部分仍算接得上 —— 上游会按实际有的跑，那是正常用法。"""
    assert _gap("700", Exchange.SEHK, Interval.MINUTE, "2023-01-01", "2026-07-28") == ""


def test_timezone_aware_bounds_do_not_break_the_comparison() -> None:
    """账目里的时间带时区，面板给的是裸时间 —— 直接比较会抛
    TypeError: can't compare offset-naive and offset-aware datetimes。"""
    assert _gap("NBIS", Exchange.SMART, Interval.HOUR, "2026-01-01", "2026-06-01") == ""


# ── 残缺账目不能把提示搞崩 ──────────────────────────────────────────

def test_rows_with_missing_fields_are_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    """interval/start/end 在 BarOverview 里都声明为可空。拿残缺行去讲
    "有多少根、覆盖到哪天"本来就讲不出，直接跳过。"""
    rows = [
        _overview("NBIS", Exchange.SMART, None, 10, "2025-01-02", "2026-07-28"),
        _overview("NBIS", Exchange.SMART, Interval.HOUR, 6643, "", ""),
    ]
    db = types.SimpleNamespace(get_bar_overview=lambda: rows)
    monkeypatch.setattr(check, "get_database", lambda: db)

    assert "任何 K 线" in _gap("NBIS", Exchange.SMART, Interval.HOUR)


def test_other_symbols_do_not_leak_in() -> None:
    """账目是全库的，必须按标的过滤 —— 否则会拿 700 的数据说 NBIS 有。"""
    message = _gap("NBIS", Exchange.SMART, Interval.MINUTE)
    assert "700" not in message
