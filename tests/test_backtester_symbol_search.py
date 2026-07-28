"""回测器的「本地代码」要能搜，且搜完填进去的必须是合法本地代码。

用户："你这个搜索不了了"。这一栏是上游的纯输入框，预填 IF88.CFFEX，要求
手敲 `代码.交易所` —— 两万多个本地已知合约就在内存里，却得凭记忆敲。

难点在于"显示"和"填入"必须是两个东西：弹窗要显示 `700.SEHK 腾讯控股`
（带名称才认得出、也才能按中文搜），但填进输入框的只能是 `700.SEHK`，
带名称的整串不是合法本地代码，上游拿它查合约必然落空。
"""

from __future__ import annotations

import os
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from vnpy.trader.constant import Exchange, Product
from vnpy.trader.object import ContractData
from vnpy.trader.ui import QtCore, QtWidgets, create_qapp

from fluent_ui.backtester_symbol_search import SYMBOL_ROLE, attach_completer


@pytest.fixture(scope="module")
def qapp() -> QtWidgets.QApplication:
    existing = QtWidgets.QApplication.instance()
    if existing is not None:
        return existing                                     # type: ignore[return-value]
    return create_qapp()


def _contract(symbol: str, name: str, exchange: Exchange) -> ContractData:
    return ContractData(
        symbol=symbol,
        exchange=exchange,
        name=name,
        product=Product.EQUITY,
        size=1,
        pricetick=0.01,
        gateway_name="TEST",
    )


CONTRACTS = [
    _contract("NBIS", "NEBIUS GROUP", Exchange.SMART),
    _contract("700", "腾讯控股", Exchange.SEHK),
    _contract("NVDA", "NVIDIA CORP", Exchange.SMART),
    _contract("9988", "阿里巴巴", Exchange.SEHK),
]


def _line(contracts: list[ContractData] | None = None) -> QtWidgets.QLineEdit:
    # 用 is None 而不是 `contracts or CONTRACTS`：空列表是 falsy，会被当成
    # "没传参"回落到全量，而空列表正是要测的那种情况（还没连上网关）。
    rows = CONTRACTS if contracts is None else contracts
    engine = types.SimpleNamespace(get_all_contracts=lambda: list(rows))
    line = QtWidgets.QLineEdit()
    attach_completer(line, engine)                          # type: ignore[arg-type]
    return line


def _type(line: QtWidgets.QLineEdit, text: str) -> list[str]:
    """模拟用户敲字并返回弹窗内容。

    QLineEdit 被编辑时发的正是 textEdited —— 补全表按需刷新就挂在它上面，
    所以测试必须走这条路，直接 setCompletionPrefix 会跳过刷新、测不出陈旧。
    """
    line.setText(text)
    line.textEdited.emit(text)
    completer = line.completer()
    completer.setCompletionPrefix(text)
    model = completer.completionModel()
    return [model.index(row, 0).data() for row in range(model.rowCount())]


def _matches(line: QtWidgets.QLineEdit, typed: str) -> list[str]:
    completer = line.completer()
    completer.setCompletionPrefix(typed)
    model = completer.completionModel()
    return [model.index(row, 0).data() for row in range(model.rowCount())]


# ── 搜得到 ──────────────────────────────────────────────────────────

def test_search_by_code(qapp: QtWidgets.QApplication) -> None:
    assert _matches(_line(), "NB") == ["NBIS.SMART NEBIUS GROUP"]


def test_search_by_chinese_name(qapp: QtWidgets.QApplication) -> None:
    """按中文名搜是这栏最该有的能力 —— 代码记不住，名字记得住。"""
    assert _matches(_line(), "腾讯") == ["700.SEHK 腾讯控股"]


def test_search_by_english_name_ignores_case(qapp: QtWidgets.QApplication) -> None:
    assert _matches(_line(), "nvid") == ["NVDA.SMART NVIDIA CORP"]


def test_search_by_exchange_lists_that_market(qapp: QtWidgets.QApplication) -> None:
    assert _matches(_line(), "SEHK") == ["700.SEHK 腾讯控股", "9988.SEHK 阿里巴巴"]


def test_matching_is_substring_not_prefix(qapp: QtWidgets.QApplication) -> None:
    """代码在中间也要能搜到 —— 前缀匹配的话按名称搜就全废了。"""
    assert _matches(_line(), "GROUP") == ["NBIS.SMART NEBIUS GROUP"]


# ── 填进去的是纯代码 ────────────────────────────────────────────────

def _pick_from_popup(line: QtWidgets.QLineEdit, typed: str) -> str:
    """走真实的选中路径：打字 -> 弹窗 -> ↓ -> 回车，返回输入框最终内容。

    必须走真路径。上一版只手工 `completer.activated.emit(...)`，那条路能过，
    但真实选中会触发三次 textChanged —— Qt 先写显示文本、我们的槽写纯代码、
    Qt 在我们之后**又写一次**显示文本。手工 emit 看不到最后那一次，于是测试
    通过而界面上留下的仍是带名称的整串。
    """
    from PySide6.QtTest import QTest

    def pump() -> None:
        # 弹窗的显示与按键投递都要过事件循环；少了它按键会落空，
        # 表现成"文本停在刚敲进去的那几个字符"。
        QtWidgets.QApplication.processEvents()

    line.show()
    line.setFocus()
    pump()
    line.setText(typed)
    line.textEdited.emit(typed)

    completer = line.completer()
    completer.setCompletionPrefix(typed)
    completer.complete()
    pump()
    QTest.keyClick(completer.popup(), QtCore.Qt.Key.Key_Down)
    pump()
    QTest.keyClick(completer.popup(), QtCore.Qt.Key.Key_Return)
    pump()
    return line.text()


