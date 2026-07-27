"""
Fluent Design shell for the VeighNa desktop terminal, modeled on
veighna-global/vnpy_evo's MainWindow — a single dashboard "Home" page
(see home_widget.py) plus a contract-search page plus one nav item per
installed app, all reached through FluentWindow's left nav sidebar
instead of stock vnpy's QtWidgets.QMainWindow + dock-widget + menu-bar
layout.

First pass at this only swapped the window shell and left every inner
widget as a plain, now-unstyled QtWidgets.QTableWidget/QPushButton/etc.
(qdarkstyle had to be removed since it fights qfluentwidgets' own
theming) — that's why it didn't actually look like vnpy_evo. This
version also swaps the content: monitor.py/trading_widget.py/
connect_dialog.py/contract_manager.py are Fluent-native reimplementations
of vnpy.trader.ui.widget's classes (same event-handling/main_engine-call
logic, qfluentwidgets base classes). None of it touches the vnpy fork
itself — same "new functionality lives in its own package" pattern as
vnpy_futu/vnpy_agentbridge.

Untested (see the "Fluent Design GUI 皮肤" plan's verification section):
whether every third-party app widget (CtaManager, BacktesterManager,
DataManager, ChartWizard, PaperAccount's settings widget) renders
correctly inside addSubInterface() — they were only ever built against
stock vnpy's QMainWindow dock layout upstream, so a rendering glitch in
one is a real possibility, not something ruled out here.
"""

from __future__ import annotations

import os
import platform
import webbrowser
from importlib import import_module
from types import ModuleType
from typing import cast

from qfluentwidgets import FluentIcon as FIF
from qfluentwidgets import FluentWindow, MessageBox, NavigationItemPosition, Theme, setTheme
from vnpy.event import EventEngine
from vnpy.trader.app import BaseApp
from vnpy.trader.engine import EmailEngine, MainEngine
from vnpy.trader.locale import _
from vnpy.trader.setting import SETTINGS
from vnpy.trader.ui import QtCore, QtGui, QtWidgets
from vnpy.trader.ui import create_qapp as _create_qapp
from vnpy.trader.ui.widget import AboutDialog, GlobalDialog, WechatDialog

from .chart_wizard import ChartWizardWidget as FluentChartWizardWidget
from .contract_manager import ContractManager
from .data_manager import ManagerWidget as FluentDataManagerWidget
from .home_widget import HomeWidget

# Keyed by BaseApp.app_name (e.g. "CtaStrategy", not the Chinese
# display_name) so it survives locale changes. Every currently-installed
# app is covered; anything added later just falls back to FIF.ROBOT rather
# than crashing — see init_navigation()'s .get(..., FIF.ROBOT).
APP_ICONS: dict[str, object] = {
    "RiskManager": FIF.CERTIFICATE,
    "CtaStrategy": FIF.ROBOT,
    "CtaBacktester": FIF.HISTORY,
    "DataManager": FIF.CLOUD_DOWNLOAD,
    "ChartWizard": FIF.MARKET,
    "PaperAccount": FIF.EDUCATION,
}

# Third-party app widgets we've rewritten Fluent-native (see data_manager.py
# — same ManagerEngine calls, qfluentwidgets components instead of plain
# QtWidgets so exchange/interval combo boxes are searchable). Checked
# before falling back to the app's own <app_module>.ui.<widget_name> —
# apps not listed here still load their stock widget unmodified.
APP_WIDGET_OVERRIDES: dict[str, type] = {
    "DataManager": FluentDataManagerWidget,
    "ChartWizard": FluentChartWizardWidget,
}


# vnpy's own default ("font.family": "微软雅黑" — Microsoft YaHei, see
# vnpy/trader/setting.py) only exists on Windows. Every launch on this
# machine has logged "Replace uses of missing font family 微软雅黑" — Qt
# silently substitutes *something* it picks, not something we chose.
# Ordered by rendering quality on each platform, most specifically-hinted
# first: PingFang SC is Apple's own system font for Simplified Chinese
# (same one macOS itself uses for CJK UI text, so it's the smoothest
# possible rendering here — confirmed via QFontDatabase.families() that
# it's actually installed on this machine, not assumed).
_FONT_CANDIDATES: dict[str, list[str]] = {
    "Darwin": ["PingFang SC", "PingFang HK", "Heiti SC", "STHeiti"],
    "Windows": ["Microsoft YaHei UI", "Microsoft YaHei", "SimHei"],
    "Linux": ["Noto Sans CJK SC", "WenQuanYi Micro Hei", "Droid Sans Fallback"],
}


