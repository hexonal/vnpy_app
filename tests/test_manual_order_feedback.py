"""手工下单被拒时，界面必须说出来 —— 钉住「GUI 手工单 100% 静默被拒」这个回归。

改动之前：`TradingWidget.send_order` 写死 `reference="ManualTrading"`（不带
`|stop=`），并且把 `main_engine.send_order` 的返回值整个丢掉。`run_gui.py` 在
`add_app(RiskManagerApp)` 之后 `install_gate_rules()`，其中「强制止损检查」对
每一笔增敞口手工单必拒 —— 而用户在界面上看不到任何反馈，唯一痕迹是日志面板里
RiskEngine 那一行。

这份用例分三层守，缺一层都会让那个现象重现：

* **判据层** —— 空 `vt_orderid` 是拒绝；非空 `vt_orderid` **不是**受理证明，
  判据是 `Status.REJECTED`（不是 `local-reject-` 前缀，前缀是实现细节）。
* **时序层** —— 拒单回报走事件队列，`send_order` 返回那一刻 OMS 还没更新。
  没有队列屏障的状态读取一次都抓不到，所以屏障本身要有用例钉。
* **界面层** —— 两种失败都要走 `_show_error` 弹出来，而不是只写日志。

MessageBox 用手写的 Recording 替身换掉：`exec()` 会进事件循环等人点，而这里要
断言的是「面板说了什么」，不是 Qt 的模态实现。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402
from PySide6 import QtWidgets  # noqa: E402
from vnpy.event import Event, EventEngine  # noqa: E402
from vnpy.trader.constant import Direction, Exchange, Product, Status  # noqa: E402
from vnpy.trader.event import EVENT_ORDER  # noqa: E402
from vnpy.trader.object import ContractData, OrderData, OrderRequest  # noqa: E402
from vnpy_gatewaykit.order_stop import extract_stop  # noqa: E402

from fluent_ui import trading_widget as trading_widget_module  # noqa: E402
from fluent_ui.order_feedback import (  # noqa: E402
    MANUAL_REFERENCE,
    Acceptance,
    confirm_accepted,
    explain_empty_orderid,
    manual_reference,
    read_stop,
    settle_events,
)
from fluent_ui.trading_widget import TradingWidget  # noqa: E402

VT_SYMBOL = "NBIS.SMART"
GATEWAY = "USMART"


@pytest.fixture(scope="module", autouse=True)
def _qapp() -> QtWidgets.QApplication:
    """整个模块共用一个 QApplication —— 一个进程只能有一个。"""
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    assert isinstance(app, QtWidgets.QApplication)
    return app


@pytest.fixture(scope="module")
def running_engine() -> object:
    """一个真的、跑起来的 EventEngine。

    屏障要证明的正是「事件线程会把队列排空」，用同步执行的假引擎就把要测的
    东西测没了。模块级共享是安全的：每个 Fake 只把回报记进自己的 dict，别人的
    事件流过来最多多记一条没人读的数据。停引擎要 join 定时器线程（interval=1），
    所以只付一次这个代价。
    """
    engine = EventEngine()
    engine.start()
    yield engine
    engine.stop()


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class RecordingMessageBox:
    """记下面板弹了什么，不进事件循环。"""

    shown: list[tuple[str, str]] = []

    def __init__(self, title: str, content: str, parent: object = None) -> None:
        self.title = title
        self.content = content

    def hideCancelButton(self) -> None:  # noqa: N802 - qfluentwidgets 的驼峰 API
        return None

    def exec(self) -> None:
        RecordingMessageBox.shown.append((self.title, self.content))


class FakeMainEngine:
    """只提供 TradingWidget 真正用到的那几个方法。

    `mode` 决定 `send_order` 怎么回答，三种真实形态各一条：

    * `"refuse"` —— 风控闸拒绝，`RiskEngine.send_order` 返回空字符串（同步）。
    * `"local_reject"` —— 网关本地拒单，`RejectOrderMixin._reject` 在返回之前
      先把 REJECTED 的 OrderData 推进事件队列，再返回一个长得完全正常的
      `local-reject-N` 委托号（异步落到 OMS）。
    * `"accept"` —— 返回委托号，OMS 暂时还没有任何这笔的回报。
    """

    def __init__(self, event_engine: EventEngine, mode: str = "accept") -> None:
        self.event_engine = event_engine
        self.mode = mode
        self.sent: list[OrderRequest] = []
        self.orders: dict[str, OrderData] = {}
        self.event_engine.register(EVENT_ORDER, self._record_order)

    def _record_order(self, event: Event) -> None:
        order: OrderData = event.data
        self.orders[order.vt_orderid] = order

    def get_all_exchanges(self) -> list[Exchange]:
        return [Exchange.SMART, Exchange.SEHK]

    def get_all_gateway_names(self) -> list[str]:
        return [GATEWAY]

    def get_contract(self, vt_symbol: str) -> ContractData | None:
        if vt_symbol != VT_SYMBOL:
            return None
        return ContractData(
            symbol="NBIS",
            exchange=Exchange.SMART,
            name="NEBIUS",
            product=Product.EQUITY,
            size=1,
            pricetick=0.01,
            gateway_name=GATEWAY,
        )

    def subscribe(self, req: object, gateway_name: str) -> None:
        return None

    def get_order(self, vt_orderid: str) -> OrderData | None:
        return self.orders.get(vt_orderid)

    def send_order(self, req: OrderRequest, gateway_name: str) -> str:
        self.sent.append(req)

        if self.mode == "refuse":
            return ""

        if self.mode == "local_reject":
            order: OrderData = req.create_order_data("local-reject-1", gateway_name)
            order.status = Status.REJECTED
            self.event_engine.put(Event(EVENT_ORDER, order))
            return order.vt_orderid

        return f"{gateway_name}.1"


def build_widget(engine: FakeMainEngine) -> TradingWidget:
    """离屏构造下单面板并填好一笔多头开仓。

    不起 FluentMainWindow —— offscreen 下它会段错误（见
    test_fluent_offscreen_guard.py）。单独的 QWidget 没有这个问题。
    """
    widget = TradingWidget(engine, engine.event_engine)  # type: ignore[arg-type]
    widget.exchange_combo.setCurrentText(Exchange.SMART.value)
    widget.symbol_line.setText("NBIS")
    widget.direction_combo.setCurrentText(Direction.LONG.value)
    widget.price_line.setText("42.5")
    widget.volume_line.setText("100")
    widget.gateway_combo.setCurrentText(GATEWAY)
    return widget


@pytest.fixture(autouse=True)
def _quiet_message_box(monkeypatch: pytest.MonkeyPatch) -> None:
    RecordingMessageBox.shown = []
    monkeypatch.setattr(trading_widget_module, "MessageBox", RecordingMessageBox)


# ---------------------------------------------------------------------------
# read_stop：止损价输入框说了什么
# ---------------------------------------------------------------------------
def test_a_blank_stop_box_declares_nothing_rather_than_erroring() -> None:
    """留空是合法的 —— 平仓单不需要止损，判决交给闸。"""
    entry = read_stop("")
    assert entry.stop is None
    assert entry.error == ""


def test_whitespace_only_is_the_same_as_blank() -> None:
    assert read_stop("   ").stop is None
    assert read_stop("   ").error == ""


def test_a_typed_stop_is_read_back() -> None:
    assert read_stop("40.25").stop == 40.25


BAD_STOP_TEXTS: tuple[str, ...] = ("便宜", "40.2.5", "nan", "inf", "0", "-1")


def test_unusable_stop_text_becomes_an_error_instead_of_an_exception() -> None:
    """`attach_stop` 对这些值会抛 ValueError，而 Qt 槽里抛出的异常没人看得见。"""
    for text in BAD_STOP_TEXTS:
        entry = read_stop(text)
        assert entry.stop is None, text
        assert entry.error, text


def test_the_error_message_quotes_what_was_typed() -> None:
    assert "便宜" in read_stop("便宜").error


# ---------------------------------------------------------------------------
# manual_reference：止损怎么进 reference
# ---------------------------------------------------------------------------
def test_no_stop_declared_leaves_the_reference_bare() -> None:
    assert manual_reference(None) == MANUAL_REFERENCE
    assert extract_stop(manual_reference(None)) is None


def test_a_declared_stop_rides_on_the_reference() -> None:
    assert extract_stop(manual_reference(40.25)) == 40.25


# ---------------------------------------------------------------------------
# explain_empty_orderid：拒绝要指名道姓
# ---------------------------------------------------------------------------
def test_a_reference_without_a_stop_is_told_to_fill_the_stop_field() -> None:
    msg = explain_empty_orderid(VT_SYMBOL, MANUAL_REFERENCE)
    assert "止损价" in msg
    assert "强制止损检查" in msg
    assert VT_SYMBOL in msg


def test_a_reference_with_a_stop_is_pointed_at_the_other_rules() -> None:
    msg = explain_empty_orderid(VT_SYMBOL, manual_reference(40.25))
    assert "强制止损检查" not in msg
    assert "日志面板" in msg


# ---------------------------------------------------------------------------
# 队列屏障与受理判据
# ---------------------------------------------------------------------------
def test_the_barrier_returns_once_the_event_thread_drains(running_engine: EventEngine) -> None:
    assert settle_events(running_engine, 1.0) is True


def test_the_barrier_leaves_no_handler_behind(running_engine: EventEngine) -> None:
    """对话框每次打开都建一个新的 —— 留下的 handler 会随开窗次数一直涨。"""
    settle_events(running_engine, 1.0)
    assert not running_engine._handlers.get("eManualOrderSettle")


def test_a_queue_that_never_drains_is_a_warning_not_a_refusal() -> None:
    """引擎没跑起来时不能报「委托被拒」—— 那会诱使用户重发一张真在场内的单。"""
    idle = EventEngine()
    engine = FakeMainEngine(idle)
    verdict = confirm_accepted(engine, idle, "USMART.1", 0.05)  # type: ignore[arg-type]
    assert verdict.refusal == ""
    assert "未排空" in verdict.warning
    assert "不要直接重发" in verdict.warning


def test_an_order_the_oms_never_heard_of_counts_as_accepted(
    running_engine: EventEngine,
) -> None:
    engine = FakeMainEngine(running_engine)
    assert confirm_accepted(engine, running_engine, "USMART.never-pushed") == Acceptance()  # type: ignore[arg-type]


def test_a_local_rejection_queued_before_the_barrier_is_caught(
    running_engine: EventEngine,
) -> None:
    """这条是整份用例的核心：委托号非空、长得完全正常，但那单根本没出去。

    `send_order` 返回的那一刻 OMS 还没更新（实测 20 次拒单 0 次可见），
    靠的是拒单事件先于屏障事件入队 —— 摘掉屏障这条必红。
    """
    engine = FakeMainEngine(running_engine, mode="local_reject")
    req = OrderRequest(
        symbol="NBIS",
        exchange=Exchange.SMART,
        direction=Direction.LONG,
        type=trading_widget_module.OrderType.LIMIT,
        volume=100,
        price=42.5,
        reference=manual_reference(40.0),
    )
    vt_orderid = engine.send_order(req, GATEWAY)
    assert vt_orderid  # 非空委托号，看起来一切正常

    verdict = confirm_accepted(engine, running_engine, vt_orderid)  # type: ignore[arg-type]
    assert Status.REJECTED.value in verdict.refusal
    assert vt_orderid in verdict.refusal


# ---------------------------------------------------------------------------
# 界面层：拒绝到底有没有弹出来
# ---------------------------------------------------------------------------
def test_a_gate_refusal_pops_a_dialog_that_names_the_stop_field(
    running_engine: EventEngine,
) -> None:
    engine = FakeMainEngine(running_engine, mode="refuse")
    widget = build_widget(engine)

    widget.send_order()

    assert len(RecordingMessageBox.shown) == 1
    title, content = RecordingMessageBox.shown[0]
    assert "拒" in title
    assert "止损价" in content


def test_a_local_rejection_pops_a_dialog_even_though_an_orderid_came_back(
    running_engine: EventEngine,
) -> None:
    engine = FakeMainEngine(running_engine, mode="local_reject")
    widget = build_widget(engine)
    widget.stop_line.setText("40")

    widget.send_order()

    assert len(RecordingMessageBox.shown) == 1
    assert Status.REJECTED.value in RecordingMessageBox.shown[0][1]


def test_an_accepted_order_says_nothing(running_engine: EventEngine) -> None:
    """没有消息就是好消息 —— 每笔都弹一次会让人练成闭眼点确定。"""
    engine = FakeMainEngine(running_engine)
    widget = build_widget(engine)
    widget.stop_line.setText("40")

    widget.send_order()

    assert RecordingMessageBox.shown == []


def test_the_typed_stop_reaches_the_order_request(running_engine: EventEngine) -> None:
    engine = FakeMainEngine(running_engine)
    widget = build_widget(engine)
    widget.stop_line.setText("40.25")

    widget.send_order()

    assert extract_stop(engine.sent[0].reference) == 40.25


def test_an_empty_stop_box_never_invents_one(running_engine: EventEngine) -> None:
    """把闸架空的做法是「面板自己编一个止损」，这里断言它没有。"""
    engine = FakeMainEngine(running_engine, mode="refuse")
    widget = build_widget(engine)

    widget.send_order()

    assert extract_stop(engine.sent[0].reference) is None


def test_unusable_stop_text_stops_the_order_before_it_is_sent(
    running_engine: EventEngine,
) -> None:
    engine = FakeMainEngine(running_engine)
    widget = build_widget(engine)
    widget.stop_line.setText("便宜")

    widget.send_order()

    assert engine.sent == []
    assert len(RecordingMessageBox.shown) == 1


def test_switching_symbol_clears_the_stop_left_over_from_the_old_one(
    running_engine: EventEngine,
) -> None:
    engine = FakeMainEngine(running_engine)
    widget = build_widget(engine)
    widget.stop_line.setText("40.25")

    widget.symbol_line.setText("700")
    widget.exchange_combo.setCurrentText(Exchange.SEHK.value)
    widget.set_vt_symbol()

    assert widget.stop_line.text() == ""
