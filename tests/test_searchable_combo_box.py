"""Tests for SearchableComboBox type-to-search — the QCompleter that makes
typing a code surface matching contracts (the field looked broken before:
typing showed nothing until the dropdown arrow was clicked), including the
HK leading-zero tolerance (typing '001' finds '1.SEHK').
"""

from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vnpy.trader.ui import QtCore, create_qapp  # noqa: E402

_APP = create_qapp("test")

from fluent_ui.searchable_combo_box import SearchableComboBox, _strip_leading_zeros  # noqa: E402


def _assert(name: str, cond: bool) -> None:
    print(f"  {'PASS' if cond else 'FAIL'}: {name}")
    if not cond:
        raise AssertionError(name)


def _matches(box: SearchableComboBox, typed: str) -> list[str]:
    """The completion candidates for a typed query, as the qfluentwidgets
    completer machinery would compute them (sync model, then set prefix)."""
    box._sync_completer_model(typed)
    comp = box.completer()
    comp.setCompletionPrefix(typed)
    model = comp.completionModel()
    return [model.index(i, 0).data() for i in range(model.rowCount())]


def test_strip_leading_zeros() -> None:
    _assert("001 -> 1", _strip_leading_zeros("001") == "1")
    _assert("0700 -> 700", _strip_leading_zeros("0700") == "700")
    _assert("000 -> 0", _strip_leading_zeros("000") == "0")
    _assert("700 unchanged", _strip_leading_zeros("700") == "700")
    _assert("AAPL unchanged (non-numeric)", _strip_leading_zeros("AAPL") == "AAPL")


def test_fuzzy_contains_match() -> None:
    box = SearchableComboBox()
    box.addItems(["1.SEHK", "10.SEHK", "700.SEHK", "AAPL.SMART", "MU.SMART"])
    # substring, not just prefix
    _assert("700 finds 700.SEHK", "700.SEHK" in _matches(box, "700"))
    # case-insensitive
    _assert("lowercase aapl finds AAPL.SMART", _matches(box, "aapl") == ["AAPL.SMART"])
    _assert("MU finds MU.SMART", "MU.SMART" in _matches(box, "MU"))


def test_hk_leading_zero_query_finds_unpadded_symbol() -> None:
    box = SearchableComboBox()
    box.addItems(["1.SEHK", "10.SEHK", "700.SEHK", "AAPL.SMART"])
    m = _matches(box, "001")
    _assert("typing 001 finds 1.SEHK", "1.SEHK" in m)
    m2 = _matches(box, "0700")
    _assert("typing 0700 finds 700.SEHK", "700.SEHK" in m2)


def test_completer_model_tracks_items() -> None:
    box = SearchableComboBox()
    box.addItems(["1.SEHK"])
    _matches(box, "1")  # forces a sync
    _assert("model has the one symbol", box._completer_model.stringList() == ["1.SEHK"])
    # simulate a market-filter rebuild (clear + new items)
    box.clear()
    box.addItems(["AAPL.SMART", "MU.SMART"])
    got = _matches(box, "MU")
    _assert("model rebuilt after clear+addItems", "MU.SMART" in got)
    _assert("stale HK symbol gone from model", "1.SEHK" not in box._completer_model.stringList())


def main() -> None:
    tests = [
        test_strip_leading_zeros,
        test_fuzzy_contains_match,
        test_hk_leading_zero_query_finds_unpadded_symbol,
        test_completer_model_tracks_items,
    ]
    for t in tests:
        print(t.__name__)
        t()
        sys.stdout.flush()
    print(f"\nAll {len(tests)} tests passed.")


if __name__ == "__main__":
    main()
