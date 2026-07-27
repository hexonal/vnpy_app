"""端到端验证：真起回测器 GUI，跑一次真的 25 组网格寻优，看闸的裁决有没有上屏。

    QT_QPA_PLATFORM=offscreen ../vnpy/.venv/bin/python tests/e2e_backtester_gates_gui.py

走的是完整生产链路，没有替身：

    BacktesterManager.start_optimization（[参数优化]按钮调的那个方法）
      → BacktesterEngine.start_optimization（起后台线程）
      → BacktestingEngine.run_bf_optimization（spawn 多进程 + QuestDB 真 K 线）
      → optimization_gates.run_optimization_gates（真 DSR + 真 CSCV/PBO + 200 次零分布）
      → EVENT_BACKTESTER_LOG / EVENT_BACKTESTER_OPTIMIZATION_FINISHED
      → GUI 日志区（QTextEdit）+ 优化结果对话框（QDialog）

唯一被替换的是模态参数配置框：`QDialog.exec()` 会阻塞等人点按钮，这里用桩把网格
填好再 accept —— 替掉的是"人点鼠标"，不是被验证的任何一段代码。

━━━ 为什么它不是 pytest 用例 ━━━

两个硬约束，都不是"懒得写"：

1. **spawn 子进程**。vnpy 的 `run_bf_optimization` 固定用
   `ProcessPoolExecutor(mp_context=get_context("spawn"))`。spawn 会在子进程里重建
   `__main__`：用 `python -m pytest` 跑时 `__main__.__spec__.name == "pytest.__main__"`，
   multiprocessing 的 `_fixup_main_from_name` 见到以 `.__main__` 结尾就跳过，没事；
   但用 `pytest` 这个 console script 跑时 `__spec__` 是 None，走
   `_fixup_main_from_path` → `runpy.run_path(.../bin/pytest)` → **每个子进程重跑一遍
   整个测试套件**，且会继续往下 spawn。把它放进套件等于给"换一种方式启动 pytest"
   埋一颗指数级 fork 炸弹。
2. **依赖 QuestDB 里真有那段 K 线**。缺数据时它该报"没数据"，而不是变成一个
   常年 skip、谁也不看的绿点。

所以：GUI 呈现逻辑的自动化断言在 `tests/test_backtester_gates.py`（真 widget、真
QTextEdit、真 QTableWidgetItem 背景色，13 个用例，随套件跑）；本脚本是它上游那半段
——"真寻优跑完之后，裁决确实沿着事件链走到了界面上"——的手动验收，改动这条链路
（注入点、事件、寻优返回值形状）后手跑一次。
"""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

# Qt 必须在任何 PySide6 导入之前切到 offscreen。
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("LANGUAGE", "zh_CN")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6 import QtCore, QtGui, QtWidgets  # noqa: E402
from vnpy.event import EventEngine  # noqa: E402
from vnpy.trader.engine import MainEngine  # noqa: E402
from vnpy_ctabacktester import CtaBacktesterApp  # noqa: E402
from vnpy_ctabacktester.ui.widget import (  # noqa: E402
    BacktesterManager,
    OptimizationResultMonitor,
    OptimizationSettingEditor,
)

from fluent_ui.backtester_gates import (  # noqa: E402
    BANNER_OBJECT_NAME,
    install_gate_verdict,
)

SYMBOL = "700.SEHK"
STRATEGY = "DoubleMaStrategy"
START, END = QtCore.QDate(2023, 7, 24), QtCore.QDate(2026, 7, 20)

#: 参数名 -> (起, 步进, 止)。5 × 5 = 25 组，正是"扫 25 组挑第一行"那个场景。
GRID: dict[str, tuple[int, int, int]] = {
    "fast_window": (5, 5, 25),
    "slow_window": (20, 10, 60),
}


def _stub_setting_editor() -> None:
    """把模态参数配置框换成"填好网格直接 accept"（替掉的是人点鼠标）。"""

    def exec_stub(self: OptimizationSettingEditor) -> int:
        self.target_combo.setCurrentText("夏普比率")
        self.worker_spin.setValue(4)
        for name, (start, step, end) in GRID.items():
            edits = self.edits[name]
            edits["start"].setText(str(start))
            edits["step"].setText(str(step))
            edits["end"].setText(str(end))
        self.generate_parallel_setting()          # 内部会 accept()
        return int(QtWidgets.QDialog.DialogCode.Accepted)

    OptimizationSettingEditor.exec = exec_stub    # type: ignore[method-assign]


