"""Tests for the three-layer strategy state kit (strategy_state.py).

Two groups:

  * Unit tests for the declaration layers, the JSON sanitiser, the codecs and
    the atomic sidecar writer.
  * Round-trip restart tests that drive the REAL vnpy_ctastrategy.CtaEngine —
    real add_strategy / _init_strategy restore loop / local stop-order book /
    process_trade_event — and assert that a process restart while holding a
    position produces byte-identical orders. Only json persistence is
    redirected into a temp directory.

Run:  .venv/bin/python tests/test_strategy_state.py     (or via pytest)
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, cast

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import strategy_state
import vnpy_ctastrategy.engine as cta_engine_module
from strategies.long_only_turtle_strategy import LongOnlyTurtleStrategy
from strategy_state import DATETIME_CODEC, StateError, StatefulCtaTemplate
from vnpy.event import Event, EventEngine
from vnpy.trader.constant import Direction, Exchange, Interval, Offset, Product
from vnpy.trader.engine import MainEngine
from vnpy.trader.event import EVENT_TRADE
from vnpy.trader.object import BarData, ContractData, TickData, TradeData
from vnpy_ctastrategy.base import EngineType
from vnpy_ctastrategy.engine import CtaEngine

# ---------------------------------------------------------------------------
# harness
# ---------------------------------------------------------------------------

_FAILURES: list[str] = []
_CHECKS: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    """Record a check, and raise when it fails.

    Raising matters: pytest calls the test functions directly and never runs
    main(), so a check that only appended to _FAILURES was invisible under
    pytest -- the file reported "28 passed" no matter what broke. main() still
    collects every failure across tests by catching the AssertionError per
    test, so the standalone run keeps its full report.
    """
    if condition:
        _CHECKS.append(label)
        print(f"  PASS: {label}")
        return

    message = f"{label} {detail}".strip()
    _FAILURES.append(message)
    print(f"  FAIL: {message}")
    raise AssertionError(message)


def expect_error(
    label: str,
    fn: Any,
    exc_type: type[BaseException] = StateError,
) -> None:
    try:
        fn()
    except exc_type as exc:
        check(label, True)
        _ = exc
        return
    except Exception as exc:  # pragma: no cover
        check(label, False, f"(raised {type(exc).__name__}: {exc})")
        return
    check(label, False, "(no exception raised)")


class _TempState:
    """Redirect the sidecar folder and the engine's json store into a tmpdir."""

    def __init__(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="cta_state_test_"))
        self.store: dict[str, Any] = {}
        self._orig_folder = strategy_state.get_folder_path
        self._orig_load = cta_engine_module.load_json
        self._orig_save = cta_engine_module.save_json

    def __enter__(self) -> _TempState:
        def folder(name: str) -> Path:
            p = self.dir.joinpath(name)
            p.mkdir(parents=True, exist_ok=True)
            return p

        strategy_state.get_folder_path = folder
        cta_engine_module.load_json = lambda n: json.loads(json.dumps(self.store.get(n, {})))
        cta_engine_module.save_json = lambda n, d: self.store.__setitem__(
            n, json.loads(json.dumps(d))
        )
        return self

    def __exit__(self, *exc: Any) -> None:
        strategy_state.get_folder_path = self._orig_folder
        cta_engine_module.load_json = self._orig_load
        cta_engine_module.save_json = self._orig_save
        shutil.rmtree(self.dir, ignore_errors=True)

    def sidecar(self, strategy_name: str) -> Path:
        return self.dir.joinpath(strategy_state.STATE_FOLDER, f"{strategy_name}.json")


class _StubEngine:
    """Minimal cta_engine for unit tests that never touch the real engine."""

    engine_type = EngineType.LIVE

    def __init__(self) -> None:
        self.orders: list[dict[str, Any]] = []
        self.logs: list[str] = []
        self.synced: int = 0

    def send_order(
        self,
        strategy: Any,
        direction: Any,
        offset: Any,
        price: float,
        volume: float,
        stop: bool,
        lock: bool,
        net: bool,
    ) -> list[Any]:
        self.orders.append({"direction": direction, "price": price, "volume": volume})
        return [f"stub.{len(self.orders)}"]

    def cancel_all(self, strategy: Any) -> None:
        pass

    def load_bar(self, *a: Any, **k: Any) -> list[Any]:
        return []

    def write_log(self, msg: str, strategy: Any = None) -> None:
        self.logs.append(msg)

    def put_strategy_event(self, strategy: Any) -> None:
        pass

    def sync_strategy_data(self, strategy: Any) -> None:
        self.synced += 1
        strategy.get_variables()

    def get_engine_type(self) -> EngineType:
        return self.engine_type


# ---------------------------------------------------------------------------
# sample strategies used by the unit tests
# ---------------------------------------------------------------------------


