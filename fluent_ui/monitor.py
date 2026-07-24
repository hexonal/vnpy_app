"""
Fluent-native monitor widgets — same logic as vnpy.trader.ui.widget's
BaseMonitor/TickMonitor/OrderMonitor/etc. (event registration, row
insert/update, CSV export, right-click menu), copied over with exactly one
substantive change: the base table class is qfluentwidgets.TableWidget
instead of QtWidgets.QTableWidget. That swap is the actual fix for "doesn't
look like vnpy_evo" — the previous pass only changed the window shell
(FluentWindow nav) while every table/button inside stayed a plain,
now-unstyled Qt widget once qdarkstyle was removed.
"""

from __future__ import annotations

import csv
from typing import Any

from qfluentwidgets import TableWidget

from vnpy.event import Event
from vnpy.trader.engine import EventEngine, MainEngine
from vnpy.trader.event import (
    EVENT_ACCOUNT,
    EVENT_LOG,
    EVENT_ORDER,
    EVENT_POSITION,
    EVENT_TICK,
    EVENT_TRADE,
)
from vnpy.trader.locale import _
from vnpy.trader.object import CancelRequest, OrderData
from vnpy.trader.ui import QtCore, QtGui, QtWidgets

from .cells import AskCell, BaseCell, BidCell, DirectionCell, EnumCell, MsgCell, PnlCell, TimeCell


class BaseMonitor(TableWidget):
    event_type: str = ""
    data_key: str = ""
    sorting: bool = False
    headers: dict = {}

    # Row cap for insert-only monitors (data_key == "", i.e. LogMonitor/
    # TradeMonitor): stock vnpy grows these one row per event for the life
    # of the process — a long trading session leaks rows (and their
    # QTableWidgetItems) without bound. Keyed monitors are NOT capped:
    # their row count is bounded by real entity count (ticks by
    # subscription, orders by vt_orderid), every row is live data, and
    # removing keyed rows would desync self.cells' row references.
    max_rows: int = 5000

    signal: QtCore.Signal = QtCore.Signal(Event)

    def __init__(self, main_engine: MainEngine, event_engine: EventEngine) -> None:
        super().__init__()

        self.main_engine = main_engine
        self.event_engine = event_engine
        self.cells: dict[str, dict] = {}

        self.init_ui()
        self.load_setting()
        self.register_event()

    def init_ui(self) -> None:
        self.init_table()
        self.init_menu()

    def init_table(self) -> None:
        self.setColumnCount(len(self.headers))

        labels = [d["display"] for d in self.headers.values()]
        self.setHorizontalHeaderLabels(labels)

        self.verticalHeader().setVisible(False)
        self.setEditTriggers(self.EditTrigger.NoEditTriggers)
        self.setAlternatingRowColors(True)
        self.setSortingEnabled(self.sorting)

        # Stock vnpy leaves columns at Qt's default ~100px "Interactive"
        # width and relies on QSettings-restored column_state (see
        # load_setting() below) or the user dragging headers by hand — on
        # a genuinely fresh install there's nothing to restore, so every
        # column (including short ones like "接口"/gateway_name) starts
        # truncated. Stretch fills the table's actual available width
        # instead of leaving most of it as dead space next to a clipped
        # first column.
        self.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Expanding)

    def init_menu(self) -> None:
        self.menu = QtWidgets.QMenu(self)

        resize_action = QtGui.QAction(_("调整列宽"), self)
        resize_action.triggered.connect(self.resize_columns)
        self.menu.addAction(resize_action)

        save_action = QtGui.QAction(_("保存数据"), self)
        save_action.triggered.connect(self.save_csv)
        self.menu.addAction(save_action)

    def register_event(self) -> None:
        if self.event_type:
            self.signal.connect(self.process_event)
            self.event_engine.register(self.event_type, self.signal.emit)

    def process_event(self, event: Event) -> None:
        if self.sorting:
            self.setSortingEnabled(False)

        data = event.data

        if not self.data_key:
            self.insert_new_row(data)
        else:
            key = data.__getattribute__(self.data_key)
            if key in self.cells:
                self.update_old_row(data)
            else:
                self.insert_new_row(data)

        if self.sorting:
            self.setSortingEnabled(True)

    def insert_new_row(self, data: Any) -> None:
        # Cap only applies to insert-only monitors — see max_rows above.
        # New rows go in at the top, so the ones dropped off the bottom
        # are the oldest; cells{} is untouched because insert-only
        # monitors never populate it (data_key check below).
        if not self.data_key:
            while self.rowCount() >= self.max_rows:
                self.removeRow(self.rowCount() - 1)

        self.insertRow(0)

        row_cells: dict = {}
        for column, header in enumerate(self.headers.keys()):
            setting = self.headers[header]

            content = data.__getattribute__(header)
            cell = setting["cell"](content, data)
            self.setItem(0, column, cell)

            if setting["update"]:
                row_cells[header] = cell

        if self.data_key:
            key = data.__getattribute__(self.data_key)
            self.cells[key] = row_cells

    def update_old_row(self, data: Any) -> None:
        key = data.__getattribute__(self.data_key)
        row_cells = self.cells[key]

        for header, cell in row_cells.items():
            content = data.__getattribute__(header)
            cell.set_content(content, data)

    def resize_columns(self) -> None:
        self.horizontalHeader().resizeSections(QtWidgets.QHeaderView.ResizeMode.ResizeToContents)

    def save_csv(self) -> None:
        path, __ = QtWidgets.QFileDialog.getSaveFileName(self, _("保存数据"), "", "CSV(*.csv)")
        if not path:
            return

        with open(path, "w") as f:
            writer = csv.writer(f, lineterminator="\n")

            headers = [d["display"] for d in self.headers.values()]
            writer.writerow(headers)

            for row in range(self.rowCount()):
                if self.isRowHidden(row):
                    continue

                row_data = []
                for column in range(self.columnCount()):
                    item = self.item(row, column)
                    row_data.append(str(item.text()) if item else "")
                writer.writerow(row_data)

    def contextMenuEvent(self, event: QtGui.QContextMenuEvent) -> None:
        self.menu.popup(QtGui.QCursor.pos())

    def save_setting(self) -> None:
        settings = QtCore.QSettings(self.__class__.__name__, "custom")
        settings.setValue("column_state", self.horizontalHeader().saveState())

    def load_setting(self) -> None:
        settings = QtCore.QSettings(self.__class__.__name__, "custom")
        column_state = settings.value("column_state")

        if isinstance(column_state, QtCore.QByteArray):
            self.horizontalHeader().restoreState(column_state)
            self.horizontalHeader().setSortIndicator(-1, QtCore.Qt.SortOrder.AscendingOrder)