def _capture_result_dialog() -> list[OptimizationResultMonitor]:
    """把结果对话框的 exec_ 换成"记下自己就返回"，好让脚本检查它的内容。"""
    captured: list[OptimizationResultMonitor] = []

    def exec_stub(self: OptimizationResultMonitor) -> int:
        captured.append(self)
        return 0

    OptimizationResultMonitor.exec_ = exec_stub   # type: ignore[method-assign]
    return captured


def _pump(
    app: QtWidgets.QApplication, predicate: Callable[[], bool], timeout: float
) -> bool:
    """跑 Qt 事件循环直到条件成立 —— 寻优在后台线程，事件要泵才会送到界面。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        app.processEvents()
        if predicate():
            app.processEvents()
            return True
        time.sleep(0.05)
    return False


def _rendered(label: QtWidgets.QLabel | None) -> str:
    """横幅上渲染后的文字（QLabel.text() 拿到的是 HTML 源码）。"""
    if label is None:
        return ""
    document = QtGui.QTextDocument()
    document.setHtml(label.text())
    return document.toPlainText()


def main() -> int:
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    assert isinstance(app, QtWidgets.QApplication)

    print(f"[装]  运行时注入: {install_gate_verdict()}")
    _stub_setting_editor()
    captured = _capture_result_dialog()

    event_engine = EventEngine()
    main_engine = MainEngine(event_engine)
    main_engine.add_app(CtaBacktesterApp)
    manager = BacktesterManager(main_engine, event_engine)

    manager.class_combo.setCurrentText(STRATEGY)
    manager.symbol_line.setText(SYMBOL)
    manager.interval_combo.setCurrentText("d")
    manager.start_date_edit.setDate(START)
    manager.end_date_edit.setDate(END)
    manager.rate_line.setText("0.0005")
    manager.slippage_line.setText("0.2")
    manager.size_line.setText("1")
    manager.pricetick_line.setText("0.2")
    manager.capital_line.setText("1000000")

    n_grid = 1
    for start, step, end in GRID.values():
        n_grid *= len(range(start, end + 1, step))
    print(f"[跑]  {SYMBOL} 日线 · {STRATEGY} · {n_grid} 组参数 · 目标=夏普比率")

    started = datetime.now()
    manager.start_optimization()
    done = _pump(app, lambda: manager.result_button.isEnabled(), timeout=600)
    print(f"[跑]  用时 {(datetime.now() - started).total_seconds():.1f}s，完成={done}")
    if not done:
        print("[错]  寻优没在 600 秒内结束（QuestDB 里有 700.SEHK 日线吗？）")
        main_engine.close()
        return 2

    results = manager.backtester_engine.get_result_values()
    report = getattr(results, "gates", None)
    if report is None:
        print("[错]  寻优返回值上没有 .gates —— 闸根本没跑，本脚本无从验收")
        main_engine.close()
        return 2

    print(f"[闸]  N={len(results)} passed={report.passed}")
    print(f"      {report.dsr_verdict}")
    print(f"      {report.pbo_verdict}")

    log_text = manager.log_monitor.toPlainText()
    print("\n─────────── GUI 日志区（尾部 12 行）───────────")
    print("\n".join(log_text.splitlines()[-12:]))

    manager.show_optimization_result()
    dialog = captured[-1] if captured else None
    banner = (
        dialog.findChild(QtWidgets.QLabel, BANNER_OBJECT_NAME) if dialog else None
    )
    table = dialog.findChild(QtWidgets.QTableWidget) if dialog else None
    top = table.item(0, 0) if table else None

    print("\n─────────── 优化结果对话框 ───────────")
    print(f"标题: {dialog.windowTitle() if dialog else '(没打开)'}")
    print(f"横幅:\n{_rendered(banner) or '(没有横幅)'}")
    print(
        f"首行: {top.text() if top else '?'}"
        f" / 底色 {top.background().color().name() if top else '?'}"
        f" / 提示 {(top.toolTip()[:24] + '…') if top and top.toolTip() else '(无)'}"
    )

    word = "通过" if report.passed else "未通过"
    checks = {
        "日志区含裁决块": word in log_text and "寻优多重比较闸" in log_text,
        "日志区含 DSR 数值": "DSR=" in log_text,
        "日志区含 PBO 数值": "PBO=" in log_text,
        "对话框有横幅": banner is not None,
        "横幅含裁决": word in _rendered(banner),
        "标题栏含裁决": dialog is not None and word in dialog.windowTitle(),
        "未通过时首行被标记": bool(top and top.toolTip()) is not report.passed,
    }
    print("\n─────────── 断言 ───────────")
    for name, ok in checks.items():
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")

    main_engine.close()
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
