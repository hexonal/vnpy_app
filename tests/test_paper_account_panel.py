"""模拟账户设置面板：撑满整页、清空持仓要二次确认、滑点读数港美两市都要报。

三件事被钉在这里，都是上游 PaperManager 的实际行为不合格的地方：

* 上游 `setFixedWidth(500)` + `setFixedHeight(200)`。塞进 FluentWindow 的
  `addSubInterface()` 之后，容器给整页而控件把自己钉死在 500×200 —— 截图里
  「控件挤在左上角、大片空白」就是这一句。这里断言的是【控件没有给自己设上界】，
  不是断言某个具体像素数：窗口多大由容器决定，面板只需要不拒绝。
* 上游把 `engine.clear_position` 裸接在 clicked 上，而它遍历持仓归零之后立刻
  `save_data()` 写盘，不可撤销 —— 旁边就是两个改一下就能改回来的 SpinBox。
  取消与确认两条路径分别断言，取消路径必须【一次都没有】碰引擎。
* 滑点单位「跳」乘的是 `contract.pricetick`，而两个网关对这个字段的口径不同。
  港股与美股各写一组用例，任何一侧漏报都算不合格。

MessageBox 用手写的 Recording 替身换掉，不是因为它离屏建不出来（能建），而是
`exec()` 会进事件循环等人点 —— 测试要的是「面板问了什么、按不同答复怎么走」，
不是 Qt 的模态实现。
"""

from __future__ import annotations

import math
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402
from PySide6 import QtWidgets  # noqa: E402
from vnpy.trader.constant import Direction, Exchange, Product  # noqa: E402
from vnpy.trader.object import ContractData, PositionData, TickData  # noqa: E402

from fluent_ui import paper_account  # noqa: E402
from fluent_ui.paper_account import (  # noqa: E402
    PaperAccountWidget,
    SlippageReading,
    count_open_positions,
    describe_clear_impact,
    read_slippage,
)

# 港股 400 元档的真实价位，与 Futu 写进合约的地板价位 0.001 相差 200 倍。
HK_REFERENCE_PRICE = 400.0
HK_REAL_TICK = 0.2
FUTU_HK_PRICETICK = 0.001

# 美股同样有两种口径：uSMART 从行情回填的真实分档，与 Futu 的地板价位。
US_REFERENCE_PRICE = 112.34
US_REAL_TICK = 0.01
FUTU_US_PRICETICK = 0.0001

# HKEX 价位表的公布上限，超过就没有权威依据，spread_table 拒绝外推。
HK_TABLE_CEILING = 9995.0


@pytest.fixture(scope="module", autouse=True)
def _qapp() -> QtWidgets.QApplication:
    """整个模块共用一个 QApplication —— 一个进程只能有一个，且
    test_searchable_combo_box 在导入期就无条件建了一个，这里只能捡现成的。"""
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    assert isinstance(app, QtWidgets.QApplication)
    return app


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakePaperEngine:
    """PaperEngine 的替身：同样的三对 getter/setter、同样形状的 positions 字典。

    值是真存下来的，setter 记账 —— 「面板读到的是引擎里的值」与「面板改的写回了
    引擎」两条断言才都有东西可打。上游的 setter 每次都 `save_setting()` 落盘，
    这里不落盘：测试不该往 ~/.vntrader 写文件。
    """

    data_filename: str = "paper_account_data.json"

    def __init__(
        self, slippage: int = 0, interval: int = 3, instant: bool = False
    ) -> None:
        self.trade_slippage = slippage
        self.timer_interval = interval
        self.instant_trade = instant
        self.positions: dict[tuple[str, Direction], PositionData] = {}
        self.writes: list[tuple[str, object]] = []
        self.clear_calls = 0

    def get_trade_slippage(self) -> int:
        return self.trade_slippage

    def get_timer_interval(self) -> int:
        return self.timer_interval

    def get_instant_trade(self) -> bool:
        return self.instant_trade

    def set_trade_slippage(self, value: int) -> None:
        self.trade_slippage = value
        self.writes.append(("trade_slippage", value))

    def set_timer_interval(self, value: int) -> None:
        self.timer_interval = value
        self.writes.append(("timer_interval", value))

    def set_instant_trade(self, value: bool) -> None:
        self.instant_trade = bool(value)
        self.writes.append(("instant_trade", bool(value)))

    def clear_position(self) -> None:
        self.clear_calls += 1

    def add_position(self, vt_symbol: str, volume: float) -> None:
        symbol, _, suffix = vt_symbol.partition(".")
        position = PositionData(
            symbol=symbol,
            exchange=Exchange(suffix),
            direction=Direction.LONG,
            volume=volume,
            gateway_name="PAPER",
        )
        self.positions[(vt_symbol, Direction.LONG)] = position


