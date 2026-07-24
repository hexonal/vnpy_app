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

    self.items is swapped to the filtered subset only for the duration of
    the popup (menu.exec() blocks until closed, so click handlers that run
    while the menu is open — e.g. _onItemClicked's findText() lookup — see
    the filtered list; the full list is restored in `finally` once exec()
    returns, whether an item was picked or the menu was dismissed).
    """

    def _showComboMenu(self) -> None:
        query = self.text().strip().lower()
        if not query:
            super()._showComboMenu()
            return

        all_items = self.items
        matched = [item for item in all_items if query in item.text.lower()]
        if not matched:
            return

        self.items = matched
        try:
            super()._showComboMenu()
        finally:
            self.items = all_items
