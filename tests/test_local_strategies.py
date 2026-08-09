"""本仓策略必须能进两个引擎的下拉框——它们自己扫不到。

这组用例钉住的是一个【彻底静默】的故障：两个引擎都扫 `Path.cwd()/"strategies"`，
而 `MainEngine.__init__` 已经 `os.chdir(TRADER_DIR)`，于是扫的是 `~/strategies`
——不存在，`glob` 返回空，一次 import 都不发生，一条日志都没有。用户在界面上
看到的是「下拉框里只有上游那 9 个」，没有任何线索指向原因。

所以这里既测「装进去了」，也测「装不进去时会喊」——后者才是当初缺的那一半。

`install_local_strategies` 用 Fake 引擎驱动而不是真 MainEngine：真引擎会
`os.chdir`，把后续用例的相对路径一起改掉（CLAUDE.md 第 7 节记着这个坑）。
真装配的顺序另有一条用例覆盖，跑在 tmp_path 里。
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fluent_ui.local_strategies import PACKAGE, REPO_ROOT, discover, install_local_strategies


class _FakeEngineWithClasses:
    """只有 `classes` 一个字段的引擎替身——注入路径只碰这一个属性。"""

    def __init__(self) -> None:
        self.classes: dict[str, type] = {}


class _FakeBacktester(_FakeEngineWithClasses):
    """回测器多一个 `reload_strategy_class`，且它会先清空——上游就是这么写的。"""

    def __init__(self) -> None:
        super().__init__()
        self.reload_calls: int = 0

    def reload_strategy_class(self) -> None:
        self.reload_calls += 1
        self.classes.clear()
        self.classes["UpstreamOnly"] = type("UpstreamOnly", (), {})


class _FakeMainEngine:
    def __init__(self, **engines: object) -> None:
        self.engines: dict[str, object] = dict(engines)


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def test_discover_finds_the_repo_strategy_the_engines_cannot_see() -> None:
    found, failures = discover()
    assert "LongOnlyTurtleStrategy" in found
    assert failures == []


def test_discover_does_not_return_the_base_class_itself() -> None:
    """策略模块 `from vnpy_ctastrategy import CtaTemplate`，基类会进 dir()。"""
    found, _ = discover()
    assert "CtaTemplate" not in found


def test_the_strategy_package_sits_where_this_module_says_it_does() -> None:
    """PACKAGE 与 REPO_ROOT 是本模块对目录布局的两条硬假设，搬家必须让它转红。"""
    assert (REPO_ROOT / PACKAGE / "__init__.py").is_file()


# ---------------------------------------------------------------------------
# Injection
# ---------------------------------------------------------------------------


def test_both_engines_receive_the_local_strategies() -> None:
    cta = _FakeEngineWithClasses()
    backtester = _FakeBacktester()
    added, failures = install_local_strategies(
        _FakeMainEngine(CtaStrategy=cta, CtaBacktester=backtester)
    )
    assert "LongOnlyTurtleStrategy" in added
    assert failures == []
    assert "LongOnlyTurtleStrategy" in cta.classes
    assert "LongOnlyTurtleStrategy" in backtester.classes


def test_a_missing_backtester_is_not_an_error() -> None:
    """run.py 与 run_live_alpha.py 都不装回测器，缺席是常态。"""
    cta = _FakeEngineWithClasses()
    added, failures = install_local_strategies(_FakeMainEngine(CtaStrategy=cta))
    assert "LongOnlyTurtleStrategy" in added
    assert failures == []


def test_reloading_in_the_backtester_puts_the_local_strategies_back() -> None:
    """上游的 [重载策略] 先 classes.clear() 再只扫它够得到的两个目录。

    不包这一层的话，用户点一次按钮策略消失一次，而且再点也回不来。
    """
    backtester = _FakeBacktester()
    install_local_strategies(_FakeMainEngine(CtaBacktester=backtester))

    backtester.reload_strategy_class()

    assert backtester.reload_calls == 1
    assert "UpstreamOnly" in backtester.classes
    assert "LongOnlyTurtleStrategy" in backtester.classes


def test_wrapping_the_reload_twice_does_not_stack() -> None:
    """幂等：装两遍不该让上游的 reload 跑两遍（日志会出现两次）。"""
    backtester = _FakeBacktester()
    engine = _FakeMainEngine(CtaBacktester=backtester)
    install_local_strategies(engine)
    install_local_strategies(engine)

    backtester.reload_strategy_class()
    assert backtester.reload_calls == 1


# ---------------------------------------------------------------------------
# Loud failure
# ---------------------------------------------------------------------------


def test_a_broken_strategy_file_is_reported_instead_of_vanishing(
    tmp_path, monkeypatch
) -> None:
    """一个 import 错误让策略消失而界面无提示，与本模块要修的 P0 是同一类病。

    上游的 except 分支写的是 `self.write_log(traceback)`，但 CtaEngine 的
    `register_event` 在 `load_strategy_class` 之后才订阅 EVENT_CTA_LOG——装载期
    的日志没有任何 handler，直接被丢弃。所以这里自己返回，不靠日志。
    """
    package = tmp_path / "strategies"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "broken_strategy.py").write_text("import definitely_not_a_module\n")

    monkeypatch.setattr("fluent_ui.local_strategies.REPO_ROOT", tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))

    found, failures = discover()

    assert found == {}
    assert len(failures) == 1
    assert "broken_strategy" in failures[0]
    assert "definitely_not_a_module" in failures[0]
    assert "不会出现在下拉框里" in failures[0]


def test_a_missing_strategy_directory_is_reported(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("fluent_ui.local_strategies.REPO_ROOT", tmp_path)
    found, failures = discover()
    assert found == {}
    assert len(failures) == 1
    assert "策略目录不存在" in failures[0]


def test_one_broken_file_does_not_hide_its_healthy_neighbours(
    tmp_path, monkeypatch
) -> None:
    package = tmp_path / "strategies"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "broken_strategy.py").write_text("import definitely_not_a_module\n")
    (package / "healthy_strategy.py").write_text(
        "from vnpy_ctastrategy import CtaTemplate\n"
        "\n"
        "\n"
        "class HealthyStrategy(CtaTemplate):\n"
        "    pass\n"
    )

    monkeypatch.setattr("fluent_ui.local_strategies.REPO_ROOT", tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))

    found, failures = discover()

    assert "HealthyStrategy" in found
    assert len(failures) == 1


# ---------------------------------------------------------------------------
# The regression this whole module exists for
# ---------------------------------------------------------------------------


def test_the_engines_own_scan_path_still_cannot_reach_this_repo() -> None:
    """病根还在上游，本模块只是绕过它——哪天上游修了，这条会转红提醒删代码。

    刻意不去 os.chdir：直接对着 TRADER_DIR 求值，与引擎 chdir 之后看到的是同一个
    目录，而不会污染其余用例的相对路径。
    """
    from vnpy.trader.utility import TRADER_DIR

    assert not (TRADER_DIR / "strategies").is_dir()
    assert (REPO_ROOT / "strategies").is_dir()
    assert TRADER_DIR != REPO_ROOT


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
