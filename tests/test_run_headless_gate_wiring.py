"""The headless entry point must carry the same risk gate as the GUI one.

``run_gui.py`` installs vnpy_alphakit's three gate rules; ``run.py`` — the
process that serves the AgentBridge MCP bridge, i.e. the one an LLM agent
places orders through — did not.  The two entry points therefore disagreed
about whether a stop is mandatory, and the more autonomous of the two was
the permissive one.

This exercises ``run.build_main_engine`` itself rather than a hand-copied
imitation of its wiring, so the test cannot keep passing against a
re-implementation after the real function drifts.  ``FutuGateway.connect``
is patched out and the gateway class swapped for a local recorder, so no
socket is opened and no order can leave the process.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vnpy.event import EventEngine
from vnpy.trader.constant import Direction, Exchange, OrderType, Product
from vnpy.trader.engine import MainEngine
from vnpy.trader.gateway import BaseGateway
from vnpy.trader.object import (
    CancelRequest,
    ContractData,
    OrderRequest,
    SubscribeRequest,
)
from vnpy_alphakit.gate import attach_stop
from vnpy_alphakit.rules import GATE_RULE_NAMES

import run as run_module

VT_SYMBOL = "HEADLESSGATE.SEHK"


class RecordingGateway(BaseGateway):
    """Stands in for FutuGateway: records requests, connects to nothing."""

    default_name: str = "FUTU"

    def __init__(self, event_engine: EventEngine, gateway_name: str = "FUTU") -> None:
        super().__init__(event_engine, gateway_name)
        # Instance attribute rather than class-level: see the note in
        # test_risk_gate_wiring.RecordingGateway for why.
        self.exchanges: list[Exchange] = [Exchange.SEHK]
        self.sent_orders: list[OrderRequest] = []

    def connect(self, setting: dict) -> None:
        """No-op: deliberately unable to reach any OpenD."""

    def close(self) -> None:
        """No-op: nothing was opened."""

    def subscribe(self, req: SubscribeRequest) -> None:
        """No-op: this test never needs market data."""

    def send_order(self, req: OrderRequest) -> str:
        self.sent_orders.append(req)
        return f"{self.gateway_name}.REACHED_BROKER"

    def cancel_order(self, req: CancelRequest) -> None:
        """No-op: nothing is ever left working."""

    def query_account(self) -> None:
        """No-op: no account behind this gateway."""

    def query_position(self) -> None:
        """No-op: no positions behind this gateway."""


@pytest.fixture()
def engines(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[MainEngine, RecordingGateway]]:
    monkeypatch.setattr(run_module, "FutuGateway", RecordingGateway)

    main_engine, _intent_engine = run_module.build_main_engine()

    gateway = main_engine.get_gateway("FUTU")
    assert isinstance(gateway, RecordingGateway)

    contract = ContractData(
        symbol="HEADLESSGATE",
        exchange=Exchange.SEHK,
        name="HEADLESSGATE",
        product=Product.EQUITY,
        size=1,
        pricetick=0.01,
        gateway_name="FUTU",
    )
    oms = main_engine.get_engine("oms")
    oms.contracts[contract.vt_symbol] = contract      # type: ignore[attr-defined]

    # Give the risk cap a capital figure *if the rule got installed at all* —
    # the fixture must survive the unfixed state so the assertions below are
    # what reports it, rather than a KeyError during setup.
    rules = main_engine.engines["RiskManager"].rules      # type: ignore[attr-defined]
    if "单笔风险上限" in rules:
        rules["单笔风险上限"].capital = 100_000

    yield main_engine, gateway
    main_engine.close()


def _order(*, stop: float | None) -> OrderRequest:
    reference = "headless-test"
    if stop is not None:
        reference = attach_stop(reference, stop)
    return OrderRequest(
        symbol="HEADLESSGATE",
        exchange=Exchange.SEHK,
        direction=Direction.LONG,
        type=OrderType.LIMIT,
        volume=10,
        price=100.0,
        reference=reference,
    )


def test_headless_entry_point_installs_the_gate_rules(
    engines: tuple[MainEngine, RecordingGateway]
) -> None:
    main_engine, _gateway = engines
    rules = main_engine.engines["RiskManager"].rules      # type: ignore[attr-defined]

    missing = [name for name in GATE_RULE_NAMES if name not in rules]
    assert missing == []


def test_headless_gate_rules_are_active_not_merely_loaded(
    engines: tuple[MainEngine, RecordingGateway]
) -> None:
    main_engine, _gateway = engines
    rules = main_engine.engines["RiskManager"].rules      # type: ignore[attr-defined]

    inactive = [name for name in GATE_RULE_NAMES if not rules[name].active]
    assert inactive == []


def test_order_without_a_stop_never_reaches_the_broker(
    engines: tuple[MainEngine, RecordingGateway]
) -> None:
    """The defect, stated as the money question: does a naked order get out?"""
    main_engine, gateway = engines

    vt_orderid = main_engine.send_order(_order(stop=None), "FUTU")

    assert vt_orderid == ""
    assert gateway.sent_orders == []


def test_order_with_a_stop_still_passes(
    engines: tuple[MainEngine, RecordingGateway]
) -> None:
    """The gate blocks naked orders, not all orders."""
    main_engine, gateway = engines

    vt_orderid = main_engine.send_order(_order(stop=95.0), "FUTU")

    assert vt_orderid == "FUTU.REACHED_BROKER"
    assert len(gateway.sent_orders) == 1
