"""The runner must not subscribe before the broker has described its contracts.

``MainEngine.connect`` returns immediately — ``FutuGateway.connect`` hands the
work to a daemon thread (``NonBlockingConnectMixin``) and the contract query is
several seconds of futu round-trips behind it.  Calling ``init_strategy()`` on
the next line therefore asks the OMS for contracts that have not arrived:

    10:37:01  [AlphaLive] 700.SEHK 无合约信息, 跳过订阅
    10:37:01  [AlphaLive] 9988.SEHK 无合约信息, 跳过订阅
    10:37:04  [FUTU] SZSE 合约查询完成,共 2964 只

Nothing is subscribed, so no tick ever reaches the OMS, so
``build_bar_slice()`` returns ``{}`` forever and every ``on_bars`` sees ``None``
for every symbol.  The process connects, mounts the risk gate, prints a clean
reconciliation line and never sends an order — a silent no-op wearing the
costume of a live run.

These tests pin the ordering with a gateway whose contracts arrive late on
purpose, and pin the failure mode when they never arrive at all: an empty
universe has to be loud, because quiet is indistinguishable from working.
"""

from __future__ import annotations

import os
import sys
import threading
import time

import polars as pl
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vnpy.alpha.lab import AlphaLab
from vnpy.event import EventEngine
from vnpy.trader.constant import Exchange, Product
from vnpy.trader.gateway import BaseGateway
from vnpy.trader.object import (
    CancelRequest,
    ContractData,
    OrderRequest,
    SubscribeRequest,
)

import run_live_alpha

SYMBOLS = ("700.SEHK", "9988.SEHK")


class DelayedContractGateway(BaseGateway):
    """A gateway whose contract list lands ``contract_delay`` seconds late.

    That is what a real one does: ``connect()`` returns as soon as the daemon
    thread is running, and the contract query answers whenever OpenD gets
    round to it.  Subclasses set the two knobs.
    """

    default_name: str = "FUTU"

    #: Seconds between connect() and the contract push.
    contract_delay: float = 0.5
    #: False = the broker never answers (wrong market, OpenD down, bad symbol).
    push_contracts: bool = True
    #: Every instance built during a test, so assertions can reach it.
    instances: list[DelayedContractGateway] = []

    def __init__(self, event_engine: EventEngine, gateway_name: str = "FUTU") -> None:
        super().__init__(event_engine, gateway_name)
        self.exchanges: list[Exchange] = [Exchange.SEHK]
        self.subscribed: list[SubscribeRequest] = []
        self.sent_orders: list[OrderRequest] = []
        type(self).instances.append(self)

    def connect(self, setting: dict) -> None:
        """Return at once and answer later, exactly like the real gateway."""
        threading.Thread(target=self._answer_later, daemon=True).start()

    def _answer_later(self) -> None:
        time.sleep(type(self).contract_delay)
        if not type(self).push_contracts:
            return
        for vt_symbol in SYMBOLS:
            symbol, exchange_value = vt_symbol.rsplit(".", 1)
            self.on_contract(ContractData(
                symbol=symbol,
                exchange=Exchange(exchange_value),
                name=symbol,
                product=Product.EQUITY,
                size=1,
                pricetick=0.01,
                min_volume=1,
                gateway_name=self.gateway_name,
            ))

    def close(self) -> None:
        """No-op: no socket was ever opened."""

    def subscribe(self, req: SubscribeRequest) -> None:
        self.subscribed.append(req)

    def send_order(self, req: OrderRequest) -> str:
        self.sent_orders.append(req)
        return "FUTU.NOT_REACHED"

    def cancel_order(self, req: CancelRequest) -> None:
        """No-op: nothing is ever left working."""

    def query_account(self) -> None:
        """No-op."""

    def query_position(self) -> None:
        """No-op: silence is the point — the runner must not invent a book."""


def gateway_class(*, delay: float = 0.5, push: bool = True) -> type[DelayedContractGateway]:
    """A fresh subclass per test, so instances/knobs never leak between them."""

    class _Gateway(DelayedContractGateway):
        contract_delay = delay
        push_contracts = push
        instances: list[DelayedContractGateway] = []

    return _Gateway


