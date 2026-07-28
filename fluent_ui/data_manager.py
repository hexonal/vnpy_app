"""
Fluent-native replacement for vnpy_datamanager's ManagerWidget/
DownloadDialog/ImportDialog/DateRangeDialog — same logic and same
underlying ManagerEngine calls (this is a third-party pip package, not
part of the vnpy fork; the engine/business-logic layer is reused
unmodified, only the Qt widget classes are swapped), with exchange/
interval/symbol/timezone fields using SearchableComboBox (below) so they
actually filter as you type — that gap (plain QComboBox, no search) was
the concrete complaint that started this file.

SearchableComboBox exists because qfluentwidgets.EditableComboBox does NOT
actually filter its dropdown by the typed text, despite being "editable" —
read _showComboMenu() in qfluentwidgets' own combo_box.py: it unconditionally
lists every self.items entry with no text-matching logic at all. Typing is
only usable for committing a brand-new free-text entry on Enter
(_onReturnPressed), not for narrowing the existing list. An earlier version
of this file's docstring claimed EditableComboBox already "支持
type-to-filter" — that was wrong, corrected here after actually reading the
library source instead of assuming from its name.

Date fields use qfluentwidgets.CalendarPicker (setDate(QDate)/getDate(),
verified against the installed package source) — an earlier revision of
this docstring reasoned about a "qfluentwidgets.DateEdit" class and kept
raw QtWidgets.QDateEdit based on that reasoning; no class by that name
exists anywhere in the installed qfluentwidgets package (grep confirmed),
so that was a comment about a phantom API — the second such false-claim
comment caught in this file (the first was EditableComboBox's imagined
type-to-filter). The raw QDateEdit it justified was also exactly the
out-of-place native spinbox the user screenshotted in the dark theme.

Message popups use qfluentwidgets.MessageBox for the same reason: with
qdarkstyle removed (it fights qfluentwidgets), raw QtWidgets.QMessageBox
renders as an unthemed native dialog floating over the dark Fluent shell.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from functools import partial
from zoneinfo import available_timezones

from qfluentwidgets import (
    CalendarPicker,
    LineEdit,
    MessageBox,
    PushButton,
    TableWidget,
    TreeWidget,
)
from vnpy.event import EventEngine
from vnpy.trader.constant import Exchange, Interval
from vnpy.trader.database import DB_TZ
from vnpy.trader.engine import MainEngine
from vnpy.trader.locale import _
from vnpy.trader.object import BarData, ContractData
from vnpy.trader.ui import QtCore, QtWidgets
from vnpy_datamanager.engine import APP_NAME, BarOverview, ManagerEngine
from vnpy_gatewaykit.query_window import localize_bound

from .searchable_combo_box import SearchableComboBox

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

        # Tree (overview list) | table (bar preview) in a draggable
        # splitter — the table gets the larger default share (1:2) and the
        # boundary is user-adjustable, instead of both sizing to sizeHint.
        content_splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
        content_splitter.addWidget(self.tree)
        content_splitter.addWidget(self.table)
        content_splitter.setStretchFactor(0, 1)
        content_splitter.setStretchFactor(1, 2)
        content_splitter.setChildrenCollapsible(False)

        vbox = QtWidgets.QVBoxLayout()
        vbox.addLayout(hbox1)
        vbox.addWidget(content_splitter, 1)
        self.setLayout(vbox)

    def init_tree(self) -> None:
        labels = [
            _("数据"), _("本地代码"), _("代码"), _("交易所"), _("数据量"),
            _("开始时间"), _("结束时间"), "", "", "",
        ]

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
        labels = [
            _("时间"), _("开盘价"), _("最高价"), _("最低价"),
            _("收盘价"), _("成交量"), _("成交额"), _("持仓量"),
        ]

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
                partial(
                    self.output_data,
                    overview.symbol,
                    overview.exchange,
                    overview.interval,
                    overview.start,
                    overview.end,
                )
            )

            show_button = PushButton(_("查看"))
            show_button.clicked.connect(
                partial(
                    self.show_data,
                    overview.symbol,
                    overview.exchange,
                    overview.interval,
                    overview.start,
                    overview.end,
                )
            )

            delete_button = PushButton(_("删除"))
            delete_button.clicked.connect(
                partial(
                    self.delete_data, overview.symbol, overview.exchange, overview.interval
                )
            )

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
        self._show_message(_("载入成功！"), msg)

    def output_data(
        self,
        symbol: str,
        exchange: Exchange,
        interval: Interval,
        start: datetime,
        end: datetime,
    ) -> None:
        dialog = DateRangeDialog(start, end)
        n = dialog.exec()
        if n != dialog.DialogCode.Accepted:
            return
        start, end = dialog.get_date_range()

        path, _unused = QtWidgets.QFileDialog.getSaveFileName(self, _("导出数据"), "", "CSV(*.csv)")
        if not path:
            return

        result = self.engine.output_data_to_csv(
            path,
            symbol,
            exchange,
            interval,
            localize_bound(start, exchange),
            localize_bound(end, exchange),
        )
        if not result:
            self._show_message(
                _("导出失败！"),
                _("该文件已在其他程序中打开，请关闭相关程序后再尝试导出数据。"),
            )

    def show_data(
        self,
        symbol: str,
        exchange: Exchange,
        interval: Interval,
        start: datetime,
        end: datetime,
    ) -> None:
        dialog = DateRangeDialog(start, end)
        n = dialog.exec()
        if n != dialog.DialogCode.Accepted:
            return
        start, end = dialog.get_date_range()

        bars: list[BarData] = self.engine.load_bar_data(
            symbol,
            exchange,
            interval,
            localize_bound(start, exchange),
            localize_bound(end, exchange),
        )

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
        confirm = MessageBox(
            _("删除确认"),
            f"请确认是否要删除{symbol} {exchange.value} {interval.value}的全部数据",
            self.window(),
        )
        if not confirm.exec():
            return

        count = self.engine.delete_bar_data(symbol, exchange, interval)
        self._show_message(
            _("删除成功"),
            f"已删除{symbol} {exchange.value} {interval.value}共计{count}条数据",
        )

    def update_data(self) -> None:
        overviews: list[BarOverview] = self.engine.get_bar_overview()
        total = len(overviews)

        dialog = QtWidgets.QProgressDialog(_("历史数据更新中"), _("取消"), 0, 100)
        dialog.setWindowTitle(_("更新进度"))
        dialog.setWindowModality(QtCore.Qt.WindowModality.WindowModal)
        dialog.setValue(0)

        # count starts at 1 so the first finished download reports 1/total,
        # exactly as the old `count = 0` + `count += 1` after the download did.
        for count, overview in enumerate(overviews, 1):
            if dialog.wasCanceled():
                break

            self.engine.download_bar_data(
                overview.symbol,
                overview.exchange,
                overview.interval,
                overview.end,
                self.output,
            )
            dialog.setValue(int(round(count / total * 100, 0)))

        dialog.close()

    def download_data(self) -> None:
        dialog = DownloadDialog(self.engine, self)
        dialog.exec()

    def output(self, msg: str) -> None:
        self._show_message(_("数据下载"), msg)

    def _show_message(self, title: str, content: str) -> None:
        box = MessageBox(title, content, self.window())
        box.hideCancelButton()
        box.exec()


class DateRangeDialog(QtWidgets.QDialog):
    def __init__(
        self, start: datetime, end: datetime, parent: QtWidgets.QWidget | None = None
    ) -> None:
        super().__init__(parent)

        self.setWindowTitle(_("选择数据区间"))

        # The +1-day offsets replicate stock vnpy_datamanager's behavior
        # verbatim — but computed with timedelta instead of the stock
        # `QDate(y, m, day + 1)`, which constructs an INVALID QDate
        # whenever start/end falls on the last day of a month (day+1
        # overflows; QDate has no rollover). Inherited off-by-one
        # semantics preserved; the month-end crash surface removed.
        start_init = start + timedelta(days=1)
        end_init = end + timedelta(days=1)

        self.start_edit = CalendarPicker()
        self.start_edit.setDate(QtCore.QDate(start_init.year, start_init.month, start_init.day))
        self.end_edit = CalendarPicker()
        self.end_edit.setDate(QtCore.QDate(end_init.year, end_init.month, end_init.day))

        button = PushButton(_("确定"))
        button.clicked.connect(self.accept)

        form = QtWidgets.QFormLayout()
        form.addRow(_("开始时间"), self.start_edit)
        form.addRow(_("结束时间"), self.end_edit)
        form.addRow(button)
        self.setLayout(form)

    def get_date_range(self) -> tuple[datetime, datetime]:
        """The picked range as a bare wall clock — a CalendarPicker cannot say
        more than year/month/day.

        Which clock that is depends on the contract being looked at, which this
        dialog does not know, so the bounds stay naive here and every caller
        runs them through vnpy_gatewaykit.query_window.localize_bound with the
        exchange in hand. Handing them to a driver as-is would let
        vnpy.trader.database.convert_tz read them as the *host's* midnight and
        quietly slice the edges off the window.
        """
        start_date = self.start_edit.getDate()
        end_date = self.end_edit.getDate()
        start = datetime(start_date.year(), start_date.month(), start_date.day())
        end = datetime(end_date.year(), end_date.month(), end_date.day()) + timedelta(days=1)
        return start, end


class ImportDialog(QtWidgets.QDialog):
    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)

        self.setWindowTitle(_("从CSV文件导入数据"))
        # Minimum, not fixed: 320px fixed left Fluent combo fields (30px
        # arrow button + qss padding) clipped next to Chinese form labels.
        self.setMinimumWidth(420)
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

        self.exchange_combo = SearchableComboBox()
        for i in Exchange:
            self.exchange_combo.addItem(str(i.name), userData=i)

        self.interval_combo = SearchableComboBox()
        for interval in Interval:
            if interval != Interval.TICK:
                self.interval_combo.addItem(str(interval.name), userData=interval)

        self.tz_combo = SearchableComboBox()
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


def _contract_label(contract: ContractData) -> str:
    """合约 -> 下拉框里显示的文本。

    原先只放 contract.symbol，于是港股全是裸数字（1 / 2 / 5 / 700 …）——
    19000 多个合约挤在一个下拉里，看不出 1 是长和还是别的什么，只能靠背。
    加上名称与交易所才选得动：同一个数字在不同市场是不同的东西。
    """
    name = (contract.name or "").strip()
    return f"{contract.symbol} {name} · {contract.exchange.name}".replace("  ", " ")


def _symbol_from_label(text: str) -> str:
    """显示文本 -> 代码。自由输入(没选下拉项)时原样返回。

    与 _contract_label 成对：改了显示就必须改读取，否则会拿
    "700 腾讯控股 · SEHK" 整串去当代码下载，直接失败。

    先 strip 再切：手输时前后常带空格（粘贴、误按），不先去掉的话
    "  AAPL  " 会在第一个空格处切出空串，静默变成"没填代码"。
    """
    return text.strip().split(" ", 1)[0]


class DownloadDialog(QtWidgets.QDialog):
    def __init__(self, engine: ManagerEngine, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)

        self.engine = engine

        self.setWindowTitle(_("下载历史数据"))
        # Minimum, not fixed — see ImportDialog's width comment; this
        # dialog additionally holds a 20-char placeholder in symbol_combo.
        self.setMinimumWidth(420)

        self.exchange_combo = SearchableComboBox()
        for i in Exchange:
            self.exchange_combo.addItem(str(i.name), userData=i)

        # Populated from every contract the connected gateway(s) already
        # know about (main_engine.get_all_contracts() — thousands of
        # entries once FutuGateway has queried SEHK/SZSE/SSE/SMART, see
        # run_gui.py), so a real, exchange-verified symbol can be found by
        # typing part of it instead of guessing at the exact code blind.
        # Still a SearchableComboBox (not a locked-down enum-backed one
        # like exchange/interval above): downloading history for a symbol
        # not yet in main_engine's contract cache is a legitimate use case
        # (e.g. right after a fresh connect, before a full contract query
        # has finished), so free-text entry via Enter must keep working —
        # see download()'s fallback to self.symbol_combo.text() below.
        self.symbol_combo = SearchableComboBox()
        for contract in self.engine.main_engine.get_all_contracts():
            self.symbol_combo.addItem(_contract_label(contract), userData=contract)
        # currentIndexChanged 而非 activated：activated 只由 _onItemClicked 发出
        # （qfluentwidgets combo_box.py:366），也就是只在"点开箭头、点菜单里的条目"
        # 这一条路上。而实际最常走的是打字时弹出的补全popup —— 它走
        # __onActivated -> setCurrentIndex，不发 activated，交易所于是不跟随：
        # 选了港股合约，交易所还停在 CFFEX，下载下来的东西对不上。
        # currentIndexChanged 三条路（菜单点击/补全popup/手输回车）都会发。
        self.symbol_combo.currentIndexChanged.connect(self._on_symbol_selected)
        self.symbol_combo.setPlaceholderText(_("输入代码搜索本地已知合约，或直接输入新代码"))

        self.interval_combo = SearchableComboBox()
        for interval in Interval:
            self.interval_combo.addItem(str(interval.name), userData=interval)

        end_dt = datetime.now()
        start_dt = end_dt - timedelta(days=3 * 365)
        self.start_date_edit = CalendarPicker()
        self.start_date_edit.setDate(QtCore.QDate(start_dt.year, start_dt.month, start_dt.day))

        button = PushButton(_("下载"))
        button.clicked.connect(self.download)

        form = QtWidgets.QFormLayout()
        form.addRow(_("代码"), self.symbol_combo)
        form.addRow(_("交易所"), self.exchange_combo)
        form.addRow(_("周期"), self.interval_combo)
        form.addRow(_("开始日期"), self.start_date_edit)
        form.addRow(button)
        self.setLayout(form)

    def _on_symbol_selected(self, index: int) -> None:
        """选中一个已知合约时，交易所自动跟到它真正所属的那个。

        用户刚刚搜出了正确的合约，没有理由再让他自己去想它在哪个交易所挂牌 ——
        而且交易所选错不会报错，只会安静地下载到不存在的数据。

        index 可能是 -1（文本不匹配任何条目，比如正在打字或手输新代码），
        itemData 对越界返回 None（combo_box.py:200-205），下面按 None 早退。
        """
        contract: ContractData | None = self.symbol_combo.itemData(index)
        if contract is None:
            return
        exchange_index = self.exchange_combo.findText(contract.exchange.name)
        if exchange_index >= 0:
            self.exchange_combo.setCurrentIndex(exchange_index)

    def download(self) -> None:
        symbol = self._current_symbol()
        exchange = self.exchange_combo.currentData()
        interval = self.interval_combo.currentData()

        if exchange is None or interval is None:
            # EditableComboBox lets Enter commit whatever's typed as a new
            # item with no userData (see qfluentwidgets' _onReturnPressed)
            # if it doesn't exactly match an existing entry — e.g. typing
            # "CFF" and hitting Enter without picking "CFFEX" from the
            # filtered dropdown. Fail with a clear message instead of a
            # raw AttributeError/None-related crash.
            self.output(_("请从下拉列表中选择交易所和周期，不要只输入部分文字后回车"))
            return

        start_date = self.start_date_edit.getDate()
        start = datetime(start_date.year(), start_date.month(), start_date.day())
        start = start.replace(tzinfo=DB_TZ)

        if interval == Interval.TICK:
            count = self.engine.download_tick_data(symbol, exchange, start, self.output)
        else:
            count = self.engine.download_bar_data(symbol, exchange, interval, start, self.output)

        self.output(f"下载结束，总数据量：{count}条")

    def _current_symbol(self) -> str:
        """当前代码。选了下拉项就用它的 contract.symbol（权威），
        自由输入则从显示文本里取第一段。

        为什么不直接信文本：用户可能选完再手改几个字，此时 currentData 还
        指着旧合约。以文本为准、只在文本与该项显示文本一致时才用 userData，
        两种输入方式都不会错。
        """
        text = self.symbol_combo.text().strip()
        contract: ContractData | None = self.symbol_combo.currentData()
        if contract is not None and text == _contract_label(contract):
            return contract.symbol
        return _symbol_from_label(text)

    def output(self, msg: str) -> None:
        box = MessageBox(_("数据下载"), msg, self)
        box.hideCancelButton()
        box.exec()