def test_picking_writes_back_the_bare_symbol(qapp: QtWidgets.QApplication) -> None:
    """核心配对：弹窗显示带名称，输入框里只能留合法本地代码。

    若少了这一步，输入框会变成 "700.SEHK 腾讯控股" —— 上游拿它查合约必然落空，
    而报错只说"交易所后缀不正确"，看不出根因在补全上（用户实测撞到过）。
    """
    assert _pick_from_popup(_line(), "700") == "700.SEHK"


def test_picking_an_english_named_contract(qapp: QtWidgets.QApplication) -> None:
    assert _pick_from_popup(_line(), "NBIS") == "NBIS.SMART"


def test_nameless_contract_survives_normalisation(qapp: QtWidgets.QApplication) -> None:
    """名称为空时显示文本本就等于纯代码，归一必须原样放过、不能空转。"""
    line = _line([_contract("1", "", Exchange.SEHK)])
    assert _pick_from_popup(line, "1.SEHK") == "1.SEHK"


def test_pasting_a_display_string_is_normalised(qapp: QtWidgets.QApplication) -> None:
    """归一盯的是文本内容而非某条信号，所以粘贴进来的整串同样会被换掉。"""
    line = _line()
    line.setText("NBIS.SMART NEBIUS GROUP")
    assert line.text() == "NBIS.SMART"


def test_every_row_carries_a_bare_symbol(qapp: QtWidgets.QApplication) -> None:
    """每一项都得带纯代码，否则那一项被选中时就写不回去。"""
    model = _line().completer().model()
    for row in range(model.rowCount()):
        symbol = model.index(row, 0).data(SYMBOL_ROLE)
        assert symbol and " " not in symbol and "." in symbol


def test_nameless_contract_does_not_leave_a_trailing_space(
    qapp: QtWidgets.QApplication,
) -> None:
    """有些网关不回名称，别显示成 "700.SEHK "（尾随空格会被当成显示文本一部分）。"""
    line = _line([_contract("700", "", Exchange.SEHK)])
    assert _matches(line, "700") == ["700.SEHK"]


# ── 不该退化的地方 ──────────────────────────────────────────────────

def test_free_text_still_works(qapp: QtWidgets.QApplication) -> None:
    """补全是帮忙不是限制 —— 本地没有的合约照样要能手输回测。"""
    line = _line()
    line.setText("IF88.CFFEX")
    assert line.text() == "IF88.CFFEX"


def test_no_contracts_yields_an_empty_but_working_field(
    qapp: QtWidgets.QApplication,
) -> None:
    """还没连网关时装上补全也不该炸，手输仍可用。"""
    line = _line([])
    assert line.completer().model().rowCount() == 0
    line.setText("IF88.CFFEX")
    assert line.text() == "IF88.CFFEX"


# ── 合约是异步到的，不能开机快照一次 ─────────────────────────────────

def test_contracts_arriving_after_the_panel_is_built(qapp: QtWidgets.QApplication) -> None:
    """用户报的那一个：面板建好时合约还没查回来，之后一直搜不到。

    实测启动日志：补丁装好与面板建成同在 10:07:33，四个市场的合约到
    10:07:34~35 才陆续查完。而回测面板整个进程只建一次 —— 开机快照下来的
    空表再也不会更新，表现就是"打字没有任何反应"。
    """
    pool: list[ContractData] = []
    line = _line(pool)

    assert _type(line, "NBIS") == [], "合约还没到，本来就该搜不到"

    pool.append(_contract("NBIS", "NEBIUS GROUP", Exchange.SMART))
    assert _type(line, "NBIS") == ["NBIS.SMART NEBIUS GROUP"]


def test_a_gateway_connected_later_also_shows_up(qapp: QtWidgets.QApplication) -> None:
    """后连的网关同样要能搜到 —— 按需刷新顺带把这件事也办了。"""
    pool = [_contract("NBIS", "NEBIUS GROUP", Exchange.SMART)]
    line = _line(pool)
    _type(line, "NBIS")

    pool.append(_contract("9988", "阿里巴巴", Exchange.SEHK))
    assert _type(line, "阿里") == ["9988.SEHK 阿里巴巴"]


def test_rebuild_only_happens_when_the_count_changes(qapp: QtWidgets.QApplication) -> None:
    """敲一串字符不该反复重建两万多行。"""
    calls = {"n": 0}

    def counted() -> list[ContractData]:
        calls["n"] += 1
        return CONTRACTS

    engine = types.SimpleNamespace(get_all_contracts=counted)
    line = QtWidgets.QLineEdit()
    attach_completer(line, engine)                          # type: ignore[arg-type]
    before = line.completer().model().rowCount()

    for text in ("N", "NB", "NBI", "NBIS"):
        line.setText(text)
        line.textEdited.emit(text)

    assert line.completer().model().rowCount() == before, "数量没变却重建了模型"
