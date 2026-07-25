"""让回测器 GUI 显示 statistics 里的全部指标。

`vnpy_ctabacktester` 把回测结果面板要显示哪些统计项【写死】在
`StatisticsMonitor.KEY_NAME_MAP` 里，把参数寻优的目标函数候选写死在
`OptimizationSettingEditor.DISPLAY_NAME_MAP` 里。任何后来加进 statistics 字典的
指标都不会自动出现在界面上 —— 上游自己的 `rgr_ratio` 就是这样：它已经在
`calculate_statistics()` 里算了，但回测面板一直看不到。

我们 fork 的 `vnpy_ctastrategy` 又加了 RAR / R-Cubed / Robust Sharpe
（见 vnpy_ctastrategy/robust_metrics.py），同样看不到。

这里在【我们自己的 GUI 装配阶段】补上映射，而不是 fork vnpy_ctabacktester：
两个 MAP 都是普通类属性 dict，补充键值不改变上游任何行为，也就没有上游同步负担。

━━━ 一个刻意的取舍：寻优目标里不放 RAR 系列 ━━━

统计面板显示它们，但参数寻优的目标函数【不提供】RAR / R-Cubed / Robust Sharpe。
原因不是懒，是它们不适合当寻优目标：

RAR 对单期收益的隐含权重正比于 (n² − j²)，随时间单调递减到接近零
（n=250 时首期约等权的 1.50×，末期仅 0.012×）。拿它当目标函数去搜参数，
等于告诉优化器"把收益尽量堆在样本前段"——而样本前段的表现恰恰是最不该被
奖励的部分。R-Cubed 与 Robust Sharpe 的分子都是 RAR，同样继承这个性质。

它们的正确用法是与端点年化【并列阅读】，看两者的差值判断收益的时间分布，
而不是当作被最大化的目标。详见 vnpy_ctastrategy/robust_metrics.py 的模块文档。
"""

from __future__ import annotations

from vnpy.trader.locale import _


# 显示用：补进回测结果面板。键必须与 calculate_statistics() 产出的键一致。
EXTRA_STATISTICS_LABELS: dict[str, str] = {
    "rgr_ratio": _("RGR比率"),
    "regressed_annual_return": _("回归年化收益"),
    "r_cubed": _("R-Cubed"),
    "robust_sharpe": _("稳健夏普"),
    "drawdown_episode_count": _("回撤段数"),
}

# 需要按百分比格式化的新键（面板对不同量纲的键分别格式化）。
PERCENT_STATISTICS_KEYS: frozenset[str] = frozenset({"regressed_annual_return"})


def install_extra_metrics() -> list[str]:
    """把新指标补进回测器 GUI 的显示映射。返回实际新增的键，便于日志与测试。

    幂等：重复调用只会补一次。若上游某天自己加了同名键，这里不覆盖它的译名。
    导入失败（未安装回测器）时静默跳过 —— GUI 不该因为一个可选 App 缺席而起不来。
    """
    try:
        from vnpy_ctabacktester.ui.widget import StatisticsMonitor
    except ImportError:
        return []

    key_map: dict = StatisticsMonitor.KEY_NAME_MAP
    added: list[str] = []
    for key, label in EXTRA_STATISTICS_LABELS.items():
        if key not in key_map:
            key_map[key] = label
            added.append(key)
    return added
