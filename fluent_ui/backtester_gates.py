"""把参数寻优的多重比较闸（DSR + PBO）的裁决摆到用户眼前。

━━━ 为什么需要这一层 ━━━

`vnpy_ctastrategy/optimization_gates.py` 已经在寻优跑完后算了两道闸，并把报告挂在
返回值的 `.gates` 上（`OptimizationResults` 是 list 子类，元素形状逐字不变）。
但 `vnpy_ctabacktester` 的界面只认那个 list：

* `BacktesterManager.process_optimization_finished_event` 只写一行"请点击[优化结果]
  按钮查看"；
* `OptimizationResultMonitor` 只渲染两列（参数 / 目标值），按目标值降序排。

于是用户看到的是一张排好序的表，第一行的目标值最漂亮 —— 而这一行恰恰是 N 组试验里
运气分量最大的那组。DSR=0.317（扣掉"试了 N 组"后完全不显著）、PBO=0.64（样本内最优
在样本外多半掉进后半段）这两个数如果不摆在他面前，整道闸就等于白装：算了、耗了时间，
结论进了一个没人读的地方。

本模块在【我们自己的 GUI 装配阶段】把裁决接到两个位置：

1. **寻优结束的日志块** —— 不用点任何按钮就能看到。`BacktesterEngine.write_log` 把
   消息发成 `EVENT_BACKTESTER_LOG` 事件，最终 append 进面板中间那块 QTextEdit。
2. **优化结果对话框顶部的横幅** —— 用户挑参数时正看着的地方；未通过时红底，并把
   第一行（他最可能照抄的那组）标红加提示。

━━━ 为什么不是"加一列" ━━━

DSR 与 PBO 都是【整个网格】的属性，不是逐组的属性：DSR 校正的是"你试了 N 组"这件事，
PBO 测的是"从这个网格里挑第一名"这个动作在样本外还剩多少信息。给每行加一列等于暗示
"这组的 DSR 是多少"，那是对两个统计量的误读。所以裁决只能是横幅（网格级），
逐行能做的只有把"你要抄的这一行"标出来。

━━━ 实现方式：运行时注入，不碰第三方源码 ━━━

`vnpy_ctabacktester` 不是本仓的包（装在 site-packages，不在任何 git 里，改了下次升级
就没了）。和 `backtester_metrics.py` 补 `KEY_NAME_MAP` 同一个路子：包装两个方法，
幂等，已装过就不再装，导入失败静默跳过（缺一个可选 App 不该让 GUI 起不来）。

**安装时机**：`BacktesterManager.__init__` 里 `register_event()` 把
`self.process_optimization_finished_event` 连到信号上，绑定发生在连接那一刻。所以
`install_gate_verdict()` 必须在**创建主窗口之前**调用（`run_gui.main()` 里就是这个
顺序），否则已建好的实例仍连着未包装的旧函数。

━━━ 一条诚实边界 ━━━

`passed=True` 不是背书。两道闸都是否决工具：没被拦下 ≠ 样本外会赚钱。横幅在通过时
也会把这句话印出来，免得绿色被读成"可以上了"。同理，闸没跑（`.gates` 是 None）时
显示的是琥珀色警告而不是留白 —— 缺证据不是证据。
"""

from __future__ import annotations

import functools
import html
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from PySide6 import QtCore, QtGui, QtWidgets

if TYPE_CHECKING:
    from vnpy_ctastrategy.optimization_gates import OptimizationGateReport


#: 横幅 QLabel 的 objectName —— 测试与后续代码用它定位，不靠遍历文本。
BANNER_OBJECT_NAME = "optimization_gate_banner"

#: 未通过时第一行（用户最可能照抄的那组）的底色与提示。
_ALARM_ROW_BG = "#8e2222"
_ALARM_ROW_FG = "#ffffff"
_ALARM_ROW_TIP = (
    "这一行是目标值最高的那组。多重比较闸未通过 —— 它很可能只是 N 组试验里"
    "运气最大的那组，不是最好的那组。"
)

#: 四种状态各自的配色（底色, 字色）。只有 'pass' 是绿的 —— 没跑闸 / 读不出结论
#: 一律走琥珀色，绝不让"没有结论"看起来像"没有问题"。
_STATUS_COLORS: dict[str, tuple[str, str]] = {
    "pass": ("#1b5e20", "#ffffff"),
    "fail": ("#b71c1c", "#ffffff"),
    "absent": ("#e65100", "#ffffff"),
    "error": ("#e65100", "#ffffff"),
}