def _select_smooth_font() -> str:
    """
    Requires a QApplication/QGuiApplication to already exist —
    QFontDatabase.families() raises without one. Falls back to whatever
    SETTINGS["font.family"] already was (vnpy's own default) if none of
    the platform's candidates are actually installed, rather than forcing
    a family Qt would just silently substitute away from anyway.
    """
    candidates = _FONT_CANDIDATES.get(platform.system(), [])
    available = set(QtGui.QFontDatabase.families())
    for candidate in candidates:
        if candidate in available:
            return candidate
    fallback: str = SETTINGS["font.family"]
    return fallback


def create_fluent_qapp(app_name: str = "VeighNa Fluent") -> QtWidgets.QApplication:
    """
    Same icon/exception-hook setup as vnpy.trader.ui.create_qapp(), minus
    the qdarkstyle stylesheet — qfluentwidgets manages its own per-widget
    theming via setTheme(), and qdarkstyle's blanket QWidget stylesheet
    would fight it for control of every widget's appearance (double-styled
    = neither theme rendering as intended). Font is re-applied after
    _create_qapp() returns: _select_smooth_font() needs a live QApplication
    to query QFontDatabase, so this runs the vnpy default font briefly
    before overriding it with the platform's best available family.
    """
    # Point font.family at the platform's best CJK family BEFORE _create_qapp
    # runs. vnpy's create_qapp does QApplication(...) then
    # setFont(QFont(SETTINGS["font.family"], ...)) — so if the setting is
    # still 微软雅黑 (Windows-only) at that point, Qt logs the "missing font
    # family 微软雅黑" substitution during _create_qapp, before we could
    # override it. We can't query QFontDatabase yet (no QApplication exists),
    # so optimistically set the top candidate (PingFang SC is a macOS system
    # font, always installed ≥10.11); _select_smooth_font() re-checks against
    # the real font DB once the app exists and corrects if the guess is off.
    candidates = _FONT_CANDIDATES.get(platform.system(), [])
    if candidates:
        SETTINGS["font.family"] = candidates[0]

    qapp = _create_qapp(app_name)
    qapp.setStyleSheet("")

    # Now the QApplication exists — resolve the actually-installed family and
    # correct both the setting (read directly by vnpy's own widgets) and the
    # app default font.
    smooth_font = _select_smooth_font()
    SETTINGS["font.family"] = smooth_font
    qapp.setFont(QtGui.QFont(smooth_font, SETTINGS["font.size"]))

    return qapp


_OFFSCREEN_SEGFAULT = """\
FluentMainWindow 在 QT_QPA_PLATFORM={platform!r} 下会**段错误**（不是异常，是 SIGSEGV）。

原因：qfluentwidgets 的 FluentWindow 在 macOS 上要通过 PyObjC 建原生无边框窗口
（PySideSix-Frameless-Window）。offscreen 平台没有 NSWindow，某个 Objective-C
调用返回 0x1 这种非指针值，PyObjC 拿去 objc_opt_self 就崩：

    Exception Type:    EXC_BAD_ACCESS (SIGSEGV)
    KERN_INVALID_ADDRESS at 0x0000000000000001
    0  libobjc.A.dylib              objc_opt_self + 8
    1  _objc.cpython-314-darwin.so  id_to_python (objc_support.m:3403)
    2  _objc.cpython-314-darwin.so  object_new + 404

实测（2026-07-26）：offscreen 下 `FluentWindow()` 退出码 139，稳定复现。
段错误绕过一切 Python 异常处理，faulthandler 也抓不到（它的 dump 文件是 0 字节）
—— 现象看起来像"卡死"，实际是进程已经没了，只在
~/Library/Logs/DiagnosticReports 留下一份没人看的 .ips。这道守卫的意义就是
把那份 crash report 换成你正在读的这段话。

要在无显示环境里跑 GUI（自动化测试 / CI / SSH），用标准 MainWindow：

    from vnpy.trader.ui import MainWindow, create_qapp
    app = create_qapp()
    win = MainWindow(main_engine, event_engine)

它在 offscreen 下实测可用（w.grab() 返回 1200x838 的真实位图，退出码 0）。
本包的 Fluent 控件本身不依赖 FluentWindow —— 外壳换掉，控件照常工作。

确实想试（例如换了 qfluentwidgets 版本想验证是否已修）：
    FluentMainWindow(main_engine, event_engine, allow_offscreen=True)
"""


