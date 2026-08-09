"""策略下拉框的中文标注、市场标注与尺寸。

这类改动的危险在于**成对**：改了显示文本，读取方就必须跟着改。上游
BacktesterManager 用 `class_combo.currentText()` 取类名，而显示文本现在是
`AtrRsiStrategy · ATR+RSI 波动突破` —— 少改一处，回测就会拿着带中文的字符串
去找策略类。所以这里不只验"标签好看"，更验"类名仍能取到"。

后两节是同一笔账的两面：标中文名把下拉框的 sizeHint 从 198px 撑到 375px（本机
macOS 实测，+89%），而 `class_combo` 是左栏 QFormLayout 的第一行、整列宽度跟着
它走；市场标注要是也写进 itemText，弹窗还会再宽出两百多像素。

**像素值只写进断言消息，不写进断言本身。** 375 / 243 / 266 / 214 这几个数是本机
（macOS + 微软雅黑缺失时的回落字体）量出来的，而 CI 跑在 ubuntu offscreen、字体
不同，钉死数字等于让同一份代码在两处得到不同判定。断言钉的是**不随字体变的那些
性质**：收窄后宽度与条目文本彻底脱钩、弹窗高度等于「行高 × 8 + 边框」、弹窗宽度
仍大于框宽（也就是中文说明没被省略号截掉）。
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from vnpy.trader.ui import QtCore, QtWidgets, create_qapp

from fluent_ui.backtester_strategy_labels import (
    CONTENTS_LENGTH,
    VISIBLE_ITEMS,
    _class_name_of,
    _fit_combo,
    _relabel,
    describe_market,
)

#: 本机（2026-08）回测面板里真实出现的十条，含本仓自研的 LongOnlyTurtleStrategy。
#: 尺寸用例要的是"和界面上一样长的那批字"，随手编两条短名量不出被撑宽的现象。
STRATEGIES = [
    "AtrRsiStrategy",
    "BollChannelStrategy",
    "DoubleMaStrategy",
    "DualThrustStrategy",
    "KingKeltnerStrategy",
    "LongOnlyTurtleStrategy",
    "MultiSignalStrategy",
    "MultiTimeframeStrategy",
    "TestStrategy",
    "TurtleSignalStrategy",
]

TOOLTIP_ROLE = int(QtCore.Qt.ItemDataRole.ToolTipRole)


# 一个进程只能有一个 QApplication。单跑本文件时没人抢先建，create_qapp() 能过；
# 但 pytest 跑整个目录时，同目录别的模块在【导入期】就建好了 app，这里再建一次
# 会抛 "Please destroy the QApplication singleton"。所以先取现成的 —— 这也是
# test_backtester_segments / _gates / _significance_panel 用的同一个约定。
@pytest.fixture(scope="module")
def qapp() -> QtWidgets.QApplication:
    existing = QtWidgets.QApplication.instance()
    if existing is not None:
        return existing                                     # type: ignore[return-value]
    return create_qapp()


def _combo(qapp, names: list[str]) -> QtWidgets.QComboBox:
    c = QtWidgets.QComboBox()
    c.addItems(names)
    return c


def test_label_shows_chinese_but_keeps_the_class_name_readable(qapp) -> None:
    c = _combo(qapp, ["AtrRsiStrategy"])
    _relabel(c)
    assert "ATR+RSI" in c.itemText(0)
    assert "AtrRsiStrategy" in c.itemText(0), "类名不该被中文顶掉 —— 查文档要用它"


def test_class_name_is_still_retrievable(qapp) -> None:
    """最要紧的一条：改了显示文本之后，类名必须仍能原样取回。

    取不回来的后果不是界面难看，是回测直接找不到策略类。
    """
    c = _combo(qapp, ["DualThrustStrategy"])
    _relabel(c)
    assert _class_name_of(c) == "DualThrustStrategy"
    assert c.currentText() != "DualThrustStrategy", "前提不成立：显示文本没被改过"


def test_unknown_strategy_shows_its_class_name_unchanged(qapp) -> None:
    """自定义策略查不到说明时原样显示，不编中文。"""
    c = _combo(qapp, ["MyPrivateAlphaStrategy"])
    _relabel(c)
    assert c.itemText(0) == "MyPrivateAlphaStrategy"
    assert _class_name_of(c) == "MyPrivateAlphaStrategy"


def test_relabel_is_idempotent(qapp) -> None:
    """install 可能被调用多次（重连、重开面板），说明不能越叠越长。"""
    c = _combo(qapp, ["TurtleSignalStrategy"])
    _relabel(c)
    once = c.itemText(0)
    assert _relabel(c) == 0, "第二次应当没有需要处理的项"
    assert c.itemText(0) == once


def test_fallback_when_module_not_installed(qapp) -> None:
    """没标注过的下拉框，_class_name_of 要能回落到显示文本。

    那种情况下显示文本本就是原始类名，回落是正确答案而不是凑合。
    """
    c = _combo(qapp, ["DoubleMaStrategy"])          # 故意不 _relabel
    assert _class_name_of(c) == "DoubleMaStrategy"


def test_test_strategy_is_marked_as_non_trading(qapp) -> None:
    """TestStrategy 的 on_bar 是 pass，拿它回测会得到一片空白。

    它排在下拉框中间，名字看着像"测试用的策略"而不是"不交易的骨架"——
    标注必须说清，否则用户会以为是回测坏了。
    """
    c = _combo(qapp, ["TestStrategy"])
    _relabel(c)
    assert "不交易" in c.itemText(0) or "不交易" in c.itemData(0, 3)


# ── 钩子挂在哪：必须是"下拉框已经有内容"之后 ─────────────────────────

def test_relabel_on_an_empty_combo_changes_nothing(qapp) -> None:
    """这条钉住第一版的失效原因。

    第一版把钩子挂在 BacktesterManager.init_ui 上，而 class_combo 不是在那里
    填的 —— __init__ 的顺序是 init_ui()(建控件) -> init_strategy_settings()
    (addItems) -> load_backtesting_setting()(选中)。挂在 init_ui 就是对着空
    下拉改标，界面上一条中文也不会出现。
    """
    assert _relabel(QtWidgets.QComboBox()) == 0


def test_hooks_cover_every_upstream_reader(qapp) -> None:
    """改了显示文本就得改每一个读取方，漏一处那条路就拿着中文去找策略类。

    上游读 class_combo.currentText() 的共四处（widget.py:317/396/514/552）：
    start_backtesting / start_optimization / edit_strategy_code /
    reload_strategy_class。这条从模块的钩子清单核对，不是抄一份常量。
    """
    from fluent_ui.backtester_strategy_labels import (
        _POPULATED_AFTER,
        _READERS,
        _REPOPULATORS,
    )

    hooked = set(_READERS) | set(_REPOPULATORS)
    assert hooked == {
        "start_backtesting",
        "start_optimization",
        "edit_strategy_code",
        "reload_strategy_class",
    }
    # 第一次标注的时机必须是 __init__ 的最后一步，那时下拉才有内容。
    assert _POPULATED_AFTER == "load_backtesting_setting"


def test_show_optimization_result_is_not_hooked(qapp) -> None:
    """它不读 class_combo（widget.py:465-475 只用 target_display），
    第一版把它也包了 —— 白包一层，还让人以为它跟策略名有关。"""
    from fluent_ui.backtester_strategy_labels import _READERS, _REPOPULATORS

    assert "show_optimization_result" not in set(_READERS) | set(_REPOPULATORS)


# ── 市场标注：港股与美股都得是一等公民 ───────────────────────────────

def test_market_note_rides_in_the_tooltip_and_never_in_the_item_text(qapp) -> None:
    """市场标注一个字都不许进 itemText。

    itemText 决定弹窗宽度（框宽已经被 _fit_combo 与内容脱钩，弹窗宽度没有）。
    「会开空仓：港股要求…」这一句六十多字，写进去弹窗就得再宽出两百多像素 ——
    等于把刚治好的那个病换个地方复发。
    """
    c = _combo(qapp, ["AtrRsiStrategy"])
    _relabel(c)
    assert "开空仓" not in c.itemText(0)
    assert "开空仓" in c.itemData(0, TOOLTIP_ROLE)


def test_long_only_strategy_is_marked_as_runnable_on_both_cash_accounts(qapp) -> None:
    """只做多的那条要说清「两边都能跑」，而不是只提其中一个市场。

    用户的诉求是港股与美股都当一等公民；一句"适合 A 股"或只提港股，
    都会让另一半的人以为这条策略不是给他们的。
    """
    note = describe_market("LongOnlyTurtleStrategy")
    assert "港股" in note and "美股" in note
    assert "只做多" in note


def test_target_pos_strategy_is_marked_as_shorting_even_without_a_short_call() -> None:
    """MultiSignalStrategy 全文一次 `self.short(` 都没有，却会做空。

    它走 TargetPosTemplate，目标仓位由三个子信号投票得出、可以是 −1
    （multi_signal_strategy.py:48）。这条钉住的是「市场标注只能手写」这个决定 ——
    真按源码文本判定，这一行就会变成一句关于真实交易行为的错话。
    """
    assert "开空仓" in describe_market("MultiSignalStrategy")


def test_dual_thrust_note_names_both_closing_bells(qapp) -> None:
    """DualThrust 的平仓时刻硬编码 14:55（中国期货收盘前），且不是可调参数。

    港股 16:00 收、美股 16:00 ET 收 —— 对两个市场同样致命，标注就得同时点名，
    只提一个市场等于把另一个市场的人当二等公民。
    """
    note = describe_market("DualThrustStrategy")
    assert "14:55" in note
    assert "港股" in note and "美股" in note


def test_every_strategy_the_fork_labels_also_carries_a_market_note() -> None:
    """两张表必须同进同退，缺一行就是一条策略在界面上没有市场提示。

    中文名住在 vnpy_ctastrategy 的 _STRATEGY_LABELS（改策略时顺手改说明才不脱节），
    市场标注住在本仓 —— 这是任务范围切开的结果，代价就是会漂。用这条会红的用例
    兜住，而不是在源码里留一句待办。fork 那边加一条策略、本仓忘了跟，这里就红。
    """
    from vnpy_ctastrategy.ui.widget import _STRATEGY_LABELS

    from fluent_ui.backtester_strategy_labels import _MARKET_NOTES

    assert set(_MARKET_NOTES) == set(_STRATEGY_LABELS)


def test_unknown_strategy_gets_no_market_note_at_all() -> None:
    """树外策略没人核过，就什么都不说。

    对一条没核过的策略讲「现金账户能跑」是在替用户担保 —— 比不说危险得多。
    """
    assert describe_market("MyPrivateAlphaStrategy") == ""


def test_unknown_strategy_keeps_the_bare_hint_as_its_tooltip(qapp) -> None:
    c = _combo(qapp, ["MyPrivateAlphaStrategy"])
    _relabel(c)
    assert c.itemData(0, TOOLTIP_ROLE) == "MyPrivateAlphaStrategy"


# ── 尺寸：中文名是本模块加的，撑宽的账也由本模块还 ────────────────────

def _fitted(qapp, names: list[str]) -> QtWidgets.QComboBox:
    c = _combo(qapp, names)
    _relabel(c)
    _fit_combo(c)
    return c


def test_relabelling_is_what_widens_the_combo(qapp) -> None:
    """先钉住病因：宽度是被中文说明撑出来的，不是上游本来就宽。

    本机实测 198px -> 375px（+89%）。这里不比数值只比大小 —— CI 的字体不同，
    但"加了二十来个汉字之后更宽"在任何字体下都成立。
    """
    bare = _combo(qapp, STRATEGIES).sizeHint().width()

    labelled_combo = _combo(qapp, STRATEGIES)
    _relabel(labelled_combo)
    labelled = labelled_combo.sizeHint().width()

    assert labelled > bare, (
        f"前提不成立：标注前后一样宽（本机实测 198 -> 375），实得 {bare} / {labelled}"
    )


def test_fit_combo_narrows_it_back(qapp) -> None:
    """本机实测 375px -> 243px。"""
    labelled_combo = _combo(qapp, STRATEGIES)
    _relabel(labelled_combo)
    labelled = labelled_combo.sizeHint().width()

    fitted = _fitted(qapp, STRATEGIES).sizeHint().width()
    assert fitted < labelled, f"没收窄（本机实测 375 -> 243），实得 {labelled} -> {fitted}"


def test_fitted_width_stops_depending_on_the_item_text(qapp) -> None:
    """这一条才是真正买到的东西，也是唯一与字体无关的判据。

    AdjustToContentsOnFirstShow 按最长条目撑开，所以每加一条策略、每加长一句
    说明，左栏就再宽一点，且撑开之后不会回缩。换成
    AdjustToMinimumContentsLengthWithIcon 之后宽度只由 CONTENTS_LENGTH 决定 ——
    塞一条两百字的条目进去，sizeHint 也必须一模一样。
    """
    short = _fitted(qapp, ["A"])
    long = _fitted(qapp, [*STRATEGIES, "X" * 200])
    assert short.sizeHint().width() == long.sizeHint().width()
    assert short.minimumContentsLength() == CONTENTS_LENGTH


def test_fit_combo_caps_the_popup_to_eight_rows(qapp) -> None:
    """弹窗高度上限 = 8 行 × 行高 + 上下边框。本机实测 266px -> 214px。

    上限按运行时量到的行高算，不是写死的像素 —— 字号一改，266 与 214 这两个数
    就都变了，而"最多露出八条"这个意图不变。
    """
    c = _fitted(qapp, STRATEGIES)
    view = c.view()
    expected = VISIBLE_ITEMS * view.sizeHintForRow(0) + 2 * view.frameWidth()
    assert view.maximumHeight() == expected
    assert c.maxVisibleItems() == VISIBLE_ITEMS


def test_fit_combo_leaves_an_empty_combo_uncapped(qapp) -> None:
    """空下拉量不出行高（sizeHintForRow 返回 -1）。

    拿 -1 去算会得到负的 maximumHeight，弹窗塌成一条缝 —— 而这正是补丁装好、
    面板还没填内容那一瞬间的状态，不是假想情形。
    """
    c = QtWidgets.QComboBox()
    _fit_combo(c)
    assert c.view().maximumHeight() > 0


def test_popup_still_shows_the_full_label_after_fitting(qapp) -> None:
    """收窄的是框，不是弹窗 —— 中文说明在展开时必须一个字不丢。

    这条钉住的是一条被否掉的改法：`setStyleSheet("QComboBox { combobox-popup: 0 }")`
    能让 setMaxVisibleItems 在 macOS 样式下重新生效（弹窗 266px -> 214px），
    但弹窗宽度会同时从 387px 塌到与框同宽的 243px，刚加上去的中文全被省略号
    截掉。真按那条改法写，这里就红。
    """
    c = _fitted(qapp, STRATEGIES)
    c.show()
    qapp.processEvents()
    c.showPopup()
    qapp.processEvents()
    try:
        popup_width = c.view().width()
    finally:
        c.hidePopup()
        c.close()
    assert popup_width > c.sizeHint().width(), (
        f"弹窗被压到框宽，中文说明会被截断（本机实测应为 387 > 243），实得 {popup_width}"
    )


def test_the_populate_hook_labels_and_fits_in_one_pass(qapp) -> None:
    """把两件事接在同一个钩子上：标注与定宽必须同时发生。

    只标不定宽，左栏就宽 375px；只定宽不标注，中文根本没上屏。这条走的是真的
    包装函数（`_wrap(..., "populate")`），而不是分别调两个私有函数 —— 上一版
    正是因为钩子挂错位置而"函数都对、界面没变"。
    """
    from fluent_ui.backtester_strategy_labels import _wrap

    class StubManager:
        """只提供钩子会碰到的那一个属性：class_combo。"""

        def __init__(self) -> None:
            self.class_combo = QtWidgets.QComboBox()
            self.class_combo.addItems(STRATEGIES)

        def load_backtesting_setting(self) -> None:
            """上游在这一步按存档选中某一条；对本条用例它只需存在。"""

    manager = StubManager()
    _wrap(StubManager.load_backtesting_setting, "populate")(manager)

    assert "ATR+RSI" in manager.class_combo.itemText(0)
    assert manager.class_combo.minimumContentsLength() == CONTENTS_LENGTH
    assert manager.class_combo.view().maximumHeight() > 0


def test_fit_combo_is_idempotent(qapp) -> None:
    """reload_strategy_class 会清空重填，本函数于是会被反复调用。"""
    c = _fitted(qapp, STRATEGIES)
    once = (c.sizeHint().width(), c.view().maximumHeight())
    _fit_combo(c)
    assert (c.sizeHint().width(), c.view().maximumHeight()) == once


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
