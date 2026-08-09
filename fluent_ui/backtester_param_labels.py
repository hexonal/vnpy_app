"""点完「开始回测」弹出来的那个参数框，别再把 Python 的 repr 印给交易员看。

`vnpy_ctabacktester/ui/widget.py:698` 是 `form.addRow(f"{name} {type_}", edit)`，
屏幕上于是一行行写着 `atr_length <class 'int'>`。参数名对写策略的人够用，
`<class 'int'>` 却是 Python 内部表示漏到了界面上 —— 而这个框是「点开始回测」
之后**必经**的一步，不是藏在设置里的边角。

寻优那个框（`:928` 的 `grid.addWidget(QtWidgets.QLabel(name), row, 0)`）没印
repr，但也只有英文原名：对着它填「开始 / 步进 / 结束」的人得先猜 `rsi_entry`
是阈值还是周期，而猜错的后果是白跑一轮几十分钟的参数扫描。

fork 侧的 `SettingEditor` 早就换成 `describe_parameter` 了（`vnpy_ctastrategy/
ui/widget.py:394`），只有回测器这条路没人管 —— 同一个参数在实盘面板里叫
「RSI 进场阈值（rsi_entry）」，在回测面板里叫 `rsi_entry <class 'int'>`。
本模块复用同一个函数，把两处对齐，不另起一套中文。

## 为什么不改 addRow 的那行字，而是事后按控件反查标签

上游把标签文本当成参数写进了 `addRow` / `QLabel(name)`，要在原地改就得替换整段
`init_ui`。那是把上游一百多行照抄进本仓，下次上游改版即静默失配。

改成事后反查：`QFormLayout.labelForField(edit)` 与
`QGridLayout.itemAtPosition(row, 0)` 都是按**控件**定位，不依赖上游用了什么
格式串。上游哪天把 `f"{name} {type_}"` 换成别的写法，这里照样能找对标签；
真找不到（布局被重写）就跳过那一行，界面退回英文原名而不是崩掉。

反查的入口是 `self.edits` —— 两个对话框都把 `参数名 -> 控件` 存在这里
（回测框存 `(edit, type_)` 二元组，寻优框存 `{"type","start","step","end"}`），
它是上游自己要用的数据（`get_setting()` 靠它取值），不是我们额外挂的，
所以不存在「我们记的和界面上的对不上」这种偏差。

## 一条诚实边界

`describe_parameter` 查不到的参数名一律回落到原名。树外策略每加一个自研参数，
就得回 `vnpy_ctastrategy/ui/widget.py` 的 `_PARAM_LABELS` 补一行 —— 那份表的
docstring 里写清了为什么不做「策略自带标签」这条路（渲染标签的地方拿不到策略
类）。本模块只负责把已有的表接到回测器上，不重复那套判断。
"""

from __future__ import annotations

import functools
from typing import Any

from vnpy.trader.ui import QtWidgets
from vnpy_ctastrategy.ui.widget import describe_parameter, describe_strategy

_INSTALLED_FLAG = "_vnpy_app_param_labels"


def _apply(label: QtWidgets.QLabel, name: str, type_: type) -> None:
    """把一个标签换成中文，并把原名与类型挪进 tooltip。

    原名不能丢：报错信息、策略源码、`~/.vntrader` 里的存档用的都是它，
    只剩中文的界面反而对不上号。`describe_parameter` 返回的 `shown` 本身就是
    `中文（原名）` 的形式，tooltip 里再带一次类型与一句话说明。
    """
    shown, tip = describe_parameter(name, type_)
    label.setText(shown)
    label.setToolTip(tip)


def relabel_backtesting_form(dialog: Any) -> int:
    """回测参数框（QFormLayout）—— 返回换掉的行数。

    tooltip 同时挂到输入框上：用户的鼠标多半停在自己正在填的那个格子里，
    而不是左边的标签上。
    """
    changed = 0
    for name, (edit, type_) in dict(dialog.edits).items():
        parent = edit.parentWidget()
        form = parent.layout() if parent is not None else None
        if not isinstance(form, QtWidgets.QFormLayout):
            continue

        label = form.labelForField(edit)
        if not isinstance(label, QtWidgets.QLabel):
            continue

        _apply(label, name, type_)
        edit.setToolTip(label.toolTip())
        changed += 1
    return changed


