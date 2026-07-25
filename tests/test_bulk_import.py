"""Tests for data_tools.bulk_import.

Everything except the class marked ``TestQuestDbIntegration`` runs against an
in-memory fake database, so the suite is hermetic. The integration class talks
to the real QuestDB configured in ~/.vntrader/vt_setting.json and is skipped
automatically when that instance is unreachable; set
``VNPY_BULK_IMPORT_IT=0`` to skip it even when it is up.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from data_tools.bulk_import import (  # noqa: E402
    AmbiguousPolicy,
    AuctionPolicy,
    BarLabel,
    CloseShift,
    ColumnMap,
    DedupStatus,
    FileStatus,
    ImportTask,
    ProgressEvent,
    RejectReason,
    bulk_import,
    check_dedup,
    main,
    supported_exchanges,
    tasks_from_directory,
)
from vnpy.trader.constant import Exchange, Interval  # noqa: E402
from vnpy.trader.object import BarData  # noqa: E402
from vnpy_gatewaykit.bar_label import SOURCE_FUTU, to_start_label  # noqa: E402
from vnpy_gatewaykit.market_clock import market_tz  # noqa: E402

HK = ZoneInfo("Asia/Hong_Kong")
NY = ZoneInfo("America/New_York")

HEADER = "datetime,open,high,low,close,volume,turnover,open_interest"


@pytest.fixture(autouse=True)
def _gateway_normalization_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run the close-label tests with gateway normalization ON.

    A close-label import is refused unless the gateway is normalizing too —
    otherwise the same candle lands under two timestamps (see
    ImportTask._require_matching_gateway_label). These tests exercise the label
    arithmetic, not the switch, so they declare the consistent configuration
    explicitly rather than depending on whatever the global default happens to
    be. The refusal itself is covered by
    test_close_label_refused_when_gateway_is_not_normalizing, which clears it.
    """
    monkeypatch.setenv("VNPY_BAR_LABEL_NORMALIZE", "1")


class FakeDatabase:
    """Collects what would have been written; records each save_bar_data call."""

    def __init__(self) -> None:
        self.bars: list[BarData] = []
        self.calls: list[int] = []

    def save_bar_data(self, bars: list[BarData], stream: bool = False) -> bool:
        self.calls.append(len(bars))
        # bulk_import reuses and clears its buffer, so copy defensively —
        # exactly what a real adapter's serialisation does.
        self.bars.extend(bars)
        return True


def write_csv(path: Path, rows: list[str], header: str = HEADER) -> Path:
    path.write_text("\n".join([header, *rows]) + "\n", encoding="utf-8")
    return path


def good_row(dt: str, price: float = 10.0, volume: float = 100.0) -> str:
    return f"{dt},{price},{price + 1},{price - 1},{price + 0.5},{volume},1000,0"


def run(
    tasks: list[ImportTask], db: FakeDatabase | None = None, **kwargs: object
) -> tuple[object, FakeDatabase]:
    db = db or FakeDatabase()
    report = bulk_import(tasks, database=db, **kwargs)  # type: ignore[arg-type]
    return report, db


