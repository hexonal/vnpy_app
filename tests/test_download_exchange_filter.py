"""代码列表必须跟着交易所走。

用户报障："交易所选择了，但是股票显示的是 hk 的"。原因不是筛选写错了，是根本
没有筛选：22165 只合约不分市场堆在一个下拉里，只显示得下前 50 条，而这 50 条
永远是最先查回来的那个市场（FUTU 先查 SEHK）。所以无论交易所选成什么，
代码栏看到的都是港股。

配套的两件事同样重要：
- 打开时默认落在合约最多的交易所，而不是枚举第一项 CFFEX —— 那里一只合约都没有，
  代码栏会是空的，看着像功能坏了。
- 代码与交易所对不上时直说。否则报错来自下载链路更下游的 datafeed 分支，
  弹出的是"没有配置要使用的数据服务"，指向的东西和真正的原因毫无关系。
"""

from __future__ import annotations

import os
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from vnpy.trader.constant import Exchange, Interval, Product
from vnpy.trader.object import ContractData
from vnpy.trader.ui import QtWidgets, create_qapp

from fluent_ui.data_manager import DownloadDialog


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


def _dialog(contracts: list[ContractData]) -> DownloadDialog:
    """按真实分布造一个对话框：港股先入库、但美股数量更多。

    顺序与数量都照抄用户机器上的实际情况（SEHK 先查回来、SMART 最多），
    因为这个 bug 正是由"先入库的市场占满前 50 条"造成的。
    """
    engine = types.SimpleNamespace(
        main_engine=types.SimpleNamespace(get_all_contracts=lambda: contracts)
    )
    return DownloadDialog(engine)                           # type: ignore[arg-type]


def _sample() -> list[ContractData]:
    return (
        [_contract(str(i), f"港股{i}", Exchange.SEHK) for i in range(1, 61)]
        + [_contract(f"US{i}", f"美股{i}", Exchange.SMART) for i in range(200)]
        + [_contract(f"60000{i}", f"沪{i}", Exchange.SSE) for i in range(10)]
    )


def _symbols(dialog: DownloadDialog) -> list[str]:
    return [item.text for item in dialog.symbol_combo.items]


def _select(dialog: DownloadDialog, exchange: Exchange) -> None:
    dialog.exchange_combo.setCurrentIndex(list(Exchange).index(exchange))


# ── 交易所决定代码列表 ───────────────────────────────────────────────

def test_symbol_list_follows_the_selected_exchange(qapp: QtWidgets.QApplication) -> None:
    """用户报的那一个：选了美股就不该再看到港股。"""
    dialog = _dialog(_sample())
    _select(dialog, Exchange.SMART)

    symbols = _symbols(dialog)
    assert symbols, "美股列表为空"
    assert all("SMART" in s for s in symbols), "美股列表里混进了别的市场"


def test_switching_exchange_replaces_the_list(qapp: QtWidgets.QApplication) -> None:
    dialog = _dialog(_sample())
    _select(dialog, Exchange.SMART)
    assert len(_symbols(dialog)) == 200

    _select(dialog, Exchange.SEHK)
    assert len(_symbols(dialog)) == 60
    assert all("SEHK" in s for s in _symbols(dialog))

    _select(dialog, Exchange.SSE)
    assert len(_symbols(dialog)) == 10


def test_exchange_with_no_contracts_gives_an_empty_list(qapp: QtWidgets.QApplication) -> None:
    """空列表本身没问题 —— 手输仍然可用，见 _current_symbol 的自由输入分支。"""
    dialog = _dialog(_sample())
    _select(dialog, Exchange.CFFEX)
    assert _symbols(dialog) == []


# ── 打开时的默认值 ──────────────────────────────────────────────────

def test_opens_on_the_exchange_with_the_most_contracts(qapp: QtWidgets.QApplication) -> None:
    """默认落在 SMART（最多），不是枚举第一项 CFFEX（一只都没有）。"""
    dialog = _dialog(_sample())
    assert dialog.exchange_combo.currentData() is Exchange.SMART
    assert _symbols(dialog), "打开时代码栏就是空的，看着像坏了"


def test_no_contracts_at_all_does_not_crash(qapp: QtWidgets.QApplication) -> None:
    """还没连网关/合约没查完时打开这个框，不该炸。"""
    dialog = _dialog([])
    assert _symbols(dialog) == []