class SampleStrategy(StatefulCtaTemplate):
    fast: int = 10
    shown: float = 1.5
    hidden: float = 2.5
    tags: list[Any] = []
    stamp = None

    parameters = ["fast"]
    display_vars = ["shown", "tags"]
    internal_vars = ["hidden", "stamp"]
    transient_vars = ["scratch"]
    state_codecs = {"stamp": DATETIME_CODEC}

    def __init__(
        self, cta_engine: Any, strategy_name: str, vt_symbol: str, setting: dict[str, Any]
    ) -> None:
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)
        self.ready_calls: list[bool] = []
        self.reset_calls: list[str] = []

    def on_init(self) -> None:
        self.scratch = object()

    def on_ready(self, restored: bool) -> None:
        self.ready_calls.append(restored)

    def on_reset(self, reason: str) -> None:
        self.reset_calls.append(reason)


class LeakyStrategy(StatefulCtaTemplate):
    shown: float = 0.0
    parameters = []
    display_vars = ["shown"]

    def on_init(self) -> None:
        self.leaked_level = 123.45     # undeclared -> lost on restart
        self.helper = object()         # undeclared object


class LegacyStrategy(StatefulCtaTemplate):
    alpha: float = 1.0
    beta: float = 2.0
    parameters = []
    variables = ["alpha", "beta"]

    def on_init(self) -> None:
        pass


def make_sample(
    cls: type[StatefulCtaTemplate] = SampleStrategy,
    name: str = "sample",
    **setting: Any,
) -> tuple[Any, _StubEngine]:
    """Build a strategy on the stub engine. Returns Any: the tests drive
    several unrelated strategy classes through this one factory."""
    engine = _StubEngine()
    strat = cls(engine, name, "MU.NASDAQ", setting)
    strat.on_init()
    return strat, engine


# ---------------------------------------------------------------------------
# 1. declaration layers
# ---------------------------------------------------------------------------


def test_layers_compose_variables_and_split_views() -> None:
    with _TempState():
        strat, _ = make_sample()

        check(
            "variables = base3 + display + internal (engine persists everything)",
            strat.variables == ["inited", "trading", "pos", "shown", "tags", "hidden", "stamp"],
            f"got {strat.variables}",
        )

        gui = strat.get_data()["variables"]
        check(
            "get_data() hides internal_vars from the GUI",
            set(gui) == {"inited", "trading", "pos", "shown", "tags"},
            f"got {sorted(gui)}",
        )
        check(
            "get_data() keeps inited/trading (CtaManager reads them)",
            "inited" in gui and "trading" in gui,
        )

        persisted = strat.get_variables()
        check(
            "get_variables() still returns every persisted field",
            set(persisted) == {"inited", "trading", "pos", "shown", "tags", "hidden", "stamp"},
            f"got {sorted(persisted)}",
        )


def test_declaration_errors() -> None:
    def dup():
        class Dup(StatefulCtaTemplate):
            a: float = 0.0
            display_vars = ["a"]
            internal_vars = ["a"]

            def on_init(self) -> None:
                pass

    def no_default():
        class NoDefault(StatefulCtaTemplate):
            display_vars = ["never_declared"]

            def on_init(self) -> None:
                pass

    def base_clash():
        class Clash(StatefulCtaTemplate):
            internal_vars = ["pos"]

            def on_init(self) -> None:
                pass

    def codec_unknown():
        class BadCodec(StatefulCtaTemplate):
            a: float = 0.0
            display_vars = ["a"]
            state_codecs = {"b": DATETIME_CODEC}

            def on_init(self) -> None:
                pass

    def both_lists():
        class Both(StatefulCtaTemplate):
            a: float = 0.0
            b: float = 0.0
            variables = ["a"]
            display_vars = ["b"]

            def on_init(self) -> None:
                pass

    def param_clash():
        class ParamClash(StatefulCtaTemplate):
            a: float = 0.0
            parameters = ["a"]
            display_vars = ["a"]

            def on_init(self) -> None:
                pass

    expect_error("duplicate field across layers is rejected", dup)
    expect_error("persisted field without a class default is rejected", no_default)
    expect_error("declaring a base variable (pos) is rejected", base_clash)
    expect_error("codec for an undeclared field is rejected", codec_unknown)
    expect_error("declaring both `variables` and the new lists is rejected", both_lists)
    expect_error("same name as parameter and state is rejected", param_clash)


def test_legacy_variables_still_work() -> None:
    with _TempState():
        strat, _ = make_sample(LegacyStrategy, name="legacy")
        check(
            "legacy `variables` becomes the display layer",
            strat.variables == ["inited", "trading", "pos", "alpha", "beta"],
            f"got {strat.variables}",
        )
        check(
            "legacy fields still render in the GUI",
            set(strat.get_data()["variables"]) == {"inited", "trading", "pos", "alpha", "beta"},
        )


def test_mutable_default_not_shared_between_instances() -> None:
    with _TempState():
        a, _ = make_sample(name="a")
        b, _ = make_sample(name="b")
        a.tags.append("x")
        check("mutable default is deep-copied per instance", b.tags == [], f"got {b.tags}")
        check("class default untouched", SampleStrategy.tags == [])


# ---------------------------------------------------------------------------
# 2. persistence hardening
# ---------------------------------------------------------------------------


