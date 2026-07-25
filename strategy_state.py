"""Three-layer strategy state (Params / State / Vars) + on_ready / on_reset.

WHY THIS EXISTS
---------------
`vnpy_ctastrategy.CtaTemplate` has exactly two declaration lists: `parameters`
and `variables`.  `variables` carries two unrelated jobs at once:

  * CtaEngine._init_strategy() restores a strategy by looping over
    `strategy.variables` (engine.py:689) -- so anything NOT in that list is
    silently reset to its class default on every restart.
  * CtaManager's DataMonitor renders `get_data()["variables"]` verbatim
    (ui/widget.py:254) -- so anything IN that list is shown to the operator.

An author therefore has to choose between "my internal state survives a
restart" and "my GUI is readable".  Most pick the readable GUI and lose state.
On `LongOnlyTurtleStrategy` that costs real money: `_pyramid_base`,
`_entry_cost`, `_entry_vol` and `_round_pnl` live only in `on_init`, so after a
restart the pyramid ladder is re-derived from the *current* Donchian high
instead of the frozen entry breakout, and the round-trip PnL that drives the
last-trade filter is permanently zero for the open trade.

WHAT THIS MODULE ADDS
---------------------
Five declaration lists instead of one, plus two lifecycle hooks:

    parameters      unchanged vnpy semantics -- config, from the setting file
    display_vars    persisted AND shown in the GUI      (post 32750's "State")
    internal_vars   persisted, hidden from the GUI      (post 32750's "Vars")
    derived_vars    shown in the GUI, NOT persisted
    transient_vars  neither; rebuilt in on_init (ArrayManager, BarGenerator)

    on_ready(restored)  runs after state is restored, before the first order
    on_reset(reason)    runs when state is deliberately discarded

`variables` is composed automatically as
`["inited", "trading", "pos"] + display_vars + internal_vars`, so the *stock*
CtaEngine persists and restores every declared field with no engine change at
all.  `get_data()` is then narrowed to `display_vars + derived_vars` so the GUI
only shows what the author marked as operator-facing.

`derived_vars` is not decoration.  The engine only calls `sync_strategy_data`
on trade events and on stop, so a value that `on_bar` recomputes from the
ArrayManager is persisted at whatever it happened to be during the last fill --
possibly many bars ago.  Restoring that stale number *overwrites* the correct
one that replaying history through `on_init` just produced.  On the turtle,
`exit_down` (the 10-day Donchian exit) went from a correct 104.52 to a stale
97.0 that way, which is the difference between a protective stop under the
market and one 7% below it.  Anything reconstructible from replayed bars
belongs in `derived_vars`; only values that are *frozen* at entry (entry_up,
N, the ATR stop) are real state.

It also hardens persistence, because two upstream behaviours can lose or
destroy state on a machine that is holding a position:

  1. `save_json` writes with `open(mode="w+")` + `json.dump`, which is not
     atomic.  A kill mid-write truncates `cta_strategy_data.json`, and the
     next `load_strategy_data()` raises on `json.load` -- taking out the state
     of EVERY strategy, not just one.  We additionally mirror each strategy's
     state to a per-strategy sidecar written with `os.replace`, and prefer the
     sidecar when it is valid.
  2. `EventEngine._run` only catches `queue.Empty`, so an exception raised
     inside `process_trade_event` kills the event thread for good -- no more
     ticks, orders or trades, while a live position sits there.  Putting a
     `datetime` (or any non-JSON value) in `variables` does exactly that via
     `save_json`.  `get_variables()` here sanitises every value and never
     raises.

Nothing in `vnpy_ctastrategy` is modified.

PLACEMENT NOTE
--------------
Keep this module OUT of the `strategies/` folder.  CtaEngine's class scanner
(`load_strategy_class_from_module`, engine.py:812) registers every
`CtaTemplate` subclass it finds in `dir(module)`, including imported base
classes.  Strategy modules should therefore reference the base through the
module object::

    import strategy_state

    class MyStrategy(strategy_state.StatefulCtaTemplate):
        ...

so that `StatefulCtaTemplate` never appears as a name in the strategy module
and never shows up in the "add strategy" dropdown.
"""

from __future__ import annotations

import contextlib
import copy
import json
import math
import os
import tempfile
import types
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from vnpy.trader.utility import get_folder_path
from vnpy_ctastrategy.base import EngineType
from vnpy_ctastrategy.template import CtaTemplate

