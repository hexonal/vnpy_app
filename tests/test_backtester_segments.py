"""回测面板三段提示 + 测试段寻优拦截的测试。

三段样本内外流程刻意**不进 GUI**（判断与理由见
`fluent_ui/backtester_segments.py` 的模块文档），它的入口在
`python -m vnpy_ctastrategy.segment_cli`。但"不进 GUI"不等于"GUI 不管"：
面板要能力所能及地做两件事，本文件把这两件都钉住 ——

  1. **告诉用户这条命令存在**：参数区多一行提示（两行封顶，量过），当前切分
     与剩余查看次数落在悬停提示与面板日志区。藏在代码里的能力等于不存在。
  2. **拦住从 GUI 泄漏测试段的那条路**：面板的[参数优化]按钮就是扫参数，
     而 TEST 段禁止扫参数。窗口碰到 TEST 段就拒绝执行。

测试真的把 widget 实例化出来（offscreen），断言标签在布局里、按钮真的没
跑起来 —— 只跑纯函数不能证明"GUI 里看得到"。
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402
from vnpy.event import EventEngine  # noqa: E402
from vnpy.trader.engine import MainEngine  # noqa: E402
from vnpy.trader.ui import QtCore, QtGui, QtWidgets  # noqa: E402


# 刻意【不】在模块顶层建 QApplication：一个进程只能有一个，而
# test_searchable_combo_box 在【导入期】就 create_qapp() 了。pytest 先收集
# （= 导入全部测试模块）再执行，所以任何在导入期抢先建 app 的模块都会让它那句
# 直接抛 "Please destroy the QApplication singleton"。同目录的
# test_backtester_gates / test_backtester_significance_panel 用的也是这个约定。
def _qapp() -> QtWidgets.QApplication:
    existing = QtWidgets.QApplication.instance()
    if existing is not None:
        return existing                                     # type: ignore[return-value]
    from fluent_ui import create_fluent_qapp

    return create_fluent_qapp("test-segments")

from vnpy_ctabacktester import CtaBacktesterApp  # noqa: E402
from vnpy_ctabacktester.ui.widget import BacktesterManager  # noqa: E402
from vnpy_ctastrategy.segment_record import (  # noqa: E402
    RECORD_FILENAME,
    open_record,
    save_record,
)
from vnpy_ctastrategy.segments import make_three_way_split  # noqa: E402

from fluent_ui.backtester_segments import (  # noqa: E402
    NOTICE_OBJECT_NAME,
    SEGMENT_CLI,
    install_segment_notice,
)

install_segment_notice()


# ══════════════════════════════════════════════════════════════════════
# 夹具
# ══════════════════════════════════════════════════════════════════════

def _split():
    base = datetime(2024, 1, 2)
    dates = [base + timedelta(days=i) for i in range(300)]
    return make_three_way_split(dates, 180, 60, 60, anchor="start")


@pytest.fixture
def ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把账本重定向到 tmp_path。

    `segment_record.record_path` 在【调用时】才 import `get_file_path`，
    所以打在 vnpy 那一侧就够了，生产代码不需要为测试留钩子。
    """
    monkeypatch.setattr(
        "vnpy.trader.utility.get_file_path", lambda filename: tmp_path / filename
    )
    return tmp_path / RECORD_FILENAME


@pytest.fixture
def panel():
    """一个真的 BacktesterManager（offscreen）。"""
    _qapp()
    event_engine = EventEngine()
    main_engine = MainEngine(event_engine)
    main_engine.add_app(CtaBacktesterApp)
    widget = BacktesterManager(main_engine, event_engine)
    yield widget
    widget.deleteLater()
    main_engine.close()