def test_json_hostile_values_never_raise() -> None:
    with _TempState():
        strat, engine = make_sample(name="hostile")
        strat.inited = True

        strat.hidden = float("nan")
        strat.tags = [{"1": 2}]
        data = strat.get_variables()
        check("NaN is replaced by the declared default, not persisted", data["hidden"] == 2.5,
              f"got {data['hidden']!r}")

        strat.hidden = 2.5
        strat.tags = {1: "int key"}          # json would stringify the key silently
        data = strat.get_variables()
        check("non-str dict key falls back to the default", data["tags"] == [],
              f"got {data['tags']!r}")

        class Opaque:
            pass

        strat.tags = [Opaque()]
        data = strat.get_variables()
        check("unserialisable object falls back to the default", data["tags"] == [],
              f"got {data['tags']!r}")

        check("every failure is written to the strategy log",
              any("cannot persist" in m for m in engine.logs))
        check("the whole dict is still json-dumpable (event thread survives)",
              json.dumps(data) is not None)


def test_datetime_codec_roundtrip() -> None:
    with _TempState() as ts:
        strat, _ = make_sample(name="codec")
        strat.inited = True
        moment = datetime(2026, 3, 4, 15, 30, 0)
        strat.stamp = moment
        strat.get_variables()

        payload = json.loads(ts.sidecar("codec").read_text())
        check("datetime is encoded as an ISO string",
              payload["state"]["stamp"] == moment.isoformat())

        fresh, _ = make_sample(name="codec")
        fresh.inited = True
        fresh._ensure_ready()
        check("datetime decodes back to a datetime", fresh.stamp == moment, f"got {fresh.stamp!r}")


def test_int_field_stays_int_across_roundtrip() -> None:
    with _TempState():
        strat, _ = make_sample(LongOnlyTurtleStrategy, name="coerce")
        strat.inited = True
        strat.unit_size = 166
        strat.atr_value = 6.0
        strat.get_variables()

        fresh, _ = make_sample(LongOnlyTurtleStrategy, name="coerce")
        fresh.inited = True
        fresh._ensure_ready()
        check("int field restores as int, not float",
              isinstance(fresh.unit_size, int) and fresh.unit_size == 166,
              f"got {fresh.unit_size!r}")
        check("float field restores as float",
              isinstance(fresh.atr_value, float) and fresh.atr_value == 6.0)


def test_atomic_write_leaves_previous_file_intact() -> None:
    with _TempState() as ts:
        path = ts.dir.joinpath("atomic.json")
        strategy_state._atomic_write_json(path, {"schema": 1, "state": {"a": 1}})
        good = path.read_text()

        class Opaque:
            pass

        raised = False
        try:
            strategy_state._atomic_write_json(path, {"state": {"a": Opaque()}})
        except TypeError:
            raised = True
        check("a bad payload raises instead of writing garbage", raised)
        check("the previous good file is untouched", path.read_text() == good)

        raised = False
        try:
            strategy_state._atomic_write_json(path, {"state": {"a": float("nan")}})
        except ValueError:
            raised = True
        check("NaN is rejected at the writer (allow_nan=False)", raised)
        check("still intact after the NaN attempt", path.read_text() == good)

        leftovers = [p for p in path.parent.iterdir() if p.name.endswith(".tmp")]
        check("no .tmp leftovers", leftovers == [], f"got {leftovers}")


def test_backtest_engine_never_writes_state_file() -> None:
    with _TempState() as ts:
        engine = _StubEngine()
        engine.engine_type = EngineType.BACKTESTING
        strat = SampleStrategy(engine, "bt", "MU.NASDAQ", {})
        strat.on_init()
        strat.inited = True
        strat.hidden = 999.0
        strat.get_variables()
        check("a backtest writes no live state file", not ts.sidecar("bt").exists())


def test_sidecar_is_bound_to_class_and_symbol() -> None:
    with _TempState() as ts:
        strat, _ = make_sample(name="bound")
        strat.inited = True
        strat.hidden = 77.0
        strat.get_variables()

        payload = json.loads(ts.sidecar("bound").read_text())
        payload["class_name"] = "SomeOtherStrategy"
        ts.sidecar("bound").write_text(json.dumps(payload))

        fresh, _ = make_sample(name="bound")
        fresh.inited = True
        fresh._ensure_ready()
        check("a sidecar from another class is ignored", fresh.hidden == 2.5, f"got {fresh.hidden}")

        payload["class_name"] = "SampleStrategy"
        payload["vt_symbol"] = "NVDA.NASDAQ"
        ts.sidecar("bound").write_text(json.dumps(payload))
        fresh2, _ = make_sample(name="bound")
        fresh2.inited = True
        fresh2._ensure_ready()
        check("a sidecar for another symbol is ignored", fresh2.hidden == 2.5,
              f"got {fresh2.hidden}")


