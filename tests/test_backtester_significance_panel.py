"""回测统计面板必须回答"这个夏普显著吗"。

`vnpy_ctastrategy` 的 `calculate_statistics()` 早就把 Sharpe 显著性检验的结果
（`sharpe_tstat` / `sharpe_pvalue_one_sided` / `sharpe_significant` / ...）写进
statistics 字典了，但 `vnpy_ctabacktester` 的 `StatisticsMonitor.KEY_NAME_MAP`
里没有这些键 —— 用户在 GUI 里跑完回测，看到的还是收益率/夏普/回撤，
**看不到那个夏普是不是运气**。

本文件用【真回测 + 真 widget】验收，不验收"KEY_NAME_MAP 里有这个键"：
KEY_NAME_MAP 有键只说明会多一行，不说明那一行读得懂。`StatisticsMonitor.set_data()`
对自己不认识的键只做 `str(value)`，于是：

    sharpe_tstat        -> "-0.5342484307628333"   （17 位小数）
    sharpe_significant  -> "False"                 （英文；且与"没算"无法区分）
    sharpe_method       -> "hac"

所以断言落在**单元格文本**上：格式化过、中文、一眼能读。
"""

from __future__ import annotations

import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402
from vnpy.trader.constant import Exchange, Interval  # noqa: E402
from vnpy.trader.object import BarData  # noqa: E402
from vnpy.trader.ui import QtWidgets  # noqa: E402
from vnpy_ctabacktester.ui.widget import StatisticsMonitor  # noqa: E402
from vnpy_ctastrategy import backtesting as backtesting_module  # noqa: E402
from vnpy_ctastrategy.backtesting import BacktestingEngine  # noqa: E402
from vnpy_ctastrategy.strategies.turtle_signal_strategy import (  # noqa: E402
    TurtleSignalStrategy,
)

from fluent_ui.backtester_metrics import install_extra_metrics  # noqa: E402

# 刻意不在模块顶层 create_qapp()：test_searchable_combo_box 的 create_qapp() 是
# 无条件构造的，一个进程里出现第二个 QApplication 会抛。pytest 先收集完所有模块
# 再跑用例，所以到用例执行时那个 app 已经存在，这里复用即可；单独跑本文件时
# instance() 为空，才自己建一个（QTableWidget 是 QWidget，没有 app 会直接崩）。
_BACKTEST_START = datetime(2024, 1, 1)

# 数字单元格：允许千分位，固定两位小数。"0.386542037151583" 这种原样 str() 不通过。
_TWO_DP = re.compile(r"^-?[\d,]+\.\d{2}$")
# p 值：三位小数，或小到只能报"<0.001"。
_P_VALUE = re.compile(r"^(<0\.001|-?0\.\d{3}|1\.000)$")


def _qapp() -> QtWidgets.QApplication:
    app = QtWidgets.QApplication.instance()
    if app is None:
        app = QtWidgets.QApplication([])
    return app


def _bars(n: int, seed: int, drift: float) -> list[BarData]:
    """几何随机游走日线。drift=0.0008 近似无 edge，drift=0.006 是明显的趋势。"""
    rng = np.random.default_rng(seed)
    prices = 100.0 * np.exp(np.cumsum(rng.standard_normal(n) * 0.02 + drift))
    bars: list[BarData] = []
    moment = _BACKTEST_START
    for price in prices:
        while moment.weekday() >= 5:
            moment += timedelta(days=1)
        bars.append(BarData(
            symbol="NOISE", exchange=Exchange.SEHK, datetime=moment,
            interval=Interval.DAILY, open_interest=0.0,
            volume=1000.0, turnover=1000.0 * price,
            open_price=price, high_price=price * 1.01,
            low_price=price * 0.99, close_price=price, gateway_name="TEST",
        ))
        moment += timedelta(days=1)
    return bars


