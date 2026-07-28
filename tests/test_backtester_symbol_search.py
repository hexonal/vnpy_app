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
from vnpy.trader.ui import QtWidgets, create_qapp

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
    engine = types.SimpleNamespace(get_all_contracts=lambda: rows)
    line = QtWidgets.QLineEdit()
    attach_completer(line, engine)                          # type: ignore[arg-type]
    return line


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

def test_picking_writes_back_the_bare_symbol(qapp: QtWidgets.QApplication) -> None:
    """核心配对：弹窗显示带名称，输入框里只能留合法本地代码。

    若少了这一步，输入框会变成 "700.SEHK 腾讯控股"，上游拿它查合约必然落空，
    而报错只会说"找不到数据"，看不出根因在补全上。
    """
    line = _line()
    completer = line.completer()
    completer.setCompletionPrefix("腾讯")
    shown = completer.completionModel().index(0, 0).data()

    completer.activated.emit(shown)                         # 模拟点选弹窗里那一行

    assert line.text() == "700.SEHK"


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