def _headless_qpa() -> str:
    """当前 Qt 平台插件名；非无显示环境返回空串。

    只认 offscreen 与 minimal —— 这两个是 Qt 官方的无显示后端，也是实测会让
    FluentWindow 段错误的两个。vnc/eglfs 这类有真实窗口系统的不拦。
    """
    qpa = os.environ.get("QT_QPA_PLATFORM", "").strip().lower()
    return qpa if qpa.startswith(("offscreen", "minimal")) else ""


class FluentMainWindow(FluentWindow):
    """Fluent Design left-nav shell wrapping this package's Fluent-native trading widgets."""

    def __init__(
        self,
        main_engine: MainEngine,
        event_engine: EventEngine,
        *,
        allow_offscreen: bool = False,
    ) -> None:
        # 必须在 super().__init__() 之前 —— 段错误就发生在它内部。
        qpa = _headless_qpa()
        if qpa and not allow_offscreen:
            raise RuntimeError(_OFFSCREEN_SEGFAULT.format(platform=qpa))

        super().__init__()

        self.main_engine = main_engine
        self.event_engine = event_engine

        # matches the qdarkstyle look this replaces; flip to Theme.LIGHT to
        # match vnpy_evo's default
        setTheme(Theme.DARK)

        self.window_title = "VeighNa Fluent"
        self.setWindowTitle(self.window_title)

        self._fix_macos_traffic_lights()

        self.init_widgets()
        self.init_navigation()
        self.load_window_setting()

    def _fix_macos_traffic_lights(self) -> None:
        """
        qfluentwidgets' FluentWidget.__init__ is *documented* to already do
        the right thing on darwin — call setSystemTitleBarButtonVisible(True)
        (native macOS close/minimize/zoom buttons, OS-positioned top-left)
        and hide the custom FluentTitleBar's own min/max/close row (which
        defaults to a right-aligned, Windows-style layout — see
        FluentTitleBar.__init__'s buttonLayout in qfluentwidgets' own
        fluent_window.py). In practice, on this machine's PySide6 6.11.1
        (newer than what this qfluentwidgets/qframelesswindow combo was
        tested against — see run_gui.py's own version-mismatch note), the
        custom right-aligned buttons stayed visible instead of being hidden
        — reported (with a screenshot) by the user running this on real
        hardware, not something confirmed via a local screenshot here (this
        environment has no screen-recording permission for headless
        verification). Reasserting both halves explicitly here, after
        FluentWindow's own __init__ has already run, makes the fix hold
        regardless of which internal call is failing to stick — if this
        turns out not to be the actual cause, the visible symptom (right-
        aligned custom buttons still showing) won't change and that's the
        signal to look elsewhere instead of at this method.
        """
        if platform.system() != "Darwin":
            return

        self.setSystemTitleBarButtonVisible(True)
        for btn_name in ("minBtn", "maxBtn", "closeBtn"):
            btn = getattr(self.titleBar, btn_name, None)
            if btn is not None:
                btn.hide()

    def init_widgets(self) -> None:
        self.home_widget = HomeWidget(self.main_engine, self.event_engine)
        self.home_widget.setObjectName("home")

        self.contract_manager = ContractManager(self.main_engine, self.event_engine)
        self.contract_manager.setObjectName("contract")

        # (app_name, display_name, widget) — app_name is what APP_ICONS below
        # keys off; display_name is what's shown in the nav; kept apart
        # since they don't always match (e.g. "CtaStrategy" -> "CTA策略").
        self.app_widgets: list[tuple[str, str, object]] = []
        for app in self.main_engine.get_all_apps():
            widget = self._build_app_widget(app)
            if widget is not None:
                cast("QtWidgets.QWidget", widget).setObjectName(app.app_name)
                self.app_widgets.append((app.app_name, app.display_name, widget))

    def init_navigation(self) -> None:
        self.addSubInterface(self.home_widget, FIF.HOME, "主页")
        self.addSubInterface(self.contract_manager, FIF.SEARCH, "合约查询")

        for app_name, display_name, widget in self.app_widgets:
            icon = APP_ICONS.get(app_name, FIF.ROBOT)
            self.addSubInterface(widget, icon, display_name)

        # Stock mainwindow.py's "系统"/"帮助" menu items that don't map to a
        # nav page — these are dialogs, not full-screen content, so they go
        # on the bottom nav as onClick items (same pattern vnpy_evo uses for
        # its own Settings/Forum/GitHub/About). "还原窗口" is deliberately
        # NOT reproduced here: stock's version resets QMainWindow's
        # user-rearrangeable dock layout back to a captured default: a
        # FluentWindow's nav is fixed (nothing to drag/rearrange), so
        # there's no layout state to reset in the first place — not the
        # same gap as plain window-size memory, which load_window_setting()/
        # save_window_setting() below do cover.
        self.navigationInterface.addItem(
            routeKey="wechat",
            icon=FIF.CHAT,
            text=_("微信"),
            onClick=self.open_wechat_dialog,
            selectable=False,
            position=NavigationItemPosition.BOTTOM,
        )
        self.navigationInterface.addItem(
            routeKey="test_email",
            icon=FIF.MAIL,
            text=_("测试邮件"),
            onClick=self.send_test_email,
            selectable=False,
            position=NavigationItemPosition.BOTTOM,
        )
        self.navigationInterface.addItem(
            routeKey="global_setting",
            icon=FIF.SETTING,
            text=_("配置"),
            onClick=self.edit_global_setting,
            selectable=False,
            position=NavigationItemPosition.BOTTOM,
        )
        self.navigationInterface.addItem(
            routeKey="forum",
            icon=FIF.HELP,
            text=_("社区论坛"),
            onClick=self.open_forum,
            selectable=False,
            position=NavigationItemPosition.BOTTOM,
        )
        self.navigationInterface.addItem(
            routeKey="about",
            icon=FIF.INFO,
            text=_("关于"),
            onClick=self.open_about,
            selectable=False,
            position=NavigationItemPosition.BOTTOM,
        )
        self.navigationInterface.addItem(
            routeKey="exit",
            icon=FIF.POWER_BUTTON,
            text=_("退出"),
            onClick=self.close,
            selectable=False,
            position=NavigationItemPosition.BOTTOM,
        )

    def open_wechat_dialog(self) -> None:
        dialog = WechatDialog(self.main_engine, self.event_engine)
        dialog.exec()

    def send_test_email(self) -> None:
        email_engine = cast(EmailEngine, self.main_engine.get_engine("email"))
        email_engine.send_email("VeighNa Trader", "testing")

    def edit_global_setting(self) -> None:
        dialog = GlobalDialog()
        dialog.exec()

    def open_about(self) -> None:
        dialog = AboutDialog(self.main_engine, self.event_engine)
        dialog.exec()

    def open_forum(self) -> None:
        webbrowser.open("https://www.vnpy.com/forum/")

    def save_window_setting(self) -> None:
        """
        Geometry-only persistence — stock mainwindow.py also saves dock
        *state* (saveState()/restoreState()), which has no FluentWindow
        equivalent (no docks); geometry (size/position) is a plain
        QWidget property and carries over fine.
        """
        settings = QtCore.QSettings(self.window_title, "custom")
        settings.setValue("geometry", self.saveGeometry())

    def load_window_setting(self) -> None:
        settings = QtCore.QSettings(self.window_title, "custom")
        geometry = settings.value("geometry")

        if isinstance(geometry, QtCore.QByteArray):
            self.restoreGeometry(geometry)
        else:
            self.resize(1600, 1000)

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        """
        Same confirm-then-shutdown as stock mainwindow.py's closeEvent —
        main_engine.close() must run before the process exits (stops
        gateways/engines cleanly), and skipping the confirm dialog risks a
        stray click killing a connected session with no chance to back out.
        """
        # qfluentwidgets MessageBox (dark-mask overlay, theme-consistent)
        # instead of a raw native QMessageBox floating over the Fluent
        # shell; .exec() is truthy on the yes button, defaulting to "no"
        # semantics the same way the old StandardButton.No default did
        # (dismiss/cancel → falsy → ignore the close).
        if MessageBox(_("退出"), _("确认退出？"), self).exec():
            for monitor in self.home_widget.get_monitors():
                monitor.save_setting()

            self.save_window_setting()

            self.main_engine.close()
            event.accept()
        else:
            event.ignore()

    def _build_app_widget(self, app: BaseApp) -> object | None:
        """
        APP_WIDGET_OVERRIDES first (our Fluent-native rewrites); otherwise
        the same lookup vnpy's stock mainwindow.py uses in init_menu():
        <app_module>.ui.<widget_name>(main_engine, event_engine). Returns
        None (and lets the app just be missing from the nav, not a crash)
        if a third-party app's ui module can't be imported/instantiated.
        """
        try:
            widget_class = APP_WIDGET_OVERRIDES.get(app.app_name)
            if widget_class is None:
                ui_module: ModuleType = import_module(app.app_module + ".ui")
                widget_class = getattr(ui_module, app.widget_name)
            widget: object = widget_class(self.main_engine, self.event_engine)
            return widget
        except Exception as exc:  # noqa: BLE001 — a broken third-party app widget must not take down the whole window
            self.main_engine.write_log(f"App[{app.display_name}] 界面加载失败,已跳过: {exc}")
            return None
