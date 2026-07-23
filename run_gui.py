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

from vnpy.event import EventEngine
from vnpy.trader.engine import MainEngine

from fluent_ui import FluentMainWindow, create_fluent_qapp
from vnpy_chartwizard import ChartWizardApp
from vnpy_ctabacktester import CtaBacktesterApp
from vnpy_ctastrategy import CtaStrategyApp
from vnpy_datamanager import DataManagerApp
from vnpy_futu import FutuGateway
from vnpy_paperaccount import PaperAccountApp
from vnpy_riskmanager import RiskManagerApp


def main() -> None:
    qapp = create_fluent_qapp()

    event_engine = EventEngine()
    main_engine = MainEngine(event_engine)

    main_engine.add_gateway(FutuGateway)

    main_engine.add_app(RiskManagerApp)
    main_engine.add_app(CtaStrategyApp)
    main_engine.add_app(CtaBacktesterApp)
    main_engine.add_app(DataManagerApp)
    main_engine.add_app(ChartWizardApp)
    main_engine.add_app(PaperAccountApp)

    main_window = FluentMainWindow(main_engine, event_engine)
    main_window.showMaximized()

    qapp.exec()


if __name__ == "__main__":
    main()
