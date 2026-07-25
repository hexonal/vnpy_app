"""回测器 GUI 指标标签补充的测试。

vnpy_ctabacktester 把要显示的统计项写死在 KEY_NAME_MAP 里，所以后加进
statistics 字典的指标是算了但看不见。这里验证补充逻辑正确且幂等。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fluent_ui.backtester_metrics import (      # noqa: E402
    EXTRA_STATISTICS_LABELS,
    install_extra_metrics,
)


def test_install_adds_every_extra_metric() -> None:
    from vnpy_ctabacktester.ui.widget import StatisticsMonitor

    # 还原到未安装状态，保证测试与调用顺序无关
    for key in EXTRA_STATISTICS_LABELS:
        StatisticsMonitor.KEY_NAME_MAP.pop(key, None)

    added = install_extra_metrics()
    assert set(added) == set(EXTRA_STATISTICS_LABELS)
    for key, label in EXTRA_STATISTICS_LABELS.items():
        assert StatisticsMonitor.KEY_NAME_MAP[key] == label


def test_install_is_idempotent() -> None:
    install_extra_metrics()
    assert install_extra_metrics() == [], "重复调用不应重复添加"


def test_keys_match_what_the_engine_actually_produces() -> None:
    """标签的键必须与 calculate_statistics 真实产出的键一致，
    否则补了也显示不出来。用一次真实统计计算来校验，而不是照抄常量。"""
    from datetime import date, timedelta

    import numpy as np
    import pandas as pd
    from vnpy_ctastrategy.backtesting import BacktestingEngine

    n = 120
    rng = np.random.default_rng(5)
    df = pd.DataFrame(
        {
            "net_pnl": rng.normal(300.0, 5000.0, n),
            "turnover": np.zeros(n),
            "commission": np.zeros(n),
            "slippage": np.zeros(n),
            "trade_count": np.zeros(n, dtype=int),
        },
        index=pd.to_datetime([date(2026, 1, 1) + timedelta(days=i) for i in range(n)]),
    )

    engine = BacktestingEngine()
    engine.capital = 1_000_000
    engine.annual_days = 247
    stats = engine.calculate_statistics(df=df, output=False)

    for key in EXTRA_STATISTICS_LABELS:
        assert key in stats, f"标签键 {key} 在 statistics 里不存在，显示不出来"


def test_rar_family_is_not_offered_as_optimization_target() -> None:
    """刻意的取舍：RAR 对单期收益的隐含权重随时间递减到接近零，
    拿它当寻优目标等于奖励"把收益堆在样本前段"。R-Cubed 与 Robust Sharpe
    分子同为 RAR，一并排除。"""
    from vnpy_ctabacktester.ui.widget import OptimizationSettingEditor

    targets = set(OptimizationSettingEditor.DISPLAY_NAME_MAP.values())
    for forbidden in ("regressed_annual_return", "r_cubed", "robust_sharpe"):
        assert forbidden not in targets, f"{forbidden} 不应出现在寻优目标里"
