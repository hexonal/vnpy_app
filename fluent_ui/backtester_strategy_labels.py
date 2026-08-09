"""给回测器的策略下拉框标上中文名。

下拉框里原本只有类名（AtrRsiStrategy / DualThrustStrategy / KingKeltnerStrategy
…）。对读过源码的人够用，但对着界面选策略的人看不出它们分别用什么方法 ——
而选错策略再跑一遍回测，浪费的是时间和对结果的信任。

说明本身住在 vnpy_ctastrategy（策略本体在那儿，改策略时顺手改说明才不会脱节），
这里只负责把它接到回测器界面上。

## 为什么用运行时接管而不是直接改

vnpy_ctabacktester 是 PyPI 装的上游包，不在本项目的仓里。直接改 site-packages
的后果是：重装即失效，且改动不在任何 git 历史里。所以沿用本目录既有的做法
（backtester_segments / backtester_gates / backtester_metrics 都是这个路子）：
包装 BacktesterManager 的方法，在上游做完自己的事之后再改标。

## 钩子必须挂在"下拉框已经有内容"之后

第一版挂在 `init_ui` 上，一条也没改到 —— 因为 `class_combo` 不是在 `init_ui`
里填的：`__init__` 的顺序是 `init_ui()`(建控件) → `init_strategy_settings()`
(addItems 填内容) → `load_backtesting_setting()`(按存档选中)。挂在 init_ui
就等于对着一个空下拉改标，而安装函数照样返回"已包装"，启动日志于是报了一句
不成立的成功。现在挂在 `load_backtesting_setting` 之后（`__init__` 的最后一步），
并且安装函数只声称"已挂钩"，不声称"已改标" —— 改没改得等到界面真建起来。

## 一个必须配对的改动

改了下拉框的**显示文本**，就必须改**每一个读取方** —— 上游用
`class_combo.currentText()` 取类名，而显示文本现在是
`AtrRsiStrategy · ATR+RSI 波动突破`。漏掉一处，那条路就会拿着带中文的字符串
去找策略类。上游共四处读取：start_backtesting / start_optimization /
edit_strategy_code / reload_strategy_class（`show_optimization_result` 不读，
第一版把它也包了，是白包的）。

## 标上中文名，也就把左栏撑宽了 —— 这笔账由本模块自己还

`class_combo` 是左栏 QFormLayout 的第一行（`vnpy_ctabacktester/ui/widget.py:157`），
整列宽度跟着它走。而 QComboBox 默认的 sizeAdjustPolicy 是
AdjustToContentsOnFirstShow：第一次显示时按最长条目一次性撑开、此后不再回缩。
10 条策略（含本仓自研的 LongOnlyTurtleStrategy）的裸类名量出来 sizeHint 是
198px，标上中文之后 375px（+89%）—— 多出来的 177px 全部是本模块加的字，
而参数区其余九行（本地代码 / 日期 / 手续费 …）跟着一起变宽。宽度问题是本模块
造成的，修在别处等于把因果拆开。

改成 AdjustToMinimumContentsLengthWithIcon + setMinimumContentsLength(18)：
sizeHint 定在 243px，且**与条目内容彻底脱钩**，以后再加策略、再加长说明都不会
继续变宽。18 这个数是量出来的不是估的 —— 12/14/16/18/20/22/24 分别对应
177/199/221/243/265/287/309px，18 是「比裸类名那档的 198px 略宽、常见类名仍能
整条显示」的那一级。代价是最长的那条
`DualThrustStrategy · Dual Thrust 开盘区间突破`（39 字）在收起状态下会被省略号
截断 —— 展开的弹窗与 tooltip 里都是全文，所以丢的只是「不点开也能读全」。

## setMaxVisibleItems 在本机是空转，真正压住弹窗的是 view 的高度上限

弹窗盖住下方内容，直觉修法是 setMaxVisibleItems(8)。实测【无效】：本机
`app.style()` 是 QStyleSheetStyle 代理 macOS 样式，
`styleHint(SH_ComboBox_Popup, opt, combo)` 返回 1，而 Qt 文档写明该属性
「对非可编辑下拉框、且样式的 SH_ComboBox_Popup 为真时被忽略」。量到的弹窗高度
恒为 266px（10 行 × 26px + 6px 边框），设 3 / 5 / 8 / 20 四个值全是 266。

试过 `setStyleSheet("QComboBox { combobox-popup: 0; }")` 把那个 styleHint 掰回
false：弹窗高度确实降到 214px，但弹窗宽度同时从 387px 塌到与框同宽的 243px ——
Qt 只在 usePopup 那条分支里按最宽条目加宽弹窗，掰掉 styleHint 就一并掰掉了加宽，
给 view 设 minimumWidth 也救不回来（实测仍是 243px；只有给弹窗容器
`view().parentWidget()` 设才有效，那已经是在摸 Qt 的私有控件了）。结果是刚加上去
的中文说明全被省略号截掉 —— 用可读性换高度，不划算。

所以改成给 view 直接设 maximumHeight = 8 行：弹窗 266px → 214px，而宽度仍是按最宽
条目算出的 387px，中文说明一个字不丢。setMaxVisibleItems(8) 照样调 —— 它在
Fusion / Windows 样式下才是生效的那条路（CI 跑在 ubuntu），两条一起写才是两边都对。

## 市场标注只进 tooltip，不进 itemText

港股与美股都要是一等公民，那「这条策略在这两个市场能不能真的跑」就该写在选策略
的地方。但它【不能】写进 itemText：上面刚把 sizeHint 从 375px 收到 243px，靠的正是
「条目文本不再决定框宽」，可**弹窗**宽度仍按最宽条目算 —— 再往 itemText 里塞一句
「会开空仓：港股需…」，弹窗就得再宽出两百多像素，等于把刚治好的病换个地方复发。
tooltip 不参与任何尺寸计算，是唯一不付宽度代价的载体。

标注的轴是【会不会开空仓】，不是「支持哪个市场」。港股与美股的现货/现金账户都不能
自由做空，这是两市共同的门槛而不是某一市的短板；随包九条示例策略里有八条会反手
做空，回测出来的双向收益在现金账户上只能兑现一半。

这张表只能手写、不能靠扫源码自动生成：MultiSignalStrategy 走 TargetPosTemplate，
目标仓位由子信号投票得出、可以是 −1（`multi_signal_strategy.py:48` 的
`set_signal_pos(-1)`），全文一次 `self.short(` 都没有 —— 按源码文本判定会把它标成
「只做多」，而那是一句关于真实交易行为的错话。

代价说清楚：说明文本因此散在两处（中文名与一句话说明在 vnpy_ctastrategy 的
`_STRATEGY_LABELS`，市场标注在本文件）。fork 那边加一条策略而这边忘了跟，表就会
缺一行。用一条会红的测试兜住（`test_backtester_strategy_labels.py` 比对两张表的
键集），而不是留一句待办。
"""

