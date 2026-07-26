"""A date picked in the GUI must mean the market's session, not host midnight.

Both GUI entry points into stored bars build their bounds from a calendar
widget, i.e. a bare year/month/day with no timezone: ``DateRangeDialog``
(data manager: 查看数据 / 导出数据) and ``ReplayChartWidget``'s start/end
pickers. Every database driver funnels those bounds through
``vnpy.trader.database.convert_tz``, which calls ``datetime.astimezone()``;
on a naive datetime that reads the value as the **host's** zone. Picking
2024-01-26 for a Hong Kong symbol on a US-Pacific machine therefore asks for
a window that starts eight hours into the session, and the morning's bars
come back missing with no error anywhere — the table just looks short.

``TZ`` is pinned to US Pacific so the host offset is a known non-zero value
instead of whatever the machine or CI runner happens to be set to, and the
database double filters through the very ``convert_tz`` the real drivers
call, so the defect reproduces through the drivers' own code path without a
live QuestDB.

The widget methods are exercised unbound, against a duck-typed ``self``: the
bug lives entirely in how each method turns picker output into query bounds,
and building a real ``ManagerWidget`` would drag in a MainEngine, a live
database and four pyqtgraph chart panes without making the assertion any
stronger. The engine underneath the data-manager tests is the real
``vnpy_datamanager.ManagerEngine``, so the query path being measured is
production code, not a restatement of it.
"""

from __future__ import annotations

import csv
import os
import sys
import time
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vnpy.trader.constant import Exchange, Interval
from vnpy.trader.database import convert_tz
from vnpy.trader.object import BarData
from vnpy.trader.ui import QtWidgets
from vnpy_datamanager import engine as manager_engine

# Deliberately no create_qapp(): nothing here paints. QTableWidgetItem (which
# data_manager.DataCell subclasses) is not a QWidget and constructs fine
# without an application object, and every widget these tests would otherwise
# need is stubbed. Creating one here would also break
# test_searchable_combo_box, whose create_qapp() call constructs
# unconditionally and raises if a QApplication already exists.
import replay_chart
from fluent_ui import data_manager

HK_TZ = ZoneInfo("Asia/Hong_Kong")
SYMBOL = "700"
EXCHANGE = Exchange.SEHK
INTERVAL = Interval.MINUTE

# Two Hong Kong sessions, three bars each. The 09:30 and 12:00 bars of the
# first session are what a host-local reading of "2024-01-26" loses: they sit
# at 01:30 and 04:00 UTC, ahead of the 08:00 UTC that naive midnight means on
# a US-Pacific machine. The 16:00 bar lands exactly on that boundary and
# survives either way, which is what makes the loss a silent, partial one.
SESSION_DAYS = (26, 29)
SESSION_HOURS = ((9, 30), (12, 0), (16, 0))

# What the calendar pickers hand over, spanning both sessions. Naive on
# purpose: a bare year/month/day is the most a CalendarPicker can say, and
# that ambiguity is the whole subject of this module.
PICKED_START = datetime(2024, 1, 26)
PICKED_END = datetime(2024, 1, 30)

pytestmark = pytest.mark.skipif(
    not hasattr(time, "tzset"),
    reason="test pins the host timezone via TZ, which needs time.tzset (POSIX)",
)


def _bar(moment: datetime, close: float) -> BarData:
    return BarData(
        symbol=SYMBOL,
        exchange=EXCHANGE,
        datetime=moment,
        interval=INTERVAL,
        volume=100.0,
        turnover=100.0 * close,
        open_price=close,
        high_price=close,
        low_price=close,
        close_price=close,
        gateway_name="TEST",
    )


def _stored_bars() -> list[BarData]:
    """The sessions as a gateway writes them: stamped in the market's own zone.

    ``vnpy_gatewaykit.market_clock`` attaches that zone at the gateway
    boundary, so an HK 09:30 bar really is 09:30 in Hong Kong — which is why a
    host-local reading of the query bound lands on the wrong side of it.
    """
    return [
        _bar(datetime(2024, 1, day, hour, minute, tzinfo=HK_TZ), 100.0 + index)
        for index, (day, (hour, minute)) in enumerate(
            (day, hm) for day in SESSION_DAYS for hm in SESSION_HOURS
        )
    ]


