"""
Starts the VeighNa desktop trading terminal (Qt GUI) with:
  - FutuGateway connected for HK/US/CN market data + contract lists
    (read-only: quote context + contract query for all four markets, trade
    context only for HK/US, and never unlocked — no order can be sent from
    here. The user trades through separate software; this is for watching
    the market inside VeighNa, not for placing orders.)
  - The framework's own official apps, same as vnpy's stock example
    (examples/veighna_trader/run.py): CTA strategy engine + backtester,
    data manager, chart wizard, paper trading account, and the risk
    manager (mechanical order-gate, harmless here since nothing unlocks
    trading anyway).

Environment note: this fork pins PySide6==6.8.2.1 in its own pyproject,
but this machine only has Python 3.14.6 available, and 6.8.2.1 has no
cp314 wheels (PySide6 only gained 3.14 support at 6.10+). Running on
PySide6 6.11.1 instead (fork's pyproject.toml updated to match) —
untested by the upstream project against this exact Qt binding version,
flagged here rather than glossed over.
"""

from __future__ import annotations

import os

# vnpy's source strings ARE Chinese (see vnpy/trader/locale/__init__.py —
# gettext.translation("vnpy", ...) falls back to a NullTranslations/identity
# translator when no catalog matches, so Chinese passes through as-is; only
# an "en" catalog exists under locale/en/ to translate it TO English). This
# machine's system locale is LANG=en_US.UTF-8, which gettext picks up ahead
# of vnpy's own default and silently loads that English catalog. Must be
# set before the first `import vnpy...` — gettext.translation() runs at
# vnpy.trader.locale's import time.
os.environ.setdefault("LANGUAGE", "zh_CN")

# Silence Qt's benign one-time font-substitution notices from the
# qt.qpa.fonts category ("missing font family …"). Two families get
# requested that don't exist on macOS: vnpy's own 微软雅黑 (already handled by
# pointing SETTINGS["font.family"] at PingFang SC in mainwindow.
# create_fluent_qapp) and qfluentwidgets' internal "Segoe UI" — the latter is
# requested via a QFont families() FALLBACK list that already includes
# PingFang SC, so text renders correctly and only the log line is noise.
# Scoped to the fonts category alone; every other Qt warning/error still
# surfaces. Must be set before the first Qt import.
os.environ.setdefault("QT_LOGGING_RULES", "qt.qpa.fonts=false")

from fluent_ui import FluentMainWindow, create_fluent_qapp
from fluent_ui.backtester_gates import install_gate_verdict
from fluent_ui.backtester_metrics import install_extra_metrics
from fluent_ui.backtester_segments import install_segment_notice
from fluent_ui.gateway_config import load_all_configs
from vnpy.event import EventEngine
from vnpy.trader.engine import MainEngine
from vnpy.trader.utility import get_file_path
from vnpy_alphakit.rules import install_gate_rules
from vnpy_chartwizard import ChartWizardApp
from vnpy_ctabacktester import CtaBacktesterApp
from vnpy_datamanager import DataManagerApp
from vnpy_paperaccount import PaperAccountApp
from vnpy_riskmanager import RiskManagerApp

from vnpy_ctastrategy import CtaStrategyApp
from vnpy_futu import FutuGateway
from vnpy_usmart import UsmartGateway


