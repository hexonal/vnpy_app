"""下载历史数据时，代码下拉框要能看懂，且改了显示不能改坏读取。

起因：用户打开「下载历史数据」，代码栏里全是裸数字（1 / 2 / 5 / 700 …）。
港股代码本来就是数字，这不是显示错误 —— 但一万九千多个合约挤在一个下拉里，
只有数字就等于要靠背：看不出 1 是长和还是别的什么，也看不出这个 700 是港股
还是别的市场里同号的东西。

改法是把名称和交易所一起显示。风险全在配对上：`download()` 原本拿
`symbol_combo.text()`（显示文本）当代码用，显示一改，它就会拿
"700 腾讯控股 · SEHK" 整串去下载。所以下面第二组用例（变异测试）才是真正要
守住的东西 —— 第一组只证明显示变好看了，第二组证明它没变坏。
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from vnpy.trader.constant import Exchange, Product
from vnpy.trader.object import ContractData
from vnpy.trader.ui import QtWidgets, create_qapp

from fluent_ui.data_manager import _contract_label, _symbol_from_label


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


# ── 第一组：显示确实变得能读 ──────────────────────────────────────────

def test_hk_numeric_code_now_carries_its_name() -> None:
    """用户报的那一个：港股 1 不再是光秃秃一个 1。"""
    label = _contract_label(_contract("1", "长和", Exchange.SEHK))
    assert label == "1 长和 · SEHK"


def test_same_number_in_two_markets_is_distinguishable() -> None:
    """裸数字最要命的地方：不同市场的同号合约长得一模一样。"""
    hk = _contract_label(_contract("700", "腾讯控股", Exchange.SEHK))
    us = _contract_label(_contract("700", "SOME US THING", Exchange.SMART))
    assert hk != us


def test_nameless_contract_does_not_leave_double_spaces() -> None:
    """有些合约的 name 是空的（部分网关不回名称），别显示成 "1  · SEHK"。"""
    label = _contract_label(_contract("1", "", Exchange.SEHK))
    assert "  " not in label
    assert label == "1 · SEHK"


# ── 第二组：变异测试 —— 显示改了，读取必须跟着改 ─────────────────────

def test_label_round_trips_back_to_the_bare_symbol() -> None:
    """这是配对的核心：显示文本必须能还原成原始代码。

    如果 _symbol_from_label 漏了或写错，下载会拿着带中文的整串当代码，
    请求直接失败 —— 而且报错信息是数据服务返回的，看不出根因在界面层。
    """
    for symbol, name, exchange in [
        ("1", "长和", Exchange.SEHK),
        ("700", "腾讯控股", Exchange.SEHK),
        ("AAPL", "APPLE INC", Exchange.SMART),
        ("600519", "贵州茅台", Exchange.SSE),
    ]:
        contract = _contract(symbol, name, exchange)
        assert _symbol_from_label(_contract_label(contract)) == symbol


def test_free_text_input_passes_through_untouched() -> None:
    """手输一个本地还没有的代码必须照常能下载。

    这是真实用法：合约缓存只有已连网关推过来的品种，下载一个没连过的
    市场的代码是正当需求，不能因为"下拉里没有"就拦掉。
    """
    assert _symbol_from_label("IF88") == "IF88"
    assert _symbol_from_label("  AAPL  ") == "AAPL"


def test_download_reads_the_symbol_not_the_display_text(qapp: QtWidgets.QApplication) -> None:
    """端到端钉住配对：走 _current_symbol，拿到的必须是代码不是显示串。

    直接构造 DownloadDialog 要拖进整个 MainEngine，代价太大；这里复用它的
    取值逻辑（同一个函数）配一个真的 combo，覆盖的是同一条路径。
    """
    from fluent_ui.data_manager import DownloadDialog

    combo = QtWidgets.QComboBox()
    combo.setEditable(True)
    contract = _contract("700", "腾讯控股", Exchange.SEHK)
    combo.addItem(_contract_label(contract), userData=contract)
    combo.setCurrentIndex(0)

    holder = DownloadDialog.__new__(DownloadDialog)
    holder.symbol_combo = combo                             # type: ignore[assignment]
    # 真 combo 的 text() 由 SearchableComboBox 提供；QComboBox 上等价的是
    # currentText()，用它顶上以复用同一段判断逻辑。
    combo.text = combo.currentText                          # type: ignore[method-assign]

    assert DownloadDialog._current_symbol(holder) == "700"


def test_edited_text_wins_over_a_stale_selection(qapp: QtWidgets.QApplication) -> None:
    """选完再手改代码时，不能还用旧选中项的 symbol。

    combo 的 currentData() 在文本被编辑后仍指着原来那一项。若无脑信
    currentData，用户改了代码却下载到上一个 —— 静默拿错数据，比报错更坏。
    """
    from fluent_ui.data_manager import DownloadDialog

    combo = QtWidgets.QComboBox()
    combo.setEditable(True)
    contract = _contract("700", "腾讯控股", Exchange.SEHK)
    combo.addItem(_contract_label(contract), userData=contract)
    combo.setCurrentIndex(0)
    combo.setEditText("9988")                               # 用户手改成别的代码
    combo.text = combo.currentText                          # type: ignore[method-assign]

    holder = DownloadDialog.__new__(DownloadDialog)
    holder.symbol_combo = combo                             # type: ignore[assignment]

    assert DownloadDialog._current_symbol(holder) == "9988"