def test_corrupt_sidecar_is_ignored_not_fatal() -> None:
    with _TempState() as ts:
        strat, _ = make_sample(name="corrupt")
        strat.inited = True
        strat.hidden = 42.0
        strat.get_variables()
        ts.sidecar("corrupt").write_text('{"schema": 1, "state": {"hidd')  # truncated

        fresh, engine = make_sample(name="corrupt")
        fresh.inited = True
        fresh._ensure_ready()
        check("a truncated sidecar does not raise", True)
        check("values fall back to the defaults", fresh.hidden == 2.5)
        check("the problem is logged", any("unreadable" in m for m in engine.logs))


# ---------------------------------------------------------------------------
# 3. lifecycle hooks
# ---------------------------------------------------------------------------


def test_on_ready_runs_once_and_before_the_first_order() -> None:
    with _TempState():
        strat, engine = make_sample(name="ready")
        strat.inited = True
        strat.trading = True
        check("on_ready has not fired yet", strat.ready_calls == [])

        strat.buy(100.0, 1)
        check("on_ready fired before the first order reached the engine",
              strat.ready_calls == [False] and len(engine.orders) == 1,
              f"ready={strat.ready_calls} orders={len(engine.orders)}")

        strat.buy(101.0, 1)
        strat.get_data()
        check("on_ready is not fired again", len(strat.ready_calls) == 1,
              f"got {strat.ready_calls}")


def test_on_ready_reports_restored_true_after_a_restart() -> None:
    with _TempState():
        first, _ = make_sample(name="restore")
        first.inited = True
        first.hidden = 12.5
        first.get_variables()

        second, _ = make_sample(name="restore")
        second.inited = True
        second._ensure_ready()
        check("restored=True on the second process", second.ready_calls == [True],
              f"got {second.ready_calls}")
        check("hidden field came back", second.hidden == 12.5, f"got {second.hidden}")


def test_reset_state_restores_defaults_and_fires_on_reset() -> None:
    with _TempState():
        strat, _ = make_sample(name="reset")
        strat.inited = True
        strat.trading = True
        strat.hidden = 99.0
        strat.shown = 88.0
        strat.tags = ["dirty"]
        strat.pos = 100

        strat.reset_state("manual test")
        check("internal field reset", strat.hidden == 2.5)
        check("display field reset", strat.shown == 1.5)
        check("mutable field reset to a fresh copy", strat.tags == [])
        check("pos is NOT reset (broker owns it)", strat.pos == 100)
        check("on_reset fired with the reason", strat.reset_calls == ["manual test"])


def test_schema_bump_resets_only_when_flat() -> None:
    with _TempState() as ts:
        strat, _ = make_sample(name="schema")
        strat.inited = True
        strat.hidden = 55.0
        strat.get_variables()

        payload = json.loads(ts.sidecar("schema").read_text())
        payload["schema"] = 99
        ts.sidecar("schema").write_text(json.dumps(payload))

        flat, _ = make_sample(name="schema")
        flat.inited = True
        flat.trading = True
        flat._ensure_ready()
        check("flat + schema mismatch -> state reset", flat.hidden == 2.5, f"got {flat.hidden}")
        check("on_reset fired with the schema reason",
              any("schema" in r for r in flat.reset_calls), f"got {flat.reset_calls}")

        ts.sidecar("schema").write_text(json.dumps(payload))
        holding, engine = make_sample(name="schema")
        holding.inited = True
        holding.pos = 500
        holding._ensure_ready()
        check("holding + schema mismatch -> values kept, not reset",
              holding.hidden == 55.0, f"got {holding.hidden}")
        check("the mismatch is logged loudly",
              any("Reconcile manually" in m for m in engine.logs))


def test_audit_catches_undeclared_attributes() -> None:
    with _TempState():
        strat, engine = make_sample(LeakyStrategy, name="leaky")
        strat.inited = True
        audit = strat.audit_undeclared_state()
        check("undeclared scalar is reported as persistable",
              audit["persistable"] == ["leaked_level"], f"got {audit}")
        check("undeclared object is reported separately",
              "helper" in audit["objects"], f"got {audit}")

        strat._ensure_ready()
        check("the leak is written to the strategy log",
              any("undeclared attributes" in m for m in engine.logs))


def test_strict_state_blocks_orders_when_state_would_leak() -> None:
    with _TempState():
        class StrictLeaky(LeakyStrategy):
            strict_state = True

        strat, engine = make_sample(StrictLeaky, name="strict")
        strat.inited = True
        strat.trading = True
        result = strat.buy(100.0, 1)
        check("strict_state refuses the order", result == [] and engine.orders == [],
              f"got {result} / {engine.orders}")
        check("the refusal is logged", any("order refused" in m for m in engine.logs))


def test_turtle_declares_everything_it_owns() -> None:
    with _TempState():
        strat, _ = make_sample(LongOnlyTurtleStrategy, name="declared")
        strat.inited = True
        audit = strat.audit_undeclared_state()
        check("LongOnlyTurtleStrategy leaks no state",
              audit["persistable"] == [] and audit["objects"] == [], f"got {audit}")


