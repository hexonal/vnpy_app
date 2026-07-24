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

from vnpy.event import EventEngine
from vnpy.trader.engine import MainEngine

from fluent_ui import FluentMainWindow, create_fluent_qapp
from fluent_ui.gateway_config import load_all_configs
from vnpy_chartwizard import ChartWizardApp
from vnpy_ctabacktester import CtaBacktesterApp
from vnpy_ctastrategy import CtaStrategyApp
from vnpy_datamanager import DataManagerApp
from vnpy_futu import FutuGateway
from vnpy_paperaccount import PaperAccountApp
from vnpy_riskmanager import RiskManagerApp
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
    main_engine.add_app(PaperAccountApp)
    main_engine.add_app(RiskManagerApp)
    main_engine.add_app(CtaStrategyApp)
    main_engine.add_app(CtaBacktesterApp)
    main_engine.add_app(DataManagerApp)
    main_engine.add_app(ChartWizardApp)

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
