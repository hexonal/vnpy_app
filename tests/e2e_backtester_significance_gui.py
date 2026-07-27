"""真起回测器窗口，跑一次真回测，把统计面板逐行打印出来。

不是 pytest 用例（`python_files = ["test_*.py"]` 不收它），是给人看的验收脚本：
`tests/test_backtester_significance_panel.py` 只建 `StatisticsMonitor` 这一个控件，
这里建的是整个 `BacktesterManager` 窗口，并且走完整条链路 ——
`BacktesterEngine.start_backtesting()`（后台线程）→ `EVENT_BACKTESTER_BACKTESTING_FINISHED`
→ `process_backtesting_finished_event()` → `statistics_monitor.set_data()`，
与用户在界面上点【开始回测】完全相同。

    QT_QPA_PLATFORM=offscreen .venv/bin/python tests/e2e_backtester_significance_gui.py

跑两次：无 edge 的随机游走（判定应为"否"）与带漂移的趋势（判定应为"是"）。
K 线是内存里造的随机游走，不碰数据库；除此之外没有任何替身。
"""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402
from vnpy.event import EventEngine  # noqa: E402
from vnpy.trader.constant import Exchange, Interval  # noqa: E402
from vnpy.trader.engine import MainEngine  # noqa: E402
from vnpy.trader.object import BarData  # noqa: E402
from vnpy.trader.ui import QtWidgets, create_qapp  # noqa: E402
from vnpy_ctabacktester import CtaBacktesterApp  # noqa: E402
from vnpy_ctabacktester.ui.widget import BacktesterManager  # noqa: E402
from vnpy_ctastrategy import backtesting as backtesting_module  # noqa: E402

from fluent_ui.backtester_metrics import install_extra_metrics  # noqa: E402

START = datetime(2024, 1, 1)
SIGNIFICANCE_PREFIXES = ("夏普", "显著", "HAC")


def make_bars(n: int, seed: int, drift: float) -> list[BarData]:
    """几何随机游走日线。drift=0.0008 近似无 edge，drift=0.006 是明显的趋势。"""
    rng = np.random.default_rng(seed)
    prices = 100.0 * np.exp(np.cumsum(rng.standard_normal(n) * 0.02 + drift))
    bars: list[BarData] = []
    moment = START
    for price in prices:
        while moment.weekday() >= 5:
            moment += timedelta(days=1)
        bars.append(BarData(
            symbol="NOISE", exchange=Exchange.SEHK, datetime=moment,
            interval=Interval.DAILY, open_interest=0.0, volume=1000.0,
            turnover=1000.0 * price, open_price=price, high_price=price * 1.01,
            low_price=price * 0.99, close_price=price, gateway_name="TEST",
        ))
        moment += timedelta(days=1)
    return bars


def qapp() -> QtWidgets.QApplication:
    app = QtWidgets.QApplication.instance()
    if app is None:
        app = create_qapp("e2e-significance")
    assert isinstance(app, QtWidgets.QApplication)
    return app


def run(drift: float, label: str) -> None:
    bars = make_bars(700, 20260726, drift)
    backtesting_module.load_bar_data = lambda *args, **kwargs: bars

    app = qapp()

    # run_gui.py 的顺序：先补映射，再建窗口 —— StatisticsMonitor.init_ui() 是按
    # KEY_NAME_MAP 建行的，窗口建完再补映射对已存在的实例无效。
    print(f"install_extra_metrics() -> {install_extra_metrics()}")

    event_engine = EventEngine()
    main_engine = MainEngine(event_engine)
    main_engine.add_app(CtaBacktesterApp)

    manager = BacktesterManager(main_engine, event_engine)
    manager.show()
    manager.backtester_engine.start_backtesting(
        class_name="TurtleSignalStrategy", vt_symbol="NOISE.SEHK",
        interval=Interval.DAILY.value, start=START, end=bars[-1].datetime,
        rate=0.0, slippage=0.0, size=1, pricetick=0.001, capital=1_000_000,
        setting={},
    )

    monitor = manager.statistics_monitor
    deadline = time.time() + 60
    while time.time() < deadline:
        app.processEvents()
        cell = monitor.cells.get("sharpe_tstat")
        if cell is not None and cell.text():
            break
        time.sleep(0.05)

    print(f"\n===== {label} · 窗口里的统计面板（{monitor.rowCount()} 行）=====")
    for row in range(monitor.rowCount()):
        name = monitor.verticalHeaderItem(row).text()
        text = monitor.item(row, 0).text()
        mark = "  <<< 显著性" if name.startswith(SIGNIFICANCE_PREFIXES) else ""
        print(f"  {name:<22}{text}{mark}")

    stats = manager.backtester_engine.get_result_statistics()
    assert stats is not None
    print(
        "\n引擎手里的 statistics 仍是数值（格式化只作用于显示用的副本）："
        f"sharpe_tstat={stats['sharpe_tstat']!r}, capital={stats['capital']!r}"
    )

    manager.close()
    main_engine.close()


if __name__ == "__main__":
    run(0.0008, "无 edge 的回测")
    run(0.006, "有趋势的回测")
