"""三段样本内外回测在 GUI 里的位置：一行提示 + 一道拦截，流程本身不进来。

━━━ 判断：三段切分【不该】做成回测面板上的一个按钮 ━━━

`vnpy_ctastrategy/segments.py` 有一套完整的三段能力（TRAIN 选参 / VALID 复核 /
TEST 只看一次，测试段上扫参数抛 `SegmentLeakError`，第二次查看抛
`SegmentBudgetExhaustedError`）。`vnpy_ctabacktester` 的面板只有一组
start/end。很自然会想"那就再加三对日期框 + 一个三段回测按钮"。**不加。**
理由三条，都是这个界面自身的形状决定的：

1. **预算是计数器，而 GUI 是长驻进程 + 反复点按钮。**
   `SegmentedRunner._test_calls` 活在对象里。面板上真放一个"三段回测"按钮，
   只有两种实现：复用同一个 runner —— 点第二下抛异常，用户读作"这按钮坏了"，
   于是去找重置；每次新建 runner —— 预算永远是 1/1，闸等于没装。两条路都把
   "只看一次"变成"想看几次看几次"。命令行"一次调用 = 一次流程"与预算的形状
   天然对齐，跨进程那一段由 `segment_record` 的账本接上（额度真的会用完，
   见 `tests/test_segment_record.py`）。

2. **三段的产出是一份带审计轨迹的报告，不是一张统计面板。**
   `HoldoutReport` 里有整个网格在 TRAIN 上的分数、诚实警告、TEST 段的
   block-bootstrap 显著性、以及测试段被看过几次。`StatisticsMonitor` 是
   key→单值的表格，装不下这些；硬塞只能塞 TEST 那一列 —— 而"把样本外数字
   单独拎出来当结论"恰恰是这套东西要防的头号事故。

3. **面板上那两个按钮本身就是扫参数的入口。**
   [参数优化] = `run_bf_optimization`。用户完全可以把日期框填成测试段区间再点
   它 —— 那是教科书级的泄漏，而原引擎毫无察觉。所以这个界面真正缺的**不是
   三个日期框，是一道拦截**。

反方最强的论据是"样本内外纪律是投研的地基，藏在代码里等于没人用"。这条成立，
但它要的是**可达性**，不是**按钮**。所以本模块做两件事，一件也不多：

    ① 参数区多一行提示，把命令印出来；当前切分与剩余查看次数落在悬停提示与
      面板日志区（标签本身只有 331px×3 行，量过，见 `notice_text`）。
    ② [参数优化] 的窗口碰到 TEST 段就拒绝执行，并在拒绝时把命令再说一遍。
      这是上面第 3 条的执行体：不进 GUI 的能力，至少不能被 GUI 绕过。

━━━ 单次回测为什么只记一行日志、不拦 ━━━

对**已选定的一组参数**在 TEST 段跑一次，正是 `SegmentedRunner.run(setting,
Segment.TEST)` 允许的那一次；拦掉它等于禁止这个流程的最后一步。但 GUI 数不了
预算（理由见上面第 1 条），所以它做的是**说明**而不是记账：日志里写清"这一次
没有进账本，账在命令行那边"。含糊的沉默比拦截更危险。

━━━ 一个 ≤1 天的边界误差，如实写在这里 ━━━

判据 `hits_test_segment` 与 `segments.SegmentGuardedEngine.guard_optimization`
逐例同解（`tests/test_segment_record.py` 用参数化对拍钉住）。两者都把边界
`datetime` 去掉时区后按天比较，而数据库里港股日线的时间戳是 UTC（`2026-01-21
16:00+00:00` 对应港时 1/22）。于是这道闸的边界比真实交易日**早一天**：低端
多拦一天（安全方向），高端少拦一天。这是引擎守卫既有的性质，不是这一层引入的；
**刻意不在这里"修正"** —— 修了这一层就与引擎给出两种答案，那比早一天更糟。

━━━ 实现方式：运行时注入，不碰第三方源码 ━━━

`vnpy_ctabacktester` 装在 site-packages，不在任何 git 里，改了下次升级就没了。
与 `backtester_metrics.py` 补 `KEY_NAME_MAP`、`backtester_gates.py` 包装
`process_optimization_finished_event` 同一个路子：包装方法，幂等（自带标记，
不会叠两层），导入失败静默跳过。包装时捕获的是**调用那一刻的** `init_ui` /
`start_optimization`，所以与同目录另外两个注入模块谁先谁后都能正确串起来。

**安装时机**：必须在创建主窗口之前（`run_gui.main()` 里就是这个顺序），
否则已经建好的面板实例仍然连着未包装的旧函数。
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any, cast

from vnpy.trader.ui import QtCore, QtWidgets

if TYPE_CHECKING:
    from vnpy_ctastrategy.segment_record import SplitRecord


#: 提示标签的 objectName —— 测试与后续代码用它定位，不靠遍历文本。
NOTICE_OBJECT_NAME = "segment_holdout_notice"

#: 印在界面上的命令。改了这里要同步改 `vnpy_ctastrategy/segment_cli.py` 的模块名。
SEGMENT_CLI = "python -m vnpy_ctastrategy.segment_cli"

#: 标签正文。两行封顶 —— 面板左栏实测只给到 331px 宽、3 行高（见 notice_text）。
_NOTICE_LINES = (
    "三段样本内外回测走命令行·TEST 段禁优化",
    f"    {SEGMENT_CLI}",
)

_INSTALLED_FLAG = "_vnpy_app_segment_notice"


def load_current_record() -> SplitRecord | None:
    """读账本。任何读不出来的情形都返回 None —— 提示与闸都按"没有切分"处理。

    在函数内 import：`vnpy_ctastrategy` 是本仓依赖，但把它挪进调用时刻可以让
    这个模块在被单独 import 时不背上整条回测链的加载成本。
    """
    try:
        from vnpy_ctastrategy.segment_record import load_record

        return load_record()
    except Exception:       # noqa: BLE001 —— 研究文件的任何问题都不该拦住 GUI
        return None


def notice_text() -> str:
    """提示标签的正文 —— **两行，不带状态**。

    这个尺寸是量出来的，不是估的。面板左栏要塞下 10 行参数 + 8 个按钮，实测
    留给这个标签的是 **331px 宽 × 3 行高**（1400x900 窗口）。历次尝试：

        贴 record.describe() 整段  → 要 253px，给 184px，被切在一个参数字典中间
        压到"三段边界 + 额度"      → 要 115px，给 108px，仍差一行
        压到"TEST 段 + 额度"三行   → 两行超宽（388px / 445px）各自折行，要 5 行

    一个自己都显示不全的提示比没有更糟，所以状态不放在这里，而是放在两个真的
    有地方的位置：**tooltip**（`notice_tooltip`，悬停即见完整三段）与面板**日志区**
    （`_log_split_state`，开面板时打一次）。标签只负责一件事：让人知道有这条命令。

    `tests/test_backtester_segments.py::test_notice_is_not_truncated_at_a_modest_
    window_size` 按真实渲染高度钉住这一点，措辞再变长会红。
    """
    return "\n".join(_NOTICE_LINES)


def notice_tooltip(record: SplitRecord | None) -> str:
    """悬停才看的详版：三段边界、每一次测试段查看、每一次重开预算的理由。"""
    if record is None:
        return f"{notice_text()}\n（还没有切分：跑一次上面的命令就会出现）"
    return f"当前切分：\n{record.describe()}"


def refusal_text(record: SplitRecord, start: datetime, end: datetime) -> str:
    """拒绝执行寻优时给用户看的话。说清拦了什么、为什么、接下来怎么做。

    **第一行就是"已拒绝"**，不靠对话框标题传达：macOS 按平台规范忽略
    `QMessageBox` 的窗口标题（本机 offscreen 实测抓到的弹窗 `windowTitle()`
    确实是空串），标题里的措辞在这台机器上根本不会出现在屏幕上。
    """
    test_start, test_end = record.split.test_start, record.split.test_end
    return (
        f"已拒绝本次参数优化：窗口碰到 TEST 段。\n\n"
        f"本次寻优窗口 [{start:%Y-%m-%d} ~ {end:%Y-%m-%d}] 与 TEST 段 "
        f"[{test_start:%Y-%m-%d} ~ {test_end:%Y-%m-%d}] 有交集。\n\n"
        f"扫参数 = 用这一段做模型选择，做完测试段就变成样本内，"
        f"之后任何【样本外】表述都是假的。\n\n"
        f"把结束日期收到 {test_start:%Y-%m-%d} 之前再优化；"
        f"三段流程请走命令行：\n{SEGMENT_CLI} --help"
    )


def _window(widget: Any) -> tuple[datetime, datetime]:
    """面板上的回测窗口，与上游 `start_backtesting` 取法一致。"""
    start = cast(datetime, widget.start_date_edit.dateTime().toPython())
    end = cast(datetime, widget.end_date_edit.dateTime().toPython())
    return start, end


def refresh_notice(widget: Any) -> SplitRecord | None:
    """按当前账本刷新提示文本，并把账本交回调用方（顺手复用这次读取）。

    每次动作前都重读文件：命令行刚跑完的结果不该等到重启 GUI 才看得见。
    """
    record = load_current_record()
    label = widget.findChild(QtWidgets.QLabel, NOTICE_OBJECT_NAME)
    if label is not None:
        label.setToolTip(notice_tooltip(record))
    return record


def _attach_notice(widget: Any) -> None:
    """在参数区表单末尾加一行提示（跨两列）。

    表单是 `init_ui` 里的局部变量，只能从布局树上取；整个面板里只有这一个
    `QFormLayout`（就是参数区那张表），所以 `findChild` 不会认错。
    """
    form = widget.findChild(QtWidgets.QFormLayout)
    if form is None:
        return

    record = load_current_record()
    label = QtWidgets.QLabel(notice_text())
    label.setObjectName(NOTICE_OBJECT_NAME)
    label.setToolTip(notice_tooltip(record))
    label.setWordWrap(True)
    # 命令是给人抄去终端跑的 —— 不能选中就等于没印。
    label.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
    form.addRow(label)

    _log_split_state(widget, record)


def _log_split_state(widget: Any, record: SplitRecord | None) -> None:
    """开面板时把当前切分打进日志区一次。

    日志区是这个界面上唯一宽敞又可滚动的地方，状态放这里才看得全（标签只有
    331px×3 行，见 `notice_text`）。没有账本时不打 —— 没跑过命令行的用户不需要
    每次开面板都被念一遍。
    """
    if record is None:
        return
    widget.write_log(f"[三段] 当前切分（详情见参数区提示的悬停）：\n{record.describe()}")


def _guard_optimization(widget: Any) -> bool:
    """寻优前的检查。返回 True 表示放行。"""
    record = refresh_notice(widget)
    if record is None:
        return True

    start, end = _window(widget)
    if not record.hits_test(start, end):
        return True

    message = refusal_text(record, start, end)
    widget.write_log(f"[三段闸] {message}")
    QtWidgets.QMessageBox.warning(
        widget, "参数优化被拒绝：窗口碰到 TEST 段", message
    )
    return False


def _note_backtesting(widget: Any) -> None:
    """单次回测碰到 TEST 段时记一行日志。不拦 —— 理由见模块文档。"""
    record = refresh_notice(widget)
    if record is None:
        return

    start, end = _window(widget)
    if not record.hits_test(start, end):
        return

    test_start, test_end = record.split.test_start, record.split.test_end
    widget.write_log(
        f"[三段闸] 本次回测窗口碰到 TEST 段 "
        f"[{test_start:%Y-%m-%d} ~ {test_end:%Y-%m-%d}]，这是一次样本外查看。"
        f"面板不计次数，**本次不会进账本**；要让它计入请走 "
        f"{SEGMENT_CLI} --stage final。"
    )


def install_segment_notice() -> list[str]:
    """把提示与闸装进回测面板。返回实际装上的挂点，便于日志与测试。

    幂等：重复调用返回空列表，不会叠第二层包装。
    导入失败（未安装回测器）时静默跳过 —— 缺一个可选 App 不该让 GUI 起不来。
    """
    try:
        from vnpy_ctabacktester.ui.widget import BacktesterManager
    except ImportError:
        return []

    installed: list[str] = []

    original_init_ui: Callable = BacktesterManager.init_ui
    if not getattr(original_init_ui, _INSTALLED_FLAG, False):

        @functools.wraps(original_init_ui)
        def init_ui(self: Any) -> None:
            original_init_ui(self)
            _attach_notice(self)

        setattr(init_ui, _INSTALLED_FLAG, True)
        BacktesterManager.init_ui = init_ui      # type: ignore[method-assign]
        installed.append("BacktesterManager.init_ui")

    original_optimization: Callable = BacktesterManager.start_optimization
    if not getattr(original_optimization, _INSTALLED_FLAG, False):

        @functools.wraps(original_optimization)
        def start_optimization(self: Any) -> None:
            if _guard_optimization(self):
                original_optimization(self)

        setattr(start_optimization, _INSTALLED_FLAG, True)
        BacktesterManager.start_optimization = (     # type: ignore[method-assign]
            start_optimization
        )
        installed.append("BacktesterManager.start_optimization")

    original_backtesting: Callable = BacktesterManager.start_backtesting
    if not getattr(original_backtesting, _INSTALLED_FLAG, False):

        @functools.wraps(original_backtesting)
        def start_backtesting(self: Any) -> None:
            _note_backtesting(self)
            original_backtesting(self)

        setattr(start_backtesting, _INSTALLED_FLAG, True)
        BacktesterManager.start_backtesting = (      # type: ignore[method-assign]
            start_backtesting
        )
        installed.append("BacktesterManager.start_backtesting")

    return installed
