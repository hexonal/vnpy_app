"""The live alpha path must have a process that can actually run it.

Before ``run_live_alpha.py`` existed, ``AlphaLiveEngine`` was instantiated
nowhere outside its own unit tests: no entry point added the engine, loaded a
signal, or called ``run_rebalance``, and there was no way to reach
``enable_live_trading``.  These tests exercise the runner's wiring with the
gateway swapped for a recorder, so nothing connects and no order can leave.

The assertions are about the safety posture, because that is what a runner
can quietly get wrong: dry run by default, the gate rules mounted on
MainEngine, and a refusal to invent a flat book when the broker has not
answered.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vnpy.event import EventEngine
from vnpy.trader.constant import Exchange
from vnpy.trader.gateway import BaseGateway
from vnpy.trader.object import CancelRequest, OrderRequest, SubscribeRequest
from vnpy_alphakit.rules import GATE_RULE_NAMES

import run_live_alpha


class RecordingGateway(BaseGateway):
    """Stands in for FutuGateway; connects to nothing, sends nothing."""

    default_name: str = "FUTU"

    def __init__(self, event_engine: EventEngine, gateway_name: str = "FUTU") -> None:
        super().__init__(event_engine, gateway_name)
        self.exchanges: list[Exchange] = [Exchange.SEHK]
        self.sent_orders: list[OrderRequest] = []

    def connect(self, setting: dict) -> None:
        """No-op: deliberately unable to reach any OpenD."""

    def close(self) -> None:
        """No-op: nothing was opened."""

    def subscribe(self, req: SubscribeRequest) -> None:
        """No-op: this test needs no market data."""

    def send_order(self, req: OrderRequest) -> str:
        self.sent_orders.append(req)
        return "FUTU.REACHED_BROKER"

    def cancel_order(self, req: CancelRequest) -> None:
        """No-op: nothing is ever left working."""

    def query_account(self) -> None:
        """No-op."""

    def query_position(self) -> None:
        """No-op: the point of several tests here is that it stays silent."""


@pytest.fixture()
def recorded(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(run_live_alpha, "FutuGateway", RecordingGateway)


def test_build_main_engine_mounts_the_gate_rules(recorded) -> None:
    main_engine = run_live_alpha.build_main_engine()
    try:
        rules = main_engine.engines["RiskManager"].rules   # type: ignore[attr-defined]
        assert [name for name in GATE_RULE_NAMES if name not in rules] == []
        assert [name for name in GATE_RULE_NAMES if not rules[name].active] == []
    finally:
        main_engine.close()


def test_the_runner_defaults_to_dry_run() -> None:
    """Going live must take an explicit flag, never a default."""
    args = run_live_alpha.build_parser().parse_args(
        ["--lab", "/tmp/lab", "--basket", "demo", "--symbols", "700.SEHK"]
    )
    assert args.live is False
    assert args.assume_flat is False


def test_live_is_available_but_opt_in() -> None:
    args = run_live_alpha.build_parser().parse_args(
        ["--lab", "/tmp/lab", "--basket", "demo", "--symbols", "700.SEHK", "--live"]
    )
    assert args.live is True


def test_quote_freshness_and_duplicate_store_are_configured_by_default() -> None:
    """Both guards are on without the operator having to know about them."""
    args = run_live_alpha.build_parser().parse_args(
        ["--lab", "/tmp/lab", "--basket", "demo", "--symbols", "700.SEHK"]
    )
    assert args.quote_max_age > 0
    assert args.duplicate_store


def test_missing_signal_is_refused_rather_than_run_empty(
    recorded, tmp_path, capsys: pytest.CaptureFixture
) -> None:
    """No signal is a stop, not a rebalance against an empty frame."""
    code = run_live_alpha.main([
        "--lab", str(tmp_path), "--basket", "does-not-exist", "--symbols", "700.SEHK",
    ])

    assert code == 2
    assert "没有名为" in capsys.readouterr().err


def test_empty_symbol_list_is_refused(capsys: pytest.CaptureFixture) -> None:
    code = run_live_alpha.main([
        "--lab", "/tmp/lab", "--basket", "demo", "--symbols", " , ",
    ])

    assert code == 2
    assert "至少要有一个" in capsys.readouterr().err


def test_wait_for_positions_reports_broker_silence() -> None:
    """The runner must be able to tell 'flat' from 'has not answered'."""

    class Silent:
        def positions_are_trustworthy(self) -> bool:
            return False

    assert run_live_alpha.wait_for_positions(Silent(), timeout=0.05) is False


def test_wait_for_positions_returns_once_the_broker_speaks() -> None:
    class Answered:
        def positions_are_trustworthy(self) -> bool:
            return True

    assert run_live_alpha.wait_for_positions(Answered(), timeout=5.0) is True
