"""回测开跑前，先说清楚本地有没有这段数据。

用户点开始回测，得到十一行加载进度，然后一句"历史数据加载完成，数据量：0 /
策略回测失败，历史数据为空"。而当时库里 `NBIS.SMART` 明明有 6643 根 1h、
391 根 d、82 根 w —— 只是没有 1m，而周期框里选的是 1m。

报错本身没说错，但它没说手上有什么。差一句话，人就得自己去翻数据管理面板
逐个周期看，或者干脆以为"这个标的没数据"而放弃。

所以在上游取数之前先比对一次本地账目，只在真的对不上时讲一句，讲清三件事：
缺的是哪个周期、本地有哪些周期、各自覆盖到什么时候。对得上就一个字不说 ——
每次回测都刷一行"数据没问题"，跟没说一样，还会把真正的提示淹掉。

不拦截。上游该跑还跑、该报错还报错，这里只补一句能照着做的信息。拦下来就
等于替用户决定"这次不该跑"，而他可能正是想看看空跑什么样。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, NamedTuple

from vnpy.trader.constant import Exchange, Interval
from vnpy.trader.database import get_database

_INSTALLED_FLAG = "_vnpy_app_data_check_installed"


class _Have(NamedTuple):
    """本地某个周期的现货账目。字段齐全，可直接用。"""

    interval: Interval
    bars: int                                       # 不叫 count：会盖住 tuple.count
    start: datetime
    end: datetime


def _overviews_for(symbol: str, exchange: Exchange) -> list[_Have]:
    """本地这个标的的全部 K 线账目，按周期排序。

    BarOverview 的 interval/start/end 都声明为可空（残缺行确实可能存在），
    这里直接把不齐全的丢掉 —— 拿它去讲"有多少根、覆盖到哪天"本来就讲不出。
    """
    rows = [
        _Have(o.interval, o.count, o.start, o.end)
        for o in get_database().get_bar_overview()
        if o.symbol == symbol
        and o.exchange == exchange
        and o.interval is not None
        and o.start is not None
        and o.end is not None
    ]
    return sorted(rows, key=lambda h: h.interval.value)


def describe_gap(
    symbol: str,
    exchange: Exchange,
    interval: Interval,
    start: datetime,
    end: datetime,
) -> str:
    """本地数据接不上这次回测时的说明；接得上则返回空串。

    三种接不上的情形分开讲，因为该做的事不一样：
    - 这个标的一根 K 线都没有  -> 去下载
    - 有别的周期、就是没这个   -> 改周期，或补下这个周期
    - 周期对但时间段不重叠     -> 改日期区间
    """
    rows = _overviews_for(symbol, exchange)
    vt_symbol = f"{symbol}.{exchange.value}"
    if not rows:
        return f"本地没有 {vt_symbol} 的任何 K 线。先用「下载数据」取一段，再回测。"

    have = ", ".join(
        f"{h.interval.value}（{h.bars} 根 {h.start:%Y-%m-%d}~{h.end:%Y-%m-%d}）" for h in rows
    )
    match = next((h for h in rows if h.interval is interval), None)
    if match is None:
        return (
            f"本地没有 {vt_symbol} 的 {interval.value} 数据。"
            f"已有：{have}。改选已有的周期，或先下载 {interval.value}。"
        )

    # 账目里的时间是带时区的，回测面板给的是裸时间，比较前先对齐到同一口径。
    have_start = match.start.replace(tzinfo=None)
    have_end = match.end.replace(tzinfo=None)
    if have_end < start or have_start > end:
        return (
            f"{vt_symbol} 的 {interval.value} 数据在 "
            f"{have_start:%Y-%m-%d}~{have_end:%Y-%m-%d}，"
            f"与回测区间 {start:%Y-%m-%d}~{end:%Y-%m-%d} 不重叠。改一下日期。"
        )
    return ""


def _check(manager: Any) -> None:
    """从面板取出这次回测的四要素，比对本地账目，必要时写一行日志。

    整段都吞异常：这只是一句提示，取数据库、解析枚举都可能因环境而失败，
    绝不能因此挡住回测本身。
    """
    try:
        vt_symbol = str(manager.symbol_line.text()).strip()
        if "." not in vt_symbol:
            return                                  # 上游自己会报后缀问题
        symbol, exchange_str = vt_symbol.rsplit(".", 1)
        if exchange_str not in Exchange.__members__:
            return
        interval = Interval(str(manager.interval_combo.currentText()))
        start = manager.start_date_edit.dateTime().toPython()
        end = manager.end_date_edit.dateTime().toPython()

        message = describe_gap(symbol, Exchange[exchange_str], interval, start, end)
        if message:
            manager.write_log(f"[数据自检] {message}")
    except Exception:                               # noqa: BLE001 - 提示不能反过来搞垮回测
        return


def install_data_check() -> list[str]:
    """接到回测器上。返回被包装的方法名。"""
    from vnpy_ctabacktester.ui.widget import BacktesterManager

    patched: list[str] = []
    for method_name in ("start_backtesting", "start_optimization"):
        original = getattr(BacktesterManager, method_name, None)
        if original is None or getattr(original, _INSTALLED_FLAG, False):
            continue

        def make(orig: Any) -> Any:
            def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
                _check(self)
                return orig(self, *args, **kwargs)

            setattr(wrapper, _INSTALLED_FLAG, True)
            return wrapper

        setattr(BacktesterManager, method_name, make(original))
        patched.append(f"BacktesterManager.{method_name}")

    return patched