class FakeMainEngine:
    """只提供面板真正用到的四样：取引擎、取合约、取行情、写日志。"""

    def __init__(
        self,
        paper_engine: FakePaperEngine | None,
        contracts: list[ContractData] | None = None,
        ticks: dict[str, TickData] | None = None,
    ) -> None:
        self.paper_engine = paper_engine
        self.contracts = contracts or []
        self.ticks = ticks or {}
        self.logs: list[str] = []

    def get_engine(self, name: str) -> FakePaperEngine | None:
        return self.paper_engine

    def get_all_contracts(self) -> list[ContractData]:
        return list(self.contracts)

    def get_tick(self, vt_symbol: str) -> TickData | None:
        return self.ticks.get(vt_symbol)

    def write_log(self, msg: str) -> None:
        self.logs.append(msg)


class StubButton:
    """MessageBox 的按钮只被 setText 一次，够了。"""

    def __init__(self) -> None:
        self.text = ""

    def setText(self, text: str) -> None:
        self.text = text


class RecordingMessageBox:
    """替掉 qfluentwidgets.MessageBox：不画、不进事件循环，记下问了什么，
    按测试装好的答复返回。1 是「点了确认」，0 是「取消或直接关掉」。"""

    answer: int = 0
    calls: list[RecordingMessageBox] = []

    def __init__(self, title: str, content: str, parent: object = None) -> None:
        self.title = title
        self.content = content
        self.parent = parent
        self.yesButton = StubButton()
        self.cancelButton = StubButton()
        RecordingMessageBox.calls.append(self)

    def exec(self) -> int:
        return RecordingMessageBox.answer


def _install_message_box(monkeypatch: pytest.MonkeyPatch, answer: int) -> None:
    RecordingMessageBox.calls = []
    RecordingMessageBox.answer = answer
    monkeypatch.setattr(paper_account, "MessageBox", RecordingMessageBox)


def _contract(symbol: str, exchange: Exchange, pricetick: float) -> ContractData:
    return ContractData(
        symbol=symbol,
        exchange=exchange,
        name=symbol,
        product=Product.EQUITY,
        size=1,
        pricetick=pricetick,
        gateway_name="TEST",
    )


def _tick(symbol: str, exchange: Exchange, last_price: float) -> TickData:
    return TickData(
        symbol=symbol,
        exchange=exchange,
        datetime=None,
        name=symbol,
        last_price=last_price,
        gateway_name="TEST",
    )


def _both_markets() -> tuple[list[ContractData], dict[str, TickData]]:
    """港股走 Futu 的地板价位、美股走 uSMART 回填的真实分档 —— 本机分离路由下
    最常见的那一副组合。"""
    contracts = [
        _contract("700", Exchange.SEHK, FUTU_HK_PRICETICK),
        _contract("NBIS", Exchange.SMART, US_REAL_TICK),
    ]
    ticks = {
        "700.SEHK": _tick("700", Exchange.SEHK, HK_REFERENCE_PRICE),
        "NBIS.SMART": _tick("NBIS", Exchange.SMART, US_REFERENCE_PRICE),
    }
    return contracts, ticks


def _build(
    engine: FakePaperEngine,
    contracts: list[ContractData] | None = None,
    ticks: dict[str, TickData] | None = None,
) -> tuple[PaperAccountWidget, FakeMainEngine]:
    main_engine = FakeMainEngine(engine, contracts, ticks)
    widget = PaperAccountWidget(main_engine, None)
    return widget, main_engine


# ---------------------------------------------------------------------------
# 面板本身
# ---------------------------------------------------------------------------


def test_panel_builds_offscreen_and_refuses_no_size_the_container_gives_it() -> None:
    contracts, ticks = _both_markets()
    widget, _ = _build(FakePaperEngine(), contracts, ticks)

    # QWIDGETSIZE_MAX：没有任何 setFixedWidth/Height 把上界压下来。
    assert widget.maximumWidth() == 16777215
    assert widget.maximumHeight() == 16777215

    widget.resize(1200, 800)
    assert widget.grab().size().toTuple() == (1200, 800)


def test_panel_shows_the_settings_it_read_from_the_engine() -> None:
    engine = FakePaperEngine(slippage=4, interval=7, instant=True)
    widget, _ = _build(engine)

    assert widget.slippage_spin.value() == 4
    assert widget.interval_spin.value() == 7
    assert widget.instant_switch.isChecked() is True
    # SwitchButton.text 是 Property 不是方法，写 .text() 会 TypeError。
    assert widget.instant_switch.getText() == "开"


