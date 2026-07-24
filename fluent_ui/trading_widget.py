"""
Fluent-native manual trading widget — same logic as
vnpy.trader.ui.widget.TradingWidget, with QComboBox/QLineEdit/QCheckBox/
QPushButton swapped for their qfluentwidgets equivalents. Field layout and
every main_engine call are unchanged.
"""

from __future__ import annotations

from qfluentwidgets import CheckBox, EditableComboBox, LineEdit, MessageBox, PushButton

from vnpy.event import Event
from vnpy.trader.constant import Direction, Exchange, Offset, OrderType
from vnpy.trader.engine import EventEngine, MainEngine
from vnpy.trader.event import EVENT_TICK
from vnpy.trader.locale import _
from vnpy.trader.object import (
    CancelRequest,
    ContractData,
    OrderRequest,
    PositionData,
    SubscribeRequest,
    TickData,
)
from vnpy.trader.ui import QtCore, QtGui, QtWidgets
from vnpy.trader.utility import get_digits

from .cells import BaseCell


class TradingWidget(QtWidgets.QWidget):
    signal_tick: QtCore.Signal = QtCore.Signal(Event)

    def __init__(self, main_engine: MainEngine, event_engine: EventEngine) -> None:
        super().__init__()

        self.main_engine = main_engine
        self.event_engine = event_engine

        self.vt_symbol = ""
        self.price_digits = 0

        self.init_ui()
        self.register_event()

    def init_ui(self) -> None:

        exchanges = self.main_engine.get_all_exchanges()
        self.exchange_combo = EditableComboBox()
        self.exchange_combo.addItems([exchange.value for exchange in exchanges])

        self.symbol_line = LineEdit()
        self.symbol_line.returnPressed.connect(self.set_vt_symbol)

        self.name_line = LineEdit()
        self.name_line.setReadOnly(True)

        self.direction_combo = EditableComboBox()
        self.direction_combo.addItems([Direction.LONG.value, Direction.SHORT.value])

        self.offset_combo = EditableComboBox()
        self.offset_combo.addItems([offset.value for offset in Offset])

        self.order_type_combo = EditableComboBox()
        self.order_type_combo.addItems([order_type.value for order_type in OrderType])

        double_validator = QtGui.QDoubleValidator()
        double_validator.setBottom(0)

        self.price_line = LineEdit()
        self.price_line.setValidator(double_validator)

        self.volume_line = LineEdit()
        self.volume_line.setValidator(double_validator)

        self.gateway_combo = EditableComboBox()
        self.gateway_combo.addItems(self.main_engine.get_all_gateway_names())

        self.price_check = CheckBox()
        self.price_check.setToolTip(_("设置价格随行情更新"))

        send_button = PushButton(_("委托"))
        send_button.clicked.connect(self.send_order)

        cancel_button = PushButton(_("全撤"))
        cancel_button.clicked.connect(self.cancel_all)

        grid = QtWidgets.QGridLayout()
        grid.addWidget(QtWidgets.QLabel(_("交易所")), 0, 0)
        grid.addWidget(QtWidgets.QLabel(_("代码")), 1, 0)
        grid.addWidget(QtWidgets.QLabel(_("名称")), 2, 0)
        grid.addWidget(QtWidgets.QLabel(_("方向")), 3, 0)
        grid.addWidget(QtWidgets.QLabel(_("开平")), 4, 0)
        grid.addWidget(QtWidgets.QLabel(_("类型")), 5, 0)
        grid.addWidget(QtWidgets.QLabel(_("价格")), 6, 0)
        grid.addWidget(QtWidgets.QLabel(_("数量")), 7, 0)
        grid.addWidget(QtWidgets.QLabel(_("接口")), 8, 0)
        grid.addWidget(self.exchange_combo, 0, 1, 1, 2)
        grid.addWidget(self.symbol_line, 1, 1, 1, 2)
        grid.addWidget(self.name_line, 2, 1, 1, 2)
        grid.addWidget(self.direction_combo, 3, 1, 1, 2)
        grid.addWidget(self.offset_combo, 4, 1, 1, 2)
        grid.addWidget(self.order_type_combo, 5, 1, 1, 2)
        grid.addWidget(self.price_line, 6, 1, 1, 1)
        grid.addWidget(self.price_check, 6, 2, 1, 1)
        grid.addWidget(self.volume_line, 7, 1, 1, 2)
        grid.addWidget(self.gateway_combo, 8, 1, 1, 2)
        grid.addWidget(send_button, 9, 0, 1, 3)
        grid.addWidget(cancel_button, 10, 0, 1, 3)

        bid_color = "rgb(255,174,201)"
        ask_color = "rgb(160,255,160)"

        self.bp1_label = self.create_label(bid_color)
        self.bp2_label = self.create_label(bid_color)
        self.bp3_label = self.create_label(bid_color)
        self.bp4_label = self.create_label(bid_color)
        self.bp5_label = self.create_label(bid_color)

        self.bv1_label = self.create_label(bid_color, alignment=QtCore.Qt.AlignmentFlag.AlignRight)
        self.bv2_label = self.create_label(bid_color, alignment=QtCore.Qt.AlignmentFlag.AlignRight)
        self.bv3_label = self.create_label(bid_color, alignment=QtCore.Qt.AlignmentFlag.AlignRight)
        self.bv4_label = self.create_label(bid_color, alignment=QtCore.Qt.AlignmentFlag.AlignRight)
        self.bv5_label = self.create_label(bid_color, alignment=QtCore.Qt.AlignmentFlag.AlignRight)

        self.ap1_label = self.create_label(ask_color)
        self.ap2_label = self.create_label(ask_color)
        self.ap3_label = self.create_label(ask_color)
        self.ap4_label = self.create_label(ask_color)
        self.ap5_label = self.create_label(ask_color)

        self.av1_label = self.create_label(ask_color, alignment=QtCore.Qt.AlignmentFlag.AlignRight)
        self.av2_label = self.create_label(ask_color, alignment=QtCore.Qt.AlignmentFlag.AlignRight)
        self.av3_label = self.create_label(ask_color, alignment=QtCore.Qt.AlignmentFlag.AlignRight)
        self.av4_label = self.create_label(ask_color, alignment=QtCore.Qt.AlignmentFlag.AlignRight)
        self.av5_label = self.create_label(ask_color, alignment=QtCore.Qt.AlignmentFlag.AlignRight)

        self.lp_label = self.create_label()
        self.return_label = self.create_label(alignment=QtCore.Qt.AlignmentFlag.AlignRight)

        form = QtWidgets.QFormLayout()
        form.addRow(self.ap5_label, self.av5_label)
        form.addRow(self.ap4_label, self.av4_label)
        form.addRow(self.ap3_label, self.av3_label)
        form.addRow(self.ap2_label, self.av2_label)
        form.addRow(self.ap1_label, self.av1_label)
        form.addRow(self.lp_label, self.return_label)
        form.addRow(self.bp1_label, self.bv1_label)
        form.addRow(self.bp2_label, self.bv2_label)
        form.addRow(self.bp3_label, self.bv3_label)
        form.addRow(self.bp4_label, self.bv4_label)
        form.addRow(self.bp5_label, self.bv5_label)

        vbox = QtWidgets.QVBoxLayout()
        vbox.addLayout(grid)
        vbox.addLayout(form)
        self.setLayout(vbox)

        # Sidebar panel: a bounded width is intentional (same design as
        # stock vnpy's 300px trading panel — an unconstrained panel gets
        # stretched by the home splitter). But bound it ADAPTIVELY:
        # content sizeHint with 300 as the floor, measured AFTER the
        # layout is built, so longer labels/locales widen the panel
        # instead of clipping — the same fixed-width-vs-content bug
        # class as ConnectDialog's clipped USMART field names.
        self.setFixedWidth(max(300, self.sizeHint().width()))

    def create_label(
        self, color: str = "", alignment: QtCore.Qt.AlignmentFlag = QtCore.Qt.AlignmentFlag.AlignLeft
    ) -> QtWidgets.QLabel:
        label = QtWidgets.QLabel()
        if color:
            label.setStyleSheet(f"color:{color}")
        label.setAlignment(alignment)
        return label

    def register_event(self) -> None:
        self.signal_tick.connect(self.process_tick_event)
        self.event_engine.register(EVENT_TICK, self.signal_tick.emit)

    def process_tick_event(self, event: Event) -> None:
        tick: TickData = event.data
        if tick.vt_symbol != self.vt_symbol:
            return

        price_digits = self.price_digits

        self.lp_label.setText(f"{tick.last_price:.{price_digits}f}")
        self.bp1_label.setText(f"{tick.bid_price_1:.{price_digits}f}")
        self.bv1_label.setText(str(tick.bid_volume_1))
        self.ap1_label.setText(f"{tick.ask_price_1:.{price_digits}f}")
        self.av1_label.setText(str(tick.ask_volume_1))

        if tick.pre_close:
            r = (tick.last_price / tick.pre_close - 1) * 100
            self.return_label.setText(f"{r:.2f}%")

        if tick.bid_price_2:
            self.bp2_label.setText(f"{tick.bid_price_2:.{price_digits}f}")
            self.bv2_label.setText(str(tick.bid_volume_2))
            self.ap2_label.setText(f"{tick.ask_price_2:.{price_digits}f}")
            self.av2_label.setText(str(tick.ask_volume_2))

            self.bp3_label.setText(f"{tick.bid_price_3:.{price_digits}f}")
            self.bv3_label.setText(str(tick.bid_volume_3))
            self.ap3_label.setText(f"{tick.ask_price_3:.{price_digits}f}")
            self.av3_label.setText(str(tick.ask_volume_3))

            self.bp4_label.setText(f"{tick.bid_price_4:.{price_digits}f}")
            self.bv4_label.setText(str(tick.bid_volume_4))
            self.ap4_label.setText(f"{tick.ask_price_4:.{price_digits}f}")
            self.av4_label.setText(str(tick.ask_volume_4))

            self.bp5_label.setText(f"{tick.bid_price_5:.{price_digits}f}")
            self.bv5_label.setText(str(tick.bid_volume_5))
            self.ap5_label.setText(f"{tick.ask_price_5:.{price_digits}f}")
            self.av5_label.setText(str(tick.ask_volume_5))

        if self.price_check.isChecked():
            self.price_line.setText(f"{tick.last_price:.{price_digits}f}")

    def set_vt_symbol(self) -> None:
        symbol = str(self.symbol_line.text())
        if not symbol:
            return

        exchange_value = str(self.exchange_combo.currentText())
        vt_symbol = f"{symbol}.{exchange_value}"

        if vt_symbol == self.vt_symbol:
            return
        self.vt_symbol = vt_symbol

        contract: ContractData | None = self.main_engine.get_contract(vt_symbol)
        if not contract:
            self.name_line.setText("")
            gateway_name = self.gateway_combo.currentText()
        else:
            self.name_line.setText(contract.name)
            gateway_name = contract.gateway_name

            ix = self.gateway_combo.findText(gateway_name)
            self.gateway_combo.setCurrentIndex(ix)

            self.price_digits = get_digits(contract.pricetick)

        self.clear_label_text()
        self.volume_line.setText("")
        self.price_line.setText("")

        req = SubscribeRequest(symbol=symbol, exchange=Exchange(exchange_value))
        self.main_engine.subscribe(req, gateway_name)

    def clear_label_text(self) -> None:
        self.lp_label.setText("")
        self.return_label.setText("")

        for label in (self.bv1_label, self.bv2_label, self.bv3_label, self.bv4_label, self.bv5_label):
            label.setText("")
        for label in (self.av1_label, self.av2_label, self.av3_label, self.av4_label, self.av5_label):
            label.setText("")
        for label in (self.bp1_label, self.bp2_label, self.bp3_label, self.bp4_label, self.bp5_label):
            label.setText("")
        for label in (self.ap1_label, self.ap2_label, self.ap3_label, self.ap4_label, self.ap5_label):
            label.setText("")

    def _show_error(self, title: str, content: str) -> None:
        """Fluent-styled error popup — raw QtWidgets.QMessageBox renders as
        an unthemed native dialog now that qdarkstyle is gone (only
        qfluentwidgets' own classes self-style)."""
        box = MessageBox(title, content, self.window())
        box.hideCancelButton()
        box.exec()

    def send_order(self) -> None:
        symbol = str(self.symbol_line.text())
        if not symbol:
            self._show_error(_("委托失败"), _("请输入合约代码"))
            return

        volume_text = str(self.volume_line.text())
        if not volume_text:
            self._show_error(_("委托失败"), _("请输入委托数量"))
            return
        volume = float(volume_text)

        price_text = str(self.price_line.text())
        price = float(price_text) if price_text else 0.0

        req = OrderRequest(
            symbol=symbol,
            exchange=Exchange(str(self.exchange_combo.currentText())),
            direction=Direction(str(self.direction_combo.currentText())),
            type=OrderType(str(self.order_type_combo.currentText())),
            volume=volume,
            price=price,
            offset=Offset(str(self.offset_combo.currentText())),
            reference="ManualTrading",
        )

        gateway_name = str(self.gateway_combo.currentText())
        self.main_engine.send_order(req, gateway_name)

    def cancel_all(self) -> None:
        for order in self.main_engine.get_all_active_orders():
            req: CancelRequest = order.create_cancel_request()
            self.main_engine.cancel_order(req, order.gateway_name)

    def update_with_cell(self, cell: BaseCell) -> None:
        data = cell.get_data()

        self.symbol_line.setText(data.symbol)
        self.exchange_combo.setCurrentIndex(self.exchange_combo.findText(data.exchange.value))

        self.set_vt_symbol()

        if isinstance(data, PositionData):
            if data.direction == Direction.SHORT:
                direction = Direction.LONG
            elif data.direction == Direction.LONG:
                direction = Direction.SHORT
            else:
                direction = Direction.SHORT if data.volume > 0 else Direction.LONG

            self.direction_combo.setCurrentIndex(self.direction_combo.findText(direction.value))
            self.offset_combo.setCurrentIndex(self.offset_combo.findText(Offset.CLOSE.value))
            self.volume_line.setText(str(abs(data.volume)))