def test_exchange_labels_carry_the_contract_count(qapp: QtWidgets.QApplication) -> None:
    """标上数量，省得靠猜哪个交易所有数据。

    美股在 vnpy 里叫 SMART 而不是 NASDAQ/NYSE —— 光看名字选不出来，
    看到 SMART 后面有数、NASDAQ 后面没有，就一目了然。
    """
    dialog = _dialog(_sample())
    labels = {
        str(dialog.exchange_combo.itemData(i).name): dialog.exchange_combo.items[i].text
        for i in range(dialog.exchange_combo.count())
    }
    assert labels["SMART"] == "SMART（200）"
    assert labels["NASDAQ"] == "NASDAQ", "没有合约的交易所不该带括号"


# ── 代码与交易所对不上时要说清楚 ─────────────────────────────────────

def test_mismatch_names_the_right_exchange(qapp: QtWidgets.QApplication) -> None:
    dialog = _dialog(_sample())
    hint = dialog._wrong_exchange_hint("US7", Exchange.SEHK)
    assert "US7" in hint and "SMART" in hint and "SEHK" in hint


def test_matching_pair_produces_no_hint(qapp: QtWidgets.QApplication) -> None:
    dialog = _dialog(_sample())
    assert dialog._wrong_exchange_hint("US7", Exchange.SMART) == ""


def test_unknown_symbol_is_left_alone(qapp: QtWidgets.QApplication) -> None:
    """本地不认识的代码不给提示 —— 下载一个还没入库的代码是正当用法，
    不能因为"列表里没有"就拦下来。"""
    dialog = _dialog(_sample())
    assert dialog._wrong_exchange_hint("NEVER_SEEN", Exchange.SMART) == ""


def test_same_code_in_two_markets_lists_both(qapp: QtWidgets.QApplication) -> None:
    """裸数字代码会撞车：港股 1 和别处的 1 是两回事，提示要把两个都报出来。"""
    contracts = _sample() + [_contract("1", "某沪股", Exchange.SSE)]
    dialog = _dialog(contracts)
    hint = dialog._wrong_exchange_hint("1", Exchange.SMART)
    assert "SEHK" in hint and "SSE" in hint


# ── 取不到数据时，说清楚为什么 ────────────────────────────────────────

def _dialog_with_datafeed(contracts: list[ContractData], datafeed: object) -> DownloadDialog:
    engine = types.SimpleNamespace(
        main_engine=types.SimpleNamespace(
            get_all_contracts=lambda: contracts,
            get_contract=lambda vt: next(
                (c for c in contracts if f"{c.symbol}.{c.exchange.value}" == vt), None
            ),
        ),
        datafeed=datafeed,
    )
    return DownloadDialog(engine)                           # type: ignore[arg-type]


def test_tick_without_datafeed_says_tick_has_no_source(qapp: QtWidgets.QApplication) -> None:
    """用户实际撞上的那一个。

    原来的报错是数据服务给的"没有正确配置数据服务" —— 听着像少配了一步，
    其实是 Tick 在这台机器上本来就没有来源：download_tick_data 只问数据服务、
    从不问网关（vnpy_datamanager/engine.py:238），而网关只提供 K 线。
    """
    from vnpy.trader.datafeed import BaseDatafeed

    dialog = _dialog_with_datafeed(_sample(), BaseDatafeed())
    reason = dialog._unavailable_reason("US7", Exchange.SMART, Interval.TICK)
    assert "Tick" in reason
    assert "K 线" in reason, "只说不行不够，得指出改选什么"


def test_bars_from_the_gateway_need_no_datafeed(qapp: QtWidgets.QApplication) -> None:
    """网关能给 K 线时不该拦 —— 这正是当前唯一走得通的路。"""
    from vnpy.trader.datafeed import BaseDatafeed

    contracts = _sample()
    for contract in contracts:
        contract.history_data = True

    dialog = _dialog_with_datafeed(contracts, BaseDatafeed())
    assert dialog._unavailable_reason("US7", Exchange.SMART, Interval.MINUTE) == ""


def test_bars_without_gateway_history_are_reported(qapp: QtWidgets.QApplication) -> None:
    from vnpy.trader.datafeed import BaseDatafeed

    dialog = _dialog_with_datafeed(_sample(), BaseDatafeed())   # history_data 默认 False
    reason = dialog._unavailable_reason("US7", Exchange.SMART, Interval.MINUTE)
    assert "US7" in reason and "SMART" in reason


def test_a_configured_datafeed_is_never_blocked(qapp: QtWidgets.QApplication) -> None:
    """配了真数据服务就一律放行 —— 能不能取到由它自己说了算，我们不越权预判。"""
    from vnpy.trader.datafeed import BaseDatafeed

    class RealFeed(BaseDatafeed):
        pass

    dialog = _dialog_with_datafeed(_sample(), RealFeed())
    assert dialog._unavailable_reason("US7", Exchange.SMART, Interval.TICK) == ""
