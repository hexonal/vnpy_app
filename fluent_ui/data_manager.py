"""
Fluent-native replacement for vnpy_datamanager's ManagerWidget/
DownloadDialog/ImportDialog/DateRangeDialog — same logic and same
underlying ManagerEngine calls (this is a third-party pip package, not
part of the vnpy fork; the engine/business-logic layer is reused
unmodified, only the Qt widget classes are swapped), with exchange/
interval combo boxes swapped for qfluentwidgets' EditableComboBox so they
support type-to-filter — that gap (plain QComboBox, no search) was the
concrete complaint that started this file.

qfluentwidgets.DateEdit is NOT used here: unlike ComboBox/LineEdit/
PushButton/TableWidget/TreeWidget, it does not subclass QtWidgets.QDateEdit
and its constructor doesn't accept an initial QDate the way stock code
relies on (`QDateEdit(QDate(y, m, d))`) — swapping it would mean rewriting
the date-handling logic, not just the widget class, for a field nobody
complained about. Kept as plain QtWidgets.QDateEdit.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from functools import partial

from qfluentwidgets import EditableComboBox, LineEdit, PushButton, TableWidget, TreeWidget

from vnpy.trader.constant import Exchange, Interval
from vnpy.trader.database import DB_TZ
from vnpy.trader.engine import EventEngine, MainEngine
from vnpy.trader.locale import _
from vnpy.trader.object import BarData
from vnpy.trader.ui import QtCore, QtWidgets
from vnpy.trader.utility import available_timezones
from vnpy_datamanager.engine import APP_NAME, BarOverview, ManagerEngine

INTERVAL_NAME_MAP = {
    Interval.MINUTE: _("分钟线"),
    Interval.HOUR: _("小时线"),
    Interval.DAILY: _("日线"),
}


class DataCell(QtWidgets.QTableWidgetItem):
    def __init__(self, text: str = "") -> None:
        super().__init__(text)
        self.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)


class ManagerWidget(QtWidgets.QWidget):
    def __init__(self, main_engine: MainEngine, event_engine: EventEngine) -> None:
        super().__init__()

        self.engine: ManagerEngine = main_engine.get_engine(APP_NAME)

        self.init_ui()

    def init_ui(self) -> None:
        self.setWindowTitle(_("数据管理"))

        self.init_tree()
        self.init_table()

        refresh_button = PushButton(_("刷新"))
        refresh_button.clicked.connect(self.refresh_tree)

        import_button = PushButton(_("导入数据"))
        import_button.clicked.connect(self.import_data)

        update_button = PushButton(_("更新数据"))
        update_button.clicked.connect(self.update_data)

        download_button = PushButton(_("下载数据"))
        download_button.clicked.connect(self.download_data)

        hbox1 = QtWidgets.QHBoxLayout()
        hbox1.addWidget(refresh_button)
        hbox1.addStretch()
        hbox1.addWidget(import_button)
        hbox1.addWidget(update_button)
        hbox1.addWidget(download_button)

        hbox2 = QtWidgets.QHBoxLayout()
        hbox2.addWidget(self.tree)
        hbox2.addWidget(self.table)

        vbox = QtWidgets.QVBoxLayout()
        vbox.addLayout(hbox1)
        vbox.addLayout(hbox2)
        self.setLayout(vbox)

    def init_tree(self) -> None:
        labels = [_("数据"), _("本地代码"), _("代码"), _("交易所"), _("数据量"), _("开始时间"), _("结束时间"), "", "", ""]

        self.tree = TreeWidget()
        self.tree.setColumnCount(len(labels))
        self.tree.setHeaderLabels(labels)
        # Never set at all before — every column (including the three
        # embedding 查看/导出/删除 buttons) sat at Qt's tiny default width,
        # clipping both text ("700.SEHK") and the buttons themselves.
        # ResizeToContents (not Stretch, unlike the plain-text table below)
        # because this tree mixes text columns with button-widget columns
        # of very different natural widths — forcing them equal would just
        # trade "too narrow" for "buttons stretched absurdly wide".
        self.tree.header().setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.ResizeToContents)

    def init_table(self) -> None:
        labels = [_("时间"), _("开盘价"), _("最高价"), _("最低价"), _("收盘价"), _("成交量"), _("成交额"), _("持仓量")]

        self.table = TableWidget()
        self.table.setColumnCount(len(labels))
        self.table.setHorizontalHeaderLabels(labels)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.Stretch)

    def refresh_tree(self) -> None:
        self.tree.clear()

        interval_childs: dict[Interval, QtWidgets.QTreeWidgetItem] = {}
        exchange_childs: dict[tuple[Interval, Exchange], QtWidgets.QTreeWidgetItem] = {}

        overviews: list[BarOverview] = self.engine.get_bar_overview()
        overviews.sort(key=lambda x: x.symbol)

        for interval in [Interval.MINUTE, Interval.HOUR, Interval.DAILY]:
            interval_child = QtWidgets.QTreeWidgetItem()
            interval_childs[interval] = interval_child
            interval_child.setText(0, INTERVAL_NAME_MAP[interval])

        for overview in overviews:
            key = (overview.interval, overview.exchange)
            exchange_child = exchange_childs.get(key)

            if not exchange_child:
                interval_child = interval_childs[overview.interval]
                exchange_child = QtWidgets.QTreeWidgetItem(interval_child)
                exchange_child.setText(0, overview.exchange.value)
                exchange_childs[key] = exchange_child

            item = QtWidgets.QTreeWidgetItem(exchange_child)
            item.setText(1, f"{overview.symbol}.{overview.exchange.value}")
            item.setText(2, overview.symbol)
            item.setText(3, overview.exchange.value)
            item.setText(4, str(overview.count))
            item.setText(5, overview.start.strftime("%Y-%m-%d %H:%M:%S"))
            item.setText(6, overview.end.strftime("%Y-%m-%d %H:%M:%S"))

            output_button = PushButton(_("导出"))
            output_button.clicked.connect(
                partial(self.output_data, overview.symbol, overview.exchange, overview.interval, overview.start, overview.end)
            )

            show_button = PushButton(_("查看"))
            show_button.clicked.connect(
                partial(self.show_data, overview.symbol, overview.exchange, overview.interval, overview.start, overview.end)
            )

            delete_button = PushButton(_("删除"))
            delete_button.clicked.connect(partial(self.delete_data, overview.symbol, overview.exchange, overview.interval))

            self.tree.setItemWidget(item, 7, show_button)
            self.tree.setItemWidget(item, 8, output_button)
            self.tree.setItemWidget(item, 9, delete_button)

        self.tree.addTopLevelItems(list(interval_childs.values()))
        for interval_child in interval_childs.values():
            interval_child.setExpanded(True)

    def import_data(self) -> None:
        dialog = ImportDialog()
        n = dialog.exec()
        if n != dialog.DialogCode.Accepted:
            return

        start, end, count = self.engine.import_data_from_csv(
            dialog.file_edit.text(),
            dialog.symbol_edit.text(),
            dialog.exchange_combo.currentData(),
            dialog.interval_combo.currentData(),
            dialog.tz_combo.currentText(),
            dialog.datetime_edit.text(),
            dialog.open_edit.text(),
            dialog.high_edit.text(),
            dialog.low_edit.text(),
            dialog.close_edit.text(),
            dialog.volume_edit.text(),
            dialog.turnover_edit.text(),
            dialog.open_interest_edit.text(),
            dialog.format_edit.text(),
        )

        exchange = dialog.exchange_combo.currentData()
        interval = dialog.interval_combo.currentData()
        msg = (
            f"CSV载入成功\n"
            f"代码：{dialog.symbol_edit.text()}\n"
            f"交易所：{exchange.value}\n"
            f"周期：{interval.value}\n"
            f"起始：{start}\n"
            f"结束：{end}\n"
            f"总数量：{count}\n"
        )
        QtWidgets.QMessageBox.information(self, _("载入成功！"), msg)

    def output_data(self, symbol: str, exchange: Exchange, interval: Interval, start: datetime, end: datetime) -> None:
        dialog = DateRangeDialog(start, end)
        n = dialog.exec()
        if n != dialog.DialogCode.Accepted:
            return
        start, end = dialog.get_date_range()

        path, _unused = QtWidgets.QFileDialog.getSaveFileName(self, _("导出数据"), "", "CSV(*.csv)")
        if not path:
            return

        result = self.engine.output_data_to_csv(path, symbol, exchange, interval, start, end)
        if not result:
            QtWidgets.QMessageBox.warning(self, _("导出失败！"), _("该文件已在其他程序中打开，请关闭相关程序后再尝试导出数据。"))

    def show_data(self, symbol: str, exchange: Exchange, interval: Interval, start: datetime, end: datetime) -> None:
        dialog = DateRangeDialog(start, end)
        n = dialog.exec()
        if n != dialog.DialogCode.Accepted:
            return
        start, end = dialog.get_date_range()

        bars: list[BarData] = self.engine.load_bar_data(symbol, exchange, interval, start, end)

        self.table.setRowCount(0)
        self.table.setRowCount(len(bars))

        for row, bar in enumerate(bars):
            self.table.setItem(row, 0, DataCell(bar.datetime.strftime("%Y-%m-%d %H:%M:%S")))
            self.table.setItem(row, 1, DataCell(str(bar.open_price)))
            self.table.setItem(row, 2, DataCell(str(bar.high_price)))
            self.table.setItem(row, 3, DataCell(str(bar.low_price)))
            self.table.setItem(row, 4, DataCell(str(bar.close_price)))
            self.table.setItem(row, 5, DataCell(str(bar.volume)))
            self.table.setItem(row, 6, DataCell(str(bar.turnover)))
            self.table.setItem(row, 7, DataCell(str(bar.open_interest)))

    def delete_data(self, symbol: str, exchange: Exchange, interval: Interval) -> None:
        n = QtWidgets.QMessageBox.warning(
            self,
            _("删除确认"),
            f"请确认是否要删除{symbol} {exchange.value} {interval.value}的全部数据",
            QtWidgets.QMessageBox.StandardButton.Ok,
            QtWidgets.QMessageBox.StandardButton.Cancel,
        )
        if n == QtWidgets.QMessageBox.StandardButton.Cancel:
            return

        count = self.engine.delete_bar_data(symbol, exchange, interval)
        QtWidgets.QMessageBox.information(
            self, _("删除成功"), f"已删除{symbol} {exchange.value} {interval.value}共计{count}条数据"
        )

    def update_data(self) -> None:
        overviews: list[BarOverview] = self.engine.get_bar_overview()
        total = len(overviews)
        count = 0

        dialog = QtWidgets.QProgressDialog(_("历史数据更新中"), _("取消"), 0, 100)
        dialog.setWindowTitle(_("更新进度"))
        dialog.setWindowModality(QtCore.Qt.WindowModality.WindowModal)
        dialog.setValue(0)

        for overview in overviews:
            if dialog.wasCanceled():
                break

            self.engine.download_bar_data(overview.symbol, overview.exchange, overview.interval, overview.end, self.output)
            count += 1
            dialog.setValue(int(round(count / total * 100, 0)))

        dialog.close()

    def download_data(self) -> None:
        dialog = DownloadDialog(self.engine, self)
        dialog.exec()

    def output(self, msg: str) -> None:
        QtWidgets.QMessageBox.warning(self, _("数据下载"), msg)


class DateRangeDialog(QtWidgets.QDialog):
    def __init__(self, start: datetime, end: datetime, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)

        self.setWindowTitle(_("选择数据区间"))

        self.start_edit = QtWidgets.QDateEdit(QtCore.QDate(start.year, start.month, start.day + 1))
        self.end_edit = QtWidgets.QDateEdit(QtCore.QDate(end.year, end.month, end.day + 1))

        button = PushButton(_("确定"))
        button.clicked.connect(self.accept)

        form = QtWidgets.QFormLayout()
        form.addRow(_("开始时间"), self.start_edit)
        form.addRow(_("结束时间"), self.end_edit)
        form.addRow(button)
        self.setLayout(form)

    def get_date_range(self) -> tuple[datetime, datetime]:
        start = self.start_edit.dateTime().toPython()
        end = self.end_edit.dateTime().toPython() + timedelta(days=1)
        return start, end


class ImportDialog(QtWidgets.QDialog):
    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)

        self.setWindowTitle(_("从CSV文件导入数据"))
        self.setFixedWidth(320)
        self.setWindowFlags(
            (self.windowFlags() | QtCore.Qt.WindowType.CustomizeWindowHint)
            & ~QtCore.Qt.WindowType.WindowMaximizeButtonHint
        )

        file_button = PushButton(_("选择文件"))
        file_button.clicked.connect(self.select_file)

        load_button = PushButton(_("确定"))
        load_button.clicked.connect(self.accept)

        self.file_edit = LineEdit()
        self.symbol_edit = LineEdit()

        self.exchange_combo = EditableComboBox()
        for i in Exchange:
            self.exchange_combo.addItem(str(i.name), userData=i)

        self.interval_combo = EditableComboBox()
        for i in Interval:
            if i != Interval.TICK:
                self.interval_combo.addItem(str(i.name), userData=i)

        self.tz_combo = EditableComboBox()
        self.tz_combo.addItems(available_timezones())
        self.tz_combo.setCurrentIndex(self.tz_combo.findText("Asia/Shanghai"))

        self.datetime_edit = LineEdit()
        self.datetime_edit.setText("datetime")
        self.open_edit = LineEdit()
        self.open_edit.setText("open")
        self.high_edit = LineEdit()
        self.high_edit.setText("high")
        self.low_edit = LineEdit()
        self.low_edit.setText("low")
        self.close_edit = LineEdit()
        self.close_edit.setText("close")
        self.volume_edit = LineEdit()
        self.volume_edit.setText("volume")
        self.turnover_edit = LineEdit()
        self.turnover_edit.setText("turnover")
        self.open_interest_edit = LineEdit()
        self.open_interest_edit.setText("open_interest")

        self.format_edit = LineEdit()
        self.format_edit.setText("%Y-%m-%d %H:%M:%S")

        info_label = QtWidgets.QLabel(_("合约信息"))
        info_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        head_label = QtWidgets.QLabel(_("表头信息"))
        head_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        format_label = QtWidgets.QLabel(_("格式信息"))
        format_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)

        form = QtWidgets.QFormLayout()
        form.addRow(file_button, self.file_edit)
        form.addRow(QtWidgets.QLabel())
        form.addRow(info_label)
        form.addRow(_("代码"), self.symbol_edit)
        form.addRow(_("交易所"), self.exchange_combo)
        form.addRow(_("周期"), self.interval_combo)
        form.addRow(_("时区"), self.tz_combo)
        form.addRow(QtWidgets.QLabel())
        form.addRow(head_label)
        form.addRow(_("时间戳"), self.datetime_edit)
        form.addRow(_("开盘价"), self.open_edit)
        form.addRow(_("最高价"), self.high_edit)
        form.addRow(_("最低价"), self.low_edit)
        form.addRow(_("收盘价"), self.close_edit)
        form.addRow(_("成交量"), self.volume_edit)
        form.addRow(_("成交额"), self.turnover_edit)
        form.addRow(_("持仓量"), self.open_interest_edit)
        form.addRow(QtWidgets.QLabel())
        form.addRow(format_label)
        form.addRow(_("时间格式"), self.format_edit)
        form.addRow(QtWidgets.QLabel())
        form.addRow(load_button)
        self.setLayout(form)

    def select_file(self) -> None:
        result = QtWidgets.QFileDialog.getOpenFileName(self, filter="CSV (*.csv)")
        if result[0]:
            self.file_edit.setText(result[0])


class DownloadDialog(QtWidgets.QDialog):
    def __init__(self, engine: ManagerEngine, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)

        self.engine = engine

        self.setWindowTitle(_("下载历史数据"))
        self.setFixedWidth(320)

        self.symbol_edit = LineEdit()

        self.exchange_combo = EditableComboBox()
        for i in Exchange:
            self.exchange_combo.addItem(str(i.name), userData=i)

        self.interval_combo = EditableComboBox()
        for i in Interval:
            self.interval_combo.addItem(str(i.name), userData=i)

        end_dt = datetime.now()
        start_dt = end_dt - timedelta(days=3 * 365)
        self.start_date_edit = QtWidgets.QDateEdit(QtCore.QDate(start_dt.year, start_dt.month, start_dt.day))

        button = PushButton(_("下载"))
        button.clicked.connect(self.download)

        form = QtWidgets.QFormLayout()
        form.addRow(_("代码"), self.symbol_edit)
        form.addRow(_("交易所"), self.exchange_combo)
        form.addRow(_("周期"), self.interval_combo)
        form.addRow(_("开始日期"), self.start_date_edit)
        form.addRow(button)
        self.setLayout(form)

    def download(self) -> None:
        symbol = self.symbol_edit.text()
        exchange = self.exchange_combo.currentData()
        interval = self.interval_combo.currentData()

        if exchange is None or interval is None:
            # EditableComboBox lets Enter commit whatever's typed as a new
            # item with no userData (see qfluentwidgets' _onReturnPressed)
            # if it doesn't exactly match an existing entry — e.g. typing
            # "CFF" and hitting Enter without picking "CFFEX" from the
            # filtered dropdown. Fail with a clear message instead of a
            # raw AttributeError/None-related crash.
            QtWidgets.QMessageBox.warning(self, _("下载失败"), _("请从下拉列表中选择交易所和周期，不要只输入部分文字后回车"))
            return

        start_date = self.start_date_edit.date()
        start = datetime(start_date.year(), start_date.month(), start_date.day())
        start = start.replace(tzinfo=DB_TZ)

        if interval == Interval.TICK:
            count = self.engine.download_tick_data(symbol, exchange, start, self.output)
        else:
            count = self.engine.download_bar_data(symbol, exchange, interval, start, self.output)

        QtWidgets.QMessageBox.information(self, _("下载结束"), f"下载总数据量：{count}条")

    def output(self, msg: str) -> None:
        QtWidgets.QMessageBox.warning(self, _("数据下载"), msg)