def _run_backtest(drift: float, seed: int = 20260726) -> dict:
    """真引擎 + 真策略 + 真 `calculate_statistics()`，只把数据库换成内存 K 线。

    走的是 GUI 点"开始回测"完全相同的代码路径（`vnpy_ctabacktester` 的引擎也是
    `load_data()` → `run_backtesting()` → `calculate_statistics(output=False)`）。
    """
    bars = _bars(700, seed, drift)
    original_loader = backtesting_module.load_bar_data
    backtesting_module.load_bar_data = lambda *args, **kwargs: bars
    try:
        engine = BacktestingEngine()
        engine.output = lambda msg: None            # type: ignore[method-assign]
        engine.set_parameters(
            vt_symbol="NOISE.SEHK", interval=Interval.DAILY,
            start=_BACKTEST_START, end=bars[-1].datetime,
            rate=0.0, slippage=0.0, size=1, pricetick=0.001,
            capital=1_000_000, annual_days=252,
        )
        engine.add_strategy(TurtleSignalStrategy, {})
        engine.load_data()
        engine.run_backtesting()
        engine.calculate_result()
        return engine.calculate_statistics(output=False)
    finally:
        backtesting_module.load_bar_data = original_loader


def _panel(statistics: dict) -> dict[str, str]:
    """把 statistics 灌进真 widget，读回 {行名: 单元格文本}。"""
    _qapp()
    install_extra_metrics()
    monitor = StatisticsMonitor()
    monitor.set_data(statistics)
    rows: dict[str, str] = {}
    for row in range(monitor.rowCount()):
        header = monitor.verticalHeaderItem(row)
        cell = monitor.item(row, 0)
        assert header is not None and cell is not None, f"第 {row} 行没有表头或单元格"
        rows[header.text()] = cell.text()
    return rows


def test_insignificant_backtest_shows_the_verdict_in_the_panel() -> None:
    """无 edge 的回测：面板必须显示 t/p/判定，而且是读得懂的中文与定点小数。"""
    statistics = _run_backtest(drift=0.0008)
    assert not statistics["sharpe_significant"], "本用例依赖一个不显著的样本"

    rows = _panel(statistics)

    assert "夏普t值" in rows, f"面板里没有夏普 t 值这一行，现有行：{list(rows)}"
    assert _TWO_DP.match(rows["夏普t值"]), rows["夏普t值"]
    assert _P_VALUE.match(rows["夏普p值(单尾)"]), rows["夏普p值(单尾)"]
    assert rows["夏普显著(单尾5%)"] == "否", rows["夏普显著(单尾5%)"]
    assert _TWO_DP.match(rows["显著所需夏普"]), rows["显著所需夏普"]
    assert _TWO_DP.match(rows["夏普标准误"]), rows["夏普标准误"]
    assert _TWO_DP.match(rows["夏普95%下限"]), rows["夏普95%下限"]
    assert _TWO_DP.match(rows["夏普95%上限"]), rows["夏普95%上限"]
    assert _TWO_DP.match(rows["HAC标准误放大"]), rows["HAC标准误放大"]
    assert rows["夏普推断方法"] == "HAC(Newey-West)", rows["夏普推断方法"]

    # 布尔不许以 Python 字面量的样子出现在给人看的表格里
    assert "True" not in rows.values() and "False" not in rows.values(), rows


def test_significant_backtest_shows_yes_and_a_tiny_p() -> None:
    """有趋势的回测：判定翻成"是"，小到 0 的 p 值报成 "<0.001" 而不是 "0.000"。"""
    statistics = _run_backtest(drift=0.006)
    assert statistics["sharpe_significant"], "本用例依赖一个显著的样本"

    rows = _panel(statistics)
    assert rows["夏普显著(单尾5%)"] == "是", rows["夏普显著(单尾5%)"]
    assert rows["夏普p值(单尾)"] == "<0.001", rows["夏普p值(单尾)"]
    assert float(rows["夏普t值"].replace(",", "")) > 1.96, rows["夏普t值"]