class FilteringDatabase:
    """The read surface these widgets touch, filtered the way drivers filter.

    Both the stored timestamps and the query bounds go through ``convert_tz``
    — the same normalisation QuestDB/SQLite/MySQL apply — and the window is
    inclusive at both ends, matching their SQL. A bound this double accepts is
    a bound the real drivers accept too.
    """

    def __init__(self, bars: list[BarData]) -> None:
        self.bars = bars
        self.bounds: list[tuple[datetime, datetime]] = []

    def load_bar_data(
        self,
        symbol: str,
        exchange: Exchange,
        interval: Interval,
        start: datetime,
        end: datetime,
    ) -> list[BarData]:
        self.bounds.append((start, end))
        low: datetime = convert_tz(start)
        high: datetime = convert_tz(end)
        return [
            bar for bar in self.bars
            if bar.symbol == symbol
            and bar.exchange == exchange
            and bar.interval == interval
            and low <= convert_tz(bar.datetime) <= high
        ]


class StubDateRangeDialog:
    """The calendar popup, minus the calendar and the user.

    Returns bare dates because that is exactly what ``CalendarPicker`` can
    express — a naive year/month/day is the input the real dialog produces,
    not a shortcut taken by this test.
    """

    class DialogCode:
        Accepted = 1

    def __init__(self, start: datetime, end: datetime) -> None:
        self.seeded = (start, end)

    def exec(self) -> int:
        return self.DialogCode.Accepted

    def get_date_range(self) -> tuple[datetime, datetime]:
        return PICKED_START, PICKED_END


class RecordingTable:
    """The slice of TableWidget ``show_data`` writes into."""

    def __init__(self) -> None:
        self.row_count = 0
        self.cells: dict[tuple[int, int], QtWidgets.QTableWidgetItem] = {}

    def setRowCount(self, count: int) -> None:
        self.row_count = count

    def setItem(self, row: int, column: int, item: QtWidgets.QTableWidgetItem) -> None:
        self.cells[(row, column)] = item

    def column(self, index: int) -> list[str]:
        return [
            self.cells[(row, index)].text() for row in range(self.row_count)
        ]


