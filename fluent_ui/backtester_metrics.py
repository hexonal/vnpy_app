"""让回测器 GUI 显示 statistics 里的全部指标，并且显示成人能读的样子。

`vnpy_ctabacktester` 把回测结果面板要显示哪些统计项【写死】在
`StatisticsMonitor.KEY_NAME_MAP` 里，把参数寻优的目标函数候选写死在
`OptimizationSettingEditor.DISPLAY_NAME_MAP` 里。任何后来加进 statistics 字典的
指标都不会自动出现在界面上 —— 上游自己的 `rgr_ratio` 就是这样：它已经在
`calculate_statistics()` 里算了，但回测面板一直看不到。

我们 fork 的 `vnpy_ctastrategy` 又加了 RAR / R-Cubed / Robust Sharpe
（见 vnpy_ctastrategy/robust_metrics.py）与 **Sharpe 显著性检验**
（见 vnpy_ctastrategy/sharpe_inference.py），同样看不到。

这里在【我们自己的 GUI 装配阶段】补上映射，而不是 fork vnpy_ctabacktester：
两个 MAP 都是普通类属性 dict，补充键值不改变上游任何行为，也就没有上游同步负担。

━━━ 为什么光补 KEY_NAME_MAP 不够 ━━━

`StatisticsMonitor.set_data()` 对它自己认识的键逐个套 `f"{v:,.2f}"`，对别的键
只做 `str(value)`。于是补进去的键会以原始形态出现在表格里：

    sharpe_tstat        →  "-0.5342484307628333"    17 位小数
    sharpe_significant  →  "False"                  英文，且与"没算"无法区分
    sharpe_method       →  "hac"
    r_cubed             →  "-0.32875363886558245"

其中 `False` 最危险：`calculate_statistics()` 里 `sharpe_significant` 的默认值
就是 False（表示"检验没跑成"），检验跑成而不显著也是 False。**"没有证据"和
"有证据说不显著"在面板上必须分得开**，否则就是在替用户编结论。

所以这里除了补映射，还在运行时把 `set_data` 包一层格式化：只加工我们补进去的
那些键，其余原样交回上游。包装函数带幂等标记，重复安装不会叠两层。

━━━ 显著性只暴露单尾，不暴露双尾 ━━━

`statistics` 里 `sharpe_pvalue`（双尾）与 `sharpe_pvalue_one_sided`（单尾）都有，
面板只显示单尾那个：`sharpe_significant` 的判定用的就是单尾
（`sharpe_inference.py`：`p_one < alpha and sharpe_annual > 0`，理由是双尾会把
"显著为负"也叫显著）。两个 p 值并排显示，只会让人拿双尾的数字去解释单尾的结论。
双尾 p、bootstrap p、偏度峰度、Ljung-Box 这些诊断量留在 statistics 字典里，
需要的人从日志或脚本里读，不进这张给人一眼看的表。

`confidence` 在 `BacktestingEngine.calculate_statistics()` 里没有传，取
`sharpe_inference()` 的默认值 0.95，所以标签写死"单尾5%"不是猜的。

━━━ DSR / PBO 为什么不在这里 ━━━

多重比较闸（deflated Sharpe、PBO）算的是**一次寻优里 N 组参数**的过拟合程度，
产出在 `OptimizationResults.gates`，不在单次回测的 statistics 字典里。往
KEY_NAME_MAP 里补 `dsr` / `pbo` 只会得到两行永远空白的表格行 —— 那是给用户
制造"有这个指标"的错觉。它们需要的是寻优结果面板，不是这张表。

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

import functools
import math
from collections.abc import Callable
from typing import Any

from vnpy.trader.locale import _

# 显示用：补进回测结果面板。键必须与 calculate_statistics() 产出的键一致。
# 顺序即表格里的行序（KEY_NAME_MAP 是 dict，补在上游 27 行之后）。
ROBUSTNESS_LABELS: dict[str, str] = {
    "rgr_ratio": _("RGR比率"),
    "regressed_annual_return": _("回归年化收益"),
    "r_cubed": _("R-Cubed"),
    "robust_sharpe": _("稳健夏普"),
    "drawdown_episode_count": _("回撤段数"),
}

# Sharpe 显著性检验（vnpy_ctastrategy/sharpe_inference.py）。读法：
#   夏普t值 / 夏普p值(单尾)  ——  这条曲线的 Sharpe 与 0 差多远
#   夏普显著(单尾5%)        ——  判定；"未计算"表示检验没跑，不是"不显著"
#   显著所需夏普            ——  这个样本量下要多大 Sharpe 才够显著
#   夏普95%下限/上限        ——  Sharpe 的区间估计，下限为负即无法排除"其实是 0"
#   HAC标准误放大           ——  >1 表示自相关在放大不确定性（iid 公式会低估 SE）
SIGNIFICANCE_LABELS: dict[str, str] = {
    "sharpe_se": _("夏普标准误"),
    "sharpe_tstat": _("夏普t值"),
    "sharpe_pvalue_one_sided": _("夏普p值(单尾)"),
    "sharpe_significant": _("夏普显著(单尾5%)"),
    "sharpe_required_for_significance": _("显著所需夏普"),
    "sharpe_ci_low": _("夏普95%下限"),
    "sharpe_ci_high": _("夏普95%上限"),
    "sharpe_hac_inflation": _("HAC标准误放大"),
    "sharpe_method": _("夏普推断方法"),
}

EXTRA_STATISTICS_LABELS: dict[str, str] = {**ROBUSTNESS_LABELS, **SIGNIFICANCE_LABELS}

# 需要按百分比格式化的新键（面板对不同量纲的键分别格式化）。
PERCENT_STATISTICS_KEYS: frozenset[str] = frozenset({"regressed_annual_return"})

# 计数类：显示整数，不加两位小数。
INTEGER_STATISTICS_KEYS: frozenset[str] = frozenset({"drawdown_episode_count"})

# 判定类：布尔翻成中文，避免表格里出现 Python 字面量 True/False。
BOOLEAN_STATISTICS_KEYS: frozenset[str] = frozenset({"sharpe_significant"})

# p 值：三位小数；小于千分之一时报 "<0.001"，避免"0.000"被读成"概率为零"。
P_VALUE_STATISTICS_KEYS: frozenset[str] = frozenset({"sharpe_pvalue_one_sided"})

# `sharpe_method` 的取值来自 sharpe_inference.SharpeInference.method。
METHOD_LABELS: dict[str, str] = {
    "hac": _("HAC(Newey-West)"),
    "iid_normal": _("iid正态"),
    "iid_nonnormal": _("iid非正态"),
}

# `calculate_statistics()` 里检验未运行时的哨兵值，以及它在面板上的样子。
NOT_COMPUTED_SENTINEL: str = "not_computed"
NOT_COMPUTED_TEXT: str = _("未计算")

_FORMATTER_FLAG: str = "_extra_metrics_formatter"


def _to_float(value: Any) -> float | None:
    """能当数看就返回 float，否则 None（原样交回上游 str()）。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number