@pytest.fixture()
def lab(tmp_path) -> AlphaLab:
    """An AlphaLab holding one signal, the minimum ``main()`` will start on."""
    lab = AlphaLab(str(tmp_path / "lab"))
    lab.save_signal("demo", pl.DataFrame({"vt_symbol": list(SYMBOLS), "signal": [1.0, 0.5]}))
    return lab


def run_main(
    monkeypatch: pytest.MonkeyPatch,
    lab: AlphaLab,
    gateway: type[DelayedContractGateway],
    extra: list[str] | None = None,
) -> int:
    """``main()`` with the broker faked and the rebalance loop stubbed out.

    The loop is stubbed because these tests are about what the runner has
    wired up by the time it reaches the loop — subscriptions — not about what
    a rebalance does with them.
    """
    monkeypatch.setattr(run_live_alpha, "FutuGateway", gateway)
    monkeypatch.setattr(run_live_alpha, "run_loop", lambda engine, interval: 0)

    return run_live_alpha.main([
        "--lab", str(lab.lab_path),
        "--basket", "demo",
        "--symbols", ",".join(SYMBOLS),
        "--position-wait", "0.05",
        "--assume-flat",
        *(extra or []),
    ])


def test_subscribing_the_instant_connect_returns_gets_nothing() -> None:
    """The race itself, with no runner involved: connect() is not ready.

    This is why ``init_strategy()`` cannot simply follow ``connect()`` — at
    that moment the OMS has no contracts, and the engine's per-symbol
    ``continue`` turns that into an empty subscription set rather than an
    error.
    """
    gateway = gateway_class(delay=0.5)
    event_engine = EventEngine()
    from vnpy.trader.engine import MainEngine

    main_engine = MainEngine(event_engine)
    try:
        main_engine.add_gateway(gateway)
        main_engine.connect({}, "FUTU")

        missing = [s for s in SYMBOLS if main_engine.get_contract(s) is None]
        assert missing == list(SYMBOLS)
        assert gateway.instances[0].subscribed == []
    finally:
        main_engine.close()


def test_wait_for_contracts_reports_what_never_arrived() -> None:
    gateway = gateway_class(push=False)
    event_engine = EventEngine()
    from vnpy.trader.engine import MainEngine

    main_engine = MainEngine(event_engine)
    try:
        main_engine.add_gateway(gateway)
        main_engine.connect({}, "FUTU")

        assert run_live_alpha.wait_for_contracts(
            main_engine, list(SYMBOLS), timeout=0.3
        ) == list(SYMBOLS)
    finally:
        main_engine.close()


def test_wait_for_contracts_returns_empty_once_the_broker_answers() -> None:
    gateway = gateway_class(delay=0.3)
    event_engine = EventEngine()
    from vnpy.trader.engine import MainEngine

    main_engine = MainEngine(event_engine)
    try:
        main_engine.add_gateway(gateway)
        main_engine.connect({}, "FUTU")

        assert run_live_alpha.wait_for_contracts(
            main_engine, list(SYMBOLS), timeout=5.0
        ) == []
    finally:
        main_engine.close()


def test_runner_subscribes_every_symbol_despite_a_late_contract_list(
    monkeypatch: pytest.MonkeyPatch, lab: AlphaLab
) -> None:
    """The regression: contracts 0.5s late must still end in a subscription."""
    gateway = gateway_class(delay=0.5)

    code = run_main(monkeypatch, lab, gateway, ["--contract-wait", "5"])

    assert code == 0
    subscribed = {f"{r.symbol}.{r.exchange.value}" for r in gateway.instances[0].subscribed}
    assert subscribed == set(SYMBOLS)


def test_runner_refuses_to_run_when_contracts_never_arrive(
    monkeypatch: pytest.MonkeyPatch, lab: AlphaLab, capsys: pytest.CaptureFixture
) -> None:
    """No contracts = no market data = a guaranteed no-op. Say so and stop."""
    gateway = gateway_class(push=False)

    code = run_main(monkeypatch, lab, gateway, ["--contract-wait", "0.3"])

    assert code == run_live_alpha.EXIT_NO_CONTRACTS
    err = capsys.readouterr().err
    assert "合约" in err
    for vt_symbol in SYMBOLS:
        assert vt_symbol in err
    assert gateway.instances[0].subscribed == []
