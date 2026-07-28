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
包装 BacktesterManager.init_ui，等它把控件建完之后再改标。

## 一个必须配对的改动

改了下拉框的**显示文本**，就必须同时改**读取方** —— 上游 widget.py 用
`class_combo.currentText()` 取类名，而显示文本现在是
`AtrRsiStrategy · ATR+RSI 波动突破`。所以类名存进 userData，并把上游那几处
读取点一并包装成读 userData。少改一处，回测就会拿着带中文的字符串去找策略类，
报 KeyError。
"""

from __future__ import annotations

from typing import Any

from vnpy.trader.ui import QtCore, QtWidgets
from vnpy_ctastrategy.ui.widget import describe_strategy

_INSTALLED_FLAG = "_vnpy_app_strategy_labels_installed"


def _relabel(combo: QtWidgets.QComboBox) -> int:
    """把下拉框里的类名换成 `类名 · 中文`，类名存进 userData。

    幂等：已经带过 userData 的项不再处理，重复调用不会把说明叠加两遍。
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


def install_strategy_labels() -> list[str]:
    """接到回测器上。返回被包装的方法名，供启动日志打印。"""
    from vnpy_ctabacktester.ui.widget import BacktesterManager

    patched: list[str] = []

    init_ui = BacktesterManager.init_ui
    if not getattr(init_ui, _INSTALLED_FLAG, False):

        def init_ui_with_labels(self: Any) -> None:
            init_ui(self)
            _relabel(self.class_combo)

        setattr(init_ui_with_labels, _INSTALLED_FLAG, True)
        BacktesterManager.init_ui = init_ui_with_labels
        patched.append("BacktesterManager.init_ui")

    # 读取方必须跟着改，否则会拿带中文的显示文本去找策略类。
    for method_name in ("start_backtesting", "start_optimization", "show_optimization_result"):
        original = getattr(BacktesterManager, method_name, None)
        if original is None or getattr(original, _INSTALLED_FLAG, False):
            continue

        def make(orig: Any) -> Any:
            def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
                combo = getattr(self, "class_combo", None)
                if combo is not None and combo.currentData():
                    # 让上游的 currentText() 读到纯类名：临时把显示文本换回去，
                    # 调完再换回带说明的版本。比重写上游整段逻辑安全得多。
                    idx = combo.currentIndex()
                    shown = combo.itemText(idx)
                    combo.setItemText(idx, _class_name_of(combo))
                    try:
                        return orig(self, *args, **kwargs)
                    finally:
                        combo.setItemText(idx, shown)
                return orig(self, *args, **kwargs)

            setattr(wrapper, _INSTALLED_FLAG, True)
            return wrapper

        setattr(BacktesterManager, method_name, make(original))
        patched.append(f"BacktesterManager.{method_name}")

    return patched