# ---------------------------------------------------------------------------
# 4. restart round trip against the REAL CtaEngine
# ---------------------------------------------------------------------------

_START = datetime(2025, 1, 2, 16, 0)
_SETTING = dict(
    entry_window=20, breakout_window=55, exit_window=10, atr_window=20,
    trading_capital=100000.0, risk_percent=1.0, board_lot=1,
    max_units=4, atr_stop=2.0,
)


def _bar(i: int, o: float, h: float, low: float, c: float) -> BarData:
    return BarData(
        symbol="MU", exchange=Exchange.NASDAQ, datetime=_START + timedelta(days=i),
        interval=Interval.DAILY, volume=1_000_000, turnover=1e8, open_interest=0,
        open_price=o, high_price=h, low_price=low, close_price=c, gateway_name="TEST",
    )


def _scenario_bars() -> list[BarData]:
    """80 flat bars (ATR20 == 6, 20d high == 103), a breakout, then a slow grind.

    The grind keeps highs under 106 so unit 2 never fills, which is what makes
    the pyramid base observable: a live session holds base == 103 while a
    restarted session would re-derive it from the current 20-day high.
    """
    bars = [_bar(i, 100.0, 103.0, 97.0, 100.0) for i in range(80)]
    bars.append(_bar(80, 102.0, 104.0, 101.5, 103.8))
    for k in range(25):
        c = 104.0 + 0.075 * k
        bars.append(_bar(81 + k, c - 0.2, c + 0.1, c - 0.6, c))
    bars.append(_bar(106, 105.8, 105.95, 105.4, 105.9))
    return bars


class _LiveRig:
    """A real MainEngine + CtaEngine with one contract and no gateway."""

    def __init__(self) -> None:
        self.event_engine = EventEngine()
        self.main_engine = MainEngine(self.event_engine)
        self.cta_engine = CtaEngine(self.main_engine, self.event_engine)
        self.cta_engine.classes["LongOnlyTurtleStrategy"] = LongOnlyTurtleStrategy
        self.cta_engine.load_strategy_data()
        contract = ContractData(
            symbol="MU", exchange=Exchange.NASDAQ, name="MU", product=Product.EQUITY,
            size=1, pricetick=0.01, min_volume=1, stop_supported=False,
            net_position=True, gateway_name="TEST",
        )
        oms = cast(Any, self.main_engine.get_engine("oms"))
        oms.contracts[contract.vt_symbol] = contract
        self._trade_id = 0

    def start(self, name: str, history: list[BarData]) -> Any:
        # CtaEngine.init_engine() reads the persisted state right before the
        # strategies are inited; reload here so a rig created up front still
        # sees what an earlier session wrote.
        self.cta_engine.load_strategy_data()
        self.cta_engine.add_strategy("LongOnlyTurtleStrategy", name, "MU.NASDAQ", dict(_SETTING))
        self.cta_engine.load_bar = lambda *a, **k: list(history)
        self.cta_engine._init_strategy(name)
        self.cta_engine.start_strategy(name)
        return self.cta_engine.strategies[name]  # LongOnlyTurtleStrategy

    def feed(self, strategy: Any, bar: BarData) -> None:
        """Fill any stop the bar's range crosses, then deliver the bar."""
        for stop_order in list(self.cta_engine.stop_orders.values()):
            hit = (
                stop_order.direction == Direction.LONG and bar.high_price >= stop_order.price
            ) or (
                stop_order.direction == Direction.SHORT and bar.low_price <= stop_order.price
            )
            if not hit:
                continue
            self.cta_engine.cancel_local_stop_order(strategy, stop_order.stop_orderid)
            self.fill(strategy, stop_order.direction, stop_order.offset,
                      stop_order.price, stop_order.volume, bar.datetime)
        strategy.on_bar(bar)

    def fill(
        self,
        strategy: Any,
        direction: Direction,
        offset: Offset,
        price: float,
        volume: float,
        when: datetime,
    ) -> None:
        self._trade_id += 1
        trade = TradeData(
            symbol="MU", exchange=Exchange.NASDAQ, orderid=f"o{self._trade_id}",
            tradeid=f"t{self._trade_id}", direction=direction, offset=offset,
            price=price, volume=volume, datetime=when, gateway_name="TEST",
        )
        self.cta_engine.orderid_strategy_map[trade.vt_orderid] = strategy
        self.cta_engine.process_trade_event(Event(EVENT_TRADE, trade))

    def stops(self, direction: Direction) -> list[float]:
        return sorted(
            round(so.price, 2) for so in self.cta_engine.stop_orders.values()
            if so.direction == direction
        )

    def close(self) -> None:
        self.main_engine.close()