@pytest.fixture
def pacific_host(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the host away from Hong Kong, so "host-local" is a known wrong answer."""
    monkeypatch.setenv("TZ", "America/Los_Angeles")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


@pytest.fixture
def database(pacific_host: None) -> FilteringDatabase:
    return FilteringDatabase(_stored_bars())


@pytest.fixture
def engine(
    monkeypatch: pytest.MonkeyPatch, database: FilteringDatabase
) -> manager_engine.ManagerEngine:
    """The real ManagerEngine, wired to the double instead of a live database.

    Its ``load_bar_data``/``output_data_to_csv`` are the production code the
    widget calls; only ``get_database``/``get_datafeed`` are replaced, and the
    MainEngine/EventEngine it never touches in these paths are placeholders.
    """
    monkeypatch.setattr(manager_engine, "get_database", lambda: database)
    monkeypatch.setattr(manager_engine, "get_datafeed", lambda: None)
    return manager_engine.ManagerEngine(SimpleNamespace(), SimpleNamespace())


@pytest.fixture
def picked_dialog(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(data_manager, "DateRangeDialog", StubDateRangeDialog)


def _expected_stamps() -> list[str]:
    return [
        bar.datetime.strftime("%Y-%m-%d %H:%M:%S") for bar in _stored_bars()
    ]


# ── the host really is on a different clock than the market ──────────────────

def test_host_timezone_differs_from_market(pacific_host: None) -> None:
    """Guard the premise: with host == market zone every assertion below is vacuous."""
    host_offset = datetime.now().astimezone().utcoffset()
    market_offset = datetime(2024, 1, 26, tzinfo=HK_TZ).utcoffset()

    assert host_offset != market_offset


# ── data manager: 查看数据 ────────────────────────────────────────────────────

def test_show_data_table_holds_the_whole_picked_window(
    engine: manager_engine.ManagerEngine, picked_dialog: None
) -> None:
    """Picking 2024-01-26 asks for the HK session of that date, morning included."""
    table = RecordingTable()
    widget = SimpleNamespace(engine=engine, table=table)

    data_manager.ManagerWidget.show_data(
        widget, SYMBOL, EXCHANGE, INTERVAL, PICKED_START, PICKED_END
    )

    assert table.column(0) == _expected_stamps()


def test_show_data_bounds_reach_the_database_as_instants(
    engine: manager_engine.ManagerEngine,
    database: FilteringDatabase,
    picked_dialog: None,
) -> None:
    """The window must be unambiguous by the time a driver sees it."""
    widget = SimpleNamespace(engine=engine, table=RecordingTable())

    data_manager.ManagerWidget.show_data(
        widget, SYMBOL, EXCHANGE, INTERVAL, PICKED_START, PICKED_END
    )

    start, end = database.bounds[0]
    assert start.tzinfo is not None
    assert end.tzinfo is not None


# ── data manager: 导出数据 ────────────────────────────────────────────────────

def test_output_data_csv_holds_the_whole_picked_window(
    monkeypatch: pytest.MonkeyPatch,
    engine: manager_engine.ManagerEngine,
    picked_dialog: None,
    tmp_path: Path,
) -> None:
    """The export runs the same query as the table and loses the same bars."""
    target: Path = tmp_path / "export.csv"
    monkeypatch.setattr(
        data_manager.QtWidgets,
        "QFileDialog",
        SimpleNamespace(getSaveFileName=lambda *args, **kwargs: (str(target), "")),
    )
    messages: list[tuple[str, str]] = []
    widget = SimpleNamespace(
        engine=engine,
        _show_message=lambda title, body: messages.append((title, body)),
    )

    data_manager.ManagerWidget.output_data(
        widget, SYMBOL, EXCHANGE, INTERVAL, PICKED_START, PICKED_END
    )

    assert messages == []
    with target.open() as handle:
        exported = [row["datetime"] for row in csv.DictReader(handle)]
    assert exported == _expected_stamps()


# ── replay chart ─────────────────────────────────────────────────────────────

class StubMessageBox:
    """Records the "没有数据" popup instead of trying to paint one."""

    warnings: list[str] = []

    @staticmethod
    def warning(parent: object, title: str, text: str) -> None:
        StubMessageBox.warnings.append(text)


class StubButton:
    def __init__(self) -> None:
        self.enabled: bool | None = None

    def setText(self, text: str) -> None:
        self.text = text

    def setEnabled(self, enabled: bool) -> None:
        self.enabled = enabled


class StubDate:
    def __init__(self, moment: datetime) -> None:
        self.moment = moment

    def year(self) -> int:
        return self.moment.year

    def month(self) -> int:
        return self.moment.month

    def day(self) -> int:
        return self.moment.day


class StubDateEdit:
    """A CalendarPicker: it can only ever express a bare date."""

    def __init__(self, moment: datetime) -> None:
        self.picked = StubDate(moment)

    def date(self) -> StubDate:
        return self.picked


@pytest.fixture
def replay_window(
    monkeypatch: pytest.MonkeyPatch, database: FilteringDatabase
) -> SimpleNamespace:
    StubMessageBox.warnings = []
    monkeypatch.setattr(replay_chart.QtWidgets, "QMessageBox", StubMessageBox)
    window = SimpleNamespace(
        timer=SimpleNamespace(stop=lambda: None),
        play_button=StubButton(),
        symbol_edit=SimpleNamespace(text=lambda: SYMBOL),
        exchange_combo=SimpleNamespace(currentData=lambda: EXCHANGE),
        start_edit=StubDateEdit(PICKED_START),
        end_edit=StubDateEdit(PICKED_END),
        database=database,
        panes={},
        bars=[],
        cursor=0,
        aggregators={},
    )
    window._update_status = lambda: None
    return window


def test_replay_chart_loads_the_whole_picked_window(
    replay_window: SimpleNamespace,
) -> None:
    """A replay of 2024-01-26 has to start at the open, not eight hours in."""
    replay_chart.ReplayChartWidget._load(replay_window)

    assert [bar.datetime for bar in replay_window.bars] == [
        bar.datetime for bar in _stored_bars()
    ]
    assert StubMessageBox.warnings == []


def test_replay_chart_bounds_reach_the_database_as_instants(
    replay_window: SimpleNamespace, database: FilteringDatabase
) -> None:
    """The window must be unambiguous by the time a driver sees it."""
    replay_chart.ReplayChartWidget._load(replay_window)

    start, end = database.bounds[0]
    assert start.tzinfo is not None
    assert end.tzinfo is not None
