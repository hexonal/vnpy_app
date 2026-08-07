"""A signal from an older code generation must stop the runner legibly.

``AlphaLab.load_signal`` no longer fails open.  A signal parquet carrying no
``feature_semantics_version`` — or one stamped with a version this code does
not speak — raises ``AlphaSemanticsError`` instead of returning a frame, which
is the whole point of the stamp: the positions in that file were derived from
features whose values have since changed meaning.

Left uncaught, that refusal reached the operator as a bare traceback and exit
1.  Fail-closed, but illegible: nothing in forty lines of polars frames says
"recompute your artifacts", and a stack trace on a trading entry point reads
as a broken tool rather than a stale input.

These tests pin two separate things.  First, that all three shapes of the
refusal land on ``EXIT_STALE_SIGNAL`` with a Chinese sentence rather than a
traceback.  Second — the part that makes changing this file safe at all —
that the refusal happens **before** ``build_main_engine()``, so no gateway
exists, nothing has connected, and there is no working order to unwind.  The
last test guards the inverse: a correctly stamped signal must still sail past,
because a gate that also rejects good artifacts gets removed.
"""

from __future__ import annotations

import os
import sys

import polars as pl
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vnpy.alpha.lab import AlphaLab
from vnpy.alpha.semantics import FEATURE_SEMANTICS_VERSION, STAMP_ATTRIBUTE
from vnpy.event import EventEngine
from vnpy.trader.constant import Exchange
from vnpy.trader.gateway import BaseGateway
from vnpy.trader.object import CancelRequest, OrderRequest, SubscribeRequest

import run_live_alpha

SYMBOL = "700.SEHK"

# Every way a signal parquet can fail the stamp check. They share one exit code
# on purpose: the operator's move is identical in all three cases, and a code
# per shape would only invite a supervisor to treat one of them as benign.
REFUSED_STAMPS: tuple[tuple[str, dict[str, str] | None], ...] = (
    # Written by any code predating the stamp — the realistic case, and the
    # only one an existing lab on disk actually presents.
    ("unstamped", None),
    # Written by v0-aware code, or by a migration that stamped as it copied.
    ("stamped_v0", {STAMP_ATTRIBUTE: "0"}),
    # A hand-edited or truncated stamp. Refused rather than parsed leniently.
    ("stamp_not_an_integer", {STAMP_ATTRIBUTE: "v0"}),
)


class UnreachableGateway(BaseGateway):
    """Fails the test if the runner ever gets far enough to build a gateway.

    Not a no-op recorder: the claim under test is that the semantics refusal
    happens with no broker-facing object in existence, and a recorder that
    quietly does nothing would let a regression that moved the check after
    ``build_main_engine()`` keep passing.
    """

    default_name: str = "FUTU"

    def __init__(self, event_engine: EventEngine, gateway_name: str = "FUTU") -> None:
        raise AssertionError("网关不该被构造 —— 语义闸应在 build_main_engine 之前拦下")

    def connect(self, setting: dict) -> None:
        """Unreachable: construction already failed."""

    def close(self) -> None:
        """Unreachable: construction already failed."""

    def subscribe(self, req: SubscribeRequest) -> None:
        """Unreachable: construction already failed."""

    def send_order(self, req: OrderRequest) -> str:
        """Unreachable: construction already failed."""
        return ""

    def cancel_order(self, req: CancelRequest) -> None:
        """Unreachable: construction already failed."""

    def query_account(self) -> None:
        """Unreachable: construction already failed."""

    def query_position(self) -> None:
        """Unreachable: construction already failed."""


class SilentGateway(BaseGateway):
    """Connects to nothing and never describes a contract.

    Used only by the good-signal test, where the assertion is that the runner
    got *past* ``load_signal`` — reaching the contract timeout is proof of
    that, and it costs no broker.
    """

    default_name: str = "FUTU"

    def __init__(self, event_engine: EventEngine, gateway_name: str = "FUTU") -> None:
        super().__init__(event_engine, gateway_name)
        self.exchanges: list[Exchange] = [Exchange.SEHK]

    def connect(self, setting: dict) -> None:
        """No-op: deliberately unable to reach any OpenD."""

    def close(self) -> None:
        """No-op: nothing was opened."""

    def subscribe(self, req: SubscribeRequest) -> None:
        """No-op: this test never gets a contract to subscribe to."""

    def send_order(self, req: OrderRequest) -> str:
        raise AssertionError("这条路径不该发单")

    def cancel_order(self, req: CancelRequest) -> None:
        """No-op: nothing is ever left working."""

    def query_account(self) -> None:
        """No-op."""

    def query_position(self) -> None:
        """No-op."""


