"""把本仓 `strategies/` 下的策略装进两个引擎——它们自己扫不到。

## 现象

`vnpy_app/strategies/long_only_turtle_strategy.py` 从来没有在界面上出现过。
实盘 CTA 面板与回测面板的策略下拉框里都只有上游自带的 9 个（AtrRsi /
BollChannel / DoubleMa / DualThrust / KingKeltner / MultiSignal /
MultiTimeframe / Test / TurtleSignal）。不是排在后面看不见，是**根本没被装载**。

## 根因

两个引擎的第二个扫描路径都写成 `Path.cwd() / "strategies"`：

* `vnpy_ctastrategy/engine.py:867`
* `vnpy_ctabacktester/engine.py:93`（PyPI 上游，不在本工作区）

而 `MainEngine.__init__` 在任何 app 被 add 之前先 `os.chdir(TRADER_DIR)`。
本机实测 `TRADER_DIR = /Users/flink`，于是扫的是 `/Users/flink/strategies`
——**该目录不存在**。这与 `install_gate_rules` 绕开 `RiskEngine` 自带的
`Path.cwd()/rules` 扫描是同一个病灶，只是没人发现它也咬了策略加载。

失败是**彻底静默的**：`glob` 返回空列表意味着一次 import 都没发生，
连 `load_strategy_class_from_module` 的 except 分支都到不了，一条日志都没有。

## 为什么不改 fork

同一行代码在 `vnpy_ctabacktester` 里逐字存在，而那是 PyPI 装的上游包、
没有 fork。给 `vnpy_ctastrategy` 加一个「额外策略目录」注册表只能修一半，
另一半仍要在 `vnpy_app` 侧包装——那就成了一个 bug 两套机制。既然叶子层的
包装无论如何都要写，就让它同时喂两个引擎，fork 零改动。

另一个理由是所有权：**目录布局是叶子层的知识**，fork 不该知道 `vnpy_app`
在哪。

也评估并否掉了「把策略软链到 `~/.vntrader/strategies`」：符号链接是未进 git
的本机状态，换机器或新 clone 之后策略又会静默消失——同一种病，只是换了触发
条件。

## 回测器的「重载策略」按钮会撤销一次性注入

`BacktesterEngine.reload_strategy_class` 是 `self.classes.clear()` 接
`self.load_strategy_class()`，而后者只扫 path1 与 cwd。所以注入不能只做一次，
必须把 `reload_strategy_class` 也包起来。CtaEngine 侧没有对应入口
（全文无 `reload_strategy_class`），只需装载时喂一次。

## import 失败必须喊出来

即便 glob 命中，上游 `load_strategy_class_from_module` 的 except 走的是
`self.write_log(traceback)`，而 `CtaEngine.init_engine` 的顺序是
`load_strategy_class` → … → `register_event`：`log_engine` 要到
`register_event` 才订阅 `EVENT_CTA_LOG`，装载期发出的日志**没有任何 handler，
直接被丢弃**。回测器更彻底，它的日志唯一订阅者是要等用户点菜单才构造的面板。

所以这里自己捕获、自己返回，由调用方在启动日志里打印——一个 import 错误让策略
消失而界面上没有痕迹，与本模块要修的 P0 是同一类病。
"""

from __future__ import annotations

import importlib.util
import inspect
import pkgutil
import sys
import traceback
from pathlib import Path
from typing import Any

from vnpy_ctastrategy.template import CtaTemplate

#: 策略包相对本仓根的位置。与 `strategies/__init__.py` 配对，改一处必须改另一处。
PACKAGE: str = "strategies"

#: 本仓根，`fluent_ui/` 的上一级。run_gui.py 以脚本方式启动时它已经是 sys.path[0]，
#: 这里仍然自己算一遍，好让本模块在被别的入口 import 时也成立。
REPO_ROOT: Path = Path(__file__).resolve().parent.parent


