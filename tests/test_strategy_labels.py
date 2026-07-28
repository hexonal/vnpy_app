"""策略下拉框的中文标注。

这类改动的危险在于**成对**：改了显示文本，读取方就必须跟着改。上游
BacktesterManager 用 `class_combo.currentText()` 取类名，而显示文本现在是
`AtrRsiStrategy · ATR+RSI 波动突破` —— 少改一处，回测就会拿着带中文的字符串
去找策略类。所以这里不只验"标签好看"，更验"类名仍能取到"。
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from vnpy.trader.ui import QtWidgets, create_qapp

from fluent_ui.backtester_strategy_labels import _class_name_of, _relabel


# 一个进程只能有一个 QApplication。单跑本文件时没人抢先建，create_qapp() 能过；
# 但 pytest 跑整个目录时，同目录别的模块在【导入期】就建好了 app，这里再建一次
# 会抛 "Please destroy the QApplication singleton"。所以先取现成的 —— 这也是
# test_backtester_segments / _gates / _significance_panel 用的同一个约定。
@pytest.fixture(scope="module")
def qapp() -> QtWidgets.QApplication:
    existing = QtWidgets.QApplication.instance()
    if existing is not None:
        return existing                                     # type: ignore[return-value]
    return create_qapp()


def _combo(qapp, names: list[str]) -> QtWidgets.QComboBox:
    c = QtWidgets.QComboBox()
    c.addItems(names)
    return c


def test_label_shows_chinese_but_keeps_the_class_name_readable(qapp) -> None:
    c = _combo(qapp, ["AtrRsiStrategy"])
    _relabel(c)
    assert "ATR+RSI" in c.itemText(0)
    assert "AtrRsiStrategy" in c.itemText(0), "类名不该被中文顶掉 —— 查文档要用它"


def test_class_name_is_still_retrievable(qapp) -> None:
    """最要紧的一条：改了显示文本之后，类名必须仍能原样取回。

    取不回来的后果不是界面难看，是回测直接找不到策略类。
    """
    c = _combo(qapp, ["DualThrustStrategy"])
    _relabel(c)
    assert _class_name_of(c) == "DualThrustStrategy"
    assert c.currentText() != "DualThrustStrategy", "前提不成立：显示文本没被改过"


def test_unknown_strategy_shows_its_class_name_unchanged(qapp) -> None:
    """自定义策略查不到说明时原样显示，不编中文。"""
    c = _combo(qapp, ["MyPrivateAlphaStrategy"])
    _relabel(c)
    assert c.itemText(0) == "MyPrivateAlphaStrategy"
    assert _class_name_of(c) == "MyPrivateAlphaStrategy"


def test_relabel_is_idempotent(qapp) -> None:
    """install 可能被调用多次（重连、重开面板），说明不能越叠越长。"""
    c = _combo(qapp, ["TurtleSignalStrategy"])
    _relabel(c)
    once = c.itemText(0)
    assert _relabel(c) == 0, "第二次应当没有需要处理的项"
    assert c.itemText(0) == once


def test_fallback_when_module_not_installed(qapp) -> None:
    """没标注过的下拉框，_class_name_of 要能回落到显示文本。

    那种情况下显示文本本就是原始类名，回落是正确答案而不是凑合。
    """
    c = _combo(qapp, ["DoubleMaStrategy"])          # 故意不 _relabel
    assert _class_name_of(c) == "DoubleMaStrategy"


def test_test_strategy_is_marked_as_non_trading(qapp) -> None:
    """TestStrategy 的 on_bar 是 pass，拿它回测会得到一片空白。

    它排在下拉框中间，名字看着像"测试用的策略"而不是"不交易的骨架"——
    标注必须说清，否则用户会以为是回测坏了。
    """
    c = _combo(qapp, ["TestStrategy"])
    _relabel(c)
    assert "不交易" in c.itemText(0) or "不交易" in c.itemData(0, 3)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