def _format_cell(key: str, value: Any, inference_ran: bool) -> Any:
    """把一个统计值格式化成单元格文本。认不出来的原样返回。"""
    if key in SIGNIFICANCE_LABELS and not inference_ran:
        # 检验没跑成：全部显示"未计算"。此时 sharpe_significant 的 False 是
        # 默认值而不是结论，显示成"否"就是在无中生有地断言"检验过、不显著"。
        return NOT_COMPUTED_TEXT

    if key in BOOLEAN_STATISTICS_KEYS:
        return _("是") if bool(value) else _("否")

    if key == "sharpe_method":
        return METHOD_LABELS.get(str(value), str(value))

    number = _to_float(value)
    if number is None:
        return value
    if not math.isfinite(number):
        # NaN/inf 是"算不出来"，不是一个值。上游 statistics_fields() 已经把非有限值
        # 兜成 0.0/1.0，这里是防御：真漏出来也不该在表格里显示成英文 "nan"。
        return NOT_COMPUTED_TEXT

    if key in P_VALUE_STATISTICS_KEYS:
        return "<0.001" if 0.0 <= number < 0.001 else f"{number:.3f}"
    if key in PERCENT_STATISTICS_KEYS:
        return f"{number:,.2f}%"
    if key in INTEGER_STATISTICS_KEYS:
        return f"{int(number)}"
    return f"{number:,.2f}"


def format_statistics(data: dict) -> dict:
    """返回一份【副本】，其中我们补进面板的键已格式化成文本。

    走副本而不是就地改：上游 `set_data()` 是就地把 `data["capital"]` 之类换成
    字符串的，而 `BacktesterEngine.result_statistics` 存的正是同一个 dict，于是
    同一份结果显示第二次就会拿字符串去套 `:,.2f` 而抛。副本让引擎手里的数值保持
    是数值，重复显示也就不再是问题。
    """
    formatted: dict = dict(data)
    inference_ran: bool = (
        str(data.get("sharpe_method", NOT_COMPUTED_SENTINEL)) != NOT_COMPUTED_SENTINEL
    )
    for key in EXTRA_STATISTICS_LABELS:
        if key in formatted:
            formatted[key] = _format_cell(key, formatted[key], inference_ran)
    return formatted


def _safe_format(data: dict) -> dict:
    """格式化失败就退回原始 dict —— 显示层的美化不该拖垮回测结果展示。"""
    try:
        return format_statistics(data)
    except Exception:       # 任何异常都不该让整张结果表空着
        return data


def _install_cell_formatter(monitor_cls: type) -> bool:
    """给 `StatisticsMonitor.set_data` 包一层格式化。返回是否真的包了。

    幂等靠包装函数上的标记：重复安装会叠两层，第二层拿到的已是字符串，
    `float()` 会失败而把单元格退回原样（更糟的是格式化过的字符串再被加工）。
    """
    original: Callable = monitor_cls.set_data
    if getattr(original, _FORMATTER_FLAG, False):
        return False

    @functools.wraps(original)
    def set_data(self: Any, data: dict) -> None:
        original(self, _safe_format(data))

    set_data._extra_metrics_formatter = True        # type: ignore[attr-defined]
    monitor_cls.set_data = set_data                 # type: ignore[attr-defined]
    return True


def install_extra_metrics() -> list[str]:
    """把新指标补进回测器 GUI 的显示映射，并接上单元格格式化。

    返回实际新增的键，便于日志与测试。

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

    _install_cell_formatter(StatisticsMonitor)
    return added