from __future__ import annotations

from typing import Any

from vnpy.trader.ui import QtCore, QtWidgets
from vnpy_ctastrategy.ui.widget import describe_strategy

_INSTALLED_FLAG = "_vnpy_app_strategy_labels_installed"

# 只读类名的方法：临时把显示文本换回纯类名，调完换回来。
_READERS = ("start_backtesting", "start_optimization", "edit_strategy_code")

# 既读类名、又把整个下拉清空重填的方法：调完必须重新标一遍。
_REPOPULATORS = ("reload_strategy_class",)

# 在它跑完之后下拉框才有内容，是第一次标注的时机。
_POPULATED_AFTER = "load_backtesting_setting"

#: 收起状态下按多少个字符定宽。这是【定值】不是上限 —— 换成
#: AdjustToMinimumContentsLengthWithIcon 之后，条目文本再长也不会把框撑开。
CONTENTS_LENGTH = 18

#: 弹窗最多同时露出几条。超出的靠滚动，而不是把弹窗铺满整块面板。
VISIBLE_ITEMS = 8


# 会开空仓的策略共用这一句。写的是港股与美股【共同】的那道门槛，不是某一市的短板 ——
# 两边的现金账户都不能自由做空，回测里那一半空头收益在现金账户上根本不存在。
_SHORTS = (
    "会开空仓：港股要求该股在联交所「可进行卖空的指定证券」名单内、且账户为孖展账户，"
    "美股要求融券账户且借得到券。现金账户上空头那一半报不出去，"
    "回测的双向收益不能照搬"
)

_LONG_ONLY = "只做多、不反手：港股与美股的现金账户都能照跑，不依赖任何卖空资格"

# DualThrust 是唯一一条日内平仓的示例策略，而它的平仓时刻是写死的、不在
# parameters 里，界面上改不到 —— 这一条对港股和美股同样致命，所以单列。
_INTRADAY = (
    "日内进出，但平仓时刻 exit_time 硬编码为 14:55（中国期货收盘前，"
    "见 dual_thrust_strategy.py:46），不是可调参数。港股 16:00 收市、美股 16:00 ET 收市，"
    "照跑会提前约一小时清仓。另：美股保证金账户净值低于 25000 美元会触发 PDT 限制，"
    "港股股票则可日内回转（T+0 成交、T+2 交收）"
)

#: 策略类名 -> 这条策略在港股/美股落地时会撞上什么。查不到就不标，不编。
_MARKET_NOTES: dict[str, str] = {
    "AtrRsiStrategy": _SHORTS,
    "BollChannelStrategy": _SHORTS,
    "DoubleMaStrategy": _SHORTS,
    "DualThrustStrategy": f"{_SHORTS}。{_INTRADAY}",
    "KingKeltnerStrategy": _SHORTS,
    "LongOnlyTurtleStrategy": _LONG_ONLY,
    "MultiSignalStrategy": _SHORTS,
    "MultiTimeframeStrategy": _SHORTS,
    "TestStrategy": "不发任何委托，与市场无关",
    "TurtleSignalStrategy": _SHORTS,
}


