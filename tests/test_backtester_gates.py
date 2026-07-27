"""寻优多重比较闸（DSR + PBO）在回测器 GUI 里的呈现。

复现的问题：`vnpy_ctastrategy` 已经在寻优跑完后算了 DSR 与 PBO 并挂在返回值的
`.gates` 上，但 `vnpy_ctabacktester` 的界面只渲染"参数 / 目标值"两列并按目标值降序 ——
用户看到第一行最漂亮就照着用，而那一行恰恰是 N 组试验里运气最大的那组。
安装前实测：DSR=0.181「挑出来的，不可采信」、PBO=0.459「与选参毫无信息不可区分」，
这两条在日志区与结果对话框里都搜不到。

这里的断言全部打在【Qt widget 的真实可见内容】上（QTextEdit 的 toPlainText、
QLabel 的 text、QTableWidgetItem 的背景色），不是打在格式化函数的返回值上。
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from datetime import date, timedelta
from pathlib import Path

# Qt 必须在任何 PySide6 导入之前切到 offscreen。
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("LANGUAGE", "zh_CN")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402
import pytest  # noqa: E402
from PySide6 import QtGui, QtWidgets  # noqa: E402
from vnpy.event import Event, EventEngine  # noqa: E402
from vnpy.trader.engine import MainEngine  # noqa: E402
from vnpy_ctabacktester import CtaBacktesterApp  # noqa: E402
from vnpy_ctabacktester.ui.widget import (  # noqa: E402
    BacktesterManager,
    OptimizationResultMonitor,
)
from vnpy_ctastrategy.optimization_gates import (  # noqa: E402
    OptimizationGateConfig,
    OptimizationGateReport,
    OptimizationResults,
    run_optimization_gates,
)

from fluent_ui.backtester_gates import (  # noqa: E402
    BANNER_OBJECT_NAME,
    _decorate_result_dialog,
    install_gate_verdict,
    summarize,
)

CAPITAL = 1_000_000.0
N_CONFIGS = 8
N_DAYS = 250


@pytest.fixture(scope="module", autouse=True)
def _qapp() -> QtWidgets.QApplication:
    """整个模块共用一个 QApplication —— Qt 不允许一个进程建第二个。"""
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    assert isinstance(app, QtWidgets.QApplication)
    return app


@pytest.fixture(scope="module", autouse=True)
def _installed() -> None:
    """所有 GUI 断言都在装好注入之后跑。"""
    install_gate_verdict()


def _grid(edge: float, seed: int) -> OptimizationResults:
    """跑一次真的闸：合成 N 组参数的逐日盈亏 → run_optimization_gates。

    edge=0 每组都是纯噪音 → 两道闸都该拦下；edge>0 时第一组带真实漂移 → 应通过。
    报告是 `run_optimization_gates` 真产出的对象，不是手搓的替身。
    """
    rng = np.random.default_rng(seed)
    dates = [date(2024, 1, 1) + timedelta(days=i) for i in range(N_DAYS)]

    results: list[tuple] = []
    payloads: list[tuple] = []
    for j in range(N_CONFIGS):
        pnl = rng.normal(edge if j == 0 else 0.0, 4000.0, N_DAYS)
        payloads.append((dates, pnl))
        sharpe = float(pnl.mean() / pnl.std() * np.sqrt(240))
        results.append((
            {"fast_window": 10 + j},
            sharpe,
            {"sharpe_ratio": sharpe, "total_days": N_DAYS, "total_trade_count": 60},
        ))

    order = sorted(range(N_CONFIGS), key=lambda i: -results[i][1])
    report = run_optimization_gates(
        [results[i] for i in order],
        target_name="sharpe_ratio",
        annual_days=240,
        capital=CAPITAL,
        payloads=[payloads[i] for i in order],
        config=OptimizationGateConfig(n_null_sims=40, n_offsets=2),
    )
    return OptimizationResults([results[i] for i in order], gates=report)


@pytest.fixture(scope="module")
def failing() -> OptimizationResults:
    results = _grid(edge=0.0, seed=7)
    assert results.gates is not None and not results.gates.passed
    return results


@pytest.fixture(scope="module")
def passing() -> OptimizationResults:
    results = _grid(edge=3000.0, seed=7)
    assert results.gates is not None and results.gates.passed
    return results


def _banner_text(dialog: QtWidgets.QDialog) -> str:
    """横幅上【渲染后】的文字。

    QLabel.text() 拿到的是我们塞进去的 HTML 源码（判词里的 `<` 已被转义成
    `&lt;`），断言打在它上面等于断言实现细节。用 QTextDocument 渲染一遍，
    得到的才是用户眼睛看到的那串字。
    """
    banner = dialog.findChild(QtWidgets.QLabel, BANNER_OBJECT_NAME)
    assert banner is not None, "结果对话框里没有闸横幅"
    document = QtGui.QTextDocument()
    document.setHtml(banner.text())
    return document.toPlainText()


# ── 结果对话框（用户挑参数时正看着的地方）────────────────────────


def test_dialog_shows_the_failing_verdict(failing: OptimizationResults) -> None:
    """未通过时，DSR 与 PBO 的判词必须出现在对话框顶部横幅里。"""
    dialog = OptimizationResultMonitor(failing, "夏普比率")
    text = _banner_text(dialog)

    assert "未通过" in text
    assert "DSR=" in text and "PBO=" in text
    assert failing.gates is not None
    # 判词逐字取自报告，不在 GUI 层另写一套判据。
    assert failing.gates.dsr_verdict in text


def test_dialog_marks_the_row_the_user_would_copy(failing: OptimizationResults) -> None:
    """第一行 = 目标值最高、最可能被照抄的那组。未通过时它必须被标出来。"""
    dialog = OptimizationResultMonitor(failing, "夏普比率")
    table = dialog.findChild(QtWidgets.QTableWidget)
    assert table is not None

    top = table.item(0, 0)
    second = table.item(1, 0)
    assert top is not None and second is not None
    assert top.background().color() != second.background().color(), "首行未被标出"
    assert "运气" in top.toolTip()


def test_dialog_window_title_carries_the_verdict(failing: OptimizationResults) -> None:
    """标题栏也带结论 —— 对话框被缩到一边时仍看得见。"""
    dialog = OptimizationResultMonitor(failing, "夏普比率")
    assert "未通过" in dialog.windowTitle()


def test_passing_banner_refuses_to_read_as_an_endorsement(
    passing: OptimizationResults,
) -> None:
    """通过 ≠ 背书。绿色横幅必须自己说清这一点。"""
    dialog = OptimizationResultMonitor(passing, "夏普比率")
    text = _banner_text(dialog)

    assert "通过" in text and "未通过" not in text
    assert "不是背书工具" in text
    assert "Walk-Forward" in text


def test_passing_grid_leaves_the_top_row_unmarked(passing: OptimizationResults) -> None:
    dialog = OptimizationResultMonitor(passing, "夏普比率")
    table = dialog.findChild(QtWidgets.QTableWidget)
    assert table is not None
    top, second = table.item(0, 0), table.item(1, 0)
    assert top is not None and second is not None
    assert top.background().color() == second.background().color()


def test_missing_gates_is_amber_not_blank(failing: OptimizationResults) -> None:
    """没跑闸的结果集（普通 list）不能显示成"没问题"—— 缺证据不是证据。"""
    plain = list(failing)                       # 丢掉 .gates，退回上游形状
    dialog = OptimizationResultMonitor(plain, "夏普比率")
    text = _banner_text(dialog)

    assert "未运行" in text
    assert "选择偏差" in text
    assert "未通过" not in text                 # 别把"没算"说成"算了没过"


def test_broken_report_reports_the_error_instead_of_going_green() -> None:
    """报告结构漂移（字段改名/删了）时必须显性报错，绝不因为读不出来就当通过。

    模拟的是真实的漂移形态：`.gates` 还在、报告对象也在，只是某个字段没了。
    """

    class DriftedReport:
        passed = True                       # 就算它自称通过，读不出判词也不许放行
        target_name = "sharpe_ratio"
        n_results = 8
        source = "bf"
        matrix_shape = None
        notes: list[str] = []

        @property
        def dsr_verdict(self) -> str:
            raise AttributeError("dsr_verdict 在新版里改名了")

    results = OptimizationResults([({"a": 1}, 1.0, {})])
    results.gates = DriftedReport()         # type: ignore[assignment]

    dialog = OptimizationResultMonitor(results, "夏普比率")
    text = _banner_text(dialog)
    assert "读取失败" in text
    assert "AttributeError" in text
    assert "不要采用" in text


# ── 寻优结束的日志块（不点任何按钮就能看到）──────────────────────


@pytest.fixture(scope="module")
def manager() -> Iterator[BacktesterManager]:
    """真的回测器面板：真 MainEngine + 真 BacktesterEngine + 真 QTextEdit 日志区。

    收尾必须停掉 MainEngine 与 EventEngine —— 它们各起了非 daemon 线程，
    不停的话进程跑完测试也退不出去（实测：pytest 挂死不返回）。
    """
    event_engine = EventEngine()
    main_engine = MainEngine(event_engine)
    main_engine.add_app(CtaBacktesterApp)
    widget = BacktesterManager(main_engine, event_engine)
    widget.target_display = "夏普比率"
    yield widget
    main_engine.close()


def _finish(widget: BacktesterManager, results: object) -> str:
    widget.log_monitor.clear()
    widget.backtester_engine.result_values = results        # type: ignore[assignment]
    widget.process_optimization_finished_event(
        Event("eBacktesterOptimizationFinished")
    )
    return widget.log_monitor.toPlainText()


def test_log_carries_the_verdict_without_any_click(
    manager: BacktesterManager, failing: OptimizationResults
) -> None:
    """寻优一结束，裁决就进日志区 —— 用户不需要先去点[优化结果]。"""
    text = _finish(manager, failing)

    assert "未通过" in text
    assert "DSR=" in text and "PBO=" in text
    assert "⛔" in text
    # 上游那句提示仍在，说明是"追加"不是"替换"。
    assert "优化结果" in text


def test_log_keeps_upstream_behaviour_and_enables_the_button(
    manager: BacktesterManager, failing: OptimizationResults
) -> None:
    manager.result_button.setEnabled(False)
    _finish(manager, failing)
    assert manager.result_button.isEnabled(), "上游原行为被包装吃掉了"


def test_log_says_pass_when_the_grid_passes(
    manager: BacktesterManager, passing: OptimizationResults
) -> None:
    text = _finish(manager, passing)
    assert "✅" in text and "⛔" not in text


def test_log_flags_a_run_with_no_gate_report(
    manager: BacktesterManager, failing: OptimizationResults
) -> None:
    text = _finish(manager, list(failing))
    assert "未运行" in text


# ── 纯逻辑（判词拼装）─────────────────────────────────────────────


def test_uncalibrated_pbo_gets_its_point_estimate_printed() -> None:
    """没跑零分布时判词只有定性四档，数值必须由 GUI 层补上。"""
    results = _grid(edge=0.0, seed=11)
    assert results.gates is not None
    report = OptimizationGateReport(
        dsr=results.gates.dsr,
        dsr_detail=results.gates.dsr_detail,
        pbo=results.gates.pbo,
        target_name=results.gates.target_name,
        annual_days=results.gates.annual_days,
        n_results=results.gates.n_results,
        matrix_shape=results.gates.matrix_shape,
    )
    assert report.pbo is not None
    object.__setattr__(report.pbo, "null", None)            # 模拟 n_null_sims=0

    verdict = summarize(report)
    joined = "\n".join(verdict.lines)
    assert f"PBO={report.pbo.result.pbo:.3f}" in joined
    assert "未校准" in joined


def test_install_is_idempotent() -> None:
    assert install_gate_verdict() == [], "重复安装不应再包一层"


def test_banner_survives_a_layout_the_upstream_might_switch_to(
    failing: OptimizationResults,
) -> None:
    """上游哪天把 QVBoxLayout 换掉，横幅可以位置不对，但不许安静消失 ——
    "闸算了却没上屏"正是本模块要治的病，不能由一次上游改版复发。"""
    dialog = QtWidgets.QDialog()
    dialog.setLayout(QtWidgets.QGridLayout())
    dialog.result_values = failing                          # type: ignore[attr-defined]

    _decorate_result_dialog(dialog)

    assert dialog.findChild(QtWidgets.QLabel, BANNER_OBJECT_NAME) is not None
    assert "未通过" in dialog.windowTitle()
