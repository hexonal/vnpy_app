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
"""

from __future__ import annotations

from typing import cast

from qfluentwidgets import BodyLabel, CheckBox, EditableComboBox, LineEdit, MessageBoxBase, SubtitleLabel

from vnpy.trader.engine import MainEngine
from vnpy.trader.locale import _
from vnpy.trader.ui import QtGui, QtWidgets
from vnpy.trader.utility import load_json, save_json

from .gateway_config import GatewayConfig, load_config, save_config


class ConnectDialog(MessageBoxBase):
    def __init__(self, main_engine: MainEngine, gateway_name: str, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)

        self.main_engine = main_engine
        self.gateway_name = gateway_name
        self.filename = f"connect_{gateway_name.lower()}.json"

        self.widgets: dict[str, tuple[QtWidgets.QWidget, type]] = {}

        self.init_ui()

    def init_ui(self) -> None:
        self.title_label = SubtitleLabel(_("连接{}").format(self.gateway_name), self)

        default_setting: dict | None = self.main_engine.get_default_setting(self.gateway_name)
        loaded_setting: dict = load_json(self.filename)
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

    def connect_gateway(self) -> None:
        setting: dict = {}

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
