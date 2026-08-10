"""Manual-order feedback — make a refused hand-typed order visible on screen.

What was broken
---------------

``TradingWidget.send_order`` built its ``OrderRequest`` with a fixed
``reference="ManualTrading"`` — no ``|stop=`` suffix — and then threw the
return value of ``main_engine.send_order`` away.  ``run_gui.py`` installs
``vnpy_alphakit``'s gate rules on ``MainEngine.send_order``, and one of them
(强制止损检查) refuses **every** exposure-increasing order that declares no
stop price.  So every manual buy typed into the panel was refused, the panel
said nothing, and the only trace was one RiskEngine line in the log dock that
scrolls away.  The class already had ``_show_error`` — it was simply never
reached from the order path.

Why this module exists rather than a few lines inside the widget
----------------------------------------------------------------

The judgement "did this order actually leave" is not GUI code and is worth
testing without a widget: it needs an event-queue barrier and a status read,
and getting either wrong silently reinstates the bug.  The widget keeps the
Qt work (read a field, pop a dialog) and calls in here for the verdict.

The two rules this module encodes
---------------------------------

* **A non-empty ``vt_orderid`` does not mean the order was accepted.**
  ``RejectOrderMixin._reject`` in gatewaykit — reached from the first line of
  every gateway's ``send_order`` via ``reject_if_invalid`` — honours
  ``BaseGateway``'s documented contract by minting an ordinary-looking
  ``local-reject-N`` id and pushing an ``OrderData`` carrying
  ``Status.REJECTED``.  The judge here is therefore the **status**, never the
  ``local-reject-`` prefix: the prefix is one mixin's private numbering, the
  status is the contract every gateway and every broker rejection speaks.
  This is the same judgement ``CtaEngine._any_order_live`` and
  ``AlphaLiveEngine.confirm_accepted`` already make.

* **Never invent a stop price.**  Filling in a plausible-looking stop so the
  order passes would defeat the gate outright: that number then sizes the
  risk computation (``|entry-stop| × volume × size``) and nobody ever decided
  it.  The panel grows a 止损价 field instead — the trader declares the stop,
  or the order is refused and told why.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from vnpy.event import Event, EventEngine
from vnpy.trader.constant import Status
from vnpy.trader.engine import MainEngine
from vnpy.trader.object import OrderData
from vnpy_gatewaykit.order_stop import attach_stop, extract_stop, is_finite

#: ``OrderRequest.reference`` for orders typed into the trading panel. Kept
#: byte-identical to what the widget used before so existing log greps and
#: any downstream reference matching keep working; the stop suffix is the
#: only thing that is ever appended to it.
MANUAL_REFERENCE: str = "ManualTrading"

#: Private event type used only as a queue barrier — see ``settle_events``.
EVENT_MANUAL_SETTLE: str = "eManualOrderSettle"

#: How long to wait for the event queue to drain before giving up on knowing
#: whether the order was refused. One second is the same budget
#: ``AlphaLiveEngine`` uses; the measured drain is sub-millisecond, so this is
#: a deadlock bound rather than a latency budget.
SETTLE_TIMEOUT: float = 1.0


@dataclass(frozen=True)
class StopEntry:
    """What the 止损价 box says: a usable stop, nothing at all, or an error.

    Blank and unusable are deliberately different answers.  Blank means the
    trader declared nothing, which is a legal thing to do (an exit needs no
    stop) and lets the gate decide; unusable means the text cannot become a
    price, and sending anyway would refuse the order for the wrong reason.
    """

    stop: float | None
    error: str = ""

    def describe(self) -> str:
        if self.error:
            return f"止损价无效: {self.error}"
        if self.stop is None:
            return "未声明止损价"
        return f"止损价 {self.stop:.10g}"


@dataclass(frozen=True)
class Acceptance:
    """Verdict on one ``vt_orderid``: refused, unknown, or accepted.

    ``refusal`` and ``warning`` are separate because they call for different
    words on screen.  A refusal is a fact — the order is not working and the
    trader may re-send.  A warning means the question could not be answered
    (the event thread did not drain), and re-sending on that would be how a
    live order gets duplicated, so the trader is told to go look rather than
    told the order failed.
    """

    refusal: str = ""
    warning: str = ""

    def describe(self) -> str:
        if self.refusal:
            return self.refusal
        if self.warning:
            return self.warning
        return "委托已受理"


def read_stop(text: str) -> StopEntry:
    """Parse the 止损价 field.

    Non-finite and non-positive values are refused here rather than passed on:
    ``attach_stop`` raises ``ValueError`` on both, and an exception thrown from
    a Qt slot unwinds into the event loop where nobody sees it — the exact
    class of silence this whole change is about.
    """
    stripped: str = text.strip()
    if not stripped:
        return StopEntry(stop=None)

    try:
        value: float = float(stripped)
    except ValueError:
        return StopEntry(stop=None, error=f"止损价必须是数字, 收到 {stripped!r}")

    if not is_finite(value):
        return StopEntry(stop=None, error=f"止损价必须是有限数值, 收到 {stripped!r}")
    if value <= 0:
        return StopEntry(stop=None, error=f"止损价必须为正数, 收到 {value:.10g}")
    return StopEntry(stop=value)


def manual_reference(stop: float | None) -> str:
    """Build the request reference, with the declared stop encoded into it.

    No stop declared returns the bare reference — that is not a fallback to
    success, it means the order will be refused if it increases exposure. The
    caller has to say so; see ``explain_empty_orderid``.
    """
    if stop is None:
        return MANUAL_REFERENCE
    return attach_stop(MANUAL_REFERENCE, stop)


def settle_events(event_engine: EventEngine, timeout: float = SETTLE_TIMEOUT) -> bool:
    """Wait until every event queued before this call has been processed.

    ``BaseGateway.on_order`` does not touch the OMS synchronously — it puts an
    event on the queue that ``OmsEngine.process_order_event`` handles on the
    EventEngine thread.  Measured in ``vnpy_alphakit`` against a real
    ``MainEngine`` whose gateway refuses everything: **0 of 20** rejections
    were visible to ``get_order`` at the instant ``send_order`` returned, and
    the OMS caught up 0.30-0.61 ms later.  Reading the status without this
    barrier would therefore have caught none of them.

    A FIFO queue is what makes the barrier proof rather than a guess: the
    rejection event was queued by ``_reject`` before it returned the id, hence
    before this barrier event, so it is processed first.  No sleep length is
    being bet on and nothing waits for the broker.

    The handler is registered and removed around the wait rather than kept for
    the lifetime of a widget: dialogs are constructed per open, and a handler
    left behind on the shared EventEngine every time one opens is a leak that
    grows for as long as the terminal runs.
    """
    barrier: threading.Event = threading.Event()

    def release(event: Event) -> None:
        waiting: threading.Event = event.data
        waiting.set()

    event_engine.register(EVENT_MANUAL_SETTLE, release)
    try:
        event_engine.put(Event(EVENT_MANUAL_SETTLE, barrier))
        return barrier.wait(timeout)
    finally:
        event_engine.unregister(EVENT_MANUAL_SETTLE, release)


def confirm_accepted(
    main_engine: MainEngine,
    event_engine: EventEngine,
    vt_orderid: str,
    timeout: float = SETTLE_TIMEOUT,
) -> Acceptance:
    """Did this order leave for the broker?  Say why not, if not.

    Silence still counts as accepted.  An order the OMS has never heard of is
    the normal state for a gateway that acknowledges asynchronously, and
    reading that as a rejection would tell the trader a working order failed —
    which invites a duplicate.  After the barrier the optimism is narrow: a
    local rejection cannot be silent, because its event was queued before the
    barrier event was.
    """
    if not settle_events(event_engine, timeout):
        return Acceptance(
            warning=(
                f"事件队列 {timeout:g}s 内未排空, 无法确认委托 {vt_orderid} 是否被拒 —— "
                "请到委托栏核对该笔状态, 不要直接重发(可能重复下单); "
                "事件线程若卡死, 成交与拒单回报都会一起堵在队列里"
            )
        )

    order: OrderData | None = main_engine.get_order(vt_orderid)
    if order is None or order.status is not Status.REJECTED:
        return Acceptance()

    return Acceptance(
        refusal=(
            f"委托被拒: {order.vt_symbol} {vt_orderid} 状态 {order.status.value} "
            "—— 委托未进场, 拒单原因见日志面板(委托失败…)"
        )
    )


def explain_empty_orderid(vt_symbol: str, reference: str) -> str:
    """The message for a ``send_order`` that returned an empty id.

    An empty id is what ``RiskEngine.send_order`` returns when one of its
    rules refuses, so this is the message the mandatory-stop gate produces.
    It names the 止损价 field on purpose: from the symptom ("nothing happened
    when I clicked 委托") nobody would guess that the cause is a missing
    suffix on ``OrderRequest.reference``.
    """
    if extract_stop(reference) is None:
        hint: str = (
            "该委托未声明止损价 —— 风控的「强制止损检查」要求增敞口委托必须带止损。"
            "请在下单面板的「止损价」里填入这一笔的止损价后重发"
        )
    else:
        hint = (
            "已带止损价, 拒绝原因见日志面板 RiskEngine 那一行"
            "(委托规模/活动委托/每日上限/单笔风险/重复委托等)"
        )
    return f"委托未被接受: {vt_symbol} reference={reference!r} —— {hint}"
