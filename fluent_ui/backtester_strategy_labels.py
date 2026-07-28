"""给回测器的策略下拉框标上中文名。

下拉框里原本只有类名（AtrRsiStrategy / DualThrustStrategy / KingKeltnerStrategy
…）。对读过源码的人够用，但对着界面选策略的人看不出它们分别用什么方法 ——
而选错策略再跑一遍回测，浪费的是时间和对结果的信任。

说明本身住在 vnpy_ctastrategy（策略本体在那儿，改策略时顺手改说明才不会脱节），
这里只负责把它接到回测器界面上。

## 为什么用运行时接管而不是直接改

vnpy_ctabacktester 是 PyPI 装的上游包，不在本项目的仓里。直接改 site-packages
的后果是：重装即失效，且改动不在任何 git 历史里。所以沿用本目录既有的做法
（backtester_segments / backtester_gates / backtester_metrics 都是这个路子）：
包装 BacktesterManager 的方法，在上游做完自己的事之后再改标。

## 钩子必须挂在"下拉框已经有内容"之后

第一版挂在 `init_ui` 上，一条也没改到 —— 因为 `class_combo` 不是在 `init_ui`
里填的：`__init__` 的顺序是 `init_ui()`(建控件) → `init_strategy_settings()`
(addItems 填内容) → `load_backtesting_setting()`(按存档选中)。挂在 init_ui
就等于对着一个空下拉改标，而安装函数照样返回"已包装"，启动日志于是报了一句
不成立的成功。现在挂在 `load_backtesting_setting` 之后（`__init__` 的最后一步），
并且安装函数只声称"已挂钩"，不声称"已改标" —— 改没改得等到界面真建起来。

## 一个必须配对的改动

改了下拉框的**显示文本**，就必须改**每一个读取方** —— 上游用
`class_combo.currentText()` 取类名，而显示文本现在是
`AtrRsiStrategy · ATR+RSI 波动突破`。漏掉一处，那条路就会拿着带中文的字符串
去找策略类。上游共四处读取：start_backtesting / start_optimization /
edit_strategy_code / reload_strategy_class（`show_optimization_result` 不读，
第一版把它也包了，是白包的）。
"""

from __future__ import annotations

from typing import Any

from vnpy.trader.ui import QtCore, QtWidgets
from vnpy_ctastrategy.ui.widget import describe_strategy

_INSTALLED_FLAG = "_vnpy_app_strategy_labels_installed"

# 只读类名的方法：临时把显示文本换回纯类名，调完换回来。
_READERS = ("start_backtesting", "start_optimization", "edit_strategy_code")

# 既读类名、又把整个下拉清空重填的方法：调完必须重新标一遍。
_REPOPULATORS = ("reload_strategy_class",)

# 在它跑完之后下拉框才有内容，是第一次标注的时机。
_POPULATED_AFTER = "load_backtesting_setting"


def _relabel(combo: QtWidgets.QComboBox) -> int:
    """把下拉框里的类名换成 `类名 · 中文`，类名存进 userData。

    幂等：已经带过 userData 的项不再处理，重复调用不会把说明叠加两遍。
    返回本次改动的条目数（0 表示下拉框是空的或早已标过）。
    """
    changed = 0
    for i in range(combo.count()):
        if combo.itemData(i):            # 已处理过
            continue
        class_name = combo.itemText(i)
        shown, hint = describe_strategy(class_name)
        combo.setItemText(i, shown)
        combo.setItemData(i, class_name)
        combo.setItemData(i, hint, QtCore.Qt.ItemDataRole.ToolTipRole)
        changed += 1
    return changed


def _class_name_of(combo: QtWidgets.QComboBox) -> str:
    """当前选中项的类名。userData 缺失时回落到显示文本 —— 那说明本模块没装上，
    此时显示文本就还是原始类名，回落是正确的而不是凑合。"""
    data = combo.currentData()
    return str(data) if data else str(combo.currentText())


def _combo_of(manager: Any) -> QtWidgets.QComboBox | None:
    combo = getattr(manager, "class_combo", None)
    return combo if isinstance(combo, QtWidgets.QComboBox) else None


def _wrap(original: Any, kind: str) -> Any:
    """按用途包装一个上游方法。

    kind='read'      读类名 -> 调用期间把显示文本临时换回纯类名
    kind='repopulate' 读类名且会清空重填 -> 同上，调完重新标注整个列表
    kind='populate'  调完下拉框才有内容 -> 直接标注
    """

    def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
        combo = _combo_of(self)
        if combo is None:
            return original(self, *args, **kwargs)

        if kind == "populate":
            result = original(self, *args, **kwargs)
            _relabel(combo)
            return result

        if not combo.currentData():                 # 还没标过，无需换回
            result = original(self, *args, **kwargs)
            if kind == "repopulate":
                _relabel(combo)
            return result

        # 让上游的 currentText() 读到纯类名：临时把显示文本换回去。
        index = combo.currentIndex()
        shown = combo.itemText(index)
        combo.setItemText(index, _class_name_of(combo))
        try:
            result = original(self, *args, **kwargs)
        finally:
            if kind == "repopulate":
                # 列表已被清空重填，逐项标注即可；旧的 shown 已无对应项。
                _relabel(combo)
            else:
                combo.setItemText(index, shown)
        return result

    setattr(wrapper, _INSTALLED_FLAG, True)
    return wrapper


def install_strategy_labels() -> list[str]:
    """挂上钩子。返回被包装的方法名。

    注意返回的是"已挂钩"，不是"已改标" —— 真正改标要等界面建起来、下拉框
    填上内容之后才发生。调用方的日志措辞不要越过这一点。
    """
    from vnpy_ctabacktester.ui.widget import BacktesterManager

    patched: list[str] = []
    plan = (
        [(name, "read") for name in _READERS]
        + [(name, "repopulate") for name in _REPOPULATORS]
        + [(_POPULATED_AFTER, "populate")]
    )

    for method_name, kind in plan:
        original = getattr(BacktesterManager, method_name, None)
        if original is None or getattr(original, _INSTALLED_FLAG, False):
            continue
        setattr(BacktesterManager, method_name, _wrap(original, kind))
        patched.append(f"BacktesterManager.{method_name}")

    return patched