@pytest.fixture
def fluent_look():
    """把 QApplication 调成 run_gui 真正跑的那副样子，用完还原。

    量"标签会不会被截断"必须在真界面的度量下量。`create_fluent_qapp` 与 vnpy 的
    `create_qapp` 差两样东西，两样都改变文本尺寸：

        字体    前者换成平台最佳 CJK 字族（macOS 上 PingFang SC），后者用
                SETTINGS 里的默认（微软雅黑，本机没有，Qt 会替换掉）
        样式表  前者显式清空（qfluentwidgets 自己管主题），后者铺一层 qdarkstyle
                的全局 QWidget 样式，字号与内边距都被它改写

    实测同一段文字：fluent 下要 46px，qdarkstyle 下要 73px，而布局给的高度也从
    46px 变成 50px —— 用错的那副皮量出来的"没截断"是假的。本进程的 QApplication
    由谁创建取决于测试执行顺序（test_searchable_combo_box 在导入期就建了），
    所以这里不管是谁建的都先调成真界面的样子，退出时原样还原。
    """
    from vnpy.trader.setting import SETTINGS

    from fluent_ui.mainwindow import _select_smooth_font

    qapp = _qapp()
    before_font = qapp.font()
    before_style = qapp.styleSheet()
    qapp.setStyleSheet("")
    qapp.setFont(QtGui.QFont(_select_smooth_font(), SETTINGS["font.size"]))
    yield
    qapp.setFont(before_font)
    qapp.setStyleSheet(before_style)


def _write_ledger(path: Path, budget: int = 1) -> None:
    save_record(
        open_record(
            _split(),
            vt_symbol="700.SEHK",
            strategy_class="LongOnlyTurtleStrategy",
            interval="d",
            target_name="sharpe_ratio",
            test_budget=budget,
            path=path,
        ),
        path=path,
    )


def _set_window(widget, start: datetime, end: datetime) -> None:
    widget.start_date_edit.setDate(QtCore.QDate(start.year, start.month, start.day))
    widget.end_date_edit.setDate(QtCore.QDate(end.year, end.month, end.day))


def _notice(widget) -> QtWidgets.QLabel:
    label = widget.findChild(QtWidgets.QLabel, NOTICE_OBJECT_NAME)
    assert label is not None, "参数区里没有找到三段提示标签"
    return label


class _Spy:
    """记下被拦截的调用有没有真的透传下去。"""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def __call__(self, *args: object, **kwargs: object) -> bool:
        self.calls.append((args, kwargs))
        return True


# ══════════════════════════════════════════════════════════════════════
# 1. 安装
# ══════════════════════════════════════════════════════════════════════

def test_install_is_idempotent() -> None:
    """模块导入时已装过一次；再装不该叠第二层包装。"""
    assert install_segment_notice() == []