def test_building_the_panel_writes_nothing_back_to_the_engine() -> None:
    """填初值不该触发 setter —— 上游每个 setter 都 save_setting() 落盘，
    连上信号再填值等于每次开窗都无缘无故写一次 paper_account_setting.json。"""
    engine = FakePaperEngine(slippage=2, interval=5, instant=True)
    _build(engine)

    assert engine.writes == []


def test_editing_the_spin_boxes_writes_through_to_the_engine() -> None:
    engine = FakePaperEngine(slippage=0, interval=3)
    widget, _ = _build(engine)

    widget.slippage_spin.setValue(3)
    widget.interval_spin.setValue(9)

    assert engine.trade_slippage == 3
    assert engine.timer_interval == 9
    assert ("trade_slippage", 3) in engine.writes
    assert ("timer_interval", 9) in engine.writes


def test_toggling_instant_trade_writes_through_to_the_engine() -> None:
    engine = FakePaperEngine(instant=False)
    widget, _ = _build(engine)

    widget.instant_switch.setChecked(True)

    assert engine.instant_trade is True
    assert ("instant_trade", True) in engine.writes


def test_missing_paper_engine_refuses_to_build_instead_of_painting_dead_switches() -> None:
    with pytest.raises(RuntimeError, match="模拟账户引擎未加载"):
        PaperAccountWidget(FakeMainEngine(None), None)


# ---------------------------------------------------------------------------
# 清空持仓的二次确认
# ---------------------------------------------------------------------------


def test_clearing_positions_asks_before_touching_the_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_message_box(monkeypatch, answer=0)
    engine = FakePaperEngine()
    engine.add_position("700.SEHK", 100)
    widget, _ = _build(engine)

    widget.clear_card.clicked.emit()

    assert len(RecordingMessageBox.calls) == 1
    assert engine.clear_calls == 0


def test_cancelling_the_confirmation_leaves_every_position_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_message_box(monkeypatch, answer=0)
    engine = FakePaperEngine()
    engine.add_position("700.SEHK", 100)
    widget, main_engine = _build(engine)

    widget.clear_card.clicked.emit()

    assert engine.clear_calls == 0
    assert main_engine.logs == []


