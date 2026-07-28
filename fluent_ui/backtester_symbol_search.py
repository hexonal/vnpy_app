"""让回测器的「本地代码」能搜。

这一栏是个纯输入框，预填 `IF88.CFFEX`，要求填 `代码.交易所`。用户手上有
两万多个本地已知合约，却得凭记忆敲出代码和交易所后缀 —— 敲错的结果是回测
面板里一行 "本地代码缺失交易所后缀，请检查"，或者更糟：后缀合法但市场不对，
于是取不到数据，看起来像"这个标的没有历史"。

## 为什么挂 completer 而不是换成下拉框

换控件要动布局，还会把 `symbol_line` 的类型改掉 —— 上游有好几处
`symbol_line.text()` / `setText()`，全都得跟着改，漏一处就出错（今天已经
在别处踩过一次"改了显示没改读取"）。挂一个 QCompleter 不动控件类型、不动
布局，上游那些读写原样成立。

## 弹窗里显示名称，填进去的却只有代码

补全项显示 `NBIS.SMART NEBIUS GROUP` —— 带名称才认得出是谁，也才搜得到
（输入"腾讯"要能找到 700.SEHK）。但真正写进输入框的必须是 `NBIS.SMART`，
带名称的整串不是合法本地代码。

做法：匹配走显示文本（所以按名称搜得到），纯代码另存一个角色，选中时按显示
文本反查它写回输入框，覆盖掉 QCompleter 默认插入的显示文本。

第一版试过"DisplayRole 给人看、EditRole 给补全用"，不成立：QStandardItem 把
这两个角色存在同一处，`setData(..., EditRole)` 会把显示文本一并改掉 ——
实测弹窗里名称消失、输入"腾讯"零结果。

## 合约表必须按需刷新，不能开机快照一次

回测面板是开机时建的，而网关的合约查询是异步的。实测启动日志：补丁装好与
面板建成都在 10:07:33，四个市场的合约到 10:07:34~35 才陆续查完 —— 建面板
时一个合约都还没有。而这个面板整个进程只建一次，快照下来的空表就再也不会
更新，表现正是"打字没有任何反应"。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from vnpy.trader.engine import MainEngine
from vnpy.trader.object import ContractData
from vnpy.trader.ui import QtCore, QtGui, QtWidgets

_INSTALLED_FLAG = "_vnpy_app_symbol_search_installed"
_COMPLETER_ATTR = "_vnpy_app_symbol_completer"


#: 纯本地代码存这个角色；显示文本另带名称，两者不能共用 EditRole（见模块说明）。
SYMBOL_ROLE = QtCore.Qt.ItemDataRole.UserRole + 1


def _symbol_rows(contracts: Sequence[ContractData]) -> list[tuple[str, str]]:
    """合约 -> (纯本地代码, 显示文本) 列表，按代码排序。

    显示文本带名称：认得出是谁，也才搜得到（输入"腾讯"要能找到 700.SEHK）。
    """
    rows = [
        (
            f"{c.symbol}.{c.exchange.value}",
            f"{c.symbol}.{c.exchange.value} {(c.name or '').strip()}".rstrip(),
        )
        for c in contracts
    ]
    return sorted(rows)


def attach_completer(line: QtWidgets.QLineEdit, main_engine: MainEngine) -> int:
    """给输入框装上合约补全，返回装上那一刻的合约数（通常是 0，见下）。

    合约列表不能只在这里取一次。回测面板是开机时建的，而网关的合约查询是
    异步的 —— 实测启动日志：补丁装好与面板建成都在 10:07:33，四个市场的
    合约到 10:07:34~35 才陆续查完。也就是说建面板时一个合约都还没有，
    快照下来就是一张空表，而这个面板整个进程只建一次，于是永远搜不出东西。

    改为每次打字前按需刷新：只有合约总数变了才重建，所以敲一串字符不会
    反复重建两万多行。这样后连的网关也能自动补上，不必订阅事件。
    """
    model = QtGui.QStandardItemModel(line)
    by_display: dict[str, str] = {}
    counted = -1

    def refresh() -> None:
        nonlocal counted
        contracts = main_engine.get_all_contracts()
        if len(contracts) == counted:
            return
        counted = len(contracts)

        model.clear()
        by_display.clear()
        for vt_symbol, display in _symbol_rows(contracts):
            item = QtGui.QStandardItem(display)
            item.setData(vt_symbol, SYMBOL_ROLE)
            model.appendRow(item)
            by_display[display] = vt_symbol

    refresh()
    line.textEdited.connect(lambda _text: refresh())
    completer = QtWidgets.QCompleter(model, line)
    completer.setCaseSensitivity(QtCore.Qt.CaseSensitivity.CaseInsensitive)
    completer.setFilterMode(QtCore.Qt.MatchFlag.MatchContains)
    completer.setCompletionMode(QtWidgets.QCompleter.CompletionMode.PopupCompletion)
    completer.setMaxVisibleItems(20)
    line.setCompleter(completer)

    def write_back(text: str) -> None:
        """把选中项的纯代码写回输入框，覆盖 QCompleter 默认插入的显示文本。

        默认插入的是 `NBIS.SMART NEBIUS GROUP`，那不是合法本地代码 ——
        上游拿它去查合约必然落空。QCompleter 自己那条连接建立在 setCompleter
        里，早于这条，所以这里跑在它之后，覆盖成立。

        接的是 activated 的 str 重载（不带参数的 connect 默认就绑它，实测确认），
        而不是 `activated[QModelIndex]` —— 后者运行时可用，但 PySide6 的存根
        没标这个重载索引，只能靠 Any 绕过类型检查，那等于把类型闸关掉。
        """
        symbol = by_display.get(text)
        if symbol:
            line.setText(str(symbol))

    completer.activated.connect(write_back)

    # 留住引用：QCompleter 的父对象是 line，但模型只被 completer 引用，
    # 在某些绑定下会被提前回收，弹窗随后空白。
    setattr(line, _COMPLETER_ATTR, completer)
    return model.rowCount()


def install_symbol_search() -> list[str]:
    """接到回测器上。返回被包装的方法名。

    钩在 `init_ui` 之后：`symbol_line` 是在那里建的，而合约来自
    `self.main_engine`，此时已经挂好。
    """
    from vnpy_ctabacktester.ui.widget import BacktesterManager

    init_ui = BacktesterManager.init_ui
    if getattr(init_ui, _INSTALLED_FLAG, False):
        return []

    def init_ui_with_search(self: Any) -> None:
        init_ui(self)
        line = getattr(self, "symbol_line", None)
        engine = getattr(self, "main_engine", None)
        if isinstance(line, QtWidgets.QLineEdit) and engine is not None:
            attach_completer(line, engine)
            line.setPlaceholderText("输入代码搜索本地合约，或直接输入 代码.交易所")

    setattr(init_ui_with_search, _INSTALLED_FLAG, True)
    BacktesterManager.init_ui = init_ui_with_search
    return ["BacktesterManager.init_ui"]
