"""Startup-wiring regression test for the live-path risk gate.

``run_gui.py`` registers vnpy_alphakit's three gate rules explicitly, right
after ``add_app(RiskManagerApp)``.  This test reproduces that wiring without
Qt and asserts the properties that make it work, because each one is easy to
break silently:

* the rules end up on ``MainEngine.send_order``, so *every* order path is
  covered — not just AlphaLiveEngine's;
* vnpy's own five built-in rules are still there (extended, not replaced);
* the folder-scan alternative genuinely does not work, which is *why*
  run_gui registers explicitly. That one is measured here rather than
  asserted in a comment, so if a future vnpy release drops the
  ``os.chdir(TRADER_DIR)`` the test fails and the comment gets revisited.

No gateway is connected and no order leaves the process: the only gateway
registered is a local recorder.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from pathlib import Path

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
from vnpy_alphakit.rules import GATE_RULE_NAMES, install_gate_rules
from vnpy_riskmanager import RiskManagerApp
from vnpy_riskmanager.engine import RiskEngine

VT_SYMBOL = "WIRINGTEST.SEHK"


class RecordingGateway(BaseGateway):
    """Records order requests; connects to nothing."""

    default_name: str = "WIRETEST"

    def __init__(self, event_engine: EventEngine, gateway_name: str = "WIRETEST") -> None:
        super().__init__(event_engine, gateway_name)
        # BaseGateway 把 exchanges 声明成带类级默认值的*实例*变量，MainEngine.add_gateway
        # 也是构造完实例再读它。所以这里按实例属性设，而不是类属性（类级可变列表 =
        # 共享状态，ruff RUF012）；改成 ClassVar 则会变成对基类实例变量的不兼容覆盖，
        # 触发 mypy [misc] 与 pyright reportIncompatibleVariableOverride。
        # 与 vnpy_router/tests/conftest.py 的同款处理保持一致。
        self.exchanges: list[Exchange] = [Exchange.SEHK]
        self.sent_orders: list[OrderRequest] = []

    def connect(self, setting: dict) -> None:
        """No-op: test gateway, deliberately unable to connect."""

    def close(self) -> None:
        """No-op: nothing was opened."""

    def subscribe(self, req: SubscribeRequest) -> None:
        """No-op: this test sends orders, not market-data subscriptions."""

    def send_order(self, req: OrderRequest) -> str:
        self.sent_orders.append(req)
        order = req.create_order_data(str(len(self.sent_orders)), self.gateway_name)
        self.on_order(order)
        return order.vt_orderid

    def cancel_order(self, req: CancelRequest) -> None:
        """No-op: no order is ever cancelled in this test."""

    def query_account(self) -> None:
        """No-op: the account is injected directly."""

    def query_position(self) -> None:
        """No-op: no position is needed."""


@pytest.fixture
def wired() -> Iterator[tuple[MainEngine, RecordingGateway, RiskEngine]]:
    event_engine = EventEngine()
    event_engine._interval = 0.02      # keep close() prompt; see vnpy_alphakit tests
    main_engine = MainEngine(event_engine)
    gateway: RecordingGateway = main_engine.add_gateway(RecordingGateway)  # type: ignore[assignment]

    contract = ContractData(
        symbol="WIRINGTEST",
        exchange=Exchange.SEHK,
        name="WIRINGTEST",
        product=Product.EQUITY,
        size=1,
        pricetick=0.01,
        gateway_name="WIRETEST",
    )
    oms = main_engine.get_engine("oms")
    oms.contracts[contract.vt_symbol] = contract        # type: ignore[attr-defined]

    # --- the two lines run_gui.py runs, in the same order ---
    risk_engine: RiskEngine = main_engine.add_app(RiskManagerApp)  # type: ignore[assignment]
    install_gate_rules(main_engine)

    risk_engine.rules["单笔风险上限"].capital = 100_000

    try:
        yield main_engine, gateway, risk_engine
    finally:
        main_engine.close()


def test_install_reports_the_three_rules_it_added(
    wired: tuple[MainEngine, RecordingGateway, RiskEngine],
) -> None:
    _, _, risk_engine = wired
    for name in GATE_RULE_NAMES:
        assert name in risk_engine.rules


def test_builtin_rules_survive_the_extension(
    wired: tuple[MainEngine, RecordingGateway, RiskEngine],
) -> None:
    _, _, risk_engine = wired
    for name in ("委托指令检查", "委托规模检查", "重复报单检查", "每日上限检查", "活动委托检查"):
        assert name in risk_engine.rules


def test_gate_sits_on_main_engine_send_order(
    wired: tuple[MainEngine, RecordingGateway, RiskEngine],
) -> None:
    """Covers CtaEngine and manual GUI orders too, not just AlphaLiveEngine."""
    main_engine, _, _ = wired
    assert isinstance(getattr(main_engine.send_order, "__self__", None), RiskEngine)


def test_order_without_a_stop_is_blocked_at_the_main_engine(
    wired: tuple[MainEngine, RecordingGateway, RiskEngine],
) -> None:
    main_engine, gateway, _ = wired
    req = OrderRequest(
        symbol="WIRINGTEST",
        exchange=Exchange.SEHK,
        direction=Direction.LONG,
        type=OrderType.LIMIT,
        volume=100,
        price=100.0,
        reference="manual-order",
    )

    assert main_engine.send_order(req, "WIRETEST") == ""
    assert gateway.sent_orders == []


def test_order_with_a_stop_and_within_risk_passes(
    wired: tuple[MainEngine, RecordingGateway, RiskEngine],
) -> None:
    main_engine, gateway, _ = wired
    req = OrderRequest(
        symbol="WIRINGTEST",
        exchange=Exchange.SEHK,
        direction=Direction.LONG,
        type=OrderType.LIMIT,
        volume=100,
        price=100.0,
        reference=attach_stop("manual-order", 98.0),
    )

    assert main_engine.send_order(req, "WIRETEST")
    assert len(gateway.sent_orders) == 1


def test_riskengine_folder_scan_cannot_reach_this_checkout(
    wired: tuple[MainEngine, RecordingGateway, RiskEngine],
) -> None:
    """Why run_gui registers explicitly instead of shipping a rules/ folder.

    ``MainEngine.__init__`` runs ``os.chdir(TRADER_DIR)`` before any app is
    added, so ``RiskEngine``'s ``Path.cwd()/rules`` scan resolves under the
    vnpy home directory — never inside this repository. If a future vnpy drops
    that chdir, this fails and the explicit registration can be revisited.
    """
    scan_target = Path.cwd() / "rules"
    repo_root = Path(__file__).resolve().parent.parent

    assert repo_root not in scan_target.parents
    assert scan_target != repo_root / "rules"