def task_for(
    path: Path,
    *,
    exchange: Exchange = Exchange.SEHK,
    interval: Interval = Interval.MINUTE,
    label: BarLabel = BarLabel.OPEN,
    symbol: str | None = "0700",
    **kwargs: object,
) -> ImportTask:
    return ImportTask(
        path=path,
        exchange=exchange,
        interval=interval,
        label=label,
        symbol=symbol,
        **kwargs,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------- timezone


def test_hk_naive_timestamp_gets_hong_kong_offset(tmp_path: Path) -> None:
    csv_path = write_csv(tmp_path / "0700.csv", [good_row("2024-01-02 09:30:00")])
    report, db = run([task_for(csv_path)])

    assert report.accepted == 1
    assert db.bars[0].datetime == datetime(2024, 1, 2, 9, 30, tzinfo=HK)
    assert db.bars[0].datetime.utcoffset() == timedelta(hours=8)


def test_us_timestamps_follow_dst_on_both_sides_of_the_year(tmp_path: Path) -> None:
    csv_path = write_csv(
        tmp_path / "AAPL.csv",
        [good_row("2024-01-02 09:30:00"), good_row("2024-07-02 09:30:00")],
    )
    report, db = run([task_for(csv_path, exchange=Exchange.SMART, symbol="AAPL")])

    assert report.accepted == 2
    winter, summer = db.bars
    assert winter.datetime.utcoffset() == timedelta(hours=-5)
    assert summer.datetime.utcoffset() == timedelta(hours=-4)
    assert winter.datetime.astimezone(timezone.utc) == datetime(
        2024, 1, 2, 14, 30, tzinfo=timezone.utc
    )
    assert summer.datetime.astimezone(timezone.utc) == datetime(
        2024, 7, 2, 13, 30, tzinfo=timezone.utc
    )


def test_same_wall_clock_in_hk_and_us_are_different_instants(tmp_path: Path) -> None:
    """The bug market_clock exists to prevent, exercised through the importer."""
    hk_csv = write_csv(tmp_path / "0700.csv", [good_row("2024-07-02 10:00:00")])
    us_csv = write_csv(tmp_path / "AAPL.csv", [good_row("2024-07-02 10:00:00")])

    _, db = run(
        [
            task_for(hk_csv),
            task_for(us_csv, exchange=Exchange.SMART, symbol="AAPL"),
        ]
    )

    hk_bar, us_bar = db.bars
    assert us_bar.datetime - hk_bar.datetime == timedelta(hours=12)


def test_offset_bearing_timestamp_is_trusted_not_overwritten(tmp_path: Path) -> None:
    """Regression against vnpy_datamanager engine.py:62.

    That line does dt.replace(tzinfo=tz) unconditionally, so a parsed -05:00
    silently becomes the declared market offset. Ours keeps the instant.
    """
    csv_path = write_csv(
        tmp_path / "AAPL.csv", [good_row("2024-01-02T09:30:00-05:00")]
    )
    report, db = run([task_for(csv_path, exchange=Exchange.SMART, symbol="AAPL")])

    stored = db.bars[0].datetime
    assert stored.astimezone(timezone.utc) == datetime(
        2024, 1, 2, 14, 30, tzinfo=timezone.utc
    )
    assert report.files[0].tz_aware_rows == 1

    third_party_behaviour = datetime.fromisoformat(
        "2024-01-02T09:30:00-05:00"
    ).replace(tzinfo=HK)
    assert third_party_behaviour.astimezone(timezone.utc) == datetime(
        2024, 1, 2, 1, 30, tzinfo=timezone.utc
    )
    assert stored.astimezone(timezone.utc) != third_party_behaviour.astimezone(
        timezone.utc
    )


def test_nonexistent_local_time_is_rejected(tmp_path: Path) -> None:
    """2024-03-10 02:30 never happened in New York."""
    csv_path = write_csv(tmp_path / "AAPL.csv", [good_row("2024-03-10 02:30:00")])
    report, db = run([task_for(csv_path, exchange=Exchange.SMART, symbol="AAPL")])

    assert db.bars == []
    assert report.files[0].reject_counts == {RejectReason.NONEXISTENT_LOCAL_TIME: 1}
    assert report.files[0].status is FileStatus.FAILED


def test_ambiguous_local_time_rejected_by_default(tmp_path: Path) -> None:
    """2024-11-03 01:30 happened twice in New York."""
    csv_path = write_csv(tmp_path / "AAPL.csv", [good_row("2024-11-03 01:30:00")])
    report, _ = run([task_for(csv_path, exchange=Exchange.SMART, symbol="AAPL")])

    assert report.files[0].reject_counts == {RejectReason.AMBIGUOUS_LOCAL_TIME: 1}


@pytest.mark.parametrize(
    ("policy", "offset"),
    [(AmbiguousPolicy.EARLIER, -4), (AmbiguousPolicy.LATER, -5)],
)
def test_ambiguous_local_time_policies_pick_a_side(
    tmp_path: Path, policy: AmbiguousPolicy, offset: int
) -> None:
    csv_path = write_csv(tmp_path / "AAPL.csv", [good_row("2024-11-03 01:30:00")])
    _, db = run(
        [
            task_for(
                csv_path,
                exchange=Exchange.SMART,
                symbol="AAPL",
                ambiguous=policy,
            )
        ]
    )

    assert db.bars[0].datetime.utcoffset() == timedelta(hours=offset)


def test_hong_kong_has_no_dst_edge_cases(tmp_path: Path) -> None:
    """The same wall clocks that break in New York are ordinary in Hong Kong."""
    csv_path = write_csv(
        tmp_path / "0700.csv",
        [good_row("2024-03-10 02:30:00"), good_row("2024-11-03 01:30:00")],
    )
    report, db = run([task_for(csv_path)])

    assert report.accepted == 2
    assert all(b.datetime.utcoffset() == timedelta(hours=8) for b in db.bars)


# ------------------------------------------------------------- bar label


def test_open_label_does_not_shift(tmp_path: Path) -> None:
    csv_path = write_csv(tmp_path / "0700.csv", [good_row("2024-01-02 09:31:00")])
    _, db = run([task_for(csv_path, label=BarLabel.OPEN)])

    assert db.bars[0].datetime == datetime(2024, 1, 2, 9, 31, tzinfo=HK)


def test_close_label_shifts_back_one_minute(tmp_path: Path) -> None:
    csv_path = write_csv(tmp_path / "0700.csv", [good_row("2024-01-02 09:31:00")])
    _, db = run([task_for(csv_path, label=BarLabel.CLOSE)])

    assert db.bars[0].datetime == datetime(2024, 1, 2, 9, 30, tzinfo=HK)


def test_close_label_shifts_back_one_hour_for_hourly_bars(tmp_path: Path) -> None:
    """A wall-clock-aligned vendor: 11:00 is not on HK's session grid.

    HK's hourly grid runs 09:30/10:30/11:30 off the 09:30 open, so an 11:00
    close label can only come from a vendor that ignores the session. That is
    what ``CloseShift.FIXED_SPAN`` is for, and it subtracts exactly one hour.
    """
    csv_path = write_csv(tmp_path / "0700.csv", [good_row("2024-01-02 11:00:00")])
    _, db = run(
        [
            task_for(
                csv_path,
                interval=Interval.HOUR,
                label=BarLabel.CLOSE,
                close_shift=CloseShift.FIXED_SPAN,
            )
        ]
    )

    assert db.bars[0].datetime == datetime(2024, 1, 2, 10, 0, tzinfo=HK)


# ------------------------------------------- close label on the session grid


def test_hk_hourly_close_label_truncated_by_lunch_lands_on_the_grid(
    tmp_path: Path,
) -> None:
    """The 12:00 bar covers [11:30, 12:00) — half an hour, not a full one.

    HK's morning session is 09:30-12:00, so the last hourly bar is truncated
    by lunch. Subtracting a nominal hour puts it at 11:00, which is inside the
    10:30 bar's window and on no grid point at all.
    """
    csv_path = write_csv(
        tmp_path / "0700.csv",
        [
            good_row("2024-01-02 10:30:00"),
            good_row("2024-01-02 11:30:00"),
            good_row("2024-01-02 12:00:00"),
        ],
    )
    _, db = run([task_for(csv_path, interval=Interval.HOUR, label=BarLabel.CLOSE)])

    assert [b.datetime for b in db.bars] == [
        datetime(2024, 1, 2, 9, 30, tzinfo=HK),
        datetime(2024, 1, 2, 10, 30, tzinfo=HK),
        datetime(2024, 1, 2, 11, 30, tzinfo=HK),
    ]


def test_us_hourly_close_label_truncated_by_the_close_lands_on_the_grid(
    tmp_path: Path,
) -> None:
    """09:30-16:00 is six and a half hours, so the 16:00 bar is a half-hour stub."""
    csv_path = write_csv(
        tmp_path / "AAPL.csv",
        [good_row("2024-01-02 15:30:00"), good_row("2024-01-02 16:00:00")],
    )
    _, db = run(
        [
            task_for(
                csv_path,
                exchange=Exchange.SMART,
                symbol="AAPL",
                interval=Interval.HOUR,
                label=BarLabel.CLOSE,
            )
        ]
    )

    assert [b.datetime for b in db.bars] == [
        datetime(2024, 1, 2, 14, 30, tzinfo=NY),
        datetime(2024, 1, 2, 15, 30, tzinfo=NY),
    ]


def test_fixed_span_reproduces_the_off_grid_result(tmp_path: Path) -> None:
    """The two policies genuinely differ; the default is not cosmetic."""
    rows = [good_row("2024-01-02 12:00:00")]
    grid_path = write_csv(tmp_path / "grid.csv", rows)
    span_path = write_csv(tmp_path / "span.csv", rows)

    _, grid_db = run(
        [task_for(grid_path, interval=Interval.HOUR, label=BarLabel.CLOSE)]
    )
    _, span_db = run(
        [
            task_for(
                span_path,
                interval=Interval.HOUR,
                label=BarLabel.CLOSE,
                close_shift=CloseShift.FIXED_SPAN,
            )
        ]
    )

    assert grid_db.bars[0].datetime == datetime(2024, 1, 2, 11, 30, tzinfo=HK)
    assert span_db.bars[0].datetime == datetime(2024, 1, 2, 11, 0, tzinfo=HK)


def test_csv_close_label_agrees_with_the_gateway_normalizer(tmp_path: Path) -> None:
    """The whole point of sharing bar_label: same bar, same key, so re-import upserts.

    A CSV row and the same bar arriving from Futu (END-labelled) must produce
    one identical ``datetime``. If they disagree, QuestDB's
    ``DEDUP UPSERT KEYS(datetime, symbol, exchange, interval)`` sees two keys
    and keeps both copies.
    """
    labels = ["10:30", "11:30", "12:00", "14:00", "16:00"]
    csv_path = write_csv(
        tmp_path / "0700.csv", [good_row(f"2024-01-02 {t}:00") for t in labels]
    )
    _, db = run([task_for(csv_path, interval=Interval.HOUR, label=BarLabel.CLOSE)])

    gateway = [
        to_start_label(
            datetime.strptime(f"2024-01-02 {t}", "%Y-%m-%d %H:%M").replace(tzinfo=HK),
            source=SOURCE_FUTU,
            exchange=Exchange.SEHK,
            interval=Interval.HOUR,
        ).datetime
        for t in labels
    ]

    assert [b.datetime for b in db.bars] == gateway


def test_minute_close_labels_across_the_lunch_boundary(tmp_path: Path) -> None:
    """Minute bars: 12:00 closes the morning, 13:01 opens the afternoon."""
    csv_path = write_csv(
        tmp_path / "0700.csv",
        [good_row("2024-01-02 12:00:00"), good_row("2024-01-02 13:01:00")],
    )
    _, db = run([task_for(csv_path, label=BarLabel.CLOSE)])

    assert [b.datetime for b in db.bars] == [
        datetime(2024, 1, 2, 11, 59, tzinfo=HK),
        datetime(2024, 1, 2, 13, 0, tzinfo=HK),
    ]


def test_stamp_outside_every_session_falls_back_and_is_counted(
    tmp_path: Path,
) -> None:
    """No window contains 12:30 HK, so there is no grid — the fallback is counted."""
    csv_path = write_csv(tmp_path / "0700.csv", [good_row("2024-01-02 12:30:00")])
    report, db = run([task_for(csv_path, label=BarLabel.CLOSE)])

    assert db.bars[0].datetime == datetime(2024, 1, 2, 12, 29, tzinfo=HK)
    assert report.files[0].outside_session_rows == 1
    assert "outside_session=1" in report.files[0].summary()


def test_auction_close_label_maps_to_the_window_start(tmp_path: Path) -> None:
    """HK's 09:00-09:30 opening auction is one bar; it opens when the window does."""
    csv_path = write_csv(tmp_path / "0700.csv", [good_row("2024-01-02 09:30:00")])
    _, db = run([task_for(csv_path, label=BarLabel.CLOSE)])

    assert db.bars[0].datetime == datetime(2024, 1, 2, 9, 0, tzinfo=HK)


def test_auction_drop_policy_rejects_rather_than_swallows(tmp_path: Path) -> None:
    csv_path = write_csv(
        tmp_path / "0700.csv",
        [good_row("2024-01-02 09:30:00"), good_row("2024-01-02 09:31:00")],
    )
    report, db = run(
        [task_for(csv_path, label=BarLabel.CLOSE, auction=AuctionPolicy.DROP)]
    )

    assert [b.datetime for b in db.bars] == [datetime(2024, 1, 2, 9, 30, tzinfo=HK)]
    assert report.files[0].reject_counts == {RejectReason.AUCTION_DROPPED: 1}


def test_close_label_shift_is_absolute_time_across_a_dst_boundary(
    tmp_path: Path,
) -> None:
    """An hourly bar closing at 03:00 EDT started at 01:00 EST — one real hour.

    Wall-clock subtraction would say 02:00, a local time that does not exist
    on that date.
    """
    csv_path = write_csv(tmp_path / "SPY.csv", [good_row("2024-03-10 03:00:00")])
    _, db = run(
        [
            task_for(
                csv_path,
                exchange=Exchange.SMART,
                symbol="SPY",
                interval=Interval.HOUR,
                label=BarLabel.CLOSE,
            )
        ]
    )

    stored = db.bars[0].datetime
    assert stored == datetime(2024, 3, 10, 1, 0, tzinfo=NY)
    assert stored.utcoffset() == timedelta(hours=-5)
    assert stored.astimezone(timezone.utc) == datetime(
        2024, 3, 10, 6, 0, tzinfo=timezone.utc
    )


def test_close_label_with_explicit_span_for_five_minute_bars(tmp_path: Path) -> None:
    csv_path = write_csv(tmp_path / "0700.csv", [good_row("2024-01-02 09:35:00")])
    _, db = run(
        [
            task_for(
                csv_path,
                label=BarLabel.CLOSE,
                bar_span=timedelta(minutes=5),
            )
        ]
    )

    assert db.bars[0].datetime == datetime(2024, 1, 2, 9, 30, tzinfo=HK)


def test_close_label_is_refused_for_daily_bars(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="labelled by trading date"):
        task_for(tmp_path / "0700.csv", interval=Interval.DAILY, label=BarLabel.CLOSE)


def test_daily_bars_import_fine_with_open_label(tmp_path: Path) -> None:
    csv_path = write_csv(tmp_path / "0700.csv", [good_row("2024-01-02")])
    _, db = run([task_for(csv_path, interval=Interval.DAILY, label=BarLabel.OPEN)])

    assert db.bars[0].datetime == datetime(2024, 1, 2, 0, 0, tzinfo=HK)
    assert db.bars[0].interval is Interval.DAILY


def test_label_has_no_default() -> None:
    """Guessing the label shifts every bar by a period, so it must be declared."""
    with pytest.raises(TypeError):
        ImportTask(  # type: ignore[call-arg]
            path=Path("x.csv"),
            exchange=Exchange.SEHK,
            interval=Interval.MINUTE,
            symbol="0700",
        )


# ------------------------------------------------------------- validation


@pytest.mark.parametrize(
    ("row", "reason"),
    [
        ("2024-01-02 09:30:00,10,9,11,10,100,0,0", RejectReason.OHLC_INCONSISTENT),
        ("2024-01-02 09:30:00,10,11,-1,10,100,0,0", RejectReason.NEGATIVE_VALUE),
        ("2024-01-02 09:30:00,-10,11,9,10,100,0,0", RejectReason.NEGATIVE_VALUE),
        ("2024-01-02 09:30:00,10,11,9,10,-5,0,0", RejectReason.NEGATIVE_VALUE),
        ("2024-01-02 09:30:00,0,11,9,10,100,0,0", RejectReason.ZERO_PRICE),
        ("2024-01-02 09:30:00,abc,11,9,10,100,0,0", RejectReason.BAD_NUMBER),
        ("2024-01-02 09:30:00,nan,11,9,10,100,0,0", RejectReason.NON_FINITE),
        ("2024-01-02 09:30:00,inf,11,9,10,100,0,0", RejectReason.NON_FINITE),
        ("2024-01-02 09:30:00,,11,9,10,100,0,0", RejectReason.EMPTY_FIELD),
        (",10,11,9,10,100,0,0", RejectReason.EMPTY_FIELD),
        ("not-a-date,10,11,9,10,100,0,0", RejectReason.BAD_DATETIME),
        ("2024-01-02 09:30:00,10,11,9,12,100,0,0", RejectReason.OHLC_INCONSISTENT),
        ("2024-01-02 09:30:00,10,11,9.5,9,100,0,0", RejectReason.OHLC_INCONSISTENT),
    ],
)
def test_each_bad_row_shape_is_rejected_with_its_reason(
    tmp_path: Path, row: str, reason: RejectReason
) -> None:
    csv_path = write_csv(tmp_path / "0700.csv", [good_row("2024-01-02 09:29:00"), row])
    report, db = run([task_for(csv_path)])

    file_report = report.files[0]
    assert file_report.reject_counts == {reason: 1}
    assert file_report.accepted == 1
    assert file_report.status is FileStatus.PARTIAL
    assert len(db.bars) == 1
    assert file_report.reject_samples[0].reason is reason
    assert file_report.reject_samples[0].line_no == 3


def test_volume_and_turnover_may_legitimately_be_zero(tmp_path: Path) -> None:
    csv_path = write_csv(
        tmp_path / "0700.csv", ["2024-01-02 09:30:00,10,11,9,10,0,0,0"]
    )
    report, db = run([task_for(csv_path)])

    assert report.accepted == 1
    assert db.bars[0].volume == 0.0


def test_duplicate_timestamps_within_a_file_are_rejected(tmp_path: Path) -> None:
    csv_path = write_csv(
        tmp_path / "0700.csv",
        [
            good_row("2024-01-02 09:30:00"),
            good_row("2024-01-02 09:30:00", price=20.0),
            good_row("2024-01-02 09:31:00"),
        ],
    )
    report, db = run([task_for(csv_path)])

    assert report.files[0].reject_counts == {RejectReason.DUPLICATE_TIMESTAMP: 1}
    assert len(db.bars) == 2
    assert db.bars[0].open_price == 10.0  # first occurrence wins


def test_duplicate_detection_is_per_symbol(tmp_path: Path) -> None:
    """The same minute for two symbols is not a duplicate."""
    csv_path = write_csv(
        tmp_path / "mixed.csv",
        [
            "0700,2024-01-02 09:30:00,10,11,9,10,100,0,0",
            "9988,2024-01-02 09:30:00,20,21,19,20,100,0,0",
        ],
        header="symbol," + HEADER,
    )
    report, db = run(
        [task_for(csv_path, symbol=None, columns=ColumnMap(symbol="symbol"))]
    )

    assert report.accepted == 2
    assert {b.symbol for b in db.bars} == {"0700", "9988"}


def test_differently_spelled_identical_instants_still_collide(tmp_path: Path) -> None:
    """Duplicate detection keys on the stored instant, not the raw text."""
    csv_path = write_csv(
        tmp_path / "AAPL.csv",
        [
            good_row("2024-01-02 09:30:00"),
            good_row("2024-01-02T14:30:00+00:00", price=20.0),
        ],
    )
    report, db = run([task_for(csv_path, exchange=Exchange.SMART, symbol="AAPL")])

    assert report.files[0].reject_counts == {RejectReason.DUPLICATE_TIMESTAMP: 1}
    assert len(db.bars) == 1


def test_reject_counts_are_complete_even_when_samples_are_capped(
    tmp_path: Path,
) -> None:
    rows = [f"2024-01-02 09:{minute:02d}:00,10,9,11,10,100,0,0" for minute in range(30)]
    rows.append(good_row("2024-01-02 10:00:00"))
    csv_path = write_csv(tmp_path / "0700.csv", rows)

    report, _ = run([task_for(csv_path)], max_reject_samples=5)

    file_report = report.files[0]
    assert file_report.reject_counts == {RejectReason.OHLC_INCONSISTENT: 30}
    assert len(file_report.reject_samples) == 5
    assert file_report.samples_truncated is True


def test_blank_symbol_cell_is_rejected(tmp_path: Path) -> None:
    csv_path = write_csv(
        tmp_path / "mixed.csv",
        [
            ",2024-01-02 09:30:00,10,11,9,10,100,0,0",
            "0700,2024-01-02 09:31:00,10,11,9,10,100,0,0",
        ],
        header="symbol," + HEADER,
    )
    report, db = run(
        [task_for(csv_path, symbol=None, columns=ColumnMap(symbol="symbol"))]
    )

    assert report.files[0].reject_counts == {RejectReason.EMPTY_SYMBOL: 1}
    assert len(db.bars) == 1


# --------------------------------------------------- batching & isolation


def test_many_files_many_symbols_in_one_call(tmp_path: Path) -> None:
    for symbol in ("0700", "9988", "3690"):
        write_csv(tmp_path / f"{symbol}.csv", [good_row("2024-01-02 09:30:00")])

    tasks = tasks_from_directory(
        tmp_path, Exchange.SEHK, Interval.MINUTE, BarLabel.OPEN
    )
    report, db = run(tasks)

    assert len(tasks) == 3
    assert report.accepted == 3
    assert {b.symbol for b in db.bars} == {"0700", "9988", "3690"}
    assert report.ok is True


def test_one_file_can_carry_many_symbols(tmp_path: Path) -> None:
    csv_path = write_csv(
        tmp_path / "all.csv",
        [
            "0700,2024-01-02 09:30:00,10,11,9,10,100,0,0",
            "9988,2024-01-02 09:30:00,20,21,19,20,200,0,0",
            "0700,2024-01-02 09:31:00,11,12,10,11,150,0,0",
        ],
        header="symbol," + HEADER,
    )
    report, db = run(
        [task_for(csv_path, symbol=None, columns=ColumnMap(symbol="symbol"))]
    )

    groups = report.files[0].groups
    assert set(groups) == {"0700", "9988"}
    assert groups["0700"].count == 2
    assert groups["0700"].start == datetime(2024, 1, 2, 9, 30, tzinfo=HK)
    assert groups["0700"].end == datetime(2024, 1, 2, 9, 31, tzinfo=HK)
    assert groups["9988"].count == 1
    assert len(db.bars) == 3


def test_a_missing_file_does_not_stop_the_batch(tmp_path: Path) -> None:
    ok_path = write_csv(tmp_path / "0700.csv", [good_row("2024-01-02 09:30:00")])
    missing = tmp_path / "nope.csv"
    later = write_csv(tmp_path / "9988.csv", [good_row("2024-01-02 09:30:00")])

    report, db = run(
        [
            task_for(ok_path),
            task_for(missing, symbol="GONE"),
            task_for(later, symbol="9988"),
        ]
    )

    statuses = [f.status for f in report.files]
    assert statuses == [FileStatus.OK, FileStatus.FAILED, FileStatus.OK]
    assert "FileNotFoundError" in (report.files[1].error or "")
    assert {b.symbol for b in db.bars} == {"0700", "9988"}
    assert report.ok is False


def test_a_file_with_a_wrong_header_fails_alone(tmp_path: Path) -> None:
    bad = tmp_path / "bad.csv"
    bad.write_text("time,o,h,l,c,v\n2024-01-02 09:30:00,1,2,0.5,1,10\n", encoding="utf-8")
    good = write_csv(tmp_path / "0700.csv", [good_row("2024-01-02 09:30:00")])

    report, db = run([task_for(bad, symbol="BAD"), task_for(good)])

    assert report.files[0].status is FileStatus.FAILED
    assert "header missing required column" in (report.files[0].error or "")
    assert report.files[1].status is FileStatus.OK
    assert len(db.bars) == 1


def test_header_only_file_is_empty_not_a_crash(tmp_path: Path) -> None:
    """Regression against vnpy_datamanager engine.py:89 UnboundLocalError."""
    empty = tmp_path / "empty.csv"
    empty.write_text(HEADER + "\n", encoding="utf-8")
    good = write_csv(tmp_path / "0700.csv", [good_row("2024-01-02 09:30:00")])

    report, db = run([task_for(empty, symbol="EMPTY"), task_for(good)])

    assert report.files[0].status is FileStatus.EMPTY
    assert report.files[0].error is None
    assert report.files[1].status is FileStatus.OK
    assert len(db.bars) == 1


def test_completely_empty_file_is_empty_not_a_crash(tmp_path: Path) -> None:
    empty = tmp_path / "empty.csv"
    empty.write_text("", encoding="utf-8")

    report, _ = run([task_for(empty, symbol="EMPTY")])

    assert report.files[0].status is FileStatus.EMPTY


def test_good_rows_before_and_after_a_bad_row_all_land(tmp_path: Path) -> None:
    """Regression against the third-party all-or-nothing failure mode."""
    csv_path = write_csv(
        tmp_path / "0700.csv",
        [
            good_row("2024-01-02 09:30:00"),
            "2024-01-02 09:31:00,oops,11,9,10,100,0,0",
            good_row("2024-01-02 09:32:00"),
        ],
    )
    report, db = run([task_for(csv_path)])

    assert report.files[0].status is FileStatus.PARTIAL
    assert [b.datetime.minute for b in db.bars] == [30, 32]


def test_null_bytes_are_stripped(tmp_path: Path) -> None:
    csv_path = tmp_path / "0700.csv"
    csv_path.write_bytes(
        (HEADER + "\n" + good_row("2024-01-02 09:30:00") + "\n").encode().replace(
            b"09:30", b"09\x00:30"
        )
    )
    report, _ = run([task_for(csv_path)])

    assert report.accepted == 1


def test_progress_callback_reports_each_file(tmp_path: Path) -> None:
    for symbol in ("0700", "9988"):
        write_csv(tmp_path / f"{symbol}.csv", [good_row("2024-01-02 09:30:00")])
    tasks = tasks_from_directory(
        tmp_path, Exchange.SEHK, Interval.MINUTE, BarLabel.OPEN
    )

    events: list[ProgressEvent] = []
    run(tasks, progress=events.append)

    done = [e for e in events if e.done]
    assert len(done) == 2
    assert [e.file_index for e in done] == [0, 1]
    assert all(e.file_total == 2 for e in done)
    assert all(e.rows_read == 1 and e.accepted == 1 for e in done)


def test_progress_fires_mid_file_for_large_inputs(tmp_path: Path) -> None:
    rows = [good_row(f"2024-01-02 {9 + i // 60:02d}:{i % 60:02d}:00") for i in range(50)]
    csv_path = write_csv(tmp_path / "0700.csv", rows)

    events: list[ProgressEvent] = []
    run([task_for(csv_path)], progress=events.append, progress_every=10)

    assert [e.rows_read for e in events if not e.done] == [10, 20, 30, 40, 50]


def test_writes_are_chunked_but_lossless(tmp_path: Path) -> None:
    rows = [good_row(f"2024-01-02 {9 + i // 60:02d}:{i % 60:02d}:00") for i in range(25)]
    csv_path = write_csv(tmp_path / "0700.csv", rows)

    report, db = run([task_for(csv_path)], chunk_size=10)

    assert db.calls == [10, 10, 5]
    assert len(db.bars) == 25
    assert report.written == 25
    assert len({b.datetime for b in db.bars}) == 25


def test_chunk_size_must_be_positive(tmp_path: Path) -> None:
    csv_path = write_csv(tmp_path / "0700.csv", [good_row("2024-01-02 09:30:00")])
    with pytest.raises(ValueError, match="chunk_size"):
        run([task_for(csv_path)], chunk_size=0)


def test_dry_run_validates_but_writes_nothing(tmp_path: Path) -> None:
    csv_path = write_csv(
        tmp_path / "0700.csv",
        [good_row("2024-01-02 09:30:00"), "2024-01-02 09:31:00,10,9,11,10,1,0,0"],
    )
    report, db = run([task_for(csv_path)], dry_run=True)

    assert db.calls == []
    assert db.bars == []
    assert report.written == 1
    assert report.rejected == 1
    assert report.dry_run is True


def test_all_rows_rejected_marks_the_file_failed(tmp_path: Path) -> None:
    csv_path = write_csv(tmp_path / "0700.csv", ["2024-01-02 09:30:00,10,9,11,10,1,0,0"])
    report, db = run([task_for(csv_path)])

    assert report.files[0].status is FileStatus.FAILED
    assert "all 1 row(s) rejected" in (report.files[0].error or "")
    assert db.bars == []


def test_optional_columns_default_to_zero_when_absent(tmp_path: Path) -> None:
    csv_path = write_csv(
        tmp_path / "0700.csv",
        ["2024-01-02 09:30:00,10,11,9,10,100"],
        header="datetime,open,high,low,close,volume",
    )
    report, db = run([task_for(csv_path)])

    assert report.accepted == 1
    assert db.bars[0].turnover == 0.0
    assert db.bars[0].open_interest == 0.0


def test_custom_column_names_and_strptime_format(tmp_path: Path) -> None:
    csv_path = write_csv(
        tmp_path / "0700.csv",
        ["02/01/2024 09:30,10,11,9,10,100"],
        header="ts,o,h,l,c,vol",
    )
    report, db = run(
        [
            task_for(
                csv_path,
                columns=ColumnMap(
                    datetime="ts",
                    open="o",
                    high="h",
                    low="l",
                    close="c",
                    volume="vol",
                    turnover=None,
                    open_interest=None,
                ),
                datetime_format="%d/%m/%Y %H:%M",
            )
        ]
    )

    assert report.accepted == 1
    assert db.bars[0].datetime == datetime(2024, 1, 2, 9, 30, tzinfo=HK)


# ------------------------------------------------------------ task config


def test_symbol_must_come_from_exactly_one_place(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="pick one"):
        task_for(tmp_path / "x.csv", symbol="0700", columns=ColumnMap(symbol="symbol"))

    with pytest.raises(ValueError, match="no symbol"):
        task_for(tmp_path / "x.csv", symbol=None)


def test_tick_interval_is_not_bar_data(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not bar data"):
        task_for(tmp_path / "x.csv", interval=Interval.TICK)


def test_tasks_from_directory_uses_the_stem_and_sorts(tmp_path: Path) -> None:
    for name in ("9988.csv", "0700.csv", "notes.txt"):
        (tmp_path / name).write_text(HEADER + "\n", encoding="utf-8")

    tasks = tasks_from_directory(
        tmp_path, Exchange.SEHK, Interval.MINUTE, BarLabel.OPEN
    )

    assert [t.symbol for t in tasks] == ["0700", "9988"]


def test_tasks_from_directory_accepts_a_symbol_rule(tmp_path: Path) -> None:
    (tmp_path / "AAPL_1m.csv").write_text(HEADER + "\n", encoding="utf-8")

    tasks = tasks_from_directory(
        tmp_path,
        Exchange.SMART,
        Interval.MINUTE,
        BarLabel.OPEN,
        symbol_from_path=lambda p: p.stem.split("_")[0],
    )

    assert [t.symbol for t in tasks] == ["AAPL"]


def test_tasks_from_directory_defers_to_a_symbol_column(tmp_path: Path) -> None:
    """With a symbol column the filename means nothing, so no fixed symbol."""
    (tmp_path / "whatever.csv").write_text("symbol," + HEADER + "\n", encoding="utf-8")

    tasks = tasks_from_directory(
        tmp_path,
        Exchange.SEHK,
        Interval.MINUTE,
        BarLabel.OPEN,
        columns=ColumnMap(symbol="symbol"),
    )

    assert [t.symbol for t in tasks] == [None]


def test_tasks_from_directory_rejects_a_non_directory(tmp_path: Path) -> None:
    with pytest.raises(NotADirectoryError):
        tasks_from_directory(
            tmp_path / "nope", Exchange.SEHK, Interval.MINUTE, BarLabel.OPEN
        )


# ------------------------------------------------------------------ dedup


def test_check_dedup_is_unknown_for_a_backend_without_conninfo() -> None:
    status, detail = check_dedup(FakeDatabase())  # type: ignore[arg-type]

    assert status is DedupStatus.UNKNOWN
    assert "conninfo" in detail


def test_check_dedup_is_unknown_when_the_server_is_unreachable() -> None:
    class Unreachable(FakeDatabase):
        conninfo = "host=127.0.0.1 port=1 dbname=x user=x password=x"

    status, detail = check_dedup(Unreachable())  # type: ignore[arg-type]

    assert status is DedupStatus.UNKNOWN
    assert detail


def test_require_dedup_refuses_rather_than_duplicating(tmp_path: Path) -> None:
    csv_path = write_csv(tmp_path / "0700.csv", [good_row("2024-01-02 09:30:00")])
    db = FakeDatabase()

    with pytest.raises(RuntimeError, match="would duplicate bars"):
        bulk_import([task_for(csv_path)], database=db, require_dedup=True)  # type: ignore[arg-type]

    assert db.bars == []


# -------------------------------------------------------------------- CLI


class _Capture:
    def __init__(self) -> None:
        self.text = ""

    def write(self, chunk: str) -> int:
        self.text += chunk
        return len(chunk)

    def flush(self) -> None:
        return None


def test_cli_dry_run_reports_and_exits_clean(tmp_path: Path) -> None:
    write_csv(tmp_path / "0700.csv", [good_row("2024-01-02 09:30:00")])
    write_csv(tmp_path / "9988.csv", [good_row("2024-01-02 09:30:00")])
    out = _Capture()

    code = main(
        [
            "--dir", str(tmp_path),
            "--exchange", "SEHK",
            "--interval", "1m",
            "--label", "open",
            "--dry-run",
        ],
        stream=out,  # type: ignore[arg-type]
    )

    assert code == 0
    assert "read=2 ok=2 rejected=0" in out.text
    assert "[DRY-RUN]" in out.text


def test_cli_exit_code_2_when_rows_were_rejected(tmp_path: Path) -> None:
    write_csv(
        tmp_path / "0700.csv",
        [good_row("2024-01-02 09:30:00"), "2024-01-02 09:31:00,10,9,11,10,1,0,0"],
    )
    out = _Capture()

    code = main(
        [
            "--file", str(tmp_path / "0700.csv"),
            "--exchange", "SEHK",
            "--interval", "1m",
            "--label", "open",
            "--dry-run",
        ],
        stream=out,  # type: ignore[arg-type]
    )

    assert code == 2
    assert "ohlc_inconsistent" in out.text


def test_cli_exit_code_1_when_a_file_failed(tmp_path: Path) -> None:
    out = _Capture()

    code = main(
        [
            "--file", str(tmp_path / "missing.csv"),
            "--exchange", "SEHK",
            "--interval", "1m",
            "--label", "open",
            "--symbol", "0700",
            "--dry-run",
        ],
        stream=out,  # type: ignore[arg-type]
    )

    assert code == 1
    assert "FAILED" in out.text


def test_cli_close_label_on_daily_is_a_clean_error(tmp_path: Path) -> None:
    write_csv(tmp_path / "0700.csv", [good_row("2024-01-02")])
    out = _Capture()

    code = main(
        [
            "--dir", str(tmp_path),
            "--exchange", "SEHK",
            "--interval", "d",
            "--label", "close",
            "--dry-run",
        ],
        stream=out,  # type: ignore[arg-type]
    )

    assert code == 1
    assert "labelled by trading date" in out.text


def test_cli_symbol_column_overrides_the_filename(tmp_path: Path) -> None:
    write_csv(
        tmp_path / "whatever.csv",
        ["0700,2024-01-02 09:30:00,10,11,9,10,100,0,0"],
        header="symbol," + HEADER,
    )
    out = _Capture()

    code = main(
        [
            "--dir", str(tmp_path),
            "--exchange", "SEHK",
            "--interval", "1m",
            "--label", "open",
            "--symbol-column", "symbol",
            "--dry-run",
        ],
        stream=out,  # type: ignore[arg-type]
    )

    assert code == 0
    assert "ok=1" in out.text


def test_cli_only_offers_exchanges_that_can_be_localized() -> None:
    """An unmapped exchange has no market timezone, so it must not be offered."""
    names = [e.name for e in supported_exchanges()]

    assert "SEHK" in names
    assert "SMART" in names
    assert "NASDAQ" not in names
    for exchange in supported_exchanges():
        assert market_tz(exchange) is not None


def test_cli_rejects_an_unsupported_exchange(tmp_path: Path) -> None:
    write_csv(tmp_path / "AAPL.csv", [good_row("2024-01-02 09:30:00")])

    with pytest.raises(SystemExit) as excinfo:
        main(
            [
                "--dir", str(tmp_path),
                "--exchange", "NASDAQ",
                "--interval", "1m",
                "--label", "open",
                "--dry-run",
            ]
        )

    assert excinfo.value.code == 2  # argparse usage error


def test_cli_reports_no_files_matched(tmp_path: Path) -> None:
    out = _Capture()

    code = main(
        [
            "--dir", str(tmp_path),
            "--exchange", "SEHK",
            "--interval", "1m",
            "--label", "open",
            "--dry-run",
        ],
        stream=out,  # type: ignore[arg-type]
    )

    assert code == 1
    assert "no files matched" in out.text


# ------------------------------------------------------- QuestDB integration


def _questdb_or_skip() -> object:
    if os.environ.get("VNPY_BULK_IMPORT_IT") == "0":
        pytest.skip("integration disabled via VNPY_BULK_IMPORT_IT=0")
    try:
        from vnpy.trader.database import get_database

        database = get_database()
        status, detail = check_dedup(database)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"QuestDB unavailable: {exc}")
    if status is DedupStatus.UNKNOWN:
        pytest.skip(f"QuestDB unavailable: {detail}")
    return database


class TestQuestDbIntegration:
    """Proves the idempotency claim against the real table, not a fake."""

    SYMBOL = "ZZ_BULK_IMPORT_TEST"

    def test_dedup_is_enabled_on_the_live_bar_table(self) -> None:
        database = _questdb_or_skip()
        status, detail = check_dedup(database)  # type: ignore[arg-type]

        assert status is DedupStatus.ENABLED
        assert "datetime" in detail and "symbol" in detail

    def test_importing_twice_does_not_duplicate_bars(self, tmp_path: Path) -> None:
        database = _questdb_or_skip()
        rows = [
            good_row(f"2024-01-02 09:{minute:02d}:00", price=10.0 + minute)
            for minute in range(5)
        ]
        csv_path = write_csv(tmp_path / f"{self.SYMBOL}.csv", rows)
        task = task_for(csv_path, symbol=self.SYMBOL)

        try:
            first = bulk_import([task], database=database)  # type: ignore[arg-type]
            assert first.ok is True
            assert first.written == 5
            after_first = self._count(database)

            second = bulk_import([task], database=database)  # type: ignore[arg-type]
            assert second.written == 5
            after_second = self._count(database)

            assert after_first == 5
            assert after_second == 5, "re-import must upsert, not append"
        finally:
            database.delete_bar_data(  # type: ignore[attr-defined]
                self.SYMBOL, Exchange.SEHK, Interval.MINUTE
            )

    def test_stored_instant_survives_the_round_trip(self, tmp_path: Path) -> None:
        """A close-labelled US bar comes back as the instant we computed."""
        database = _questdb_or_skip()
        csv_path = write_csv(tmp_path / f"{self.SYMBOL}.csv", [good_row("2024-03-10 03:00:00")])
        task = task_for(
            csv_path,
            exchange=Exchange.SMART,
            symbol=self.SYMBOL,
            interval=Interval.HOUR,
            label=BarLabel.CLOSE,
        )

        try:
            report = bulk_import([task], database=database)  # type: ignore[arg-type]
            assert report.ok is True

            bars = database.load_bar_data(  # type: ignore[attr-defined]
                self.SYMBOL,
                Exchange.SMART,
                Interval.HOUR,
                datetime(2024, 3, 9, tzinfo=timezone.utc),
                datetime(2024, 3, 11, tzinfo=timezone.utc),
            )
            assert len(bars) == 1
            assert bars[0].datetime.astimezone(timezone.utc) == datetime(
                2024, 3, 10, 6, 0, tzinfo=timezone.utc
            )
            assert bars[0].datetime.astimezone(NY) == datetime(
                2024, 3, 10, 1, 0, tzinfo=NY
            )
        finally:
            database.delete_bar_data(  # type: ignore[attr-defined]
                self.SYMBOL, Exchange.SMART, Interval.HOUR
            )

    def _count(self, database: object) -> int:
        bars = database.load_bar_data(  # type: ignore[attr-defined]
            self.SYMBOL,
            Exchange.SEHK,
            Interval.MINUTE,
            datetime(2024, 1, 1, tzinfo=timezone.utc),
            datetime(2024, 1, 3, tzinfo=timezone.utc),
        )
        return len(bars)


def test_close_label_refused_when_gateway_is_not_normalizing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A close-label import must refuse while the gateway side is not normalizing.

    The two bar_label entry points differ on purpose: the gateway path consults
    the global switch, this primitive does not (only the caller knows whether a
    vendor stamped the open or the close). That is fine while both ends agree on
    the resulting convention — and corrupting when they don't. Measured on HK
    60m 2024-01-02: gateway wrote 10:30/11:30/12:00/14:00/15:00/16:00, a
    close-label CSV of the same bars wrote 09:30/10:30/11:30/13:00/14:00/15:00.
    Six for six, different DEDUP keys, both copies kept.

    So the mismatch has to stop the import at construction, before any file is
    read — not produce a "successful" run that doubles the series.
    """
    monkeypatch.delenv("VNPY_BAR_LABEL_NORMALIZE", raising=False)
    csv_path = write_csv(tmp_path / "0700.csv", [good_row("2024-01-02 10:30:00")])

    with pytest.raises(ValueError, match="label=close"):
        task_for(csv_path, interval=Interval.HOUR, label=BarLabel.CLOSE)

    # open-label needs no normalization, so it stays available either way.
    task_for(csv_path, interval=Interval.HOUR, label=BarLabel.OPEN)
