"""Runnable entry point for the AlphaLiveEngine signal → order path.

Why this file exists
--------------------
``AlphaLiveEngine`` was a library with no caller.  Outside its own tests it
was never instantiated: ``run.py`` and ``run_gui.py`` neither add the engine,
load a signal, nor call ``run_rebalance``.  So "live signal execution
(dry-run by default)" was a set of parts, not something a process could do —
there was nowhere to load the signal parquet, nothing to trigger a rebalance,
and no way to reach ``enable_live_trading``.  This is that process.

Safety posture (all of it deliberate)
-------------------------------------
* **Dry run is the default and needs no flag.**  Going live requires
  ``--live``, which routes through ``enable_live_trading()`` and its
  fail-closed pre-flight (trade gateway registered, capital known, stop
  policy present, all three gate rules loaded *and* active).
* **The gate rules are installed here**, on ``MainEngine.send_order``, so
  they cover every order path in the process rather than just this engine's.
* **The position snapshot is not guessed at.**  The engine refuses to
  overwrite a non-empty local book from an empty broker snapshot; this runner
  waits for the broker to answer ``query_position`` and only declares the
  book flat via ``--assume-flat`` if the operator says so explicitly.
* **The universe is subscribed or the run stops.**  ``connect()`` returns
  before the gateway's contract query answers, so this waits for the
  contracts before subscribing and exits with ``EXIT_NO_CONTRACTS`` if they
  never come.  An unsubscribed universe produces no ticks, hence empty bar
  slices, hence no orders — a no-op that otherwise looks like a healthy run.
* **Stale quotes do not become prices** — ``quote_max_age_seconds`` is set,
  and a rebalance with no fresh quote fails closed in live mode.
* **The duplicate window persists to disk**, so a cron rerun of this script
  cannot re-send the same order.

Usage::

    # look at what it would do, touching nothing
    python run_live_alpha.py --lab ~/alphalab --basket demo --symbols 700.SEHK

    # same, but orders really leave the process
    python run_live_alpha.py --lab ~/alphalab --basket demo \\
        --symbols 700.SEHK --live --capital 1000000
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

# Must precede any `import vnpy...` — see run_gui.py for the reasoning.
os.environ.setdefault("LANGUAGE", "zh_CN")

from vnpy.alpha.lab import AlphaLab
from vnpy.alpha.strategy.strategies.equity_demo_strategy import EquityDemoStrategy
from vnpy.event import Event, EventEngine
from vnpy.trader.engine import MainEngine
from vnpy.trader.event import EVENT_LOG
from vnpy_alphakit.live import AlphaLiveEngine, AlphaLiveEngineError
from vnpy_alphakit.rules import install_gate_rules
from vnpy_futu import FutuGateway
from vnpy_riskmanager import RiskManagerApp

DEFAULT_DUPLICATE_STORE = Path.home() / ".vntrader" / "alpha_live_duplicates.csv"

#: Declared stops of open positions, mirrored so a restart does not leave them
#: unwatched. Defaulted on rather than opt-in: the failure it prevents is
#: "already in a position, then restart", which is ordinary operations.
DEFAULT_STOP_STORE = Path.home() / ".vntrader" / "alpha_live_stops.json"

#: The broker never described the universe, so nothing could be subscribed.
EXIT_NO_CONTRACTS = 5


def _on_log(event: Event) -> None:
    log = event.data
    print(f"[{log.gateway_name or 'MAIN'}] {log.msg}", flush=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run an AlphaStrategy against live market data (dry run by default).",
    )
    parser.add_argument("--lab", required=True, help="AlphaLab directory")
    parser.add_argument("--basket", required=True, help="Signal/basket name inside the lab")
    parser.add_argument(
        "--symbols", required=True,
        help="Comma-separated vt_symbols, e.g. 700.SEHK,9988.SEHK",
    )
    parser.add_argument(
        "--live", action="store_true",
        help="Actually send orders. Without this nothing leaves the process.",
    )
    parser.add_argument("--capital", type=float, default=0.0, help="0 = use account balance")
    parser.add_argument(
        "--stop-loss-pct", type=float, default=0.08,
        help="Stop distance for entries when the strategy supplies none",
    )
    parser.add_argument("--max-risk-pct", type=float, default=0.02)
    parser.add_argument(
        "--quote-max-age", type=float, default=300.0,
        help="Seconds before a cached tick stops counting as a price",
    )
    parser.add_argument(
        "--interval", type=float, default=0.0,
        help="Seconds between rebalances. 0 = run once and exit.",
    )
    parser.add_argument(
        "--assume-flat", action="store_true",
        help=(
            "Declare the account genuinely flat when the broker reports no "
            "positions. Only pass this having checked: without it an empty "
            "snapshot is treated as 'the broker has not answered yet' and the "
            "rebalance refuses rather than re-buying the basket."
        ),
    )
    parser.add_argument(
        "--position-wait", type=float, default=10.0,
        help="Seconds to wait for the broker's first position push",
    )
    parser.add_argument(
        "--contract-wait", type=float, default=60.0,
        help=(
            "Seconds to wait for the gateway's contract query before giving "
            "up. Subscriptions cannot be placed until the contracts land, and "
            "an unsubscribed universe produces no ticks and therefore no "
            "orders — so this timeout expiring is a hard stop, not a warning."
        ),
    )
    parser.add_argument(
        "--duplicate-store", default=str(DEFAULT_DUPLICATE_STORE),
        help="File the duplicate-order window persists to across processes",
    )
    parser.add_argument(
        "--stop-store", default=str(DEFAULT_STOP_STORE),
        help=(
            "File the declared stops of open positions are mirrored to, so a "
            "restart keeps watching them instead of starting blind"
        ),
    )
    parser.add_argument("--futu-host", default=os.environ.get("FUTU_OPEND_HOST", "127.0.0.1"))
    parser.add_argument(
        "--futu-port", type=int, default=int(os.environ.get("FUTU_OPEND_PORT", "11111"))
    )
    return parser


def build_main_engine() -> MainEngine:
    """MainEngine with the risk gate mounted before any gateway connects."""
    event_engine = EventEngine()
    event_engine.register(EVENT_LOG, _on_log)

    main_engine = MainEngine(event_engine)
    main_engine.add_app(RiskManagerApp)

    installed = install_gate_rules(main_engine)
    if installed:
        main_engine.write_log(f"已加载风控闸: {', '.join(installed)}", "system")

    main_engine.add_gateway(FutuGateway)
    return main_engine


def wait_for_contracts(
    main_engine: MainEngine, vt_symbols: list[str], timeout: float
) -> list[str]:
    """Block until every symbol has a contract, and report those that never do.

    ``MainEngine.connect`` is not a handshake — ``FutuGateway.connect`` starts
    a daemon thread and returns, and the contract query is several seconds of
    OpenD round-trips behind it.  Subscribing on the next line therefore asks
    the OMS for contracts it has not received, which the live engine turns
    into "跳过订阅" for the entire universe.  Nothing is subscribed, no tick
    ever arrives, and every rebalance prices an empty slice: a process that
    connects, mounts the risk gate, prints a clean reconciliation line and
    never sends an order.

    Polling ``get_contract`` rather than listening for ``EVENT_CONTRACT``
    because that is the state the subscription actually depends on: an event
    can be missed if it fires before the handler is registered, whereas the
    OMS dict is the thing ``init_strategy`` will read.
    """
    deadline = time.monotonic() + timeout
    missing = [s for s in vt_symbols if main_engine.get_contract(s) is None]
    while missing and time.monotonic() < deadline:
        time.sleep(0.2)
        missing = [s for s in vt_symbols if main_engine.get_contract(s) is None]
    return missing


def wait_for_positions(engine: AlphaLiveEngine, timeout: float) -> bool:
    """Block until the broker has described its book, or the timeout expires."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if engine.positions_are_trustworthy():
            return True
        time.sleep(0.2)
    return engine.positions_are_trustworthy()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    vt_symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    if not vt_symbols:
        print("--symbols 至少要有一个 vt_symbol", file=sys.stderr)
        return 2

    lab = AlphaLab(args.lab)
    signal = lab.load_signal(args.basket)
    if signal is None or signal.is_empty():
        print(
            f"AlphaLab {args.lab!r} 里没有名为 {args.basket!r} 的信号 —— "
            f"先跑研究流程生成信号再上实盘",
            file=sys.stderr,
        )
        return 2

    main_engine = build_main_engine()
    try:
        main_engine.connect(
            {
                "host": args.futu_host,
                "port": args.futu_port,
                "unlock_password_md5": os.environ.get("FUTU_UNLOCK_PASSWORD_MD5", ""),
                "trd_env": os.environ.get("FUTU_TRD_ENV", "SIMULATE"),
            },
            "FUTU",
        )

        # Before anything subscribes. connect() above only started the
        # gateway's connect thread; the contract list it needs is still in
        # flight, and subscribing without it silently yields an empty universe.
        missing = wait_for_contracts(main_engine, vt_symbols, args.contract_wait)
        if missing:
            print(
                f"等待 {args.contract_wait:g}s 后仍无以下标的的合约信息: "
                f"{', '.join(missing)} —— 未订阅行情则每轮调仓都拿到空行情、"
                f"永不下单。请检查 OpenD 是否已启动、账户是否有该市场行情权限、"
                f"标的代码是否正确, 或加大 --contract-wait 后重跑。",
                file=sys.stderr,
            )
            return EXIT_NO_CONTRACTS

        engine: AlphaLiveEngine = main_engine.add_engine(AlphaLiveEngine)
        engine.set_parameters(
            vt_symbols=vt_symbols,
            lab=lab,
            capital=args.capital,
            stop_loss_pct=args.stop_loss_pct,
            max_risk_pct=args.max_risk_pct,
            trade_gateway="FUTU",
            quote_max_age_seconds=args.quote_max_age,
            duplicate_store=args.duplicate_store,
            stop_store=args.stop_store,
        )
        engine.add_strategy(EquityDemoStrategy, {}, signal)
        # Belt and braces: wait_for_contracts already proved the OMS has them,
        # so this can only fire if one disappeared in between — in which case
        # stopping beats running a half-subscribed universe.
        try:
            engine.init_strategy(require_contracts=True)
        except AlphaLiveEngineError as exc:
            print(f"订阅行情失败: {exc}", file=sys.stderr)
            return EXIT_NO_CONTRACTS

        if not wait_for_positions(engine, args.position_wait):
            if args.assume_flat:
                engine.confirm_flat_book()
                main_engine.write_log(
                    "券商未推送持仓，按 --assume-flat 视为空仓", "system"
                )
            else:
                print(
                    "券商在超时内未推送任何持仓 —— 无法区分'空仓'与'尚未答复'。"
                    "确认账户确为空仓后加 --assume-flat 重跑。",
                    file=sys.stderr,
                )
                return 3

        if args.live:
            try:
                engine.enable_live_trading()
            except AlphaLiveEngineError as exc:
                print(f"实盘前置检查未通过: {exc}", file=sys.stderr)
                return 4

        return run_loop(engine, args.interval)
    finally:
        main_engine.close()


def run_loop(engine: AlphaLiveEngine, interval: float) -> int:
    """One rebalance, or one every ``interval`` seconds until interrupted."""
    while True:
        try:
            report = engine.run_rebalance()
            print(report.describe(), flush=True)
            for breach in engine.scan_stop_breaches():
                print(breach.describe(), flush=True)
        except AlphaLiveEngineError as exc:
            # Fail-closed conditions (untrusted position snapshot, no fresh
            # quote) are expected operational states, not crashes: report and
            # keep the loop alive so the next cycle can recover.
            print(f"本轮跳过: {exc}", flush=True)

        if interval <= 0:
            print(engine.reconcile_eod().describe(), flush=True)
            return 0
        try:
            time.sleep(interval)
        except KeyboardInterrupt:
            print(engine.reconcile_eod().describe(), flush=True)
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