def relabel_optimization_grid(dialog: Any) -> int:
    """寻优参数框（QGridLayout）—— 返回换掉的行数。

    行号从「开始」那个输入框反查（`getItemPosition`），而不是按 `self.edits` 的
    插入顺序数：上游是从第 3 行起排的，且只收 int/float 参数、其余 `continue`
    跳过（`:919`）。按顺序数一旦遇到被跳过的参数就整体错位，而错位的后果是
    把「ATR 周期」的中文标签贴到 RSI 那一行上 —— 比不标更糟。
    """
    changed = 0
    for name, spec in dict(dialog.edits).items():
        start = spec.get("start")
        parent = start.parentWidget() if start is not None else None
        grid = parent.layout() if parent is not None else None
        if not isinstance(grid, QtWidgets.QGridLayout):
            continue

        index = grid.indexOf(start)
        if index < 0:
            continue
        # PySide6 把 C++ 的四个出参折成一个元组返回，但 stub 里写的是 `-> object`
        # （QtWidgets.pyi:1982），mypy 于是判定它不可下标。运行时确实是
        # (row, column, rowSpan, columnSpan)，这里只取第一个。
        position: tuple[int, int, int, int] = grid.getItemPosition(index)  # type: ignore[assignment]
        row = position[0]

        item = grid.itemAtPosition(row, 0)
        label = item.widget() if item is not None else None
        if not isinstance(label, QtWidgets.QLabel):
            continue

        _apply(label, name, spec["type"])
        changed += 1
    return changed


def retitle(dialog: Any) -> bool:
    """标题栏里的裸类名换成 `类名 · 中文`，与下拉框里选中的那一条对上。

    做法是在原标题里做字符串替换而不是自己重拼一句：上游那两句标题
    （「策略参数配置：{}」「优化参数配置：{}」）是过 gettext 的，重拼就等于把
    翻译写死在本仓。类名不在标题里（上游改了措辞）就什么都不做。
    """
    class_name = str(dialog.class_name)
    shown = describe_strategy(class_name)[0]
    title = str(dialog.windowTitle())
    if shown == class_name or shown in title or class_name not in title:
        return False
    dialog.setWindowTitle(title.replace(class_name, shown))
    return True


def _wrap(original: Any, relabel: Any) -> Any:
    """按对话框种类包装 `init_ui`：上游先把界面搭完，我们再改字。

    整段吞异常：这里改的全是显示文本，一个都改不上也只是回到今天的样子；
    而抛出去会让「开始回测」点下去什么都不发生 —— 换标签绝不能有权阻断回测。
    """

    @functools.wraps(original)
    def init_ui_with_labels(self: Any) -> None:
        original(self)
        try:
            relabel(self)
            retitle(self)
        except Exception:                           # noqa: BLE001 — 换标签不能挡住对话框
            return

    setattr(init_ui_with_labels, _INSTALLED_FLAG, True)
    return init_ui_with_labels


def install_param_labels() -> list[str]:
    """接到回测器的两个参数对话框上。返回实际包装了哪些方法，便于日志与测试。

    幂等：重复调用返回空列表。回测器没装时同样返回空列表 —— 缺一个可选 App
    不该让 GUI 起不来（与 `install_gate_verdict` / `install_segment_notice` 同款）。

    必须在建主窗口之前调？这一处**不必**：两个对话框都是点按钮时才 new 出来的，
    补丁只要早于那一次点击即可。写在与其它 backtester 补丁同一处，是为了让
    「回测器的运行时接管」集中在一段里，而不是因为顺序有要求。
    """
    try:
        from vnpy_ctabacktester.ui.widget import (
            BacktestingSettingEditor,
            OptimizationSettingEditor,
        )
    except ImportError:
        return []

    plan = (
        (BacktestingSettingEditor, relabel_backtesting_form),
        (OptimizationSettingEditor, relabel_optimization_grid),
    )

    installed: list[str] = []
    for dialog_cls, relabel in plan:
        original = dialog_cls.init_ui
        if getattr(original, _INSTALLED_FLAG, False):
            continue
        dialog_cls.init_ui = _wrap(original, relabel)
        installed.append(f"{dialog_cls.__name__}.init_ui")

    return installed
