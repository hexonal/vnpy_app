"""
Fluent-native contract query widget — same logic as
vnpy.trader.ui.widget.ContractManager, with QLineEdit/QPushButton/
QTableWidget swapped for qfluentwidgets equivalents.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from qfluentwidgets import LineEdit, PushButton, TableWidget

from vnpy.trader.engine import EventEngine, MainEngine
from vnpy.trader.locale import _
from vnpy.trader.object import ContractData
from vnpy.trader.ui import QtWidgets

from .cells import BaseCell, DateCell, EnumCell


class ContractManager(QtWidgets.QWidget):
    headers: dict[str, str] = {
        "vt_symbol": _("本地代码"),
        "symbol": _("代码"),
        "exchange": _("交易所"),
        "name": _("名称"),
        "product": _("合约分类"),
        "size": _("合约乘数"),
        "pricetick": _("价格跳动"),
        "min_volume": _("最小委托量"),
        "option_portfolio": _("期权产品"),
        "option_expiry": _("期权到期日"),
        "option_strike": _("期权行权价"),
        "option_type": _("期权类型"),
        "gateway_name": _("交易接口"),
    }

    def __init__(self, main_engine: MainEngine, event_engine: EventEngine) -> None:
        super().__init__()

        self.main_engine = main_engine
        self.event_engine = event_engine

        self.init_ui()

    def init_ui(self) -> None:
        self.setWindowTitle(_("合约查询"))
        self.resize(1000, 600)

        self.filter_line = LineEdit()
        self.filter_line.setPlaceholderText(_("输入合约代码或者交易所，留空则查询所有合约"))

        self.button_show = PushButton(_("查询"))
        self.button_show.clicked.connect(self.show_contracts)

        labels = list(self.headers.values())

        self.contract_table = TableWidget()
        self.contract_table.setColumnCount(len(self.headers))
        self.contract_table.setHorizontalHeaderLabels(labels)
        self.contract_table.verticalHeader().setVisible(False)
        self.contract_table.setEditTriggers(self.contract_table.EditTrigger.NoEditTriggers)
        self.contract_table.setAlternatingRowColors(True)

        hbox = QtWidgets.QHBoxLayout()
        hbox.addWidget(self.filter_line)
        hbox.addWidget(self.button_show)

        vbox = QtWidgets.QVBoxLayout()
        vbox.addLayout(hbox)
        vbox.addWidget(self.contract_table)

        self.setLayout(vbox)

    def show_contracts(self) -> None:
        flt = str(self.filter_line.text())

        all_contracts: list[ContractData] = self.main_engine.get_all_contracts()
        if flt:
            contracts = [c for c in all_contracts if flt in c.vt_symbol]
        else:
            contracts = all_contracts

        self.contract_table.clearContents()
        self.contract_table.setRowCount(len(contracts))

        for row, contract in enumerate(contracts):
            for column, name in enumerate(self.headers.keys()):
                value = getattr(contract, name)

                if value in {None, 0, 0.0}:
                    value = ""

                if isinstance(value, Enum):
                    cell = EnumCell(value, contract)
                elif isinstance(value, datetime):
                    cell = DateCell(value, contract)
                else:
                    cell = BaseCell(value, contract)
                self.contract_table.setItem(row, column, cell)

        self.contract_table.resizeColumnsToContents()