def test_not_computed_reads_as_not_computed_rather_than_no() -> None:
    """检验没跑成时 `sharpe_significant` 也是 False —— 面板不能把它显示成"否"。

    "否"=检验过、不显著；"未计算"=根本没检验。两者的操作含义完全不同
    （前者是证据，后者是没有证据），显示成同一个字就是在编结论。
    """
    statistics = _run_backtest(drift=0.0008)
    statistics.update({
        "sharpe_se": 0.0, "sharpe_tstat": 0.0, "sharpe_pvalue": 1.0,
        "sharpe_pvalue_one_sided": 1.0, "sharpe_ci_low": 0.0, "sharpe_ci_high": 0.0,
        "sharpe_significant": False, "sharpe_method": "not_computed",
        "sharpe_hac_inflation": 0.0, "sharpe_required_for_significance": 0.0,
    })

    rows = _panel(statistics)
    for label in (
        "夏普t值", "夏普p值(单尾)", "夏普显著(单尾5%)", "显著所需夏普",
        "夏普标准误", "夏普95%下限", "夏普95%上限", "HAC标准误放大", "夏普推断方法",
    ):
        assert rows[label] == "未计算", f"{label} = {rows[label]!r}"


def test_non_finite_values_read_as_not_computed_rather_than_nan() -> None:
    """算不出来的数（NaN/inf）不能以 "nan" 的样子出现在给人看的表格里。

    `statistics_fields()` 目前会把非有限值兜成 0.0/1.0，所以这条走的是防御路线：
    哪天上游把 NaN 透出来，面板也不该显示一个英文 "nan"。
    """
    statistics = _run_backtest(drift=0.0008)
    statistics["sharpe_tstat"] = float("nan")
    statistics["r_cubed"] = float("inf")

    rows = _panel(statistics)
    assert rows["夏普t值"] == "未计算", rows["夏普t值"]
    assert rows["R-Cubed"] == "未计算", rows["R-Cubed"]


def test_every_injected_key_exists_in_real_statistics() -> None:
    """补进 KEY_NAME_MAP 的键必须真在 statistics 里 —— 键名写错只会多一行空白。"""
    install_extra_metrics()
    statistics = _run_backtest(drift=0.0008)
    prefixes = ("sharpe_", "rgr_", "r_cubed", "robust_", "regressed_", "drawdown_episode")
    injected = [
        key for key in StatisticsMonitor.KEY_NAME_MAP if key.startswith(prefixes)
    ]
    assert injected, "一个显著性/稳健性指标都没补进去"
    missing = [key for key in injected if key not in statistics]
    assert not missing, f"这些键在 statistics 里不存在，面板上永远是空行：{missing}"


def test_display_does_not_stringify_the_engines_statistics_dict() -> None:
    """格式化只作用于要显示的副本，不改调用方持有的 statistics。

    上游 `set_data()` 是就地把 `data["capital"]` 之类改成字符串的，而
    `BacktesterEngine.result_statistics` 存的正是同一个 dict —— 于是同一份结果
    显示第二次就会拿字符串去套 `:,.2f` 而抛。格式化走副本顺带堵住这个洞。
    """
    statistics = _run_backtest(drift=0.0008)
    _panel(statistics)

    assert not isinstance(statistics["sharpe_tstat"], str), "显著性数值被改成了字符串"
    assert not isinstance(statistics["capital"], str), "上游数值被改成了字符串"
    _panel(statistics)          # 显示第二次不该抛


def test_existing_extra_metrics_are_formatted_too() -> None:
    """先前补进面板的 RAR 家族也走同一套格式化，不再是 17 位小数的原始 float。"""
    statistics = _run_backtest(drift=0.0008)
    rows = _panel(statistics)

    assert _TWO_DP.match(rows["R-Cubed"]), rows["R-Cubed"]
    assert _TWO_DP.match(rows["稳健夏普"]), rows["稳健夏普"]
    assert _TWO_DP.match(rows["RGR比率"]), rows["RGR比率"]
    assert rows["回归年化收益"].endswith("%"), rows["回归年化收益"]
    assert rows["回撤段数"].isdigit(), rows["回撤段数"]


def test_install_is_idempotent_including_the_formatter() -> None:
    """重复安装不能把格式化叠两层（第二层会拿字符串再格式化一次而抛）。"""
    install_extra_metrics()
    install_extra_metrics()
    install_extra_metrics()

    statistics = _run_backtest(drift=0.0008)
    rows = _panel(statistics)
    assert _TWO_DP.match(rows["夏普t值"]), rows["夏普t值"]
    assert rows["夏普推断方法"] == "HAC(Newey-West)", rows["夏普推断方法"]
