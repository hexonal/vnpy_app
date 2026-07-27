"""FluentMainWindow 在无显示环境下必须抛异常，而不是段错误。

这道守卫不是洁癖。段错误的代价是具体的：它绕过一切 Python 异常处理，
faulthandler 抓不到（dump 文件 0 字节），进程直接消失，只在
~/Library/Logs/DiagnosticReports 留一份没人会去看的 .ips。

2026-07-26 的实际经过：连着三四次在 offscreen 下起 FluentMainWindow，
每次进程都没了。因为 faulthandler 的 dump 是空的，我判断成"卡在 C++ 层"，
围着这个错误结论查了很久 —— 直到看到 crash report 才知道是 SIGSEGV。
把 crash report 换成一段能读的话，省掉的就是那段时间。

真正的崩溃点在 qfluentwidgets：FluentWindow 在 macOS 上要通过 PyObjC 建原生
无边框窗口，offscreen 没有 NSWindow，ObjC 调用返回 0x1，PyObjC 拿去
objc_opt_self 解引用即崩。我们改不了它，只能在进入它之前拦住。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fluent_ui.mainwindow import _headless_qpa

REPO = Path(__file__).resolve().parent.parent
VPY = sys.executable


@pytest.mark.parametrize(
    ("qpa", "blocked"),
    [
        ("", False),            # 未设置 = 正常桌面
        ("cocoa", False),       # macOS 原生
        ("xcb", False),         # X11
        ("wayland", False),
        ("offscreen", True),
        ("minimal", True),
        ("OFFSCREEN", True),    # 大小写不敏感
        ("offscreen:enable_fonts", True),   # Qt 允许带参数后缀
    ],
)
def test_only_headless_backends_are_blocked(
    monkeypatch: pytest.MonkeyPatch, qpa: str, blocked: bool
) -> None:
    """只拦无显示后端。vnc/eglfs 这类有真实窗口系统的不该被误伤。"""
    monkeypatch.setenv("QT_QPA_PLATFORM", qpa)
    assert bool(_headless_qpa()) is blocked


def test_missing_env_var_is_not_headless(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("QT_QPA_PLATFORM", raising=False)
    assert _headless_qpa() == ""


def _run(code: str, *, qpa: str = "offscreen") -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "QT_QPA_PLATFORM": qpa}
    return subprocess.run(
        [VPY, "-c", code], capture_output=True, text=True, cwd=str(REPO), env=env, timeout=120
    )


_BUILD = """
import sys
sys.path.insert(0, {repo!r})
from vnpy.event import EventEngine
from vnpy.trader.engine import MainEngine
from fluent_ui import FluentMainWindow, create_fluent_qapp

app = create_fluent_qapp()
engine = MainEngine(EventEngine())
try:
    FluentMainWindow(engine, engine.event_engine{extra})
    print("BUILT")
except RuntimeError as exc:
    print("REFUSED:" + str(exc).splitlines()[0])
finally:
    engine.close()
"""


def test_offscreen_refuses_instead_of_segfaulting() -> None:
    """核心断言：退出码不是 139/-11，而是干净退出并说明原因。

    修复前实测：退出码 139（SIGSEGV），无任何 Python 侧输出。
    """
    proc = _run(_BUILD.format(repo=str(REPO), extra=""))

    assert proc.returncode not in (139, -11), (
        f"仍在段错误（退出码 {proc.returncode}）—— 守卫没有在 super().__init__() 之前生效。"
        f"\nstderr 末尾: {proc.stderr[-400:]}"
    )
    assert "REFUSED:" in proc.stdout, f"未拒绝: {proc.stdout[-300:]}"
    assert "SIGSEGV" in proc.stdout, "提示里要写明它本来会段错误，否则读的人不知道严重性"


def test_the_refusal_names_the_way_out() -> None:
    """光拒绝不够 —— 得告诉人在无显示环境里该用什么。

    钉住这一条是因为：一条只说"不行"的报错，会让人接着去猜、去试
    allow_offscreen、再撞一次段错误。
    """
    proc = _run(_BUILD.format(repo=str(REPO), extra=""))
    combined = proc.stdout + proc.stderr
    # 只断言首行会漏掉替代方案（它在后面几行），所以直接查异常全文
    code = _BUILD.format(repo=str(REPO), extra="").replace(
        'print("REFUSED:" + str(exc).splitlines()[0])', 'print("REFUSED:" + str(exc))'
    )
    full = _run(code).stdout
    assert "MainWindow" in full, "没有指出可用的替代外壳"
    assert "allow_offscreen" in full, "没有说明确实想试时怎么绕过"
    assert combined  # 保底：进程真的产出了东西


@pytest.mark.skipif(
    not os.environ.get("VNPY_APP_ALLOW_SEGFAULT_TEST"),
    reason=(
        "本用例会**故意**让一个子进程段错误，而 macOS 每次都会为此写一份"
        " ~/Library/Logs/DiagnosticReports/Python-*.ips 崩溃报告。"
        "跑一次测试掉一份报告，看起来像项目在不停崩 —— 实测 25 份报告"
        "全部来自这一条(特征一致：objc_opt_self + libqoffscreen)。"
        "设 VNPY_APP_ALLOW_SEGFAULT_TEST=1 显式开启。"
    ),
)
def test_allow_offscreen_is_a_real_escape_hatch() -> None:
    """逃生舱必须真的放行 —— 否则将来 qfluentwidgets 修好了也没法验证。

    放行的结果就是那个真段错误（139）。这里断言的正是"守卫没有一刀切"，
    所以退出码为 139 是**预期**行为，不是失败。

    默认不跑：见上面 skipif 的理由。这条断言的价值是"将来能发现上游修好了"，
    属于低频复核，不值得每次跑测试都在系统里留一份崩溃报告。想验证时：

        VNPY_APP_ALLOW_SEGFAULT_TEST=1 pytest tests/test_fluent_offscreen_guard.py

    守卫本身仍然每次都测 —— test_offscreen_refuses_instead_of_segfaulting
    覆盖的正是"不再段错误"这个真正要防的回归，且它不产生崩溃报告。
    """
    proc = _run(_BUILD.format(repo=str(REPO), extra=", allow_offscreen=True"))

    if proc.returncode == 0 and "BUILT" in proc.stdout:
        pytest.skip(
            "offscreen 下 FluentWindow 竟然建成了 —— qfluentwidgets 可能已修复此问题。"
            "若稳定复现，本模块的守卫可以放宽，请复核后调整。"
        )
    assert proc.returncode in (139, -11), (
        f"逃生舱既没崩也没建成，退出码 {proc.returncode} —— 说明它被守卫拦住了，"
        f"allow_offscreen 没起作用。stderr: {proc.stderr[-300:]}"
    )
