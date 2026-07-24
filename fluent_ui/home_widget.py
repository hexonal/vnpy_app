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

        # Left column: trading panel (fixed-ish width) + connect button.
        # Wrapped in a plain widget so it can be one pane of the splitter.
        left_widget = QtWidgets.QWidget()
        vbox2 = QtWidgets.QVBoxLayout(left_widget)
        vbox2.setContentsMargins(0, 0, 0, 0)
        vbox2.addWidget(self.trading_widget)
        vbox2.addWidget(self.menu_button)
        vbox2.addStretch()

        # Right column: the three monitor sections stacked in a VERTICAL
        # splitter so they share the full height adaptively and the user
        # can drag the boundaries. Stretch factors give tick/orders/logs
        # 2:3:3 of the height by default (logs+trades want the most room),
        # but every pane stays resizable. Previously these were bare
        # addWidget calls in a QVBoxLayout with no stretch — Qt then sized
        # each to its sizeHint and left everything clustered top-left with
        # the rest of the window empty (the reported layout bug).
        right_splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical)
        right_splitter.addWidget(self.tick_monitor)
        right_splitter.addWidget(mid_pivot)
        right_splitter.addWidget(bottom_pivot)
        right_splitter.setStretchFactor(0, 2)
        right_splitter.setStretchFactor(1, 3)
        right_splitter.setStretchFactor(2, 3)
        # Without this, childrenCollapsible defaults True and a drag can
        # crush a monitor to 0 height regardless of its setMinimumHeight
        # (Qt only honors the min size when collapsing is disabled) — the
        # monitor's own 80px floor comment relies on this being set on the
        # splitter that actually holds it, not just the outer one.
        right_splitter.setChildrenCollapsible(False)

        # Top-level horizontal splitter: trading panel | monitors. The
        # monitor side takes all extra width (stretch 1 vs 0), and the
        # boundary is draggable rather than a hardcoded proportion.
        main_splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
        main_splitter.addWidget(left_widget)
        main_splitter.addWidget(right_splitter)
        main_splitter.setStretchFactor(0, 0)
        main_splitter.setStretchFactor(1, 1)
        main_splitter.setChildrenCollapsible(False)

        hbox = QtWidgets.QHBoxLayout(self)
        hbox.setContentsMargins(0, 0, 0, 0)
        hbox.addWidget(main_splitter)

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
