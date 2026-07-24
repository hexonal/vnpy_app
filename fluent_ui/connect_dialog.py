"""
Fluent-native gateway connect dialog — same logic as
vnpy.trader.ui.widget.ConnectDialog, rebuilt on qfluentwidgets'
MessageBoxBase (a proper Fluent modal: title/body area + yes/cancel
buttons) instead of a plain QDialog, with QComboBox/QLineEdit swapped for
qfluentwidgets equivalents. Same connect_gateway() semantics: reads the
current field values, persists them to connect_<gateway>.json, then calls
main_engine.connect(setting, gateway_name) exactly like the stock dialog.
"""

from __future__ import annotations

from typing import cast

from qfluentwidgets import BodyLabel, EditableComboBox, LineEdit, MessageBoxBase, SubtitleLabel

from vnpy.trader.engine import MainEngine
from vnpy.trader.locale import _
from vnpy.trader.ui import QtGui, QtWidgets
from vnpy.trader.utility import load_json, save_json


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

                    if _("密码") in field_name:
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

        self.viewLayout.addWidget(self.title_label)
        self.viewLayout.addLayout(grid)

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

        self.main_engine.connect(setting, self.gateway_name)
        self.accept()