def _load_from_file(path: Path, dotted: str) -> Any:
    """按**文件路径**装载，而不是 `importlib.import_module(dotted)`。

    按包名装载会命中 `sys.modules` 的缓存：`strategies` 一旦被别处 import 过，
    后续对同名包的请求就永远拿到第一次那份，与本模块声明的 `REPO_ROOT` 无关。
    这是一条不成文的前提（「仓根必须排在 sys.path 前面」），而不成文的前提正是
    本模块存在要消灭的东西——路径来自哪里必须看得见。

    仍然注册进 `sys.modules`：策略模块内部的 `dataclass`、`pickle` 与
    `inspect.getsource` 都会回头按 `__module__` 找自己，不注册会在这些地方炸。
    """
    spec = importlib.util.spec_from_file_location(dotted, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法为 {path} 构造 import spec")
    module: Any = importlib.util.module_from_spec(spec)
    sys.modules[dotted] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        # 装载失败的半成品留在 sys.modules 里，下一次 import 会拿到一个空壳而
        # 不再报错——那正是「策略静默消失」的另一种走法。
        sys.modules.pop(dotted, None)
        raise
    return module


def discover() -> tuple[dict[str, type[CtaTemplate]], list[str]]:
    """扫 `strategies/` 下每个模块，返回 `{类名: 类}` 与失败说明。

    返回失败说明而不是记日志，是因为本函数在两个引擎的日志订阅建立之前就要跑完
    （见模块 docstring）。调用方拿到列表后用 `main_engine.write_log` 打印，那时
    `log_engine` 已经在听。

    只收 `CtaTemplate` 的**真子类**：策略模块里 `import strategy_state` 这类
    模块级 import 不会把基类带进 `dir()`，但 `from ... import CtaTemplate` 会，
    所以排除基类本身。
    """
    found: dict[str, type[CtaTemplate]] = {}
    failures: list[str] = []

    package_dir: Path = REPO_ROOT / PACKAGE
    if not package_dir.is_dir():
        return found, [f"策略目录不存在：{package_dir}"]

    for info in pkgutil.iter_modules([str(package_dir)]):
        dotted: str = f"{PACKAGE}.{info.name}"
        try:
            module: Any = _load_from_file(package_dir / f"{info.name}.py", dotted)
        except Exception as exc:  # noqa: BLE001 — 一个坏文件不该让其余策略跟着消失
            failures.append(
                f"策略文件 {dotted} 加载失败，该策略不会出现在下拉框里："
                f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"
            )
            continue

        for name in dir(module):
            value: Any = getattr(module, name)
            if (
                inspect.isclass(value)
                and issubclass(value, CtaTemplate)
                and value is not CtaTemplate
            ):
                found[value.__name__] = value

    return found, failures


def install_local_strategies(main_engine: Any) -> tuple[list[str], list[str]]:
    """把本仓策略喂给 CtaEngine，并包好回测器的重载按钮。

    返回 `(装进去的类名, 失败说明)`。调用方必须把两者都打印出来——**装了几个**
    和**哪个没装上**是两条不同的信息，后者才是排障时要的那条。

    幂等：重复调用只是覆盖同名键，并且用 `getattr` 哨兵防止重复包装
    `reload_strategy_class`（包两层会让日志出现两遍）。
    """
    classes, failures = discover()

    # 用 `in main_engine.engines` 而不是直接 get_engine：后者在引擎缺席时会写一条
    # 「找不到引擎：X」的日志，而 headless 入口本来就不装回测器——那行日志会把一个
    # 正常情形说成异常，正是本模块要减少的那类误导。
    engines: dict[str, Any] = main_engine.engines

    if "CtaStrategy" in engines:
        engines["CtaStrategy"].classes.update(classes)

    # 回测器在 run.py / run_live_alpha.py 里不装，缺席是常态而非故障。
    if "CtaBacktester" in engines:
        backtester: Any = engines["CtaBacktester"]
        backtester.classes.update(classes)
        _wrap_reload(backtester, classes)

    return sorted(classes), failures


def _wrap_reload(backtester: Any, classes: dict[str, type[CtaTemplate]]) -> None:
    """让「重载策略」之后本仓策略还在。

    上游的 `reload_strategy_class` 先 `classes.clear()` 再重扫 path1 与 cwd，
    两个都够不到本仓——不包的话，用户点一次按钮策略就消失一次，而且第二次点也
    回不来。包在**之后**而不是之前：要等它清完、扫完，再把我们的补回去。
    """
    if getattr(backtester, "_local_strategies_wrapped", False):
        return

    original = backtester.reload_strategy_class

    def reload_and_restore() -> None:
        original()
        fresh, _ = discover()
        classes.update(fresh)
        backtester.classes.update(fresh)

    backtester.reload_strategy_class = reload_and_restore
    backtester._local_strategies_wrapped = True