def test_confirming_the_dialog_clears_and_says_so_in_the_log(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_message_box(monkeypatch, answer=1)
    engine = FakePaperEngine()
    engine.add_position("700.SEHK", 100)
    engine.add_position("NBIS.SMART", 50)
    widget, main_engine = _build(engine)

    widget.clear_card.clicked.emit()

    assert engine.clear_calls == 1
    assert main_engine.logs == ["模拟账户已清空 2 个非零持仓并写盘"]


def test_the_confirmation_names_how_many_positions_are_about_to_be_destroyed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_message_box(monkeypatch, answer=0)
    engine = FakePaperEngine()
    engine.add_position("700.SEHK", 100)
    engine.add_position("NBIS.SMART", 50)
    widget, _ = _build(engine)

    widget.clear_card.clicked.emit()

    box = RecordingMessageBox.calls[0]
    assert "2 个非零持仓" in box.content
    assert "不可撤销" in box.content
    assert box.yesButton.text == "确认清空"
    assert box.cancelButton.text == "取消"


def test_count_open_positions_ignores_the_zero_volume_placeholders() -> None:
    """`get_position()` 给碰过的每个 (vt_symbol, direction) 都留一条零仓记录且
    永不删除。照着字典长度报数会告诉持两个仓的人「即将清掉十一个」。"""
    engine = FakePaperEngine()
    engine.add_position("700.SEHK", 0)
    engine.add_position("NBIS.SMART", 50)

    assert count_open_positions(engine.positions.values()) == 1


def test_count_open_positions_skips_a_nan_volume_instead_of_counting_it() -> None:
    """NaN != 0 恒为真 —— 不先 isfinite 就会把一条坏数据算成真实持仓。"""
    engine = FakePaperEngine()
    engine.add_position("700.SEHK", math.nan)

    assert count_open_positions(engine.positions.values()) == 0


def test_with_no_open_positions_the_dialog_says_the_account_is_already_flat() -> None:
    text = describe_clear_impact(0, "paper_account_data.json")

    assert "没有非零持仓" in text
    assert "paper_account_data.json" in text


# ---------------------------------------------------------------------------
# 滑点读数 —— 港股与美股各一组
# ---------------------------------------------------------------------------


def test_hk_reading_calls_the_futu_floor_tick_worthless_slippage() -> None:
    contracts, ticks = _both_markets()
    hk, _us = read_slippage(2, contracts, ticks.get)

    assert hk.exchange is Exchange.SEHK
    assert hk.vt_symbol == "700.SEHK"
    assert hk.engine_tick == pytest.approx(FUTU_HK_PRICETICK)
    assert hk.market_tick == pytest.approx(HK_REAL_TICK)

    text = hk.describe()
    assert "港股" in text
    assert "0.5%" in text
    assert "形同虚设" in text


def test_us_reading_from_a_usmart_backfilled_pricetick_is_trustworthy() -> None:
    contracts, ticks = _both_markets()
    _hk, us = read_slippage(2, contracts, ticks.get)

    assert us.exchange is Exchange.SMART
    assert us.engine_tick == pytest.approx(US_REAL_TICK)
    assert us.market_tick == pytest.approx(US_REAL_TICK)

    text = us.describe()
    assert "美股" in text
    assert "读数可信" in text
    assert "0.02 美元" in text


def test_us_reading_from_a_futu_floor_tick_is_flagged_exactly_like_hk() -> None:
    """美股不是「另一侧天生正确」的市场 —— 由 Futu 供合约时它同样被低报一百倍，
    只报港股等于把这一半漏掉。"""
    contracts = [_contract("NBIS", Exchange.SMART, FUTU_US_PRICETICK)]
    ticks = {"NBIS.SMART": _tick("NBIS", Exchange.SMART, US_REFERENCE_PRICE)}

    _hk, us = read_slippage(2, contracts, ticks.get)

    assert us.market_tick == pytest.approx(US_REAL_TICK)
    text = us.describe()
    assert "1.0%" in text
    assert "形同虚设" in text


def test_every_reported_market_gets_a_line_even_with_no_contracts_loaded() -> None:
    readings = read_slippage(2, [], lambda vt_symbol: None)

    assert [r.exchange for r in readings] == [Exchange.SEHK, Exchange.SMART]
    for reading in readings:
        assert reading.vt_symbol is None
        assert "尚未加载合约" in reading.describe()


def test_a_contract_without_a_quote_reports_the_engine_tick_but_no_band() -> None:
    contracts = [_contract("700", Exchange.SEHK, FUTU_HK_PRICETICK)]

    hk, _us = read_slippage(2, contracts, lambda vt_symbol: None)

    assert hk.vt_symbol == "700.SEHK"
    assert hk.reference_price is None
    assert hk.market_tick is None
    assert "还没有报价" in hk.describe()


def test_a_quoted_contract_wins_over_an_unquoted_one_so_the_readout_stays_put() -> None:
    """挑合约的顺序若跟着字典插入顺序走，面板每次刷新可能换一个标的显示，
    读数看起来像在跳。有报价的优先，其余按 vt_symbol 排序。"""
    contracts = [
        _contract("1", Exchange.SEHK, FUTU_HK_PRICETICK),
        _contract("700", Exchange.SEHK, FUTU_HK_PRICETICK),
    ]
    ticks = {"700.SEHK": _tick("700", Exchange.SEHK, HK_REFERENCE_PRICE)}

    hk, _us = read_slippage(2, contracts, ticks.get)

    assert hk.vt_symbol == "700.SEHK"


def test_a_non_finite_pricetick_is_refused_instead_of_multiplied_into_a_fill() -> None:
    reading = SlippageReading(
        exchange=Exchange.SMART,
        slippage=2,
        vt_symbol="NBIS.SMART",
        engine_tick=math.nan,
        reference_price=US_REFERENCE_PRICE,
        market_tick=US_REAL_TICK,
    )

    text = reading.describe()
    assert "不是正有限数" in text
    assert "网关缺陷" in text


def test_a_price_past_the_published_table_reports_no_band_rather_than_a_guess() -> None:
    contracts = [_contract("ZZTOP", Exchange.SEHK, FUTU_HK_PRICETICK)]
    ticks = {
        "ZZTOP.SEHK": _tick("ZZTOP", Exchange.SEHK, HK_TABLE_CEILING + 1.0),
    }

    hk, _us = read_slippage(2, contracts, ticks.get)

    assert hk.reference_price == pytest.approx(HK_TABLE_CEILING + 1.0)
    assert hk.market_tick is None
    assert "不在价位表里" in hk.describe()


def test_the_panel_readout_covers_both_markets_and_follows_the_spin_box() -> None:
    contracts, ticks = _both_markets()
    widget, _ = _build(FakePaperEngine(slippage=1), contracts, ticks)

    first = widget.reading_label.text()
    assert "港股" in first
    assert "美股" in first
    assert "1 跳 = 0.001 港元" in first

    widget.slippage_spin.setValue(2)

    second = widget.reading_label.text()
    assert "2 跳 = 0.002 港元" in second
    assert "2 跳 = 0.02 美元" in second
