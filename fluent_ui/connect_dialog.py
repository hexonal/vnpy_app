"""
Fluent-native gateway connect dialog — same logic as
vnpy.trader.ui.widget.ConnectDialog, rebuilt on qfluentwidgets'
MessageBoxBase (a proper Fluent modal: title/body area + yes/cancel
buttons) instead of a plain QDialog, with QComboBox/QLineEdit swapped for
qfluentwidgets equivalents.

This session adds three capability checkboxes (仅提供行情 / 允许交易下单 /
启动时自动连接) that persist to QuestDB via gateway_config alongside the
connect setting. The is_quote/is_trade flags feed the split quote/trade
routing design; auto_connect makes run_gui reconnect the gateway on
startup. connect setting still also saves to connect_<gateway>.json so
nothing regresses if QuestDB is unreachable.

For USMART the dialog also grows an environment switcher (see
usmart_profiles.py): a dropdown over ~/.vntrader/connect_usmart.<name>.json
plus a colour-coded banner that never leaves the screen while the dialog is
open. The banner is not decoration — 「以为在 UAT 结果打到生产」is the one way
this feature can lose real money, so the active environment is stated in
words (【生产 REAL · 真实资金】) as well as in colour, since colour alone
survives neither a screenshot nor a restyled theme.
"""

from __future__ import annotations

from typing import Any, cast

from qfluentwidgets import (
    BodyLabel,
    CheckBox,
    ComboBox,
    EditableComboBox,
    LineEdit,
    MessageBoxBase,
    StrongBodyLabel,
    SubtitleLabel,
)
from vnpy.trader.engine import MainEngine
from vnpy.trader.locale import _
from vnpy.trader.ui import QtGui, QtWidgets
from vnpy.trader.utility import get_file_path, load_json, save_json

from .gateway_config import GatewayConfig, load_config, save_config
from .usmart_profiles import (
    USMART_GATEWAY_NAME,
    ProfileSummary,
    UsmartProfileError,
    activate_profile,
    active_summary,
    discover_profiles,
    harden_permissions,
)

# 横幅配色。生产是告警红,UAT 是琥珀,读不出环境是灰 —— 灰这一档不能省:
# 一个坏掉的 connect_usmart.json 必须长得和「我知道自己在 UAT」明显不同。
BANNER_PRODUCTION = (
    "color: #ffffff; background-color: #a4262c; padding: 6px; border-radius: 4px;"
)
BANNER_SIMULATION = (
    "color: #202020; background-color: #f7d060; padding: 6px; border-radius: 4px;"
)
BANNER_UNKNOWN = (
    "color: #ffffff; background-color: #6b6b6b; padding: 6px; border-radius: 4px;"
)


def paint_environment_banner(label: QtWidgets.QLabel) -> ProfileSummary | None:
    """把一个 label 刷成【当前生效那份文件】的样子,并回传它的摘要。

    对话框里的横幅和主页上的常驻指示器共用这一个函数,不是为了省几行 ——
    两处如果各写一套配色/文案,总有一天它们会对不上,而「两个地方说的环境不
    一样」比「没有指示」更容易让人按错。

    判定源是文件而不是任何一个控件的选中项:选中项表达的是用户点了什么,
    文件表达的是网关待会儿会拿到什么,后者才是会亏钱的那个。
    """
    summary: ProfileSummary | None = active_summary()
    if summary is None:
        label.setText(_("uSMART 环境:未知 —— 读不到 connect_usmart.json"))
        label.setStyleSheet(BANNER_UNKNOWN)
        return None

    label.setText(summary.banner())
    label.setStyleSheet(BANNER_PRODUCTION if summary.is_production else BANNER_SIMULATION)
    return summary