def describe_market(class_name: str) -> str:
    """策略类名 -> 港股/美股落地提示。查不到返回空串。

    空串是有意的回落：树外策略随时可能出现，而对一条没人核过的策略讲
    「现金账户能跑」是在替用户担保，比什么都不说危险得多。
    """
    return _MARKET_NOTES.get(class_name, "")


def _tooltip_for(class_name: str, hint: str) -> str:
    """把一句话说明与市场标注拼成 tooltip 文本。

    分两行而不是拼成一句：这两件事的时效不一样 —— 说明跟着策略逻辑走，
    市场标注跟着账户与交易所规则走，看的人得分得清哪句是哪句。
    """
    note = describe_market(class_name)
    return f"{hint}\n市场：{note}" if note else hint


def _fit_combo(combo: QtWidgets.QComboBox) -> None:
    """把下拉框的宽度与弹窗高度钉住。为什么是这三行、为什么不是别的，见模块说明。

    对已经调过的下拉框重复调用无害：三个属性都是幂等赋值，view 的高度上限每次
    按当下的行高重算（字号变了也能跟上）。
    """
    combo.setSizeAdjustPolicy(
        QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
    )
    combo.setMinimumContentsLength(CONTENTS_LENGTH)
    combo.setMaxVisibleItems(VISIBLE_ITEMS)

    # 行高得有条目才量得出来（空列表时 sizeHintForRow 返回 -1）。量不出来就不设
    # 上限 —— 拿 -1 去算会得到一个负的 maximumHeight，弹窗直接塌成一条缝。
    view = combo.view()
    row_height = view.sizeHintForRow(0)
    if row_height > 0:
        view.setMaximumHeight(VISIBLE_ITEMS * row_height + 2 * view.frameWidth())


def _relabel(combo: QtWidgets.QComboBox) -> int:
    """把下拉框里的类名换成 `类名 · 中文`，类名存进 userData。

    幂等：已经带过 userData 的项不再处理，重复调用不会把说明叠加两遍。
    返回本次改动的条目数（0 表示下拉框是空的或早已标过）。
    """
    changed = 0
    for i in range(combo.count()):
        if combo.itemData(i):            # 已处理过
            continue
        class_name = combo.itemText(i)
        shown, hint = describe_strategy(class_name)
        combo.setItemText(i, shown)
        combo.setItemData(i, class_name)
        combo.setItemData(i, _tooltip_for(class_name, hint), QtCore.Qt.ItemDataRole.ToolTipRole)
        changed += 1
    return changed


def _class_name_of(combo: QtWidgets.QComboBox) -> str:
    """当前选中项的类名。userData 缺失时回落到显示文本 —— 那说明本模块没装上，
    此时显示文本就还是原始类名，回落是正确的而不是凑合。"""
    data = combo.currentData()
    return str(data) if data else str(combo.currentText())


def _combo_of(manager: Any) -> QtWidgets.QComboBox | None:
    combo = getattr(manager, "class_combo", None)
    return combo if isinstance(combo, QtWidgets.QComboBox) else None


def _wrap(original: Any, kind: str) -> Any:
    """按用途包装一个上游方法。

    kind='read'      读类名 -> 调用期间把显示文本临时换回纯类名
    kind='repopulate' 读类名且会清空重填 -> 同上，调完重新标注整个列表
    kind='populate'  调完下拉框才有内容 -> 直接标注
    """

    def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
        combo = _combo_of(self)
        if combo is None:
            return original(self, *args, **kwargs)

        if kind == "populate":
            result = original(self, *args, **kwargs)
            _relabel(combo)
            _fit_combo(combo)
            return result

        if not combo.currentData():                 # 还没标过，无需换回
            result = original(self, *args, **kwargs)
            if kind == "repopulate":
                _relabel(combo)
                _fit_combo(combo)
            return result

        # 让上游的 currentText() 读到纯类名：临时把显示文本换回去。
        index = combo.currentIndex()
        shown = combo.itemText(index)
        combo.setItemText(index, _class_name_of(combo))
        try:
            result = original(self, *args, **kwargs)
        finally:
            if kind == "repopulate":
                # 列表已被清空重填，逐项标注即可；旧的 shown 已无对应项。
                _relabel(combo)
                _fit_combo(combo)
            else:
                combo.setItemText(index, shown)
        return result

    setattr(wrapper, _INSTALLED_FLAG, True)
    return wrapper


def install_strategy_labels() -> list[str]:
    """挂上钩子。返回被包装的方法名。

    注意返回的是"已挂钩"，不是"已改标" —— 真正改标要等界面建起来、下拉框
    填上内容之后才发生。调用方的日志措辞不要越过这一点。
    """
    from vnpy_ctabacktester.ui.widget import BacktesterManager

    patched: list[str] = []
    plan = (
        [(name, "read") for name in _READERS]
        + [(name, "repopulate") for name in _REPOPULATORS]
        + [(_POPULATED_AFTER, "populate")]
    )

    for method_name, kind in plan:
        original = getattr(BacktesterManager, method_name, None)
        if original is None or getattr(original, _INSTALLED_FLAG, False):
            continue
        setattr(BacktesterManager, method_name, _wrap(original, kind))
        patched.append(f"BacktesterManager.{method_name}")

    return patched
