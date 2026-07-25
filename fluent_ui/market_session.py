"""Which trading session a bar belongs to — regular (盘中) vs extended
pre-market (盘前) / after-hours (盘后), for marking night-session bars
distinctly on intraday charts.

The bar's datetime is the market's own local time (futu stamps US intraday
bars in US Eastern, HK bars in HK time — both parsed naive by
futu_mapping.kline_row_to_bar), so a plain time-of-day comparison against
that market's session boundaries is correct without any timezone math.

Only US equities (Exchange.SMART) have an extended stock session here:
pre-market 04:00–09:30 ET and after-hours 16:00–20:00 ET. HK/CN stocks have
no equivalent night session, so every bar is 盘中.
"""

from __future__ import annotations

from vnpy.trader.constant import Exchange
from vnpy.trader.object import BarData
from vnpy_gatewaykit.sessions import SessionKind, sessions_for

REGULAR = "盘中"
PRE_MARKET = "盘前"
AFTER_HOURS = "盘后"

# US regular cash session in ET (the timezone futu stamps US intraday bars in),
# read from the shared session table rather than re-declared here — the
# recorder service schedules against the same rows, and two copies of 09:30 /
# 16:00 are exactly how a chart and a scheduler end up disagreeing about when
# the session changed.
_US_REGULAR = next(
    session
    for session in sessions_for(Exchange.SMART)
    if session.kind is SessionKind.REGULAR
)
_US_OPEN = _US_REGULAR.start
_US_CLOSE = _US_REGULAR.end


def market_session(bar: BarData) -> str:
    """Return 盘前 / 盘中 / 盘后 for a bar. Markets without an extended stock
    session (HK/CN) always return 盘中."""
    if bar.exchange == Exchange.SMART:
        t = bar.datetime.time()
        if t < _US_OPEN:
            return PRE_MARKET
        if t >= _US_CLOSE:
            return AFTER_HOURS
    return REGULAR


def is_extended(bar: BarData) -> bool:
    """True for pre-market / after-hours (夜盘) bars that should be drawn and
    marked distinctly from the regular session."""
    return market_session(bar) != REGULAR