__all__ = [
    "StateCodec",
    "DATETIME_CODEC",
    "StatefulCtaTemplate",
    "STATE_FOLDER",
]

STATE_FOLDER = "cta_strategy_state"

# Attributes owned by CtaTemplate / TargetPosTemplate.  The undeclared-state
# audit ignores these; they are engine plumbing, not strategy state.
_RESERVED_ATTRS: frozenset[str] = frozenset(
    {
        "cta_engine",
        "strategy_name",
        "vt_symbol",
        "inited",
        "trading",
        "pos",
        "variables",
        "parameters",
        "author",
        # TargetPosTemplate
        "active_orderids",
        "cancel_orderids",
        "last_tick",
        "last_bar",
        "target_pos",
    }
)

_BASE_VARIABLES: tuple[str, ...] = ("inited", "trading", "pos")


class StateCodec:
    """Encoder/decoder pair for a state field whose value is not JSON native.

    ``encode`` must return something ``json.dumps`` accepts; ``decode`` gets
    that value back.  Both must tolerate the field's declared default.
    """

    __slots__ = ("encode", "decode", "name")

    def __init__(
        self,
        encode: Callable[[Any], Any],
        decode: Callable[[Any], Any],
        name: str = "custom",
    ) -> None:
        self.encode = encode
        self.decode = decode
        self.name = name

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<StateCodec {self.name}>"


def _encode_datetime(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"expected datetime, got {type(value).__name__}")


def _decode_datetime(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


DATETIME_CODEC = StateCodec(_encode_datetime, _decode_datetime, "datetime")


class StateError(Exception):
    """Raised for declaration mistakes, at class-definition time."""


def _is_mutable_default(value: Any) -> bool:
    return isinstance(value, (list, dict, set, bytearray))


def _json_sanitize(value: Any, path: str) -> Any:
    """Return a JSON-native copy of ``value`` or raise ``TypeError``.

    Rejects non-finite floats (a NaN stop level makes every comparison False,
    which is worse than no stop level) and non-string dict keys (``json`` turns
    them into strings silently, so the value changes type across a restart).
    """
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise TypeError(f"{path}: non-finite float {value!r}")
        return value
    if isinstance(value, (list, tuple)):
        return [_json_sanitize(v, f"{path}[{i}]") for i, v in enumerate(value)]
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if not isinstance(k, str):
                raise TypeError(
                    f"{path}: dict key {k!r} is {type(k).__name__}, not str "
                    f"(json would stringify it silently -- declare a StateCodec)"
                )
            out[k] = _json_sanitize(v, f"{path}.{k}")
        return out
    raise TypeError(f"{path}: {type(value).__name__} is not JSON serialisable")


def _coerce_to_default_type(value: Any, default: Any) -> Any:
    """Undo the small type drifts a JSON round trip introduces."""
    if default is None or value is None:
        return value
    if isinstance(default, bool):
        return bool(value)
    if isinstance(default, int) and not isinstance(default, bool):
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, float) and value.is_integer():
            return int(value)
        return value
    if isinstance(default, float):
        if isinstance(value, bool):
            return float(value)
        if isinstance(value, (int, float)):
            return float(value)
        return value
    if isinstance(default, tuple) and isinstance(value, list):
        return tuple(value)
    return value


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write ``payload`` so that ``path`` is either the old or the new file.

    A truncated state file is the failure this exists to prevent, so the
    temp file is fsync'd before the rename.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False)
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