def test_install_is_a_noop_without_the_backtester(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """缺一个可选 App 不该让 GUI 起不来。"""
    monkeypatch.setitem(sys.modules, "vnpy_ctabacktester.ui.widget", None)
    assert install_segment_notice() == []


# ══════════════════════════════════════════════════════════════════════
# 2. 提示真的出现在面板上
# ══════════════════════════════════════════════════════════════════════

def test_notice_row_is_in_the_parameter_form(ledger: Path, panel) -> None:
    form = panel.findChild(QtWidgets.QFormLayout)
    assert form is not None
    labels = [
        form.itemAt(i).widget()
        for i in range(form.count())
        if form.itemAt(i).widget() is not None
    ]
    assert _notice(panel) in labels, "提示不在参数区的表单里"


def test_notice_prints_the_command_when_there_is_no_ledger(
    ledger: Path, panel
) -> None:
    assert not ledger.exists()
    text = _notice(panel).text()
    assert SEGMENT_CLI in text, "没有账本时至少要把命令印出来"
    assert "样本内外" in text or "三段" in text


def test_split_state_shows_up_in_the_tooltip_and_the_log(ledger: Path) -> None:
    """账本在时，状态必须真的能看到。

    标签本身只有 331px×3 行（实测），塞不下三段边界，所以状态落在两个真的有
    地方的位置：tooltip 与日志区。这条测试钉的是"看得到"，不是"在哪一行"。
    """
    _write_ledger(ledger, budget=3)

    _qapp()
    event_engine = EventEngine()
    main_engine = MainEngine(event_engine)
    main_engine.add_app(CtaBacktesterApp)
    widget = BacktesterManager(main_engine, event_engine)
    try:
        tip = _notice(widget).toolTip()
        assert "TRAIN" in tip and "VALID" in tip and "TEST" in tip
        assert "2024-06-30" in tip, "TRAIN 段的结束日期应当看得到"
        assert "剩余 3 次" in tip

        log = widget.log_monitor.toPlainText()
        assert "TRAIN" in log and "剩余 3 次" in log, "开面板时应当把切分打进日志"
    finally:
        widget.deleteLater()
        main_engine.close()


def test_no_ledger_means_no_log_noise(ledger: Path, panel) -> None:
    """没跑过命令行的人不该每次开面板都被念一遍三段。"""
    assert not ledger.exists()
    assert "[三段]" not in panel.log_monitor.toPlainText()


def _spent_ledger(path: Path) -> None:
    from vnpy_ctastrategy.segment_record import (
        SegmentPeek,
        load_record,
        record_peek,
    )

    _write_ledger(path)
    save_record(
        record_peek(
            load_record(path=path),
            SegmentPeek(
                datetime(2026, 7, 26), {"entry_window": 10.0}, "sharpe_ratio", -1.78
            ),
        ),
        path=path,
    )


def test_notice_stays_compact_and_puts_the_detail_in_the_tooltip(
    ledger: Path,
) -> None:
    """标签只放"有这条命令"，明细一律挂 tooltip。"""
    _spent_ledger(ledger)

    _qapp()
    event_engine = EventEngine()
    main_engine = MainEngine(event_engine)
    main_engine.add_app(CtaBacktesterApp)
    widget = BacktesterManager(main_engine, event_engine)
    try:
        label = _notice(widget)
        assert len(label.text().splitlines()) <= 2, "标签超过两行就会被布局截断"
        assert SEGMENT_CLI in label.text()
        assert "entry_window" not in label.text()
        assert "TRAIN" in label.toolTip(), "三段边界不能就这么丢了"
        assert "entry_window" in label.toolTip(), "逐次查看的明细不能就这么丢了"
    finally:
        widget.deleteLater()
        main_engine.close()


def test_notice_is_not_truncated_at_a_modest_window_size(
    ledger: Path, fluent_look
) -> None:
    """真面板上量出来的那次失败：标签要的高度（253px）大于布局给它的（184px），
    于是被切在一个参数字典中间，最后那句"会被拒绝"整行消失。

    这里按真实渲染判：`heightForWidth`（换行标签唯一靠得住的高度）不许超过
    布局分配的高度。1400x900 是一个偏小但完全正常的窗口尺寸。
    """
    _spent_ledger(ledger)

    _qapp()
    event_engine = EventEngine()
    main_engine = MainEngine(event_engine)
    main_engine.add_app(CtaBacktesterApp)
    widget = BacktesterManager(main_engine, event_engine)
    try:
        widget.show()
        _qapp().processEvents()
        widget.setGeometry(0, 0, 1400, 900)
        _qapp().processEvents()

        label = _notice(widget)
        needed = label.heightForWidth(label.width())
        assert needed <= label.height(), (
            f"提示被截断：需要 {needed}px，只拿到 {label.height()}px"
        )
    finally:
        widget.hide()
        widget.deleteLater()
        main_engine.close()


def test_notice_survives_a_corrupt_ledger(ledger: Path) -> None:
    """写坏的研究文件不该让交易终端起不来。"""
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text("{ not json at all", encoding="utf-8")

    _qapp()
    event_engine = EventEngine()
    main_engine = MainEngine(event_engine)
    main_engine.add_app(CtaBacktesterApp)
    widget = BacktesterManager(main_engine, event_engine)
    try:
        assert SEGMENT_CLI in _notice(widget).text()
    finally:
        widget.deleteLater()
        main_engine.close()


# ══════════════════════════════════════════════════════════════════════
# 3. 拦住"在测试段上扫参数"
# ══════════════════════════════════════════════════════════════════════

def test_optimization_over_the_test_window_is_refused(
    ledger: Path, panel, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_ledger(ledger)
    split = _split()
    _set_window(panel, split.train_start, split.test_end)

    spy = _Spy()
    monkeypatch.setattr(panel.backtester_engine, "start_optimization", spy)
    boxes: list[str] = []
    monkeypatch.setattr(
        QtWidgets.QMessageBox, "warning",
        lambda *args, **kwargs: boxes.append(str(args[2])),
    )

    panel.start_optimization()

    assert spy.calls == [], "窗口碰到 TEST 段还是把寻优跑起来了"
    assert boxes, "拒绝要让用户看见，不能只写进日志"
    assert "TEST" in boxes[0]
    assert SEGMENT_CLI in panel.log_monitor.toPlainText()

    # 实测（offscreen 抓到的 QMessageBox）：windowTitle() 是空的 —— macOS 按
    # 平台规范忽略 QMessageBox 的标题。所以"这是一次拒绝"必须落在正文第一行，
    # 靠标题传达等于在 macOS 上没传达。
    assert boxes[0].splitlines()[0].startswith("已拒绝"), (
        "拒绝的措辞不能只放在标题里 —— macOS 会把 QMessageBox 标题丢掉"
    )


def test_optimization_inside_the_training_window_is_untouched(
    ledger: Path, panel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """样本内照常跑 —— 这道闸只拦一件事，不许顺手把别的也拦了。"""
    _write_ledger(ledger)
    split = _split()
    _set_window(panel, split.train_start, split.train_end)

    spy = _Spy()
    monkeypatch.setattr(panel.backtester_engine, "start_optimization", spy)
    monkeypatch.setattr(
        QtWidgets.QMessageBox, "warning", lambda *a, **k: pytest.fail("不该弹窗")
    )
    # 上游会先弹参数设置对话框，直接当成"取消"，本例只关心闸放不放行
    from vnpy_ctabacktester.ui.widget import OptimizationSettingEditor

    monkeypatch.setattr(
        OptimizationSettingEditor, "exec",
        lambda self: int(QtWidgets.QDialog.DialogCode.Rejected),
    )
    panel.start_optimization()   # 未被本闸拦下（对话框取消后自然返回）


def test_no_ledger_means_no_guard(
    ledger: Path, panel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """没跑过命令行就没有切分可言，这时不该凭空拦人。"""
    assert not ledger.exists()
    _set_window(panel, datetime(2024, 1, 2), datetime(2026, 1, 1))

    monkeypatch.setattr(
        QtWidgets.QMessageBox, "warning", lambda *a, **k: pytest.fail("不该弹窗")
    )
    from vnpy_ctabacktester.ui.widget import OptimizationSettingEditor

    monkeypatch.setattr(
        OptimizationSettingEditor, "exec",
        lambda self: int(QtWidgets.QDialog.DialogCode.Rejected),
    )
    panel.start_optimization()


# ══════════════════════════════════════════════════════════════════════
# 4. 单次回测：不拦，但要说清楚这一次动了样本外数据
# ══════════════════════════════════════════════════════════════════════

def test_single_backtest_over_test_is_allowed_but_logged(
    ledger: Path, panel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """对【已选定的一组参数】在 TEST 段跑一次是合法动作（`run(setting, TEST)`），
    所以不拦；但 GUI 数不了预算，必须说明这一次没进账本。"""
    _write_ledger(ledger)
    split = _split()
    _set_window(panel, split.test_start, split.test_end)

    from vnpy_ctabacktester.ui.widget import BacktestingSettingEditor

    monkeypatch.setattr(
        BacktestingSettingEditor, "exec",
        lambda self: int(QtWidgets.QDialog.DialogCode.Rejected),
    )
    panel.start_backtesting()

    log = panel.log_monitor.toPlainText()
    assert "TEST" in log
    assert "账本" in log, "必须讲明这次查看没有被记进账本"
