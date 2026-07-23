"""
The single dashboard page vnpy_evo actually ships as its FluentWindow's
"Home" — this, not the nav-shell swap, is most of what makes it look like
a cohesive trading terminal instead of a pile of separate full-page
tables. Ticks + trading panel on top, active/history orders in one Pivot-
tabbed section, log/trades/positions/assets in another. Structure copied
from vnpy_evo's HomeWidget; the monitors/trading widget it wires together
are this package's Fluent-native ones (see monitor.py, trading_widget.py).
"""

from __future__ import annotations

from functools import partial
from typing import Callable

from qfluentwidgets import PushButton, RoundMenu
from qfluentwidgets import Action as FluentAction

from vnpy.trader.engine import EventEngine, MainEngine
from vnpy.trader.locale import _
from vnpy.trader.ui import QtCore, QtWidgets

from .connect_dialog import ConnectDialog
from .monitor import (
    AccountMonitor,
    ActiveOrderMonitor,
    LogMonitor,
    OrderMonitor,
    PositionMonitor,
    TickMonitor,
    TradeMonitor,
)
from .pivot_widget import PivotWidgdet
from .trading_widget import TradingWidget


class HomeWidget(QtWidgets.QWidget):
    def __init__(self, main_engine: MainEngine, event_engine: EventEngine) -> None:
        super().__init__()

        self.main_engine = main_engine
        self.event_engine = event_engine

        self.init_ui()
        self.init_menu()

    def init_ui(self) -> None:
        self.trading_widget = TradingWidget(self.main_engine, self.event_engine)
        self.tick_monitor = TickMonitor(self.main_engine, self.event_engine)
        self.order_monitor = OrderMonitor(self.main_engine, self.event_engine)
        self.active_monitor = ActiveOrderMonitor(self.main_engine, self.event_engine)
        self.trade_monitor = TradeMonitor(self.main_engine, self.event_engine)
        self.position_monitor = PositionMonitor(self.main_engine, self.event_engine)
        self.account_monitor = AccountMonitor(self.main_engine, self.event_engine)
        self.log_monitor = LogMonitor(self.main_engine, self.event_engine)

        self.menu = RoundMenu(parent=self)

        self.menu_button = PushButton(_("连接网关"))
        self.menu_button.clicked.connect(self.show_menu)

        mid_pivot = PivotWidgdet(self)
        mid_pivot.add_widget(self.active_monitor, _("活动委托"))
        mid_pivot.add_widget(self.order_monitor, _("全部委托"))

        bottom_pivot = PivotWidgdet(self)
        bottom_pivot.add_widget(self.log_monitor, _("日志"))
        bottom_pivot.add_widget(self.trade_monitor, _("成交"))
        bottom_pivot.add_widget(self.position_monitor, _("持仓"))
        bottom_pivot.add_widget(self.account_monitor, _("资金"))

        vbox1 = QtWidgets.QVBoxLayout()
        vbox1.addWidget(self.tick_monitor)
        vbox1.addWidget(mid_pivot)
        vbox1.addWidget(bottom_pivot)

        vbox2 = QtWidgets.QVBoxLayout()
        vbox2.addWidget(self.trading_widget)
        vbox2.addWidget(self.menu_button)
        vbox2.addStretch()

        hbox = QtWidgets.QHBoxLayout()
        hbox.addLayout(vbox2)
        hbox.addLayout(vbox1)
        self.setLayout(hbox)

        self.tick_monitor.itemDoubleClicked.connect(self.trading_widget.update_with_cell)
        self.position_monitor.itemDoubleClicked.connect(self.trading_widget.update_with_cell)

    def init_menu(self) -> None:
        for name in self.main_engine.get_all_gateway_names():
            func: Callable = partial(self.connect_gateway, name)

            action = FluentAction(_("连接{}").format(name))
            action.triggered.connect(func)

            self.menu.addAction(action)

    def show_menu(self) -> None:
        pos = self.menu_button.mapToGlobal(QtCore.QPoint(self.menu_button.width() + 5, 0))
        self.menu.exec(pos, ani=True)

    def connect_gateway(self, gateway_name: str) -> None:
        dialog = ConnectDialog(self.main_engine, gateway_name, self)
        dialog.exec()

    def get_monitors(self) -> list:
        """
        For FluentMainWindow.closeEvent() to persist column widths — stock
        mainwindow.py calls monitor.save_setting() on every dock monitor at
        shutdown (BaseMonitor.load_setting() already restores them on
        __init__, but that only round-trips if something actually saved
        first). These monitors live inside HomeWidget rather than being
        tracked directly by the main window, hence this getter.
        """
        return [
            self.tick_monitor,
            self.order_monitor,
            self.active_monitor,
            self.trade_monitor,
            self.position_monitor,
            self.account_monitor,
            self.log_monitor,
        ]