def write_signal(lab_path, name: str, metadata: dict[str, str] | None) -> None:
    """Write a signal parquet with the stamp of our choosing.

    Deliberately bypasses ``AlphaLab.save_signal``, which always stamps with
    the current version — a v0 artifact cannot be produced through the lab's
    own writer, which is exactly the property the stamp is meant to have.
    """
    signal_path = lab_path / "signal"
    signal_path.mkdir(parents=True, exist_ok=True)
    frame = pl.DataFrame({"vt_symbol": [SYMBOL], "signal": [1.0]})
    frame.write_parquet(signal_path / f"{name}.parquet", metadata=metadata)


def run_main(lab_path, basket: str, extra: list[str] | None = None) -> int:
    return run_live_alpha.main([
        "--lab", str(lab_path),
        "--basket", basket,
        "--symbols", SYMBOL,
        *(extra or []),
    ])


# ---------------------------------------------------------------------------
# The refusal
# ---------------------------------------------------------------------------

def test_every_incompatible_stamp_exits_stale_signal_instead_of_raising(
    tmp_path, capsys: pytest.CaptureFixture
) -> None:
    for name, metadata in REFUSED_STAMPS:
        lab_path = tmp_path / name
        write_signal(lab_path, "demo", metadata)

        code = run_main(lab_path, "demo")

        assert code == run_live_alpha.EXIT_STALE_SIGNAL, name
        err = capsys.readouterr().err
        assert "特征语义版本" in err, name
        assert "Traceback" not in err, name


def test_the_refusal_names_the_basket_and_the_step_to_rerun(
    tmp_path, capsys: pytest.CaptureFixture
) -> None:
    """An exit code alone leaves the operator guessing which artifact is stale."""
    write_signal(tmp_path, "us_ai_basket", None)

    assert run_main(tmp_path, "us_ai_basket") == run_live_alpha.EXIT_STALE_SIGNAL

    err = capsys.readouterr().err
    assert "us_ai_basket" in err
    assert "dataset" in err and "model" in err and "signal" in err


def test_stale_signal_and_missing_signal_do_not_share_an_exit_code(
    tmp_path, capsys: pytest.CaptureFixture
) -> None:
    """One is fixed by naming another basket, the other only by recomputing."""
    write_signal(tmp_path, "stale", None)

    stale = run_main(tmp_path, "stale")
    capsys.readouterr()
    absent = run_main(tmp_path, "does-not-exist")
    capsys.readouterr()

    assert stale != absent
    assert (stale, absent) == (run_live_alpha.EXIT_STALE_SIGNAL, 2)


# ---------------------------------------------------------------------------
# Why this is safe to touch on a live-trading entry point
# ---------------------------------------------------------------------------

def test_the_refusal_lands_before_any_engine_or_gateway_exists(
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """The whole safety argument for this change, asserted rather than assumed.

    ``build_main_engine`` is replaced by a bomb and ``FutuGateway`` by a class
    that cannot be constructed, so if the check ever migrates below either of
    them this turns red instead of merely getting slower.
    """

    def exploding_build() -> None:
        raise AssertionError("语义闸之后才建引擎 —— 起飞前检查跑到了起飞之后")

    monkeypatch.setattr(run_live_alpha, "build_main_engine", exploding_build)
    monkeypatch.setattr(run_live_alpha, "FutuGateway", UnreachableGateway)
    write_signal(tmp_path, "demo", None)

    assert run_main(tmp_path, "demo") == run_live_alpha.EXIT_STALE_SIGNAL
    assert "拒绝上实盘" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# The inverse: the gate must not fire on a good artifact
# ---------------------------------------------------------------------------

def test_a_currently_stamped_signal_still_reaches_the_contract_wait(
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """A signal written by this code loads, and the run continues past it.

    Asserting ``EXIT_NO_CONTRACTS`` rather than a successful rebalance because
    that is the first place the run can stop *after* the signal — reaching it
    proves the new ``except`` swallowed nothing, without needing a broker.
    """
    monkeypatch.setattr(run_live_alpha, "FutuGateway", SilentGateway)
    lab = AlphaLab(str(tmp_path / "lab"))
    lab.save_signal("demo", pl.DataFrame({"vt_symbol": [SYMBOL], "signal": [1.0]}))

    code = run_main(lab.lab_path, "demo", ["--contract-wait", "0.3"])

    assert code == run_live_alpha.EXIT_NO_CONTRACTS
    assert "合约" in capsys.readouterr().err


def test_the_lab_writes_the_stamp_this_runner_expects(tmp_path) -> None:
    """Pins the two halves together: a bump on either side must break here.

    Without this, raising ``FEATURE_SEMANTICS_VERSION`` while leaving the
    writer behind would show up first as every live run exiting 6.
    """
    lab = AlphaLab(str(tmp_path / "lab"))
    lab.save_signal("demo", pl.DataFrame({"vt_symbol": [SYMBOL], "signal": [1.0]}))

    metadata = pl.read_parquet_metadata(lab.signal_path / "demo.parquet")

    assert metadata[STAMP_ATTRIBUTE] == str(FEATURE_SEMANTICS_VERSION)
