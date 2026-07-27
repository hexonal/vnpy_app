# vnpy_app

VeighNa 桌面终端的启动脚本与 Fluent Design 外壳。三个入口：

| 入口 | 用途 | 风控闸 |
|---|---|---|
| `run_gui.py` | 带界面的交易终端（Fluent 外壳） | ✅ 三条闸 + RiskManager 五条内建 |
| `run.py` | headless，供 MCP / AgentBridge 用（LLM Agent 下单走这条） | ✅ 三条闸（2026-07-26 前**没有**，见下） |
| `run_live_alpha.py` | 投研信号驱动的实盘 rebalance | ✅ 默认 dry-run，`--live` 才真发单 |

---

## 无显示环境下不能用 FluentMainWindow

**结论先行**：`QT_QPA_PLATFORM=offscreen`（或 `minimal`）下起 `FluentMainWindow`
会**段错误**，不是抛异常。自动化测试请改用标准 `MainWindow`。

```python
# ❌ 段错误（退出码 139），进程直接消失
QT_QPA_PLATFORM=offscreen python -c "FluentMainWindow(engine, event_engine)"

# ✅ 可用，w.grab() 返回真实位图
from vnpy.trader.ui import MainWindow, create_qapp
app = create_qapp()
win = MainWindow(main_engine, event_engine)
```

崩溃点在 qfluentwidgets，不在本包：`FluentWindow` 在 macOS 上要通过 PyObjC 建
原生无边框窗口（`PySideSix-Frameless-Window`），offscreen 平台没有 NSWindow，
某个 Objective-C 调用返回 `0x1` 这种非指针值，PyObjC 拿去解引用即崩：

```
Exception Type:    EXC_BAD_ACCESS (SIGSEGV)
KERN_INVALID_ADDRESS at 0x0000000000000001
0  libobjc.A.dylib              objc_opt_self + 8
1  _objc.cpython-314-darwin.so  id_to_python (objc_support.m:3403)
2  _objc.cpython-314-darwin.so  object_new + 404
```

**为什么值得专门写一节**：段错误绕过一切 Python 异常处理，`faulthandler` 也抓不到
（它的 dump 文件是 0 字节）。现象是进程无声消失，看起来像"卡死"——
2026-07-26 我就是这么误判的，围着"卡在 C++ 层"这个错误结论查了很久，
直到翻 `~/Library/Logs/DiagnosticReports` 才看到 SIGSEGV。

`FluentMainWindow.__init__` 现在会在进入那段 ObjC 代码**之前**拦截，把 crash
report 换成一段说清原因和替代方案的报错。确实想试（例如换了 qfluentwidgets
版本想验证是否已修复）：

```python
FluentMainWindow(engine, event_engine, allow_offscreen=True)   # 放行到真段错误
```

守卫只拦 `offscreen` / `minimal` 这两个 Qt 官方无显示后端；`cocoa` / `xcb` /
`wayland` 这类有真实窗口系统的一律放行。回归测试见
`tests/test_fluent_offscreen_guard.py`（含变异验证：守卫一移除即有用例转红）。

**做 GUI 自动化的两条路**：
1. 用标准 `MainWindow` 起专供自动化的实例（本仓测试用的就是这条）
2. 给终端开屏幕录制权限，用 `screencapture` 操作真窗口

---

## 图表查询用标的所在市场的时区，不是机器时区

`fluent_ui/chart_wizard.py` 的 `_query_tz(vt_symbol)` 按交易所取时区
（SEHK→Asia/Hong_Kong，SMART→America/New_York），走
`vnpy_gatewaykit.market_clock.market_tz` 这个单一真相源。

原先用 `ZoneInfo(get_localzone_name())`，即**这台机器所在地**。这两者在本项目里
从来不相等（机器在 US Pacific/Eastern，标的是港股），GUI 日志里留下过现场：

```
查询K线 -> FUTU: HistoryRequest(symbol='1', exchange=SEHK,
    start=... tzinfo=ZoneInfo(key='America/New_York'), ...)
```

查 SEHK 却带着 New_York 的墙钟 → 查"最近 1 天"实际取的是港股时间的另一段，
边界那根 K 线必然错位。

---

## run.py 曾经没有风控闸

`run_gui.py` 一直在 `add_app(RiskManagerApp)` 之后显式调 `install_gate_rules()`，
而 `run.py`（headless，MCP/AgentBridge 用）**从未调用**。后果：同一张无止损
LIMIT 买单，在 GUI 进程里会被「强制止损检查」拒掉，在 `run.py` 起的进程里直达券商——
而那正是 LLM Agent 下单的那条路。已于 `build_main_engine()` 补上。

三条闸挂在 `MainEngine.send_order` 上而非只挂 `AlphaLiveEngine`，所以 CTA 策略与
GUI 手工下单这两条路径的裸单同样会被拦。回归测试
`tests/test_run_headless_gate_wiring.py` 直接 `import run` 调真函数——
不是手抄一份 `build_main_engine` 的副本（第一版探针就是手抄的，改完照样显示有 bug，
因为它测的根本不是被改的那个）。

---

## 测试

```bash
cd vnpy_app && ../vnpy/.venv/bin/python -m pytest tests -q
```

`tests/test_searchable_combo_box.py` 与另一模块各自在 import 期建 QApplication，
同轮收集会报 `libshiboken: Please destroy the QApplication singleton`。
这是预先存在的问题，拆开跑即可。