class ConnectDialog(MessageBoxBase):
    def __init__(
        self,
        main_engine: MainEngine,
        gateway_name: str,
        parent: QtWidgets.QWidget | None = None,
    ) -> None:
        super().__init__(parent)

        self.main_engine = main_engine
        self.gateway_name = gateway_name
        self.filename = f"connect_{gateway_name.lower()}.json"

        self.widgets: dict[str, tuple[QtWidgets.QWidget, type]] = {}

        # uSMART 之外的网关这两个永远是 None,init_ui 里的每一处使用都先判空。
        self.profile_combo: ComboBox | None = None
        self.env_banner: StrongBodyLabel | None = None
        self.defaults: dict[str, Any] = {}

        self.init_ui()

    def init_ui(self) -> None:
        self.title_label = SubtitleLabel(_("连接{}").format(self.gateway_name), self)

        default_setting: dict[str, Any] | None = self.main_engine.get_default_setting(
            self.gateway_name
        )
        self.defaults = dict(default_setting or {})
        loaded_setting: dict[str, Any] = load_json(self.filename)
        saved_config: GatewayConfig | None = load_config(self.gateway_name)

        grid = QtWidgets.QGridLayout()
        row = 0

        if default_setting:
            for field_name, field_value in default_setting.items():
                field_type = type(field_value)

                if field_type is list:
                    widget: QtWidgets.QWidget = EditableComboBox()
                    cast(EditableComboBox, widget).addItems(field_value)

                    if field_name in loaded_setting:
                        saved_value = loaded_setting[field_name]
                        ix = cast(EditableComboBox, widget).findText(saved_value)
                        cast(EditableComboBox, widget).setCurrentIndex(ix)
                else:
                    line_widget = LineEdit()
                    line_widget.setText(str(field_value))

                    if field_name in loaded_setting:
                        saved_value = loaded_setting[field_name]
                        line_widget.setText(str(saved_value))

                    lowered = field_name.lower()
                    if _("密码") in field_name or "password" in lowered or "pwd" in lowered:
                        line_widget.setEchoMode(LineEdit.EchoMode.Password)

                    if field_type is int:
                        validator = QtGui.QIntValidator()
                        line_widget.setValidator(validator)

                    widget = line_widget

                # Field gets a usable minimum; the label keeps its natural
                # (sizeHint) width — adaptive sizing below guarantees the
                # dialog grows to fit BOTH, so neither ever clips.
                widget.setMinimumWidth(220)

                label = BodyLabel(f"{field_name} <{field_type.__name__}>")
                grid.addWidget(label, row, 0)
                grid.addWidget(widget, row, 1)
                self.widgets[field_name] = (widget, field_type)

                row += 1

        # Any extra width goes to the input column, never squeezed out of
        # the label column.
        grid.setColumnStretch(1, 1)

        # Capability checkboxes — persisted to QuestDB (gateway_config),
        # pre-filled from the saved config if any. is_quote/is_trade feed
        # the split quote/trade routing; auto_connect drives startup.
        # First-time default (no saved config) is the same conservative
        # choice for every gateway: quote-only, no trading, no auto-connect —
        # the user opts into trading/auto explicitly. (vnpy's default_setting
        # carries no read-only marker to derive capability from, so there's
        # nothing gateway-specific to branch on here.)
        self.quote_check = CheckBox(_("仅提供行情(不接单)"))
        self.trade_check = CheckBox(_("允许交易下单"))
        self.auto_check = CheckBox(_("启动时自动连接"))
        if saved_config is not None:
            self.quote_check.setChecked(saved_config.is_quote)
            self.trade_check.setChecked(saved_config.is_trade)
            self.auto_check.setChecked(saved_config.auto_connect)
        else:
            self.quote_check.setChecked(True)
            self.trade_check.setChecked(False)
            self.auto_check.setChecked(False)

        self.viewLayout.addWidget(self.title_label)

        # 环境切换器排在字段网格【之前】:它一改,下面每个字段的值都会被整份
        # 换掉,顺序上先因后果比反过来好读。非 uSMART 网关这里什么都不加。
        switcher = self._build_profile_switcher()
        if switcher is not None:
            self.viewLayout.addWidget(switcher)

        self.viewLayout.addLayout(grid)
        self.viewLayout.addWidget(self.quote_check)
        self.viewLayout.addWidget(self.trade_check)
        self.viewLayout.addWidget(self.auto_check)

        self.yesButton.setText(_("连接"))
        self.yesButton.clicked.connect(self.connect_gateway)

        self.cancelButton.setText(_("取消"))

        # Adaptive width — the CONTENT decides, not a constant. The old
        # line here was `self.widget.setFixedWidth(self.widget.width() * 2)`:
        # it read width() BEFORE any real layout pass (i.e. a construction-
        # time placeholder value) and doubled it — a number with no
        # relationship to the actual field names. FUTU's short field names
        # happened to fit; USMART's longer ones (rsa_key_path <str>,
        # orderbook_push <str>) got their labels clipped mid-text, because
        # a fixed width forces QGridLayout to shrink the label column below
        # its sizeHint and QLabel just truncates. MessageBoxBase itself
        # imposes no width constraint (verified in its source), so simply
        # removing the fixed width lets Qt's own minimum-size propagation
        # size the dialog to the widest label + the 220px field minimum —
        # correct for any gateway, any field-name length, any font, any
        # locale, with a small floor so a gateway with one tiny field
        # doesn't produce a comically narrow dialog.
        self.widget.setMinimumWidth(max(380, self.widget.sizeHint().width()))

    # ━━━ uSMART 环境切换 ━━━

    def _build_profile_switcher(self) -> QtWidgets.QWidget | None:
        """环境下拉 + 常驻横幅。不是 uSMART 网关就返回 None。

        下拉的第一项固定是「不切换」并且不带 data —— 一个下拉框在被填充时会
        自己发一次 currentIndexChanged,如果第 0 项就是某个 profile,光是打开
        对话框就会静默激活它。这一项存在的唯一理由就是吃掉那次信号。
        """
        if self.gateway_name.upper() != USMART_GATEWAY_NAME:
            return None

        self.env_banner = StrongBodyLabel("", self)
        self.env_banner.setWordWrap(True)

        self.profile_combo = ComboBox(self)
        self._reload_profile_items(None)
        self.profile_combo.currentIndexChanged.connect(self._on_profile_selected)

        row = QtWidgets.QHBoxLayout()
        row.addWidget(BodyLabel(_("环境")))
        row.addWidget(self.profile_combo, 1)

        holder = QtWidgets.QWidget(self)
        box = QtWidgets.QVBoxLayout(holder)
        box.setContentsMargins(0, 0, 0, 0)
        box.addWidget(self.env_banner)
        box.addLayout(row)

        self._show_active_environment()
        return holder

    def _reload_profile_items(self, selected: str | None) -> None:
        """重建下拉内容,并把选中项停在 ``selected`` 上。

        每次切换成功后都要重建一遍,不是为了好看:激活会现产出一份
        ``previous`` 备份,而它本身就是回退入口。不重建的话「切错了一步切回」
        要先关掉对话框再打开 —— 在切错了的那一刻,这一步是最不该有的。

        全程 blockSignals:clear() 与 addItem() 都会发 currentIndexChanged,
        而这个槽会去激活 profile。不挡住信号,重建列表这个纯显示动作会连锁
        触发一次真实的文件替换。
        """
        combo = self.profile_combo
        if combo is None:
            return

        combo.blockSignals(True)
        combo.clear()
        combo.addItem(_("(不切换,沿用当前生效配置)"), userData=None)
        for index, summary in enumerate(discover_profiles(), start=1):
            combo.addItem(summary.describe(), userData=summary.name)
            if summary.name == selected:
                combo.setCurrentIndex(index)
        combo.blockSignals(False)

    def _show_active_environment(self) -> None:
        """横幅回到真实状态。校验失败把下拉退回第 0 项之后也调这一下。"""
        if self.env_banner is not None:
            paint_environment_banner(self.env_banner)

    def _on_profile_selected(self, index: int) -> None:
        combo = self.profile_combo
        if combo is None:
            return

        name = combo.itemData(index)
        if not name:
            return

        try:
            result = activate_profile(str(name))
        except UsmartProfileError as exc:
            # 拒绝是终点,不是提示:配置没有被动过,下拉必须退回「不切换」,
            # 否则界面上写着 uat 而文件里还是 real。blockSignals 是为了让这次
            # 复位不再触发一轮 _on_profile_selected。
            combo.blockSignals(True)
            combo.setCurrentIndex(0)
            combo.blockSignals(False)
            self._show_profile_error(str(exc))
            return

        # 字段值整份换掉,而不是只覆盖新 profile 里出现过的键 —— 见 _apply_setting。
        self._apply_setting(load_json(self.filename))
        self._reload_profile_items(str(name))
        self._show_active_environment()
        # 「还要点连接」这一句不能省:开机自动连接读的是 QuestDB 里那份 setting
        # (run_gui.py:268),不是刚被替换掉的 connect_usmart.json。只切不连,下次
        # 开机自动连上的仍是上一个环境 —— 详见 usmart_profiles 的诚实边界一节。
        self.main_engine.write_log(
            f"uSMART 环境已切换 → {result.describe()} | 需点「连接」才生效,"
            "并同步开机自动连接所用的配置"
        )

    def _show_profile_error(self, message: str) -> None:
        banner = self.env_banner
        if banner is not None:
            banner.setText(_("切换被拒绝(配置未改动):{}").format(message))
            banner.setStyleSheet(BANNER_UNKNOWN)
        self.main_engine.write_log(f"uSMART 环境切换被拒绝: {message}")

    def _apply_setting(self, setting: dict[str, Any]) -> None:
        """用一份 setting 重填全部字段。

        新 profile 里【没有】的键会被退回网关默认值,而不是留着上一个环境的
        旧值。这一条是安全性质的:两份 profile 的键并不完全一致(real 那份多
        一个 kline_right),如果缺键就保留,那么从生产切到一份没写
        trade_password 的 UAT profile 之后,输入框里躺着的仍是生产的交易密码,
        点连接就把它随 UAT 配置一起存了回去。
        """
        for field_name, (widget, field_type) in self.widgets.items():
            default = self.defaults.get(field_name, "")
            if field_type is list:
                fallback = str(default[0]) if isinstance(default, list) and default else ""
                value = str(setting.get(field_name, fallback))
                combo = cast(EditableComboBox, widget)
                ix = combo.findText(value)
                if ix >= 0:
                    combo.setCurrentIndex(ix)
                else:
                    combo.setCurrentText(value)
            else:
                fallback = "" if isinstance(default, list) else str(default)
                cast(LineEdit, widget).setText(str(setting.get(field_name, fallback)))

    def connect_gateway(self) -> None:
        setting: dict[str, Any] = {}

        for field_name, (widget, field_type) in self.widgets.items():
            if field_type is list:
                field_value = str(cast(EditableComboBox, widget).currentText())
            else:
                line_widget = cast(LineEdit, widget)
                try:
                    field_value = field_type(line_widget.text())
                except ValueError:
                    field_value = field_type()
            setting[field_name] = field_value

        save_json(self.filename, setting)

        # save_json 用 open(mode="w+") 建文件,权限落在 umask 上(本机 0644,
        # 同机其他用户可读),而这份文件里是登录密码与交易密码。每次写完都收
        # 一次 —— 收不紧不抛,不能让权限问题挡住连接。
        harden_permissions(get_file_path(self.filename))

        is_trade = self.trade_check.isChecked()

        # Persist capability flags + setting to QuestDB so startup can
        # auto-connect and the routing layer can read is_quote/is_trade.
        # The persisted setting stays pure connection params — the runtime
        # quote_only flag is derived from is_trade at connect time.
        save_config(GatewayConfig(
            gateway_name=self.gateway_name,
            is_quote=self.quote_check.isChecked(),
            is_trade=is_trade,
            auto_connect=self.auto_check.isChecked(),
            setting=setting,
        ))

        # Trading not enabled → connect in quote-only mode so the gateway
        # skips the trade context + account/position queries (which error
        # out when the account has no trading authority for a market).
        connect_setting = {**setting, "quote_only": not is_trade}
        self.main_engine.connect(connect_setting, self.gateway_name)
        self.accept()