def test_removing_a_strategy_deletes_its_sidecar() -> None:
    """A removed strategy must not come back holding a position.

    The engine's own cleanup (remove_strategy_setting) only clears
    cta_strategy_setting.json and cta_strategy_data.json; the sidecar under
    STATE_FOLDER is written by the strategy and the engine has never heard of
    it. Before on_remove existed, creating a strategy again under the same name
    -- the obvious move after removing one by mistake -- restored the old pos
    from that file and the next bar put out an exit order for a position the
    account did not hold.
    """
    bars = _scenario_bars()
    with _TempState() as tmp:
        live = _LiveRig()
        try:
            strat = live.start("ghost", [])
            for bar in bars[:106]:
                live.feed(strat, bar)
            held = strat.pos
            check("the strategy is holding before removal", held > 0, f"pos={held}")
            check("the sidecar exists while it runs", tmp.sidecar("ghost").exists())

            live.cta_engine.stop_strategy("ghost")
            removed = live.cta_engine.remove_strategy("ghost")
            check("remove_strategy succeeded", removed is True, f"got {removed}")
            check("the sidecar is gone after removal",
                  not tmp.sidecar("ghost").exists(),
                  f"still there: {tmp.sidecar('ghost')}")
        finally:
            live.close()

        # Re-create under the same name on a fresh engine: no resurrection.
        fresh = _LiveRig()
        try:
            reborn = fresh.start("ghost", [])
            check("a re-created strategy starts flat", reborn.pos == 0,
                  f"resurrected pos={reborn.pos}")
            fresh.feed(reborn, bars[106])
            sells = fresh.stops(Direction.SHORT)
            check("no exit order for a position that does not exist", sells == [],
                  f"ghost stops: {sells}")
        finally:
            fresh.close()


def test_on_remove_failure_does_not_block_removal() -> None:
    """A disk error while deleting must not leave the strategy in the engine.

    Removal is a user action that has to complete; a strategy stuck in the
    engine because a file could not be unlinked is worse than a stale file,
    which the operator can delete by hand once the log says so.
    """
    with _TempState():
        live = _LiveRig()
        try:
            strat = live.start("brittle", [])
            live.cta_engine.stop_strategy("brittle")

            def boom() -> None:
                raise OSError("read-only file system")

            strat.on_remove = boom  # type: ignore[method-assign]
            removed = live.cta_engine.remove_strategy("brittle")

            check("removal still succeeds", removed is True, f"got {removed}")
            check("the strategy is out of the engine",
                  "brittle" not in live.cta_engine.strategies,
                  f"still registered: {list(live.cta_engine.strategies)}")
        finally:
            live.close()


def test_restart_while_holding_reproduces_identical_orders() -> None:
    """The regression test for the bug this whole module exists to fix."""
    bars = _scenario_bars()
    with _TempState():
        live = _LiveRig()
        restarted = _LiveRig()
        try:
            # Session A: one continuous process through bar 105.
            strat_a = live.start("turtle", [])
            for bar in bars[:106]:
                live.feed(strat_a, bar)

            check("session A is holding a position", strat_a.pos > 0, f"pos={strat_a.pos}")
            check("session A froze the pyramid base at the breakout",
                  abs(strat_a._pyramid_base - 103.0) < 1e-9, f"got {strat_a._pyramid_base}")

            # Session B: the process died at bar 105 and comes back up. on_init
            # replays bars 0..105, then the engine restores `variables`.
            strat_b = restarted.start("turtle", bars[:106])

            check("restart recovers pos", strat_b.pos == strat_a.pos,
                  f"{strat_b.pos} vs {strat_a.pos}")
            check("restart recovers the pyramid base",
                  abs(strat_b._pyramid_base - strat_a._pyramid_base) < 1e-9,
                  f"{strat_b._pyramid_base} vs {strat_a._pyramid_base}")
            check("restart recovers the open-position cost basis",
                  (strat_b._entry_cost, strat_b._entry_vol)
                  == (strat_a._entry_cost, strat_a._entry_vol),
                  f"{(strat_b._entry_cost, strat_b._entry_vol)} vs "
                  f"{(strat_a._entry_cost, strat_a._entry_vol)}")
            check("restart recovers the frozen N and unit size",
                  (strat_b.atr_value, strat_b.unit_size)
                  == (strat_a.atr_value, strat_a.unit_size))
            check("restart recovers the ATR stop", strat_b.long_stop == strat_a.long_stop)

            # Both processes now see the same next bar.
            live.feed(strat_a, bars[106])
            restarted.feed(strat_b, bars[106])

            buys_a, buys_b = live.stops(Direction.LONG), restarted.stops(Direction.LONG)
            sells_a, sells_b = live.stops(Direction.SHORT), restarted.stops(Direction.SHORT)
            check("pyramid add levels are identical after a restart", buys_a == buys_b,
                  f"{buys_a} vs {buys_b}")
            check("protective stop levels are identical after a restart", sells_a == sells_b,
                  f"{sells_a} vs {sells_b}")
            check("the ladder is the frozen one, not the current Donchian high",
                  buys_a == [106.0, 109.0, 112.0], f"got {buys_a}")
        finally:
            live.close()
            restarted.close()