def main() -> None:
    qapp = create_fluent_qapp()

    event_engine = EventEngine()
    main_engine = MainEngine(event_engine)

    main_engine.add_gateway(FutuGateway)
    # uSMART quote gateway (read-only, like FutuGateway here). Registering
    # it only adds a "连接USMART" entry to the home widget's gateway menu
    # (home_widget iterates get_all_gateway_names(); ConnectDialog builds
    # the form from default_setting) — nothing connects until credentials
    # from uSMART's open-api application are entered. See
    # vnpy_usmart/README.md for the token/RSA-key setup and the four
    # documented spec assumptions pending first live calibration.
    main_engine.add_gateway(UsmartGateway)

    # Optional split quote/trade routing (vnpy_router). Activates ONLY if the
    # user has created ~/.vntrader/routing_setting.json
    # ({"quote_gateway": "FUTU", "trade_gateway": "<broker>"}). Without that
    # file this is a no-op and the terminal behaves exactly as before (single
    # gateway, contract owner = whoever pushes last). With it, RouterEngine
    # forces market data to the quote gateway and orders to the trade gateway,
    # and fixes the OMS contract-table collision (FUTU size=100 vs a trade
    # gateway's size=1 for the same HK symbol). Must be added AFTER the
    # gateways and BEFORE the apps whose send_order patches wrap it. See
    # vnpy_router and docs/plans/2026-07-23-split-quote-trade-routing-design.
    router = None
    if get_file_path("routing_setting.json").exists():
        from vnpy_router import RouterEngine

        router = main_engine.add_engine(RouterEngine)

    # PAPER (default) simulates fills locally via PaperAccountApp and never
    # reaches a real gateway. LIVE routes orders to the trade gateway for real
    # — so PaperAccountApp MUST NOT load (its send_order hijack would silently
    # swallow every real order; verify_patch_chain("LIVE") enforces this by
    # raising if it finds PaperAccountApp loaded). Read the profile here so the
    # app-load block below can honour it. LIVE only takes effect together with
    # routing_setting.json; without routing there is no trade gateway to route
    # to, so PaperAccountApp always loads (single-gateway paper trading).
    profile = os.environ.get("VNPY_ROUTING_PROFILE", "PAPER").upper()
    load_paper = router is None or profile != "LIVE"

    # Load order matters here — both RiskManagerApp and PaperAccountApp
    # monkey-patch main_engine.send_order (see vnpy_riskmanager.engine.
    # RiskEngine.patch_functions() and vnpy_paperaccount.engine.PaperEngine
    # .__init__). RiskEngine *wraps* whatever send_order was at patch time
    # (captures it as self._send_order, calls it only after its own checks
    # pass); PaperEngine does NOT chain — it unconditionally replaces
    # send_order with its own simulated-fill version and never forwards to
    # whatever was patched in before it (correct: paper trading must never
    # reach a real gateway).
    #
    # PaperAccountApp must load FIRST so RiskManagerApp's patch — applied
    # second — wraps PaperEngine's send_order. That makes the chain
    # main_engine.send_order -> RiskEngine.send_order (checks rules) ->
    # PaperEngine.send_order (simulated fill), so a rule that fails blocks
    # a paper order the same way it would block a real one. Loading them
    # in the reverse order (as this file did previously) makes
    # PaperAccountApp's patch win and RiskManagerApp's rules never run —
    # confirmed by reading both patch_functions()/__init__() rather than
    # assumed.
    if load_paper:
        main_engine.add_app(PaperAccountApp)
    main_engine.add_app(RiskManagerApp)

    # vnpy_alphakit's three live-path gate rules (强制止损 / 单笔风险上限 /
    # 重复委托时间窗) join RiskManagerApp's five built-ins, so they sit on
    # main_engine.send_order and cover every order path in this process —
    # AlphaLiveEngine, CtaEngine, and manual orders typed into the GUI alike.
    #
    # Registered explicitly rather than through RiskEngine's own
    # Path.cwd()/rules folder scan, because that scan cannot reach this
    # checkout: MainEngine.__init__ runs os.chdir(TRADER_DIR) before any app
    # is added, so by the time RiskEngine is constructed the working
    # directory is the vnpy home dir (measured: cwd becomes /Users/flink, and
    # the scan target is /Users/flink/rules), not vnpy_app. Explicit
    # registration also keeps this wiring version-controlled and greppable.
    #
    # What the built-ins do NOT cover, and these do: a stop on every
    # exposure-increasing order; a cap on |entry-stop| x qty x size rather
    # than on raw notional; and a short-window duplicate guard (vnpy's own
    # DuplicateOrderRule is a cumulative session counter that lets a retry
    # seconds later straight through).
    installed_rules = install_gate_rules(main_engine)
    if installed_rules:
        main_engine.write_log(f"已加载风控闸: {', '.join(installed_rules)}", "system")

    main_engine.add_app(CtaStrategyApp)
    main_engine.add_app(CtaBacktesterApp)
    main_engine.add_app(DataManagerApp)
    main_engine.add_app(ChartWizardApp)

    # The backtester panel hardcodes which statistics keys it renders, so
    # anything added to the statistics dict later is computed but invisible —
    # including upstream's own rgr_ratio and our fork's RAR / R-Cubed /
    # Robust Sharpe. Register their labels here rather than forking the
    # backtester. Deliberately display-only: see the module docstring for why
    # RAR must not become an optimization target.
    added_metrics = install_extra_metrics()
    if added_metrics:
        main_engine.write_log(f"回测统计面板已补充指标: {', '.join(added_metrics)}")

    # 参数寻优的 DSR / PBO 闸已经在 vnpy_ctastrategy 里算好并挂在返回值的 .gates
    # 上，但回测器界面只渲染"参数 / 目标值"两列 —— 用户照着排第一的那行用，而闸的
    # 结论（"这是从 N 组里挑出来的，扣掉选择偏差后不显著"）根本没上屏。这里把裁决
    # 接到寻优结束的日志块和优化结果对话框顶部。
    #
    # 必须在建主窗口之前装：BacktesterManager.__init__ 里 register_event() 会把
    # process_optimization_finished_event 绑到信号上，绑定发生在连接那一刻，
    # 之后再打补丁对已建好的实例无效。
    installed_gates = install_gate_verdict()
    if installed_gates:
        main_engine.write_log(f"寻优多重比较闸已接入回测器界面: {len(installed_gates)} 处")

    # Three-way in/out-of-sample backtesting (TRAIN/VALID/TEST) deliberately
    # does NOT get a button here — the reasoning is in backtester_segments.py's
    # module docstring, and its entry point is
    # `python -m vnpy_ctastrategy.segment_cli`. What this call installs is the
    # part the GUI *can* honestly do: print that command in the parameter form
    # (with the current split and remaining TEST-peek budget when the ledger
    # exists), and refuse to run [参数优化] over a window that overlaps the TEST
    # segment — that button is a parameter scan, and scanning the test segment
    # is exactly what turns it into in-sample data. Must run before the main
    # window is built, or the already-constructed panel keeps the unwrapped
    # methods.
    installed_segment_hooks = install_segment_notice()
    if installed_segment_hooks:
        main_engine.write_log(
            f"回测面板已接入三段闸: {', '.join(installed_segment_hooks)}"
        )

    # Router startup audit — PAPER allows PaperAccountApp (loaded above); LIVE
    # skipped it (load_paper=False), so this passes and orders reach the trade
    # gateway. The check is the last line of defence: if some future edit loads
    # PaperAccountApp under LIVE anyway, this raises before any gateway connects
    # rather than silently swallowing real orders.
    if router is not None:
        router.verify_patch_chain(profile)

    main_window = FluentMainWindow(main_engine, event_engine)
    # Must show() before showMaximized(): FluentWindow (built on a frameless-
    # window implementation) computes its NavigationInterface/stacked-widget
    # child layout against the widget's *actual displayed* geometry — calling
    # showMaximized() directly on a widget that has never been shown skips
    # the layout pass that breakpoint-dependent Fluent widgets rely on, so
    # everything renders at some stale pre-layout (narrow/collapsed) size
    # until a real user resize event forces Qt to recompute it. show() first
    # forces that initial layout pass while still small, then showMaximized()
    # correctly recomputes it again for the maximized size.
    main_window.show()
    main_window.showMaximized()

    # Auto-connect every gateway the user flagged auto_connect in its
    # ConnectDialog (persisted to QuestDB via gateway_config). Done after
    # show() so any connect-time logs land in the already-visible log
    # monitor. connect() is non-blocking per NonBlockingConnectMixin, so
    # this doesn't stall the UI even for several gateways.
    for cfg in load_all_configs():
        if cfg.auto_connect and cfg.setting:
            main_engine.write_log(f"启动自动连接: {cfg.gateway_name}", "system")
            # Same quote_only derivation as ConnectDialog: a gateway the user
            # flagged quote-only (no is_trade) connects without a trade
            # context so startup doesn't raise trade-authority errors.
            connect_setting = {**cfg.setting, "quote_only": not cfg.is_trade}
            main_engine.connect(connect_setting, cfg.gateway_name)

    qapp.exec()


if __name__ == "__main__":
    main()