class StatefulCtaTemplate(CtaTemplate):
    """CtaTemplate with a declared state layer and restart lifecycle hooks.

    Subclasses declare::

        display_vars    persisted, shown in the GUI
        internal_vars   persisted, hidden from the GUI
        derived_vars    shown in the GUI, recomputed each bar, never persisted
        transient_vars  not persisted, rebuilt every process start

    Every name in ``display_vars``, ``internal_vars`` and ``derived_vars`` MUST
    have a class-level default; that default is the reset value and the fallback
    when a stored value cannot be decoded.  Declaring the default is what makes
    the field recoverable, so it is enforced at class-definition time.
    """

    # --- declaration layers (subclasses override) ---
    display_vars: list[str] = []
    internal_vars: list[str] = []
    derived_vars: list[str] = []
    transient_vars: list[str] = []

    # Optional per-field codecs for values JSON cannot hold, e.g.
    #   state_codecs = {"last_entry_dt": strategy_state.DATETIME_CODEC}
    state_codecs: dict[str, StateCodec] = {}

    # Bump when the meaning of a stored field changes.  A mismatch resets the
    # state layer -- but only while flat; see _apply_state_file().
    state_version: int = 1

    # When True, refuse to send orders while undeclared scalar attributes exist.
    strict_state: bool = False

    # Filled in by __init_subclass__.
    _persisted_names: tuple[str, ...] = ()
    _display_names: tuple[str, ...] = ()
    _state_defaults: dict[str, Any] = {}

    # Instance attributes owned by this base class; excluded from the audit.
    _KIT_ATTRS: frozenset[str] = frozenset(
        {
            "_state_ready",
            "_state_restored",
            "_state_seq",
            "_state_issues",
            "_undeclared_scalars",
            "_state_file_pos",
        }
    )

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)

        display: list[str] = list(cls.display_vars)
        internal: list[str] = list(cls.internal_vars)
        derived: list[str] = list(cls.derived_vars)
        transient: list[str] = list(cls.transient_vars)

        # Legacy migration: a subclass that still declares `variables` and has
        # not adopted the new lists keeps working, its variables become the
        # display layer.  Declaring both is ambiguous, so it is an error.
        legacy = list(cls.__dict__.get("variables", []) or [])
        if legacy:
            if display or internal or derived:
                raise StateError(
                    f"{cls.__name__}: declares both `variables` and "
                    f"display_vars/internal_vars/derived_vars. "
                    f"Use the new lists only."
                )
            display = legacy

        seen: dict[str, str] = {}
        for layer, names in (
            ("display_vars", display),
            ("internal_vars", internal),
            ("derived_vars", derived),
            ("transient_vars", transient),
            ("parameters", list(cls.parameters)),
        ):
            for name in names:
                if name in _BASE_VARIABLES:
                    raise StateError(
                        f"{cls.__name__}.{layer}: '{name}' is managed by "
                        f"CtaTemplate and is always persisted and displayed."
                    )
                if name in seen:
                    raise StateError(
                        f"{cls.__name__}: '{name}' declared in both "
                        f"{seen[name]} and {layer}."
                    )
                seen[name] = layer

        persisted = tuple(display) + tuple(internal)

        defaults: dict[str, Any] = {}
        missing: list[str] = []
        for name in persisted + tuple(derived):
            if not hasattr(cls, name):
                missing.append(name)
                continue
            defaults[name] = getattr(cls, name)
        if missing:
            raise StateError(
                f"{cls.__name__}: declared field(s) {missing} have no "
                f"class-level default. A declared field needs a default: it "
                f"is the reset value and the fallback for undecodable data."
            )

        for name, codec in cls.state_codecs.items():
            if name not in persisted:
                raise StateError(
                    f"{cls.__name__}.state_codecs: '{name}' is not a persisted "
                    f"field (declare it in display_vars or internal_vars)."
                )
            if not isinstance(codec, StateCodec):
                raise StateError(
                    f"{cls.__name__}.state_codecs['{name}'] is not a StateCodec."
                )

        cls._display_names = tuple(display) + tuple(derived)
        cls._persisted_names = persisted
        cls._state_defaults = defaults

        # This is the whole trick: the stock CtaEngine persists and restores by
        # iterating `strategy.variables`, so both layers go in there.  The GUI
        # is narrowed separately, in get_data().
        cls.variables = list(persisted)

    def __init__(
        self,
        cta_engine: Any,
        strategy_name: str,
        vt_symbol: str,
        setting: dict[str, Any],
    ) -> None:
        self._state_ready: bool = False
        self._state_restored: bool = False
        self._state_seq: int = 0
        self._state_issues: list[str] = []
        self._undeclared_scalars: list[str] = []
        self._state_file_pos: float | None = None

        super().__init__(cta_engine, strategy_name, vt_symbol, setting)

        # Give every instance its own copy of any mutable default, otherwise
        # all instances of the class would share one list/dict.
        for name, default in self._state_defaults.items():
            if _is_mutable_default(default):
                setattr(self, name, copy.deepcopy(default))

    # ------------------------------------------------------------------
    # lifecycle hooks
    # ------------------------------------------------------------------
    def on_ready(self, restored: bool) -> None:
        """Called once per process, after state is restored, before any order.

        ``restored`` is True when values came from disk (a restart) and False
        on a cold start or in a backtest.

        Note that ``self.trading`` is still False when this runs from the live
        init path, so orders cannot be sent from here.  Re-arm protective
        orders from the first ``on_tick`` / ``on_bar`` after start instead --
        set a flag here, act on it there.
        """
        return

    def on_reset(self, reason: str) -> None:
        """Called after the state layer has been reset to its declared defaults."""
        return

    # ------------------------------------------------------------------
    # engine-facing overrides
    # ------------------------------------------------------------------
    def get_variables(self) -> dict[str, Any]:
        """Persistence view -- everything, sanitised.  Never raises.

        CtaEngine.sync_strategy_data() calls this and then hands the result to
        `save_json`, on the event thread.  An exception here would kill that
        thread (EventEngine._run only catches queue.Empty), so encode failures
        degrade to the declared default plus a log line.
        """
        data: dict[str, Any] = {}
        issues: list[str] = []

        for name in self.variables:
            value = getattr(self, name)
            if name in _BASE_VARIABLES:
                data[name] = value
                continue
            data[name] = self._encode_field(name, value, issues)

        if issues:
            self._state_issues = issues
            for msg in issues:
                self.write_log(f"[state] {msg}")

        self._write_state_file(data)
        return data

    def get_data(self) -> dict[str, Any]:
        """GUI view -- base status plus the display layer only."""
        self._ensure_ready()

        variables: dict[str, Any] = {name: getattr(self, name) for name in _BASE_VARIABLES}
        for name in self._display_names:
            variables[name] = getattr(self, name)

        return {
            "strategy_name": self.strategy_name,
            "vt_symbol": self.vt_symbol,
            "class_name": self.__class__.__name__,
            "author": self.author,
            "parameters": self.get_parameters(),
            "variables": variables,
        }

    def send_order(
        self,
        direction: Any,
        offset: Any,
        price: float,
        volume: float,
        stop: bool = False,
        lock: bool = False,
        net: bool = False,
    ) -> list[Any]:
        """Last-resort guarantee that on_ready() ran before any order leaves."""
        self._ensure_ready()

        if self.strict_state and self._undeclared_scalars:
            self.write_log(
                f"[state] order refused: undeclared state "
                f"{self._undeclared_scalars} would be lost on restart "
                f"(strict_state=True)"
            )
            return []

        sent: list[Any] = super().send_order(
            direction, offset, price, volume, stop, lock, net
        )
        return sent

    # ------------------------------------------------------------------
    # public state API
    # ------------------------------------------------------------------
    def get_state(self) -> dict[str, Any]:
        """Current value of every persisted field (live objects, not encoded)."""
        return {name: getattr(self, name) for name in self._persisted_names}

    def reset_state(self, reason: str = "manual") -> None:
        """Restore every persisted field to its declared default, then persist.

        ``pos`` is deliberately untouched: it mirrors a real broker position and
        must be reconciled with the broker, not with a file.
        """
        for name, default in self._state_defaults.items():
            setattr(self, name, copy.deepcopy(default))

        self._state_issues = []
        self.write_log(f"[state] reset ({reason})")
        self.on_reset(reason)
        self.save_state()

    def save_state(self) -> None:
        """Force a state flush (sidecar + the engine's own json file)."""
        if self._is_live():
            self._write_state_file(self.get_variables())
        self.sync_data()

    def audit_undeclared_state(self) -> dict[str, list[str]]:
        """Find instance attributes that belong to no declared layer.

        Anything listed under ``"persistable"`` is a scalar/list/dict that will
        silently reset on the next restart -- exactly the bug this module
        exists to prevent.
        """
        declared: set[str] = set(self._persisted_names)
        declared |= set(self.derived_vars)
        declared |= set(self.transient_vars)
        declared |= set(self.parameters)
        declared |= _RESERVED_ATTRS
        declared |= self._KIT_ATTRS

        persistable: list[str] = []
        objects: list[str] = []

        for name, value in vars(self).items():
            if name in declared or name.startswith("__"):
                continue
            if isinstance(value, types.ModuleType) or callable(value):
                objects.append(name)
                continue
            if value is None or isinstance(value, (bool, int, float, str, list, dict, tuple)):
                persistable.append(name)
            else:
                objects.append(name)

        return {"persistable": sorted(persistable), "objects": sorted(objects)}

    def broker_net_position(self) -> float | None:
        """Net broker position for this vt_symbol, or None if unavailable.

        Use it inside ``on_ready`` to reconcile a restored ``pos`` against
        reality.  Returns None in backtests and whenever the gateway has not
        pushed position data yet -- treat None as "unknown", never as "flat".
        """
        if not self._is_live():
            return None
        main_engine = getattr(self.cta_engine, "main_engine", None)
        if main_engine is None:
            return None
        try:
            positions = main_engine.get_all_positions()
        except Exception:  # pragma: no cover - defensive
            return None

        total: float = 0.0
        found = False
        for pos in positions:
            if getattr(pos, "vt_symbol", None) != self.vt_symbol:
                continue
            found = True
            direction = getattr(getattr(pos, "direction", None), "value", "")
            volume = float(getattr(pos, "volume", 0.0))
            total += -volume if direction == "Short" else volume
        return total if found else None

    def on_remove(self) -> None:
        """Delete the sidecar when the engine removes this strategy.

        Without this the sidecar outlives the strategy: the engine drops the
        entry from cta_strategy_setting.json / cta_strategy_data.json but never
        touches ``<STATE_FOLDER>/<strategy_name>.json``.  Create a strategy
        under the same name again -- the obvious thing to do after removing one
        by mistake -- and ``on_init`` restores the old ``pos`` from that file.
        The strategy then believes it holds a position it does not have, and
        the first tick puts out exit orders for it (measured: a restored
        ``pos=500`` produced a 500-share sell stop against a flat account).

        Removal is deliberately unconditional, including when ``pos`` is
        non-zero.  Removing a strategy that still thinks it holds something is
        already a manual decision; leaving the file behind does not protect the
        position, it only arms the ghost.  The non-zero case is logged so the
        operator sees what was discarded.
        """
        if not self._is_live():
            return

        path = self.state_file_path()
        try:
            if not path.exists():
                return
            if self.pos:
                self.write_log(
                    f"[state] removing strategy while pos={self.pos}; "
                    f"discarding sidecar {path.name} -- verify the broker position"
                )
            path.unlink()
            self.write_log(f"[state] sidecar {path.name} deleted")
        except Exception as exc:  # pragma: no cover - disk failure
            self.write_log(
                f"[state] could not delete sidecar {path}: {exc}; "
                f"delete it by hand before reusing this strategy name"
            )

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------
    def _is_live(self) -> bool:
        try:
            return bool(self.get_engine_type() == EngineType.LIVE)
        except Exception:  # pragma: no cover - engine stubs in tests
            return False

    def state_file_path(self) -> Path:
        return get_folder_path(STATE_FOLDER).joinpath(f"{self.strategy_name}.json")

    def _encode_field(self, name: str, value: Any, issues: list[str]) -> Any:
        codec = self.state_codecs.get(name)
        try:
            if codec is not None:
                return _json_sanitize(codec.encode(value), name)
            return _json_sanitize(value, name)
        except Exception as exc:
            default = self._state_defaults.get(name)
            issues.append(
                f"{name}: cannot persist {type(value).__name__} ({exc}); "
                f"storing default {default!r} instead"
            )
            try:
                if codec is not None:
                    return _json_sanitize(codec.encode(default), name)
                return _json_sanitize(default, name)
            except Exception:
                return None

    def _decode_field(self, name: str, raw: Any, issues: list[str]) -> Any:
        default = self._state_defaults.get(name)
        codec = self.state_codecs.get(name)
        try:
            if codec is not None:
                return codec.decode(raw)
            return _coerce_to_default_type(raw, default)
        except Exception as exc:
            issues.append(f"{name}: cannot decode {raw!r} ({exc}); using default")
            return copy.deepcopy(default)

    def _write_state_file(self, encoded: dict[str, Any]) -> None:
        if not self._is_live():
            return  # a backtest must never touch live state
        self._state_seq += 1
        payload = {
            "schema": self.state_version,
            "class_name": self.__class__.__name__,
            "strategy_name": self.strategy_name,
            "vt_symbol": self.vt_symbol,
            "seq": self._state_seq,
            "saved_at": datetime.now().isoformat(timespec="seconds"),
            "pos": encoded.get("pos", self.pos),
            "state": {
                name: encoded[name] for name in self._persisted_names if name in encoded
            },
        }
        try:
            _atomic_write_json(self.state_file_path(), payload)
        except Exception as exc:  # pragma: no cover - disk failure
            self.write_log(f"[state] sidecar write failed: {exc}")

    def _read_state_file(self) -> dict[str, Any] | None:
        path = self.state_file_path()
        try:
            if not path.exists():
                return None
            with open(path, encoding="utf-8") as f:
                payload = json.load(f)
        except Exception as exc:
            self.write_log(f"[state] sidecar unreadable ({exc}); ignoring it")
            return None

        if not isinstance(payload, dict) or not isinstance(payload.get("state"), dict):
            self.write_log("[state] sidecar malformed; ignoring it")
            return None
        if payload.get("class_name") != self.__class__.__name__:
            self.write_log(
                f"[state] sidecar belongs to {payload.get('class_name')!r}, "
                f"not {self.__class__.__name__!r}; ignoring it"
            )
            return None
        if payload.get("vt_symbol") != self.vt_symbol:
            self.write_log(
                f"[state] sidecar is for {payload.get('vt_symbol')!r}, "
                f"not {self.vt_symbol!r}; ignoring it"
            )
            return None
        return payload

    def _apply_state_file(self) -> bool:
        payload = self._read_state_file()
        if payload is None:
            return False

        stored_schema = payload.get("schema")
        if stored_schema != self.state_version:
            if self.pos:
                self.write_log(
                    f"[state] stored schema {stored_schema} != {self.state_version} "
                    f"but pos={self.pos} is open -- keeping the stored values. "
                    f"Reconcile manually, then call reset_state()."
                )
            else:
                self.write_log(
                    f"[state] stored schema {stored_schema} != {self.state_version} "
                    f"and flat -- resetting the state layer."
                )
                self.reset_state(f"schema {stored_schema}->{self.state_version}")
                return False

        issues: list[str] = []
        state = payload["state"]
        applied = 0
        for name in self._persisted_names:
            if name not in state:
                continue  # new field in a newer class: keep the default
            setattr(self, name, self._decode_field(name, state[name], issues))
            applied += 1

        self._state_seq = int(payload.get("seq", 0) or 0)
        self._state_file_pos = payload.get("pos")
        for msg in issues:
            self.write_log(f"[state] {msg}")
        self._state_issues = issues

        self._reconcile_pos()
        return applied > 0

    def _reconcile_pos(self) -> None:
        """Cross-check the engine-restored ``pos`` against the sidecar.

        Believing you are flat while the broker holds shares is the dangerous
        direction -- the strategy would take the entry signal again and end up
        double size. So when the engine restored nothing (its json was lost or
        truncated) but the sidecar carries a position, the sidecar wins.

        Any other mismatch keeps the engine's value, because that is the one the
        GUI shows, and logs loudly. Either way the truth is the broker: use
        ``broker_net_position()`` inside ``on_ready``.
        """
        stored = self._state_file_pos
        if stored is None:
            return
        stored = float(stored)
        current = float(self.pos)
        if stored == current:
            return

        if current == 0.0:
            self.pos = stored
            self.write_log(
                f"[state] engine restored pos=0 but the sidecar holds {stored}. "
                f"Adopting {stored} (assuming flat while holding would re-enter "
                f"at double size). Verify against the broker before trading."
            )
            return

        self.write_log(
            f"[state] pos mismatch: engine restored {current}, sidecar has "
            f"{stored}. Keeping {current} -- reconcile against the broker "
            f"(see broker_net_position())."
        )

    def _ensure_ready(self) -> None:
        """Run the post-restore sequence exactly once per process."""
        if self._state_ready:
            return
        if not self.inited and self._is_live():
            # CtaEngine restores `variables` after on_init and before setting
            # inited=True, so anything earlier than that would see stale values.
            return

        self._state_ready = True

        restored = False
        try:
            restored = self._apply_state_file()
        except Exception as exc:  # pragma: no cover - defensive
            self.write_log(f"[state] restore failed ({exc}); using engine values")

        if not restored:
            # The engine may still have restored `variables` from its own json.
            restored = any(
                getattr(self, name) != self._state_defaults[name]
                for name in self._persisted_names
            ) or bool(self.pos)

        audit = self.audit_undeclared_state()
        self._undeclared_scalars = audit["persistable"]
        if audit["persistable"]:
            self.write_log(
                f"[state] undeclared attributes {audit['persistable']} are not "
                f"in display_vars/internal_vars/transient_vars -- they WILL "
                f"reset on the next restart."
            )

        self._state_restored = restored
        self.on_ready(restored)