def test_restart_preserves_round_trip_pnl_for_the_filter() -> None:
    bars = _scenario_bars()
    with _TempState():
        live = _LiveRig()
        restarted = _LiveRig()
        try:
            strat_a = live.start("turtle", [])
            for bar in bars[:106]:
                live.feed(strat_a, bar)
            strat_b = restarted.start("turtle", bars[:106])

            for rig, strat in ((live, strat_a), (restarted, strat_b)):
                rig.fill(strat, Direction.SHORT, Offset.CLOSE, 108.0, strat.pos,
                         bars[106].datetime)

            check("continuous session records the true round-trip PnL",
                  abs(strat_a.last_trade_pnl - 830.0) < 1e-6, f"got {strat_a.last_trade_pnl}")
            check("restarted session records the same PnL",
                  abs(strat_b.last_trade_pnl - strat_a.last_trade_pnl) < 1e-6,
                  f"{strat_b.last_trade_pnl} vs {strat_a.last_trade_pnl}")
            check("the last-trade filter still sees a winner after a restart",
                  strat_b.last_trade_pnl > 0)
        finally:
            live.close()
            restarted.close()


def test_restart_survives_a_lost_engine_json() -> None:
    """`save_json` is not atomic; a truncated cta_strategy_data.json takes out
    every strategy at once. The sidecar has to carry the state on its own."""
    bars = _scenario_bars()
    with _TempState() as ts:
        live = _LiveRig()
        restarted = _LiveRig()
        try:
            strat_a = live.start("turtle", [])
            for bar in bars[:106]:
                live.feed(strat_a, bar)

            check("the sidecar exists", ts.sidecar("turtle").exists())

            # Operator wipes the corrupt engine file and restarts.
            ts.store["cta_strategy_data.json"] = {}
            restarted.cta_engine.strategy_data = {}
            strat_b = restarted.start("turtle", bars[:106])

            check("state layer survives on the sidecar alone",
                  abs(strat_b._pyramid_base - strat_a._pyramid_base) < 1e-9,
                  f"{strat_b._pyramid_base} vs {strat_a._pyramid_base}")
            check("cost basis survives on the sidecar alone",
                  strat_b._entry_vol == strat_a._entry_vol,
                  f"{strat_b._entry_vol} vs {strat_a._entry_vol}")
            check("frozen N survives on the sidecar alone",
                  strat_b.atr_value == strat_a.atr_value)
            check("pos is adopted from the sidecar rather than assumed flat",
                  strat_b.pos == strat_a.pos, f"{strat_b.pos} vs {strat_a.pos}")
            check("the adoption is logged for the operator",
                  strat_b._state_file_pos == strat_a.pos,
                  f"{strat_b._state_file_pos} vs {strat_a.pos}")
        finally:
            live.close()
            restarted.close()


def test_protective_stop_is_rearmed_on_the_first_tick_after_restart() -> None:
    bars = _scenario_bars()
    with _TempState():
        live = _LiveRig()
        restarted = _LiveRig()
        try:
            strat_a = live.start("turtle", [])
            for bar in bars[:106]:
                live.feed(strat_a, bar)

            strat_b = restarted.start("turtle", bars[:106])
            check("a restarted process starts with no orders at all",
                  restarted.stops(Direction.SHORT) == [] and restarted.stops(Direction.LONG) == [])
            check("on_ready flagged the position for re-arming", strat_b._rearm_pending is True)

            tick = TickData(
                symbol="MU", exchange=Exchange.NASDAQ, name="MU",
                datetime=bars[106].datetime, last_price=105.9, gateway_name="TEST",
            )
            strat_b.on_tick(tick)

            expected = round(max(strat_a.long_stop, strat_a.exit_down), 2)
            check("the protective stop is back before the next bar",
                  restarted.stops(Direction.SHORT) == [expected],
                  f"got {restarted.stops(Direction.SHORT)}, expected [{expected}]")
            check("re-arm does not repeat on the next tick", strat_b._rearm_pending is False)

            strat_b.on_tick(tick)
            check("still exactly one protective stop",
                  len(restarted.stops(Direction.SHORT)) == 1,
                  f"got {restarted.stops(Direction.SHORT)}")
        finally:
            live.close()
            restarted.close()


def test_derived_var_is_shown_but_never_persisted() -> None:
    bars = _scenario_bars()
    with _TempState() as ts:
        live = _LiveRig()
        restarted = _LiveRig()
        try:
            strat_a = live.start("turtle", [])
            for bar in bars[:106]:
                live.feed(strat_a, bar)

            check("exit_down is not in `variables` (engine never persists it)",
                  "exit_down" not in strat_a.variables, f"got {strat_a.variables}")
            check("exit_down is still shown in the GUI",
                  "exit_down" in strat_a.get_data()["variables"])
            payload = json.loads(ts.sidecar("turtle").read_text())
            check("exit_down is absent from the sidecar",
                  "exit_down" not in payload["state"], f"got {sorted(payload['state'])}")

            stale = ts.store["cta_strategy_data.json"]["turtle"].get("exit_down")
            check("the engine's json has no stale exit_down either", stale is None,
                  f"got {stale}")

            strat_b = restarted.start("turtle", bars[:106])
            check("a restart recomputes exit_down from replayed bars",
                  abs(strat_b.exit_down - strat_a.exit_down) < 1e-9,
                  f"{strat_b.exit_down} vs {strat_a.exit_down}")
        finally:
            live.close()
            restarted.close()


