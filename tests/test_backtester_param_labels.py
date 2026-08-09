"""回测的两个参数对话框：屏幕上不许再出现 Python 的 repr。

上游 `vnpy_ctabacktester/ui/widget.py:698` 是
`form.addRow(f"{name} {type_}", edit)`，点完「开始回测」弹出来的框里于是一行行
写着 `atr_length <class 'int'>`。寻优那个框（`:928`）没印 repr，但也只有英文
原名。这两个框都是必经的一步，不是藏在设置里的边角。

用例直接构造上游真的对话框，不做假替身：本模块的全部风险都在「反查得到反查
不到那个 QLabel」上 —— 拿 Fake 布局测等于把唯一会出错的地方测掉了。

寻优那条尤其要在**混入非数值参数**的情况下测：上游只收 int/float、其余
`continue` 跳过（`:919`），按 `self.edits` 的插入顺序数行号就会整体错位，把
「ATR 周期」的中文贴到 RSI 那一行上 —— 比不标更糟，而且从截图上看不出来。
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from vnpy.trader.ui import QtWidgets, create_qapp
from vnpy_ctabacktester.ui.widget import (
    BacktestingSettingEditor,
    OptimizationSettingEditor,
)

from fluent_ui.backtester_param_labels import (
    install_param_labels,
    relabel_backtesting_form,
    relabel_optimization_grid,
    retitle,
)


# 一个进程只能有一个 QApplication；同目录别的模块可能已在导入期建好了。
# 与 test_strategy_labels / test_backtester_segments 用同一个约定。
@pytest.fixture(scope="module")
def qapp() -> QtWidgets.QApplication:
    existing = QtWidgets.QApplication.instance()
    if existing is not None:
        return existing                                     # type: ignore[return-value]
    return create_qapp()


# 三个 AtrRsiStrategy 的真参数 + 一个 describe_parameter 查不到的自造名。
# vt_symbol 是文本型，寻优框会跳过它 —— 混在中间正是为了逼出错位。
PARAMETERS = {
    "atr_length": 22,
    "vt_symbol": "700.SEHK",
    "rsi_entry": 16,
    "zz_unknown_knob": 3,
}


def _labels_of_form(dialog: BacktestingSettingEditor) -> dict[str, str]:
    """参数名 -> 那一行标签上现在写着什么。"""
    texts = {}
    for name, (edit, _type) in dialog.edits.items():
        form = edit.parentWidget().layout()
        texts[name] = form.labelForField(edit).text()
    return texts


def _labels_of_grid(dialog: OptimizationSettingEditor) -> dict[str, str]:
    texts = {}
    for name, spec in dialog.edits.items():
        grid = spec["start"].parentWidget().layout()
        row = grid.getItemPosition(grid.indexOf(spec["start"]))[0]
        texts[name] = grid.itemAtPosition(row, 0).widget().text()
    return texts


# ---------------------------------------------------------------------------
# 回测参数框
# ---------------------------------------------------------------------------

def test_backtesting_form_no_longer_prints_the_python_repr(qapp) -> None:
    """这条是用户看到的那句话本身：`atr_length <class 'int'>`。"""
    dialog = BacktestingSettingEditor("AtrRsiStrategy", dict(PARAMETERS))
    relabel_backtesting_form(dialog)
    shown = "".join(_labels_of_form(dialog).values())
    assert "<class" not in shown, f"repr 还在界面上：{shown}"


def test_backtesting_form_shows_chinese_but_keeps_the_original_name(qapp) -> None:
    """原名不能丢：报错信息、策略源码、~/.vntrader 存档用的都是它。"""
    dialog = BacktestingSettingEditor("AtrRsiStrategy", dict(PARAMETERS))
    relabel_backtesting_form(dialog)
    label = _labels_of_form(dialog)["rsi_entry"]
    assert "RSI 进场阈值" in label
    assert "rsi_entry" in label


def test_backtesting_form_falls_back_to_the_bare_name_for_unknown_parameters(qapp) -> None:
    """查不到就用原名，不编中文 —— 猜错的中文会让人按错误的理解填参数，
    而这些参数直接决定用真钱下多少股。"""
    dialog = BacktestingSettingEditor("AtrRsiStrategy", dict(PARAMETERS))
    relabel_backtesting_form(dialog)
    assert _labels_of_form(dialog)["zz_unknown_knob"] == "zz_unknown_knob"


def test_backtesting_form_puts_the_type_into_the_tooltip(qapp) -> None:
    """类型没被删掉，只是从标签挪进了 tooltip，且写成中文的「整数」而不是
    `<class 'int'>`。输入框自己也带一份 —— 鼠标多半停在正在填的格子里。"""
    dialog = BacktestingSettingEditor("AtrRsiStrategy", dict(PARAMETERS))
    relabel_backtesting_form(dialog)
    edit = dialog.edits["atr_length"][0]
    assert "整数" in edit.toolTip()
    assert "atr_length" in edit.toolTip()


def test_backtesting_form_reports_how_many_rows_it_changed(qapp) -> None:
    dialog = BacktestingSettingEditor("AtrRsiStrategy", dict(PARAMETERS))
    assert relabel_backtesting_form(dialog) == len(PARAMETERS)


# ---------------------------------------------------------------------------
# 寻优参数框
# ---------------------------------------------------------------------------

def test_optimization_grid_labels_land_on_the_right_rows(qapp) -> None:
    """混入被上游跳过的文本参数之后，标签仍要贴在自己那一行上。

    按 self.edits 的插入顺序数行号会把 rsi_entry 的中文贴到 zz_unknown_knob
    那一行；行号从「开始」输入框反查就不会。
    """
    dialog = OptimizationSettingEditor("AtrRsiStrategy", dict(PARAMETERS))
    relabel_optimization_grid(dialog)
    labels = _labels_of_grid(dialog)
    assert "vt_symbol" not in labels, "前提不成立：上游没有跳过文本参数"
    assert "ATR 周期" in labels["atr_length"]
    assert "RSI 进场阈值" in labels["rsi_entry"]
    assert labels["zz_unknown_knob"] == "zz_unknown_knob"


def test_optimization_grid_leaves_the_column_headers_alone(qapp) -> None:
    """第 2 行是「参数 / 开始 / 步进 / 结束」四个表头，不能被当成参数行改掉。"""
    dialog = OptimizationSettingEditor("AtrRsiStrategy", dict(PARAMETERS))
    grid = dialog.edits["atr_length"]["start"].parentWidget().layout()
    before = grid.itemAtPosition(2, 0).widget().text()
    relabel_optimization_grid(dialog)
    assert grid.itemAtPosition(2, 0).widget().text() == before


# ---------------------------------------------------------------------------
# 标题栏
# ---------------------------------------------------------------------------

def test_title_gets_the_same_chinese_name_as_the_dropdown(qapp) -> None:
    """下拉框里选的是 `AtrRsiStrategy · ATR+RSI 波动突破`，弹出来的框标题上
    却只有裸类名 —— 同一个东西在相邻两屏上叫两个名字。"""
    dialog = BacktestingSettingEditor("AtrRsiStrategy", dict(PARAMETERS))
    assert retitle(dialog) is True
    assert "ATR+RSI 波动突破" in dialog.windowTitle()
    assert "AtrRsiStrategy" in dialog.windowTitle()


def test_title_is_left_alone_for_a_strategy_nobody_labelled(qapp) -> None:
    dialog = BacktestingSettingEditor("MyPrivateAlphaStrategy", dict(PARAMETERS))
    before = dialog.windowTitle()
    assert retitle(dialog) is False
    assert dialog.windowTitle() == before


def test_title_is_not_appended_twice(qapp) -> None:
    """对话框每次都是新建的，但 retitle 万一被调两次也不能拼成三段。"""
    dialog = BacktestingSettingEditor("AtrRsiStrategy", dict(PARAMETERS))
    retitle(dialog)
    once = dialog.windowTitle()
    assert retitle(dialog) is False
    assert dialog.windowTitle() == once


# ---------------------------------------------------------------------------
# 安装
# ---------------------------------------------------------------------------

def test_install_wraps_both_dialogs_then_stops(qapp) -> None:
    """断言「装上了」而不是「这次调用装上了」—— 别的用例可能已经先装过，
    比返回值会让这条用例的成败取决于收集顺序。"""
    from fluent_ui.backtester_param_labels import _INSTALLED_FLAG

    install_param_labels()
    for dialog_cls in (BacktestingSettingEditor, OptimizationSettingEditor):
        assert getattr(dialog_cls.init_ui, _INSTALLED_FLAG, False)
    assert install_param_labels() == [], "重复安装不应再包一层"


def test_installed_dialog_relabels_itself_on_construction(qapp) -> None:
    """装上之后，谁也不用再手工调 relabel —— 上游自己 new 出来的框就是对的。"""
    install_param_labels()
    dialog = BacktestingSettingEditor("AtrRsiStrategy", dict(PARAMETERS))
    assert "<class" not in "".join(_labels_of_form(dialog).values())
    assert "ATR+RSI 波动突破" in dialog.windowTitle()


def test_a_dialog_whose_layout_we_cannot_read_still_opens(qapp) -> None:
    """换标签绝不能有权阻断回测。

    布局被上游改掉、反查不到 QLabel 时，正确行为是安静地退回英文原名，
    而不是让「开始回测」点下去什么都不发生。
    """

    class StubDialog:
        """edits 里的控件没有父窗口，因此反查不到任何布局。"""

        edits = {"atr_length": (QtWidgets.QLineEdit(), int)}

    assert relabel_backtesting_form(StubDialog()) == 0


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
