"""下拉框不能因为"已经选中了一项"就把自己过滤到只剩那一项。

用户报障："交易所只有香港的，没有美国的"。真相不是列表里缺了美国交易所 ——
49 个 Exchange 枚举一个不少 —— 而是 _showComboMenu 拿 self.text() 当搜索词，
而选中之后 text() 就等于当前选项本身。于是"用选中项过滤它自己"，列表塌缩成 1 项。
对交易所/周期这种枚举下拉，后果是选定之后再也换不掉；用户先选了个港股合约、
交易所自动跟成 SEHK，就永远回不到 SMART 了。

第二个缺陷同源同一次报障：截图里代码是 SEHK 合约、交易所却停在 CFFEX。
因为联动接在 activated 上，而 activated 只由"点下拉菜单条目"发出；打字时弹出的
补全 popup 走的是另一条路，不发这个信号。交易所选错不会报错，只会安静地下载到
对不上的数据 —— 所以下面第三组按三条选中路径分别钉住。
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from vnpy.trader.constant import Exchange
from vnpy.trader.ui import QtWidgets, create_qapp

from fluent_ui.searchable_combo_box import SearchableComboBox


@pytest.fixture(scope="module")
def qapp() -> QtWidgets.QApplication:
    existing = QtWidgets.QApplication.instance()
    if existing is not None:
        return existing                                     # type: ignore[return-value]
    return create_qapp()


def _exchange_combo() -> SearchableComboBox:
    combo = SearchableComboBox()
    for exchange in Exchange:
        combo.addItem(exchange.name, userData=exchange)
    return combo


def _menu(combo: SearchableComboBox) -> list[str]:
    """复刻 _showComboMenu 的取舍：点开箭头时会列出哪些条目。

    不直接调 _showComboMenu 是因为它要真的弹出一个 RoundMenu（需要窗口系统、
    且会留下悬挂的菜单对象）；这里走同一个 _query() 判据，覆盖的是同一个决定。
    """
    query = combo._query()
    texts = [item.text for item in combo.items]
    return [t for t in texts if query in t.lower()] if query else texts


# ── 第一组：选中之后，列表不许塌缩 ────────────────────────────────────

def test_selecting_an_exchange_does_not_hide_the_others(qapp: QtWidgets.QApplication) -> None:
    """用户报的那一个：选中 SEHK 之后，美国的交易所必须还在。"""
    combo = _exchange_combo()
    combo.setCurrentIndex([i.text for i in combo.items].index("SEHK"))

    visible = _menu(combo)
    assert len(visible) == len(list(Exchange))
    assert "SMART" in visible, "选了港股之后就换不回美股了"
    assert "NASDAQ" in visible


def test_every_selection_keeps_the_full_list(qapp: QtWidgets.QApplication) -> None:
    """不是只有 SEHK 特殊 —— 任何一项被选中都不该吃掉其余的。"""
    combo = _exchange_combo()
    total = len(list(Exchange))
    for index in range(total):
        combo.setCurrentIndex(index)
        assert len(_menu(combo)) == total, f"选中 {combo.items[index].text} 后列表塌缩"


def test_a_freshly_built_combo_shows_everything(qapp: QtWidgets.QApplication) -> None:
    """构造完就有 index 0 被选中（文本非空），这本身也不该算搜索词。"""
    assert len(_menu(_exchange_combo())) == len(list(Exchange))


# ── 第二组：过滤本身还得管用（别为了修塌缩把搜索废掉）────────────────

def test_typing_still_narrows_the_list(qapp: QtWidgets.QApplication) -> None:
    combo = _exchange_combo()
    combo.setText("sm")
    assert _menu(combo) == ["SMART"]


def test_filter_is_substring_and_case_insensitive(qapp: QtWidgets.QApplication) -> None:
    combo = _exchange_combo()
    combo.setText("hk")
    visible = _menu(combo)
    assert "SEHK" in visible and "HKFE" in visible
    assert "SMART" not in visible


def test_backspacing_out_of_a_selection_starts_filtering_again(
    qapp: QtWidgets.QApplication,
) -> None:
    """选完再退格改字：此刻文本不再等于任何一项，应当恢复成搜索词。

    这是判据的边界 —— 用 _currentIndex 而不是"文本是否等于某项"来区分，
    正是为了让这一步自然落回过滤。
    """
    combo = _exchange_combo()
    combo.setCurrentIndex([i.text for i in combo.items].index("SEHK"))
    combo.setText("SEH")                                    # 退掉一个字符
    assert _menu(combo) == ["SEHK"]


def test_empty_text_shows_everything(qapp: QtWidgets.QApplication) -> None:
    combo = _exchange_combo()
    combo.setText("")
    assert len(_menu(combo)) == len(list(Exchange))


# ── 第三组：三条选中路径都要触发联动 ─────────────────────────────────

def _symbol_combo() -> SearchableComboBox:
    combo = SearchableComboBox()
    combo.addItem("1 长和 · SEHK", userData=("1", Exchange.SEHK))
    combo.addItem("AAPL APPLE INC · SMART", userData=("AAPL", Exchange.SMART))
    return combo


@pytest.mark.parametrize("path", ["menu-click", "completer-popup", "typed-enter"])
def test_all_three_pick_paths_notify(qapp: QtWidgets.QApplication, path: str) -> None:
    """联动必须接在三条路都会发的信号上。

    activated 只由 _onItemClicked 发出（qfluentwidgets combo_box.py:366）——
    只覆盖 "menu-click"。实际最常走的是打字时的补全 popup，它经
    __onActivated -> setCurrentIndex，不发 activated。接错信号的后果不是报错，
    是交易所静默停在上一个值，下载到对不上的数据。
    """
    combo = _symbol_combo()
    seen: list[object] = []
    combo.currentIndexChanged.connect(lambda i: seen.append(combo.itemData(i)))

    if path == "menu-click":
        combo._onItemClicked(1)
    elif path == "completer-popup":
        # 补全 popup 选中后的净效果就是把文本置成该项的完整文本
        combo.setText("AAPL APPLE INC · SMART")
    else:
        combo.setText("AAPL APPLE INC · SMART")
        combo._onReturnPressed()

    assert seen, f"{path} 没有触发任何选中通知"
    assert seen[-1] == ("AAPL", Exchange.SMART)


def test_partial_text_reports_no_selection(qapp: QtWidgets.QApplication) -> None:
    """打字途中会以 index=-1 通知，下游必须能安全处理（itemData 返回 None）。"""
    combo = _symbol_combo()
    seen: list[int] = []
    combo.currentIndexChanged.connect(seen.append)
    combo.setText("AAP")

    assert combo.itemData(-1) is None
    assert all(combo.itemData(i) is None for i in seen if i < 0)