class TickMonitor(BaseMonitor):
    event_type = EVENT_TICK
    data_key = "vt_symbol"
    sorting = True

    headers = {
        "symbol": {"display": _("代码"), "cell": BaseCell, "update": False},
        "exchange": {"display": _("交易所"), "cell": EnumCell, "update": False},
        "name": {"display": _("名称"), "cell": BaseCell, "update": True},
        "last_price": {"display": _("最新价"), "cell": BaseCell, "update": True},
        "volume": {"display": _("成交量"), "cell": BaseCell, "update": True},
        "open_price": {"display": _("开盘价"), "cell": BaseCell, "update": True},
        "high_price": {"display": _("最高价"), "cell": BaseCell, "update": True},
        "low_price": {"display": _("最低价"), "cell": BaseCell, "update": True},
        "bid_price_1": {"display": _("买1价"), "cell": BidCell, "update": True},
        "bid_volume_1": {"display": _("买1量"), "cell": BidCell, "update": True},
        "ask_price_1": {"display": _("卖1价"), "cell": AskCell, "update": True},
        "ask_volume_1": {"display": _("卖1量"), "cell": AskCell, "update": True},
        "datetime": {"display": _("时间"), "cell": TimeCell, "update": True},
        "gateway_name": {"display": _("接口"), "cell": BaseCell, "update": False},
    }


class LogMonitor(BaseMonitor):
    event_type = EVENT_LOG
    data_key = ""
    sorting = False

    headers = {
        "time": {"display": _("时间"), "cell": TimeCell, "update": False},
        "msg": {"display": _("信息"), "cell": MsgCell, "update": False},
        "gateway_name": {"display": _("接口"), "cell": BaseCell, "update": False},
    }