def test_base_class_does_not_appear_in_the_strategy_dropdown() -> None:
    """The class scanner registers any CtaTemplate subclass found in dir(module),
    which is why strategy modules import the base through the module object."""
    with _TempState():
        rig = _LiveRig()
        try:
            rig.cta_engine.classes.clear()
            cwd = os.getcwd()
            os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            try:
                rig.cta_engine.load_strategy_class()
            finally:
                os.chdir(cwd)

            names = rig.cta_engine.get_all_strategy_class_names()
            check("the real strategy is registered", "LongOnlyTurtleStrategy" in names,
                  f"got {names}")
            check("StatefulCtaTemplate is NOT offered as a strategy",
                  "StatefulCtaTemplate" not in names, f"got {names}")
        finally:
            rig.close()


def test_runs_unchanged_under_the_backtesting_engine() -> None:
    from vnpy_ctastrategy.backtesting import BacktestingEngine

    with _TempState() as ts:
        engine = BacktestingEngine()
        engine.set_parameters(
            vt_symbol="MU.NASDAQ",
            interval=Interval.DAILY,
            start=_START,
            end=_START + timedelta(days=200),
            rate=0.0,
            slippage=0.0,
            size=1,
            pricetick=0.01,
            capital=100000,
        )
        engine.add_strategy(LongOnlyTurtleStrategy, dict(_SETTING))
        engine.history_data = _scenario_bars()
        engine.run_backtesting()

        check("the backtest produced trades", len(engine.trades) > 0,
              f"got {len(engine.trades)} trades")
        check("a backtest leaves no live state file behind",
              not ts.dir.joinpath(strategy_state.STATE_FOLDER).exists()
              or list(ts.dir.joinpath(strategy_state.STATE_FOLDER).iterdir()) == [])
        check("on_ready still ran before the first backtest order",
              cast(Any, engine.strategy)._state_ready is True)


def test_gui_payload_stays_small_while_state_is_complete() -> None:
    bars = _scenario_bars()
    with _TempState():
        live = _LiveRig()
        try:
            strat = live.start("turtle", [])
            for bar in bars[:106]:
                live.feed(strat, bar)

            gui = strat.get_data()["variables"]
            check("GUI shows the 7 operator fields plus the 3 base ones",
                  len(gui) == 10, f"got {sorted(gui)}")
            check("GUI does not show the internal bookkeeping",
                  not any(k.startswith("_") for k in gui), f"got {sorted(gui)}")
            check("persistence still covers the internal bookkeeping",
                  {"_pyramid_base", "_entry_cost", "_entry_vol", "_round_pnl"}
                  <= set(strat.get_variables()))
        finally:
            live.close()


# ---------------------------------------------------------------------------

TESTS = [
    test_layers_compose_variables_and_split_views,
    test_declaration_errors,
    test_legacy_variables_still_work,
    test_mutable_default_not_shared_between_instances,
    test_json_hostile_values_never_raise,
    test_datetime_codec_roundtrip,
    test_int_field_stays_int_across_roundtrip,
    test_atomic_write_leaves_previous_file_intact,
    test_backtest_engine_never_writes_state_file,
    test_sidecar_is_bound_to_class_and_symbol,
    test_corrupt_sidecar_is_ignored_not_fatal,
    test_on_ready_runs_once_and_before_the_first_order,
    test_on_ready_reports_restored_true_after_a_restart,
    test_reset_state_restores_defaults_and_fires_on_reset,
    test_schema_bump_resets_only_when_flat,
    test_audit_catches_undeclared_attributes,
    test_strict_state_blocks_orders_when_state_would_leak,
    test_turtle_declares_everything_it_owns,
    test_removing_a_strategy_deletes_its_sidecar,
    test_on_remove_failure_does_not_block_removal,
    test_restart_while_holding_reproduces_identical_orders,
    test_restart_preserves_round_trip_pnl_for_the_filter,
    test_restart_survives_a_lost_engine_json,
    test_protective_stop_is_rearmed_on_the_first_tick_after_restart,
    test_derived_var_is_shown_but_never_persisted,
    test_base_class_does_not_appear_in_the_strategy_dropdown,
    test_runs_unchanged_under_the_backtesting_engine,
    test_gui_payload_stays_small_while_state_is_complete,
]


def main() -> int:
    for test in TESTS:
        print(test.__name__)
        try:
            test()
        except AssertionError:
            pass  # check() already recorded it; keep going to see everything
        sys.stdout.flush()

    print()
    if _FAILURES:
        print(f"{len(_FAILURES)} FAILED out of {len(_CHECKS) + len(_FAILURES)} checks:")
        for failure in _FAILURES:
            print(f"  - {failure}")
        return 1
    print(f"All {len(TESTS)} tests passed ({len(_CHECKS)} checks).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