@dataclass(frozen=True)
class GateVerdict:
    """一次寻优的闸结论，压缩成可直接上屏的形状。

    status  'pass' 两道闸都明确通过 / 'fail' 至少一道没过 / 'absent' 根本没跑闸 /
            'error' 报告对象与预期不符（版本漂移），如实报错不假装通过。
    """

    status: str
    headline: str
    lines: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def alarming(self) -> bool:
        """是否要用醒目配色。**没跑闸也算醒目** —— 缺证据不是证据。"""
        return self.status != "pass"

    def log_block(self) -> str:
        """写进 GUI 日志区的一整块（一次 write_log，时间戳只落在首行）。"""
        rule = "═" * 60
        parts = [rule, self.headline]
        parts.extend(f"  {line}" for line in self.lines)
        parts.extend(f"  备注：{note}" for note in self.notes)
        parts.append(rule)
        return "\n".join(parts)

    def banner_html(self) -> str:
        """横幅用的富文本。所有动态文本都转义，报告里的尖括号不会吃掉后面的字。"""
        rows = [
            f'<div style="font-size:15px;font-weight:700;">'
            f"{html.escape(self.headline)}</div>"
        ]
        body = [html.escape(line) for line in self.lines]
        body.extend(f"备注：{html.escape(note)}" for note in self.notes)
        if body:
            rows.append(
                '<div style="font-size:12px;margin-top:4px;">'
                + "<br>".join(body)
                + "</div>"
            )
        return "".join(rows)


def _context_line(report: OptimizationGateReport) -> str:
    """网格规模一行 —— DSR 的 N 与 PBO 的矩阵形状都靠它落地。"""
    source = "穷举" if report.source == "bf" else "遗传"
    text = f"目标 {report.target_name} · {report.n_results} 组参数（{source}）"
    shape = report.matrix_shape
    if shape is not None:
        text += f" · 收益矩阵 {shape[0]}×{shape[1]}"
    return text


def _pbo_line(report: OptimizationGateReport) -> str:
    """PBO 判词。

    校准过的判词（跑了零分布）里已经带着 PBO 数值，原样用；没跑零分布时判词只有
    四档定性文字，这里把点估计补在前面。判定口径一律取 `report.pbo_verdict`，
    不在这里另写一套 —— 同一份报告出现两个互相矛盾的结论，正是那边反复警告的事。
    """
    study = report.pbo
    verdict = report.pbo_verdict
    if study is not None and study.null is None:
        return f"PBO={study.result.pbo:.3f} —— {verdict}"
    return verdict


def summarize(report: OptimizationGateReport | None) -> GateVerdict:
    """把闸报告压成一条可上屏的裁决。`report is None` = 这次寻优没跑闸。"""
    if report is None:
        return GateVerdict(
            status="absent",
            headline="⚠ 寻优多重比较闸 未运行 —— 这张表没有 DSR / PBO 结论",
            lines=[
                "这份排序没做多重比较校正：N 组里最好的那组必然带着 N 次试验中"
                "最大的运气分量，它的夏普与 t 值全部被选择偏差污染。",
                "要拿到裁决，用 run_bf_optimization(gates=True, collect_returns=True) 重跑。",
            ],
        )

    lines = [_context_line(report), report.dsr_verdict, _pbo_line(report)]
    notes = list(report.notes[:2])

    if report.passed:
        lines.append(
            "两道闸是否决工具，不是背书工具：没被拦下 ≠ 样本外会赚钱，"
            "上生产前仍需 Walk-Forward 复核。"
        )
        return GateVerdict(
            status="pass",
            headline="✅ 寻优多重比较闸 通过（两道闸都没拦下这个网格）",
            lines=lines,
            notes=notes,
        )

    lines.append(
        "低功效样本（单标的几百根 K、几十笔交易）下这也可能是"
        "「数据不足以支持从这个网格里挑参数」—— 结论不同，处置相同：别用挑出来的那组。"
    )
    return GateVerdict(
        status="fail",
        headline="⛔ 寻优多重比较闸 未通过 —— 目标值最高的那组不可直接采用",
        lines=lines,
        notes=notes,
    )


def _verdict_of(result_values: Any) -> GateVerdict:
    """从寻优返回值里取裁决。取不到 / 结构不符时报错，绝不当成通过。

    `.gates` 在 `OptimizationResults.__init__` 里是普通实例属性，所以"取不到"
    只有一种含义：这是上游形状的普通 list，压根没跑闸 → 'absent'。真正的版本
    漂移（报告还在、字段改了名）会在 `summarize` 读字段时抛出来 → 'error'。
    """
    try:
        return summarize(getattr(result_values, "gates", None))
    except Exception as exc:  # noqa: BLE001 — 版本漂移要看得见，不能静默变绿灯
        return GateVerdict(
            status="error",
            headline="⚠ 寻优多重比较闸 结论读取失败 —— 按「没有结论」处理",
            lines=[
                f"{type(exc).__name__}: {exc}",
                "闸可能已跑，但报告结构与本 GUI 适配层不符（版本漂移）。"
                "在拿到可读结论前，不要采用寻优挑出来的参数。",
            ],
        )


# ── 注入点一：寻优结束时往 GUI 日志区打裁决 ──────────────────────────


def _log_verdict(manager: Any) -> None:
    """寻优结束事件的附加动作：把裁决写进面板中间那块日志。

    走 `manager.write_log`（→ QTextEdit.append），不是 `BacktestingEngine.write_log`
    —— 后者只把消息塞进引擎自己的 `self.logs` 列表，界面上永远看不到。
    """
    verdict = _verdict_of(manager.backtester_engine.get_result_values())
    manager.write_log(verdict.log_block())
    if verdict.alarming:
        manager.write_log("点[优化结果]按钮，顶部横幅是同一条结论")