class TradeMonitor(BaseMonitor):
    event_type = EVENT_TRADE
    data_key = ""
    sorting = True

    headers = {
        "tradeid": {"display": _("成交号"), "cell": BaseCell, "update": False},
        "orderid": {"display": _("委托号"), "cell": BaseCell, "update": False},
        "symbol": {"display": _("代码"), "cell": BaseCell, "update": False},
        "exchange": {"display": _("交易所"), "cell": EnumCell, "update": False},
        "direction": {"display": _("方向"), "cell": DirectionCell, "update": False},
        "offset": {"display": _("开平"), "cell": EnumCell, "update": False},
        "price": {"display": _("价格"), "cell": BaseCell, "update": False},
        "volume": {"display": _("数量"), "cell": BaseCell, "update": False},
        "datetime": {"display": _("时间"), "cell": TimeCell, "update": False},
        "gateway_name": {"display": _("接口"), "cell": BaseCell, "update": False},
    }


class OrderMonitor(BaseMonitor):
    event_type = EVENT_ORDER
    data_key = "vt_orderid"
    sorting = True

    headers = {
        "orderid": {"display": _("委托号"), "cell": BaseCell, "update": False},
        "reference": {"display": _("来源"), "cell": BaseCell, "update": False},
        "symbol": {"display": _("代码"), "cell": BaseCell, "update": False},
        "exchange": {"display": _("交易所"), "cell": EnumCell, "update": False},
        "type": {"display": _("类型"), "cell": EnumCell, "update": False},
        "direction": {"display": _("方向"), "cell": DirectionCell, "update": False},
        "offset": {"display": _("开平"), "cell": EnumCell, "update": False},
        "price": {"display": _("价格"), "cell": BaseCell, "update": False},
        "volume": {"display": _("总数量"), "cell": BaseCell, "update": True},
        "traded": {"display": _("已成交"), "cell": BaseCell, "update": True},
        "status": {"display": _("状态"), "cell": EnumCell, "update": True},
        "datetime": {"display": _("时间"), "cell": TimeCell, "update": True},
        "gateway_name": {"display": _("接口"), "cell": BaseCell, "update": False},
    }

    def init_ui(self) -> None:
        super().init_ui()
        self.setToolTip(_("双击单元格撤单"))
        self.itemDoubleClicked.connect(self.cancel_order)

    def cancel_order(self, cell: BaseCell) -> None:
        order: OrderData = cell.get_data()
        req: CancelRequest = order.create_cancel_request()
        self.main_engine.cancel_order(req, order.gateway_name)


class ActiveOrderMonitor(OrderMonitor):
    def process_event(self, event: Event) -> None:
        super().process_event(event)

        order: OrderData = event.data
        row_cells = self.cells[order.vt_orderid]
        row = self.row(row_cells["volume"])

        if order.is_active():
            self.showRow(row)
        else:
            self.hideRow(row)


class PositionMonitor(BaseMonitor):
    event_type = EVENT_POSITION
    data_key = "vt_positionid"
    sorting = True

    headers = {
        "symbol": {"display": _("代码"), "cell": BaseCell, "update": False},
        "exchange": {"display": _("交易所"), "cell": EnumCell, "update": False},
        "direction": {"display": _("方向"), "cell": DirectionCell, "update": False},
        "volume": {"display": _("数量"), "cell": BaseCell, "update": True},
        "yd_volume": {"display": _("昨仓"), "cell": BaseCell, "update": True},
        "frozen": {"display": _("冻结"), "cell": BaseCell, "update": True},
        "price": {"display": _("均价"), "cell": BaseCell, "update": True},
        "pnl": {"display": _("盈亏"), "cell": PnlCell, "update": True},
        "gateway_name": {"display": _("接口"), "cell": BaseCell, "update": False},
    }


class AccountMonitor(BaseMonitor):
    event_type = EVENT_ACCOUNT
    data_key = "vt_accountid"
    sorting = True

    headers = {
        "accountid": {"display": _("账号"), "cell": BaseCell, "update": False},
        "balance": {"display": _("余额"), "cell": BaseCell, "update": True},
        "frozen": {"display": _("冻结"), "cell": BaseCell, "update": True},
        "available": {"display": _("可用"), "cell": BaseCell, "update": True},
        "gateway_name": {"display": _("接口"), "cell": BaseCell, "update": False},
    }
