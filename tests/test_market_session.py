"""盘前/盘中/盘后 标注,以及它与共享时段表的一致性。

这个模块以前自己写死 time(9, 30) / time(16, 0)。录制服务(vnpy_recorder)按同一组
边界调度,两份拷贝迟早会对不上——图表说已收盘、调度器说还在盘中。改成从
vnpy_gatewaykit.sessions 读之后,这里断言的就是"没有第二份拷贝"。
"""

from __future__ import annotations

import sys
from datetime import datetime, time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vnpy.trader.constant import Exchange, Interval  # noqa: E402
from vnpy.trader.object import BarData  # noqa: E402
from vnpy_gatewaykit.sessions import SessionKind, sessions_for  # noqa: E402

from fluent_ui.market_session import (  # noqa: E402
    AFTER_HOURS,
    PRE_MARKET,
    REGULAR,
    is_extended,
    market_session,
)


def bar(hour: int, minute: int = 0, exchange: Exchange = Exchange.SMART) -> BarData:
    """K 线的 datetime 是市场本地时间(futu 就是这么标的),所以直接比时分。"""
    return BarData(
        symbol="MU",
        exchange=exchange,
        datetime=datetime(2026, 7, 23, hour, minute),
        interval=Interval.MINUTE,
        gateway_name="TEST",
    )


@pytest.mark.parametrize(
    ("hour", "minute", "expected"),
    [
        (4, 0, PRE_MARKET),
        (9, 29, PRE_MARKET),
        (9, 30, REGULAR),
        (12, 0, REGULAR),
        (15, 59, REGULAR),
        (16, 0, AFTER_HOURS),
        (19, 59, AFTER_HOURS),
    ],
)
def test_us_bars_are_labelled_by_session(hour: int, minute: int, expected: str) -> None:
    assert market_session(bar(hour, minute)) == expected


@pytest.mark.parametrize("hour", [9, 12, 16])
def test_hk_bars_have_no_extended_session(hour: int) -> None:
    assert market_session(bar(hour, exchange=Exchange.SEHK)) == REGULAR
    assert not is_extended(bar(hour, exchange=Exchange.SEHK))


def test_is_extended_matches_the_label() -> None:
    assert is_extended(bar(5, 0))
    assert is_extended(bar(18, 0))
    assert not is_extended(bar(10, 0))


def test_boundaries_come_from_the_shared_session_table() -> None:
    """没有第二份 09:30 / 16:00。"""
    regular = next(
        session
        for session in sessions_for(Exchange.SMART)
        if session.kind is SessionKind.REGULAR
    )
    assert regular.start == time(9, 30)
    assert regular.end == time(16, 0)

    # 边界值直接由上面那张表决定:换表即换行为。
    assert market_session(bar(regular.start.hour, regular.start.minute)) == REGULAR
    assert market_session(bar(regular.end.hour, regular.end.minute)) == AFTER_HOURS