# ── 注入点二：优化结果对话框顶部横幅 + 首行标红 ────────────────────


def _find_table(dialog: QtWidgets.QDialog) -> QtWidgets.QTableWidget | None:
    """结果表。按布局项找而不是 findChild —— 只认上游那张顶层表，
    不会连带匹配到别处（比如我们自己以后往对话框里加的东西）里的表。"""
    layout = dialog.layout()
    if layout is None:
        return None
    for i in range(layout.count()):
        item = layout.itemAt(i)
        widget = item.widget() if item is not None else None
        if isinstance(widget, QtWidgets.QTableWidget):
            return widget
    return None


def _mark_top_row(table: QtWidgets.QTableWidget) -> None:
    """把第一行（目标值最高、也最可能被照抄的那组）标出来。"""
    if table.rowCount() == 0:
        return
    background = QtGui.QBrush(QtGui.QColor(_ALARM_ROW_BG))
    foreground = QtGui.QBrush(QtGui.QColor(_ALARM_ROW_FG))
    for column in range(table.columnCount()):
        item = table.item(0, column)
        if item is None:
            continue
        item.setBackground(background)
        item.setForeground(foreground)
        item.setToolTip(_ALARM_ROW_TIP)


def _make_banner(verdict: GateVerdict) -> QtWidgets.QLabel:
    background, color = _STATUS_COLORS.get(verdict.status, _STATUS_COLORS["error"])
    banner = QtWidgets.QLabel()
    banner.setObjectName(BANNER_OBJECT_NAME)
    banner.setTextFormat(QtCore.Qt.TextFormat.RichText)
    banner.setText(verdict.banner_html())
    banner.setWordWrap(True)
    banner.setStyleSheet(
        f"QLabel#{BANNER_OBJECT_NAME} {{"
        f" background-color: {background};"
        f" color: {color};"
        f" padding: 10px;"
        f" border-radius: 6px;"
        f" }}"
    )
    return banner


def _decorate_result_dialog(dialog: QtWidgets.QDialog) -> None:
    """在上游建完 UI 之后追加横幅，并在未通过时标红首行。"""
    verdict = _verdict_of(getattr(dialog, "result_values", None))

    table = _find_table(dialog)
    if table is not None and verdict.alarming:
        _mark_top_row(table)

    # 横幅宁可位置不对，也不许消失 —— "闸算了但没上屏"正是这个模块要治的病，
    # 上游哪天把 QVBoxLayout 换成别的，不能让它安静地退回原样。
    banner = _make_banner(verdict)
    layout = dialog.layout()
    if isinstance(layout, QtWidgets.QBoxLayout):
        layout.insertWidget(0, banner)          # 正常路径：置顶
    elif layout is not None:
        layout.addWidget(banner)                # 布局类型变了：位置将就
    else:
        banner.setParent(dialog)                # 连布局都没有：至少挂上去
        banner.show()

    suffix = {
        "pass": "✅ 多重比较闸通过",
        "fail": "⛔ 多重比较闸未通过",
        "absent": "⚠ 未跑多重比较闸",
        "error": "⚠ 闸结论读取失败",
    }[verdict.status]
    dialog.setWindowTitle(f"{dialog.windowTitle()} —— {suffix}")


# ── 安装 ──────────────────────────────────────────────────────────

_INSTALLED_FLAG = "_vnpy_app_gate_verdict"


def install_gate_verdict() -> list[str]:
    """把闸的裁决接进回测器 GUI。返回实际包装了哪些方法，便于日志与测试。

    幂等：重复调用只包装一次（靠函数对象上的标记判断，不靠模块级布尔量，
    这样即使模块被重新导入也不会叠加一层包装）。回测器没装时返回空列表。
    """
    try:
        from vnpy_ctabacktester.ui.widget import (
            BacktesterManager,
            OptimizationResultMonitor,
        )
    except ImportError:
        return []

    installed: list[str] = []

    finished = BacktesterManager.process_optimization_finished_event
    if not getattr(finished, _INSTALLED_FLAG, False):

        @functools.wraps(finished)
        def process_optimization_finished_event(self: Any, event: Any) -> None:
            finished(self, event)
            _log_verdict(self)

        setattr(process_optimization_finished_event, _INSTALLED_FLAG, True)
        BacktesterManager.process_optimization_finished_event = (      # type: ignore[method-assign]
            process_optimization_finished_event
        )
        installed.append("BacktesterManager.process_optimization_finished_event")

    init_ui = OptimizationResultMonitor.init_ui
    if not getattr(init_ui, _INSTALLED_FLAG, False):

        @functools.wraps(init_ui)
        def init_ui_with_verdict(self: Any) -> None:
            init_ui(self)
            _decorate_result_dialog(self)

        setattr(init_ui_with_verdict, _INSTALLED_FLAG, True)
        OptimizationResultMonitor.init_ui = init_ui_with_verdict       # type: ignore[method-assign]
        installed.append("OptimizationResultMonitor.init_ui")

    return installed
