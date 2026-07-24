"""
qfluentwidgets.EditableComboBox does NOT actually filter its dropdown by
the typed text, despite being "editable" — read _showComboMenu() in
qfluentwidgets' own combo_box.py: it unconditionally lists every
self.items entry with no text-matching logic at all. Typing there is only
usable for committing a brand-new free-text entry on Enter
(_onReturnPressed), not for narrowing the existing list. An earlier
version of data_manager.py's docstring claimed EditableComboBox already
"支持 type-to-filter" — that was wrong, corrected after actually reading
the library source instead of assuming from the class name.

Shared here (not defined inside data_manager.py) because chart_wizard.py
needs the exact same behavior for its "本地代码" field — one Fluent-native
app-widget module importing another's internals would be the wrong
direction of coupling for what's really a small, generic Qt widget.
"""

from __future__ import annotations

from qfluentwidgets import EditableComboBox

from vnpy.trader.ui import QtCore, QtWidgets


class SearchableComboBox(EditableComboBox):
    """
    EditableComboBox with the type-to-filter behavior its name implies but
    doesn't actually provide (see module docstring). Overrides
    _showComboMenu() to only list items whose text contains the currently
    typed text (case-insensitive substring match) — the dropdown-button
    click path is the only thing qfluentwidgets builds a menu from, so this
    single override covers both "click the arrow" and the equivalent
    internal call path; typing still falls through to EditableComboBox's
    own _onReturnPressed for committing free text not in the list.

    The visible menu is ALWAYS capped at MAX_MENU_ITEMS. This is not
    cosmetic: ComboBoxBase._showComboMenu() builds one QAction per item
    and RoundMenu.addAction() additionally constructs a QListWidgetItem
    and re-runs adjustSize() per action — with a contract list the size a
    connected FutuGateway produces (~22,000), an uncapped first click
    (empty query) synchronously allocates ~22k actions + ~22k list items
    on the UI thread and freezes the window for seconds (found by an
    independent audit of this file; verified against the installed
    qfluentwidgets source, combo_box.py:312-351 / menu.py:380-390).
    Placeholder text on the call sites tells users to type to narrow.

    self.items is swapped to the capped/filtered subset only for the
    duration of the popup. self._currentIndex MUST be swapped in lockstep:
    the base _showComboMenu() does `menu.actions()[self.currentIndex()]`
    to highlight the selected item (qfluentwidgets combo_box.py:335), and
    self._currentIndex is a raw position into the FULL list — with a
    sliced self.items, an index past the slice length is an IndexError
    raised inside the Qt click slot (crashes the app). So the index is
    remapped to the selected item's position within the sliced list (or
    -1 if the selection isn't in the shown subset), and both are restored
    in `finally`. _onItemClicked resolves clicks via findText() against
    the shown subset, so a pick still maps to the right item.
    """

    MAX_MENU_ITEMS = 50

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        # A vt_symbol is always Latin (e.g. 1.SEHK / AAPL.SMART), never CJK.
        # Declaring the entry field Latin-only stops the macOS pinyin input
        # method from popping its candidate window over this box and
        # injecting stray characters when the user has a Chinese input
        # source active — the "1啊 2阿 …" candidate bar seen hijacking symbol
        # entry. Hints are set on the inner LineEdit (the actual focus/IME
        # target), with a fallback to the combo itself.
        hints = (
            QtCore.Qt.InputMethodHint.ImhLatinOnly
            | QtCore.Qt.InputMethodHint.ImhNoPredictiveText
            | QtCore.Qt.InputMethodHint.ImhNoAutoUppercase
        )
        self.setInputMethodHints(hints)
        line_edit = getattr(self, "lineEdit", None)
        if line_edit is not None and hasattr(line_edit, "setInputMethodHints"):
            line_edit.setInputMethodHints(hints)

    def _showComboMenu(self) -> None:
        query = self.text().strip().lower()
        all_items = self.items
        all_index = self._currentIndex
        if query:
            matched = [item for item in all_items if query in item.text.lower()]
            if not matched:
                return
        else:
            matched = all_items

        shown = matched[: self.MAX_MENU_ITEMS]
        # Remap the highlighted index into the shown subset — the base
        # class indexes menu.actions() (== len(shown)) with currentIndex().
        selected = all_items[all_index] if 0 <= all_index < len(all_items) else None
        self.items = shown
        self._currentIndex = shown.index(selected) if selected in shown else -1
        try:
            super()._showComboMenu()
        finally:
            self.items = all_items
            self._currentIndex = all_index
