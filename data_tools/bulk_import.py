"""Batch import of external historical bars (CSV) into the vnpy database.

Why this exists at all
----------------------
``vnpy_datamanager.engine.ManagerEngine.import_data_from_csv`` (third-party
pip package, read at
``vnpy/.venv/lib/python3.14/site-packages/vnpy_datamanager/engine.py``)
already parses one CSV into ``BarData`` and calls ``save_bar_data``. What it
does *not* do, verified by reading it:

* one call == one file == one hard-coded ``(symbol, exchange, interval)``.
  A ``symbol`` column in the CSV is ignored entirely, so every row lands
  under whatever symbol the caller passed.
* ``engine.py:62`` does ``dt.replace(tzinfo=tz)`` unconditionally. When the
  CSV timestamp carries its own offset (``2024-01-02T09:30:00-05:00`` —
  the usual shape of a US vendor export) ``fromisoformat`` parses the
  offset and ``replace`` then *silently overwrites* it. Measured: that row
  becomes ``09:30+08:00``, 13 hours off, with no warning.
* ``engine.py:89`` reads ``end = bar.datetime`` outside the row loop, so a
  header-only CSV raises ``UnboundLocalError``.
* one bad cell raises mid-loop and ``save_bar_data`` is never reached, so
  the whole file lands zero rows — and in a batch that also kills every
  later file.
* the entire file is materialised as ``BarData`` before a single write
  (~281 bytes/bar measured), so a 10M-row file is ~2.6 GB resident.

This module is the batch layer over the same storage API. It does not
reimplement ``save_bar_data`` — QuestDB ingestion is already the fast path
(ILP ``Sender``, measured ~100k rows/s against the local instance), and the
whole bar table is currently 58,840 rows. Throughput was never the problem;
correctness, isolation and multiplicity were.

Timezone handling
-----------------
A naive timestamp in a vendor CSV is *market local wall-clock*. The mapping
from exchange to market timezone is not redefined here — it is imported
from ``vnpy_gatewaykit.market_clock``, the same single source of truth every
gateway localizes with, so a bar imported from CSV and the same bar
downloaded through Futu/uSMART land on the identical instant.

``ZoneInfo`` resolves the UTC offset per-datetime, so US DST is handled by
construction (measured: ``2024-01-02 09:30`` NY -> 14:30 UTC at -05:00,
``2024-07-02 09:30`` NY -> 13:30 UTC at -04:00). The two cases ``ZoneInfo``
cannot resolve on its own are handled explicitly rather than silently:

* the spring-forward gap (``2024-03-10 02:30`` in New York never happened;
  ``replace(tzinfo=...)`` happily produces an instant that round-trips back
  as ``03:30``) is detected and the row is rejected.
* the fall-back repeat (``2024-11-03 01:30`` in New York happens twice)
  is detected and resolved by an explicit policy, rejecting by default.
  Neither HK (no DST) nor US equity sessions (04:00-20:00 ET, transitions
  at 02:00) can legitimately contain such a stamp, so hitting one means the
  file is not what it claims to be.

Bar timestamp label
-------------------
vnpy labels a bar by its **start**: ``BarGenerator`` in
``vnpy/trader/utility.py`` floors to the slot open (``minute - minute %
window``), and both our gateways stamp bars the same way. A vendor CSV may
instead label by the bar's **close**. There is no way to tell from the data,
so ``ImportTask.label`` is required and has no default — the community
recipe's hard-coded ``dt + timedelta(minutes=-1)`` is this, generalised.

Generalising it is not the same as subtracting a period, though, and that
distinction is measured rather than assumed. The last bar before a session
break is *truncated* by the break, so it is narrower than the nominal period:
HK 60m labelled 12:00 covers ``[11:30, 12:00)``, and 12:00 minus an hour is
11:00 — a point inside the previous bar. US 60m labelled 16:00 lands on 15:00
instead of 15:30 the same way. So the opening instant is recovered by
rebuilding the session grid via ``vnpy_gatewaykit.bar_label``, which is the
same code that normalizes Futu's END-labelled bars. Sharing that one
implementation is what makes a CSV-imported bar and a gateway-downloaded bar
agree on ``datetime`` — without agreement the dedup key below does not match
and both copies survive.

Stamps that no published window contains (a lunch-break row, a holiday) have
no grid to snap to; those fall back to absolute-time subtraction and are
counted in ``FileReport.outside_session_rows`` rather than passed over
silently. Absolute-time — convert to UTC, subtract, convert back — so an
hourly bar whose label sits just after a DST transition still resolves to the
instant one real hour earlier.

Idempotency
-----------
Nothing in this module deduplicates against the database, because the
database already does it. Confirmed in
``vnpy_questdb/questdb_database.py`` ``CREATE_BAR_TABLE_SQL``::

    DEDUP UPSERT KEYS(datetime, symbol, exchange, interval)

and confirmed live on this machine's instance (``tables()`` reports
``dedup=True``; ``SHOW COLUMNS FROM dbbardata`` reports those four columns
as ``upsertKey``). Re-importing the same file therefore *upserts* — the row
count does not grow and the newer values win. That also means re-importing
a symbol that was soft-deleted (the ``deleted`` flag is not an upsert key)
resurrects it, since the fresh row carries ``deleted=False``.

The one way that guarantee can be absent is a ``dbbardata`` created by an
older schema: the DDL is ``CREATE TABLE IF NOT EXISTS``, so it will not
retrofit ``DEDUP`` onto a pre-existing table. ``check_dedup`` verifies the
live table before writing and the CLI's ``--require-dedup`` turns a missing
guarantee into a refusal instead of a silent duplicate-bar import.
"""

from __future__ import annotations

import argparse
import csv
import math
import sys
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import IO
from zoneinfo import ZoneInfo

from vnpy.trader.constant import Exchange, Interval
from vnpy.trader.database import BaseDatabase, get_database
from vnpy.trader.object import BarData
from vnpy_gatewaykit.bar_label import (
    ENV_SWITCH,
    AuctionPolicy,
    normalization_enabled,
    start_label_on_grid,
)
from vnpy_gatewaykit.market_clock import market_tz

__all__ = [
    "AmbiguousPolicy",
    "AuctionPolicy",
    "BarLabel",
    "BulkImportReport",
    "CloseShift",
    "ColumnMap",
    "DedupStatus",
    "FileReport",
    "FileStatus",
    "GroupStat",
    "ImportTask",
    "ProgressEvent",
    "RejectReason",
    "RejectedRow",
    "bulk_import",
    "check_dedup",
    "main",
    "supported_exchanges",
    "tasks_from_directory",
]


BAR_TABLE: str = "dbbardata"
"""Table name the QuestDB adapter writes bars to; used for dedup introspection."""

REQUIRED_UPSERT_KEYS: frozenset[str] = frozenset(
    {"datetime", "symbol", "exchange", "interval"}
)
"""The upsert key set that makes a re-import idempotent rather than duplicating."""

DEFAULT_CHUNK_SIZE: int = 100_000
"""Bars buffered per (symbol, exchange, interval) group before a write.

At ~281 bytes per ``BarData`` this caps a group's resident cost near 28 MB,
which is what keeps an arbitrarily large CSV from becoming an
arbitrarily large heap. Writes are chunked rather than one-shot precisely
because the dedup upsert makes a partial write safe to resume over.
"""

DEFAULT_MAX_REJECT_SAMPLES: int = 100
"""Per file cap on *retained* rejected-row detail.

Counts are always complete and per-reason; only the stored examples are
capped, so a pathological file cannot turn the report into a memory leak.
Whenever the cap bites, the report says so explicitly — the requirement is
that bad data is never silently swallowed, not that every bad row is echoed.
"""


class BarLabel(Enum):
    """Which edge of the bar's period the CSV timestamp refers to."""

    OPEN = "open"
    """Timestamp is the bar's opening instant — vnpy's own convention, no shift."""

    CLOSE = "close"
    """Timestamp is the bar's closing instant — shifted back by one bar span."""


class CloseShift(Enum):
    """How a close-labelled timestamp is turned back into the bar's open.

    Both conventions exist in vendor exports and the file itself cannot say
    which it is, so — like ``BarLabel`` — this is declared, never inferred.
    """

    SESSION_GRID = "session_grid"
    """Rebuild the exchange's session grid and take the slot the label closes.

    Correct for vendors that align bars to the *session* (Futu, uSMART,
    Longbridge — everything our own gateways produce). Handles bars truncated
    by a session break: HK 60m labelled 12:00 covers ``[11:30, 12:00)``, so it
    opens at 11:30, not at 11:00. Default, because matching the gateways is
    what keeps a CSV re-import an upsert rather than a duplicate.
    """

    FIXED_SPAN = "fixed_span"
    """Subtract exactly one span in absolute time.

    Correct for vendors that align bars to the *wall clock* regardless of
    session boundaries (hourly stamps at 10:00/11:00/12:00 rather than
    10:30/11:30/12:00). Choosing this on session-aligned data silently moves
    every truncated bar off-grid, which is why it is not the default.
    """


class AmbiguousPolicy(Enum):
    """What to do with a wall-clock time that occurs twice on a DST fall-back day."""

    REJECT = "reject"
    """Default. Such a stamp cannot occur in an HK or US equity session."""

    EARLIER = "earlier"
    """Take the first pass (still on daylight time, ``fold=0``)."""

    LATER = "later"
    """Take the second pass (back on standard time, ``fold=1``)."""


class RejectReason(Enum):
    """Why a single row was refused. Every rejection is counted under one of these."""

    EMPTY_FIELD = "empty_field"
    EMPTY_SYMBOL = "empty_symbol"
    BAD_DATETIME = "bad_datetime"
    BAD_NUMBER = "bad_number"
    NON_FINITE = "non_finite"
    NEGATIVE_VALUE = "negative_value"
    ZERO_PRICE = "zero_price"
    OHLC_INCONSISTENT = "ohlc_inconsistent"
    DUPLICATE_TIMESTAMP = "duplicate_timestamp"
    NONEXISTENT_LOCAL_TIME = "nonexistent_local_time"
    AMBIGUOUS_LOCAL_TIME = "ambiguous_local_time"
    AUCTION_DROPPED = "auction_dropped"


class FileStatus(Enum):
    """Outcome of one file within the batch."""

    OK = "ok"
    """Every row accepted."""

    PARTIAL = "partial"
    """Some rows accepted, some rejected."""

    EMPTY = "empty"
    """Parsed fine, contained no data rows."""

    FAILED = "failed"
    """Nothing imported — unreadable, bad header, or an unexpected error."""


class DedupStatus(Enum):
    """Whether the destination table can be trusted to upsert on re-import."""

    ENABLED = "enabled"
    DISABLED = "disabled"
    UNKNOWN = "unknown"
    """Not a QuestDB backend, or introspection failed. Not a failure by itself."""


@dataclass(frozen=True)
class ColumnMap:
    """CSV header names for the fields we read.

    ``turnover`` and ``open_interest`` are optional: if the named column is
    absent from the header the value defaults to 0.0 rather than failing,
    matching the third-party importer's ``.get(..., 0)`` behaviour. Every
    other name must be present in the header or the file is rejected whole —
    a typo'd price column must not silently import a column of zeros.

    ``symbol`` opts into one-file-many-symbols: when set, the row's symbol is
    read from that column and ``ImportTask.symbol`` must be ``None``.
    """

    datetime: str = "datetime"
    open: str = "open"
    high: str = "high"
    low: str = "low"
    close: str = "close"
    volume: str = "volume"
    turnover: str | None = "turnover"
    open_interest: str | None = "open_interest"
    symbol: str | None = None

    def required_headers(self) -> tuple[str, ...]:
        """Header names whose absence makes the file unusable."""
        names = [self.datetime, self.open, self.high, self.low, self.close, self.volume]
        if self.symbol:
            names.append(self.symbol)
        return tuple(names)


@dataclass(frozen=True)
class ImportTask:
    """One CSV file plus everything needed to interpret it.

    ``label`` is mandatory and has no default: whether a vendor stamps a bar
    at its open or its close is not inferable from the file, and guessing it
    shifts every bar by one period.
    """

    path: Path
    exchange: Exchange
    interval: Interval
    label: BarLabel
    symbol: str | None = None
    columns: ColumnMap = field(default_factory=ColumnMap)
    datetime_format: str | None = None
    """``strptime`` format. ``None`` uses ``datetime.fromisoformat``."""

    bar_span: timedelta | None = None
    """Overrides the period used by ``BarLabel.CLOSE``.

    Needed when ``Interval.MINUTE`` is being used to store bars that are not
    one minute wide (a 5-minute vendor export, say): the close-label shift
    must be 5 minutes, and only the caller knows that.
    """

    encoding: str = "utf-8-sig"
    ambiguous: AmbiguousPolicy = AmbiguousPolicy.REJECT

    close_shift: CloseShift = CloseShift.SESSION_GRID
    """Which close-to-open rule applies. Only consulted for ``BarLabel.CLOSE``."""

    auction: AuctionPolicy = AuctionPolicy.WINDOW_START
    """How a close-labelled stamp landing in an auction window is resolved.

    Ignored under ``CloseShift.FIXED_SPAN``, which never consults the calendar.

    Only consulted for ``BarLabel.CLOSE``. The default matches what
    ``vnpy_gatewaykit.normalize_bars`` does to gateway data, so the same bar
    arriving by CSV and by Futu lands on the same key instead of duplicating.
    """

    def __post_init__(self) -> None:
        if self.symbol and self.columns.symbol:
            raise ValueError(
                f"{self.path}: symbol given both as a fixed value ({self.symbol!r}) "
                f"and as a CSV column ({self.columns.symbol!r}); pick one"
            )
        if not self.symbol and not self.columns.symbol:
            raise ValueError(
                f"{self.path}: no symbol — set ImportTask.symbol or ColumnMap.symbol"
            )
        if self.interval is Interval.TICK:
            raise ValueError(f"{self.path}: Interval.TICK is not bar data")
        # Resolving the span here means a mis-specified task fails at
        # construction, before any file is touched, instead of after a
        # partial batch has already been written.
        if self.label is BarLabel.CLOSE:
            self.resolve_span()
            self._require_matching_gateway_label()

    def _require_matching_gateway_label(self) -> None:
        """Refuse a close-label import while the gateway side is not normalizing.

        The two paths into bar_label are deliberately different: the gateway
        entry (``to_start_label``) consults the global switch, while this
        primitive (``start_label_on_grid``) does not — for CSV, only the caller
        knows whether the vendor stamped the open or the close.

        That asymmetry is fine as long as both ends agree on the resulting
        convention. When they disagree, the same candle lands under two
        different timestamps: QuestDB's DEDUP key is
        (datetime, symbol, exchange, interval), so both copies are kept and the
        series silently carries duplicates offset by one period. Measured on HK
        60m 2024-01-02, gateway wrote 10:30/11:30/12:00/14:00/15:00/16:00 while
        a close-label CSV of the same bars wrote 09:30/10:30/11:30/13:00/...
        — six for six.

        Failing here is the whole point: an import that would corrupt the series
        must stop before touching a file, not report success.
        """
        if normalization_enabled():
            return
        raise ValueError(
            f"{self.path}: label=close 需要把收尾标签归一到起始时刻，"
            f"但网关侧的归一当前是关闭的（{ENV_SWITCH} 未设或为 0）。"
            f"两侧口径不一致会让同一根 K 线在库里落成两个时间戳（DEDUP 键不同，"
            f"两份都会保留）。要么先完成历史数据迁移并设 {ENV_SWITCH}=1，"
            f"要么用 label=open 按文件原样导入。"
        )

    def resolve_span(self) -> timedelta:
        """The period to subtract for a close-labelled bar.

        Raises for daily and weekly bars: those are labelled by trading date,
        not by an instant, so "close-labelled" has no meaning and shifting by
        one period would move every bar into the previous session.
        """
        if self.bar_span is not None:
            if self.bar_span <= timedelta(0):
                raise ValueError(f"{self.path}: bar_span must be positive")
            return self.bar_span

        span = _INTERVAL_SPAN.get(self.interval)
        if span is None:
            raise ValueError(
                f"{self.path}: BarLabel.CLOSE is undefined for {self.interval.value} "
                f"bars — they are labelled by trading date, not by an instant. "
                f"Use BarLabel.OPEN, or pass an explicit bar_span if this file "
                f"really is intraday data stored under {self.interval.value}."
            )
        return span


_INTERVAL_SPAN: dict[Interval, timedelta] = {
    Interval.MINUTE: timedelta(minutes=1),
    Interval.HOUR: timedelta(hours=1),
}


@dataclass(frozen=True)
class RejectedRow:
    """A retained example of a refused row."""

    line_no: int
    reason: RejectReason
    detail: str

    def __str__(self) -> str:
        return f"line {self.line_no}: {self.reason.value} ({self.detail})"


@dataclass
class GroupStat:
    """Per-symbol span written out of one file."""

    symbol: str
    count: int = 0
    start: datetime | None = None
    end: datetime | None = None

    def observe(self, dt: datetime) -> None:
        self.count += 1
        if self.start is None or dt < self.start:
            self.start = dt
        if self.end is None or dt > self.end:
            self.end = dt


@dataclass
class FileReport:
    """Outcome of one file. A failure here never stops the batch."""

    path: Path
    status: FileStatus = FileStatus.OK
    rows_read: int = 0
    accepted: int = 0
    written: int = 0
    reject_counts: dict[RejectReason, int] = field(default_factory=dict)
    reject_samples: list[RejectedRow] = field(default_factory=list)
    samples_truncated: bool = False
    sample_cap: int = DEFAULT_MAX_REJECT_SAMPLES
    tz_aware_rows: int = 0
    """Rows whose CSV timestamp carried its own offset and was trusted as-is."""

    outside_session_rows: int = 0
    """Close-labelled rows that fell outside every published session window.

    Those have no session grid to snap to, so the shift falls back to plain
    absolute-time subtraction. Counted rather than silent: a non-zero value
    means the file disagrees with the exchange calendar (lunch-break stamps,
    a holiday row, the wrong exchange) and deserves a look.
    """

    groups: dict[str, GroupStat] = field(default_factory=dict)
    error: str | None = None

    @property
    def rejected(self) -> int:
        return sum(self.reject_counts.values())

    def record_reject(self, line_no: int, reason: RejectReason, detail: str) -> None:
        """Count a refused row, retaining its detail up to ``sample_cap``."""
        self.reject_counts[reason] = self.reject_counts.get(reason, 0) + 1
        if len(self.reject_samples) < self.sample_cap:
            self.reject_samples.append(RejectedRow(line_no, reason, detail))
        else:
            self.samples_truncated = True

    def summary(self) -> str:
        """One dense line, safe to print per file."""
        if self.status is FileStatus.FAILED:
            return f"[FAILED ] {self.path.name}: {self.error}"

        parts = [
            f"[{self.status.value.upper():7}] {self.path.name}",
            f"read={self.rows_read}",
            f"ok={self.accepted}",
            f"rejected={self.rejected}",
            f"written={self.written}",
        ]
        if self.groups:
            parts.append(f"symbols={len(self.groups)}")
        if self.outside_session_rows:
            parts.append(f"outside_session={self.outside_session_rows}")
        if self.reject_counts:
            reasons = ", ".join(
                f"{reason.value}={count}"
                for reason, count in sorted(
                    self.reject_counts.items(), key=lambda kv: kv[0].value
                )
            )
            parts.append(f"[{reasons}]")
        return " ".join(parts)


@dataclass
class BulkImportReport:
    """Outcome of a whole batch."""

    files: list[FileReport] = field(default_factory=list)
    dedup: DedupStatus = DedupStatus.UNKNOWN
    dedup_detail: str = ""
    dry_run: bool = False

    @property
    def rows_read(self) -> int:
        return sum(f.rows_read for f in self.files)

    @property
    def accepted(self) -> int:
        return sum(f.accepted for f in self.files)

    @property
    def rejected(self) -> int:
        return sum(f.rejected for f in self.files)

    @property
    def written(self) -> int:
        return sum(f.written for f in self.files)

    @property
    def failed_files(self) -> list[FileReport]:
        return [f for f in self.files if f.status is FileStatus.FAILED]

    @property
    def ok(self) -> bool:
        """True only when nothing failed and nothing was rejected."""
        return not self.failed_files and self.rejected == 0

    def format_text(self) -> str:
        lines = [f.summary() for f in self.files]
        lines.append(
            f"--- {len(self.files)} file(s): read={self.rows_read} "
            f"ok={self.accepted} rejected={self.rejected} written={self.written} "
            f"failed_files={len(self.failed_files)}"
            + (" [DRY-RUN]" if self.dry_run else "")
        )
        lines.append(f"--- dedup: {self.dedup.value} ({self.dedup_detail})")
        return "\n".join(lines)


@dataclass(frozen=True)
class ProgressEvent:
    """Emitted to the ``progress`` callback as the batch advances."""

    file_index: int
    file_total: int
    path: Path
    rows_read: int
    accepted: int
    rejected: int
    done: bool
    """True on the final event for this file."""


ProgressCallback = Callable[[ProgressEvent], None]


def check_dedup(database: BaseDatabase, table: str = BAR_TABLE) -> tuple[DedupStatus, str]:
    """Ask the live table whether a re-import upserts or duplicates.

    Duck-typed on ``conninfo`` so a non-QuestDB backend (or a stub in a test)
    reports ``UNKNOWN`` instead of raising. Introspection failure is never
    fatal here — the caller decides whether ``UNKNOWN`` is acceptable.
    """
    conninfo = getattr(database, "conninfo", None)
    if not isinstance(conninfo, str):
        return DedupStatus.UNKNOWN, f"{type(database).__name__} exposes no conninfo"

    try:
        import psycopg
    except ImportError:  # pragma: no cover - psycopg ships with vnpy_questdb
        return DedupStatus.UNKNOWN, "psycopg not installed"

    try:
        with psycopg.connect(conninfo) as conn, conn.cursor() as cursor:
            cursor.execute("SELECT dedup FROM tables() WHERE table_name = %s", (table,))
            row = cursor.fetchone()
            if row is None:
                return DedupStatus.UNKNOWN, f"table {table} does not exist yet"
            if not row[0]:
                return DedupStatus.DISABLED, f"table {table} has DEDUP off"

            # table is the module-level BAR_TABLE constant, never user input.
            cursor.execute(f"SHOW COLUMNS FROM {table}")
            columns = [d.name for d in cursor.description or []]
            key_index = columns.index("upsertKey")
            name_index = columns.index("column")
            keys = {r[name_index] for r in cursor.fetchall() if r[key_index]}
    except Exception as exc:  # noqa: BLE001 - any driver/network error is UNKNOWN
        return DedupStatus.UNKNOWN, f"{type(exc).__name__}: {exc}"

    missing = REQUIRED_UPSERT_KEYS - keys
    if missing:
        return (
            DedupStatus.DISABLED,
            f"table {table} upsert keys {sorted(keys)} miss {sorted(missing)}",
        )
    return DedupStatus.ENABLED, f"UPSERT KEYS{sorted(keys)}"


def tasks_from_directory(
    directory: Path | str,
    exchange: Exchange,
    interval: Interval,
    label: BarLabel,
    *,
    pattern: str = "*.csv",
    symbol_from_path: Callable[[Path], str] | None = None,
    columns: ColumnMap | None = None,
    datetime_format: str | None = None,
    bar_span: timedelta | None = None,
    encoding: str = "utf-8-sig",
    ambiguous: AmbiguousPolicy = AmbiguousPolicy.REJECT,
    close_shift: CloseShift = CloseShift.SESSION_GRID,
    auction: AuctionPolicy = AuctionPolicy.WINDOW_START,
) -> list[ImportTask]:
    """Build one task per matching file, symbol taken from the filename stem.

    Sorted by path so a batch is reproducible. ``symbol_from_path`` overrides
    the stem rule for vendors that decorate filenames (``AAPL_1m.csv``).

    When ``columns.symbol`` is set the filename carries no meaning — the
    symbol comes from each row instead, and the per-file symbol is left unset.
    """
    directory = Path(directory)
    if not directory.is_dir():
        raise NotADirectoryError(str(directory))

    columns = columns or ColumnMap()

    def to_symbol(path: Path) -> str | None:
        if columns.symbol:
            return None
        if symbol_from_path is not None:
            return symbol_from_path(path)
        return path.stem

    tasks: list[ImportTask] = []
    for path in sorted(directory.glob(pattern)):
        if not path.is_file():
            continue
        tasks.append(
            ImportTask(
                path=path,
                exchange=exchange,
                interval=interval,
                label=label,
                symbol=to_symbol(path),
                columns=columns,
                datetime_format=datetime_format,
                bar_span=bar_span,
                encoding=encoding,
                ambiguous=ambiguous,
                close_shift=close_shift,
                auction=auction,
            )
        )
    return tasks


def bulk_import(
    tasks: Sequence[ImportTask],
    *,
    database: BaseDatabase | None = None,
    progress: ProgressCallback | None = None,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    max_reject_samples: int = DEFAULT_MAX_REJECT_SAMPLES,
    progress_every: int = 10_000,
    dry_run: bool = False,
    require_dedup: bool = False,
) -> BulkImportReport:
    """Import every task, isolating failures per file.

    A file that cannot be read, has a bad header, or blows up unexpectedly is
    recorded as ``FileStatus.FAILED`` and the batch moves on — that isolation
    is the point of the whole module, so no exception from one file is
    allowed to escape and take the others with it.

    ``dry_run`` runs every parse, validation and grouping step and skips only
    ``save_bar_data``; ``written`` then reports what *would* have been sent.
    """
    if chunk_size < 1:
        raise ValueError("chunk_size must be >= 1")

    report = BulkImportReport(dry_run=dry_run)

    if database is None and not dry_run:
        database = get_database()

    if database is not None:
        report.dedup, report.dedup_detail = check_dedup(database)
    else:
        report.dedup, report.dedup_detail = (
            DedupStatus.UNKNOWN,
            "dry run, database not opened",
        )

    if require_dedup and report.dedup is not DedupStatus.ENABLED:
        raise RuntimeError(
            f"refusing to import: {BAR_TABLE} dedup is {report.dedup.value} "
            f"({report.dedup_detail}); a re-import would duplicate bars"
        )

    total = len(tasks)
    for index, task in enumerate(tasks):
        file_report = FileReport(path=task.path, sample_cap=max_reject_samples)
        report.files.append(file_report)
        try:
            _import_one(
                task,
                file_report,
                database=database,
                chunk_size=chunk_size,
                dry_run=dry_run,
                progress=progress,
                progress_every=progress_every,
                file_index=index,
                file_total=total,
            )
        except Exception as exc:  # noqa: BLE001 - isolation is the contract
            file_report.status = FileStatus.FAILED
            file_report.error = f"{type(exc).__name__}: {exc}"

        if progress is not None:
            _emit(
                progress,
                file_report,
                index,
                total,
                done=True,
            )

    return report


def _emit(
    progress: ProgressCallback,
    file_report: FileReport,
    file_index: int,
    file_total: int,
    *,
    done: bool,
) -> None:
    progress(
        ProgressEvent(
            file_index=file_index,
            file_total=file_total,
            path=file_report.path,
            rows_read=file_report.rows_read,
            accepted=file_report.accepted,
            rejected=file_report.rejected,
            done=done,
        )
    )


def _strip_nulls(stream: Iterable[str]) -> Iterator[str]:
    """Drop stray NUL bytes line by line.

    The third-party importer does the same thing but slurps the whole file
    into a list first; streaming keeps a large file off the heap.
    """
    for line in stream:
        yield line.replace("\0", "") if "\0" in line else line


def _import_one(
    task: ImportTask,
    report: FileReport,
    *,
    database: BaseDatabase | None,
    chunk_size: int,
    dry_run: bool,
    progress: ProgressCallback | None,
    progress_every: int,
    file_index: int,
    file_total: int,
) -> None:
    tz = market_tz(task.exchange)
    span = task.resolve_span() if task.label is BarLabel.CLOSE else None
    columns = task.columns

    with open(task.path, encoding=task.encoding, newline="") as handle:
        reader = csv.DictReader(_strip_nulls(handle), delimiter=",")
        header = reader.fieldnames
        if not header:
            report.status = FileStatus.EMPTY
            return

        missing = [name for name in columns.required_headers() if name not in header]
        if missing:
            report.status = FileStatus.FAILED
            report.error = f"header missing required column(s): {missing}; got {header}"
            return

        has_turnover = bool(columns.turnover) and columns.turnover in header
        has_open_interest = (
            bool(columns.open_interest) and columns.open_interest in header
        )

        buffers: dict[str, list[BarData]] = {}
        seen: dict[str, set[int]] = {}

        for row in reader:
            report.rows_read += 1
            line_no = reader.line_num

            parsed = _parse_row(
                row=row,
                task=task,
                tz=tz,
                span=span,
                columns=columns,
                has_turnover=has_turnover,
                has_open_interest=has_open_interest,
                line_no=line_no,
                report=report,
            )
            if parsed is None:
                continue

            bar, was_aware = parsed
            if was_aware:
                report.tz_aware_rows += 1

            # Intra-file duplicate detection is per symbol group and keyed on
            # the final stored instant, so two rows that differ only in how
            # they spelled the same moment still collide. Duplicates *across*
            # files are not tracked: QuestDB's upsert collapses them anyway.
            key = int(bar.datetime.timestamp() * 1_000_000)
            group_seen = seen.setdefault(bar.symbol, set())
            if key in group_seen:
                report.record_reject(
                    line_no,
                    RejectReason.DUPLICATE_TIMESTAMP,
                    f"{bar.symbol} {bar.datetime.isoformat()} already seen in this file",
                )
                continue
            group_seen.add(key)

            report.accepted += 1
            report.groups.setdefault(
                bar.symbol, GroupStat(symbol=bar.symbol)
            ).observe(bar.datetime)

            buffer = buffers.setdefault(bar.symbol, [])
            buffer.append(bar)
            if len(buffer) >= chunk_size:
                _flush(buffer, database, dry_run, report)

            if progress is not None and report.rows_read % progress_every == 0:
                _emit(progress, report, file_index, file_total, done=False)

        for buffer in buffers.values():
            _flush(buffer, database, dry_run, report)

    if report.rows_read == 0:
        report.status = FileStatus.EMPTY
    elif report.accepted == 0:
        # Everything was refused. Not an exception — a fully-reported outcome.
        report.status = FileStatus.FAILED
        report.error = f"all {report.rows_read} row(s) rejected"
    elif report.rejected:
        report.status = FileStatus.PARTIAL
    else:
        report.status = FileStatus.OK


def _flush(
    buffer: list[BarData],
    database: BaseDatabase | None,
    dry_run: bool,
    report: FileReport,
) -> None:
    if not buffer:
        return
    if not dry_run:
        if database is None:  # pragma: no cover - bulk_import always opens one
            raise RuntimeError("no database to write to and dry_run is False")
        database.save_bar_data(buffer)
    report.written += len(buffer)
    buffer.clear()


def _parse_row(
    *,
    row: dict[str, str | None],
    task: ImportTask,
    tz: ZoneInfo,
    span: timedelta | None,
    columns: ColumnMap,
    has_turnover: bool,
    has_open_interest: bool,
    line_no: int,
    report: FileReport,
) -> tuple[BarData, bool] | None:
    """Turn one CSV row into a BarData, or record why it cannot be one."""
    symbol = task.symbol
    if symbol is None:
        # ImportTask.__post_init__ guarantees exactly one of the two is set.
        symbol_column = columns.symbol or ""
        raw_symbol = (row.get(symbol_column) or "").strip()
        if not raw_symbol:
            report.record_reject(line_no, RejectReason.EMPTY_SYMBOL, "symbol column is blank")
            return None
        symbol = raw_symbol

    raw_dt = (row.get(columns.datetime) or "").strip()
    if not raw_dt:
        report.record_reject(
            line_no, RejectReason.EMPTY_FIELD, f"{columns.datetime} is blank"
        )
        return None

    try:
        naive_or_aware = (
            datetime.strptime(raw_dt, task.datetime_format)
            if task.datetime_format
            else datetime.fromisoformat(raw_dt)
        )
    except ValueError as exc:
        report.record_reject(line_no, RejectReason.BAD_DATETIME, f"{raw_dt!r}: {exc}")
        return None

    localized = _localize(naive_or_aware, tz, task.ambiguous)
    if isinstance(localized, RejectReason):
        shape = (
            "DST gap, this wall clock never happened"
            if localized is RejectReason.NONEXISTENT_LOCAL_TIME
            else "DST fall-back, this wall clock happened twice"
        )
        report.record_reject(line_no, localized, f"{raw_dt!r} in {tz.key}: {shape}")
        return None

    dt, was_aware = localized
    if span is not None:
        shifted = _close_to_open(dt, task, span, tz, line_no, report)
        if shifted is None:
            return None
        dt = shifted

    numbers: dict[str, float] = {}
    fields: list[tuple[str, str | None, bool]] = [
        ("open", columns.open, True),
        ("high", columns.high, True),
        ("low", columns.low, True),
        ("close", columns.close, True),
        ("volume", columns.volume, True),
        ("turnover", columns.turnover if has_turnover else None, False),
        ("open_interest", columns.open_interest if has_open_interest else None, False),
    ]
    for name, header, required in fields:
        if header is None:
            numbers[name] = 0.0
            continue
        raw = (row.get(header) or "").strip()
        if not raw:
            if required:
                report.record_reject(line_no, RejectReason.EMPTY_FIELD, f"{header} is blank")
                return None
            numbers[name] = 0.0
            continue
        try:
            value = float(raw)
        except ValueError:
            report.record_reject(
                line_no, RejectReason.BAD_NUMBER, f"{header}={raw!r} is not a number"
            )
            return None
        if not math.isfinite(value):
            report.record_reject(
                line_no, RejectReason.NON_FINITE, f"{header}={raw!r} is not finite"
            )
            return None
        numbers[name] = value

    negative = [name for name, value in numbers.items() if value < 0]
    if negative:
        report.record_reject(
            line_no,
            RejectReason.NEGATIVE_VALUE,
            f"negative {sorted(negative)}: "
            + ", ".join(f"{n}={numbers[n]}" for n in sorted(negative)),
        )
        return None

    prices = {n: numbers[n] for n in ("open", "high", "low", "close")}
    zeros = [name for name, value in prices.items() if value == 0.0]
    if zeros:
        # A zero print on an equity bar is corrupt, not a real trade. Volume
        # and turnover may legitimately be zero (a minute with no trades) and
        # are deliberately not covered by this rule.
        report.record_reject(
            line_no, RejectReason.ZERO_PRICE, f"zero price in {sorted(zeros)}"
        )
        return None

    high, low = prices["high"], prices["low"]
    body_high = max(prices["open"], prices["close"])
    body_low = min(prices["open"], prices["close"])
    if high < low or high < body_high or low > body_low:
        report.record_reject(
            line_no,
            RejectReason.OHLC_INCONSISTENT,
            f"o={prices['open']} h={high} l={low} c={prices['close']}",
        )
        return None

    bar = BarData(
        symbol=symbol,
        exchange=task.exchange,
        datetime=dt,
        interval=task.interval,
        volume=numbers["volume"],
        turnover=numbers["turnover"],
        open_interest=numbers["open_interest"],
        open_price=prices["open"],
        high_price=high,
        low_price=low,
        close_price=prices["close"],
        gateway_name="CSV",
    )
    return bar, was_aware


def _close_to_open(
    dt: datetime,
    task: ImportTask,
    span: timedelta,
    tz: ZoneInfo,
    line_no: int,
    report: FileReport,
) -> datetime | None:
    """Map a close-labelled stamp to the instant the bar opened.

    Subtracting one span is only right in the *interior* of a session. The last
    bar before a break is truncated by the break, so its span is shorter than
    the nominal period and plain subtraction lands on no real grid point:
    HK 60m labelled 12:00 covers ``[11:30, 12:00)`` (the 30-minute remainder of
    the 09:30-12:00 morning), and 12:00 minus an hour is 11:00 — inside the
    *previous* bar. US 60m labelled 16:00 is the same shape, giving 15:00
    instead of 15:30.

    So the grid is rebuilt from the session windows rather than subtracted,
    reusing ``vnpy_gatewaykit.bar_label`` — the same code path that normalizes
    Futu's END-labelled bars. Sharing it is the point: a bar imported from CSV
    and the same bar downloaded through a gateway must produce the *same*
    ``datetime``, or QuestDB's ``DEDUP UPSERT KEYS(datetime, symbol, exchange,
    interval)`` sees two different keys and keeps both.

    A vendor that aligns to the wall clock instead of the session has no
    truncated bars to get wrong, and for those ``CloseShift.FIXED_SPAN``
    subtracts the span directly.
    """
    if task.close_shift is CloseShift.FIXED_SPAN:
        # Absolute-time arithmetic: subtracting from an aware datetime directly
        # would do wall-clock math and land on the wrong instant (or on a
        # nonexistent one) across a DST boundary.
        return (dt.astimezone(timezone.utc) - span).astimezone(tz)

    result = start_label_on_grid(
        dt, exchange=task.exchange, period=span, auction_policy=task.auction
    )

    if result.drop:
        report.record_reject(
            line_no,
            RejectReason.AUCTION_DROPPED,
            f"{dt.isoformat()} falls in an auction window and "
            f"auction policy is {task.auction.value}",
        )
        return None

    if result.note.endswith("outside-all-windows"):
        # No window contains this stamp, so there is no grid to snap to.
        # Absolute-time subtraction is the only defensible fallback; it is
        # counted so the disagreement with the calendar stays visible.
        report.outside_session_rows += 1
        return (dt.astimezone(timezone.utc) - span).astimezone(tz)

    return result.datetime.astimezone(tz)


def _localize(
    dt: datetime,
    tz: ZoneInfo,
    ambiguous: AmbiguousPolicy,
) -> tuple[datetime, bool] | RejectReason:
    """Attach the market timezone, or say why the stamp is not a real instant.

    Returns ``(aware_datetime, csv_carried_its_own_offset)``.

    An already-aware timestamp is trusted and returned untouched. That is the
    one behavioural difference from ``vnpy_datamanager``'s
    ``dt.replace(tzinfo=tz)``, which overwrites a parsed ``-05:00`` with the
    declared market zone and silently moves the bar by hours.
    """
    if dt.tzinfo is not None:
        return dt, True

    aware = dt.replace(tzinfo=tz)

    # Spring forward: the wall clock jumps, so this local time never occurred.
    # ZoneInfo does not complain; it just yields an instant that maps back to
    # a *different* wall clock. Round-tripping is how that is detected.
    if aware.astimezone(timezone.utc).astimezone(tz).replace(tzinfo=None) != dt:
        return RejectReason.NONEXISTENT_LOCAL_TIME

    # Fall back: the wall clock repeats, so this local time occurred twice and
    # the two passes have different UTC offsets.
    if aware.utcoffset() != aware.replace(fold=1).utcoffset():
        if ambiguous is AmbiguousPolicy.REJECT:
            return RejectReason.AMBIGUOUS_LOCAL_TIME
        fold = 0 if ambiguous is AmbiguousPolicy.EARLIER else 1
        return aware.replace(fold=fold), False

    return aware, False


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

_EXIT_OK = 0
_EXIT_FAILED_FILES = 1
_EXIT_REJECTED_ROWS = 2


def supported_exchanges() -> list[Exchange]:
    """Exchanges ``market_clock`` can localize, discovered through its public API.

    Offering the full ``Exchange`` enum on the CLI would advertise markets
    whose timezone is unmapped, and a naive timestamp cannot be placed
    without one. Probing rather than reading ``market_clock``'s private dict
    means a market added there shows up here for free.
    """
    supported: list[Exchange] = []
    for exchange in Exchange:
        try:
            market_tz(exchange)
        except KeyError:
            continue
        supported.append(exchange)
    return sorted(supported, key=lambda e: e.name)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m data_tools.bulk_import",
        description="Batch-import historical bar CSVs into the vnpy database.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "exit codes: 0 all clean | 1 at least one file failed | "
            "2 all files processed but some rows were rejected"
        ),
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--dir", type=Path, help="import every --pattern file in this directory"
    )
    source.add_argument("--file", type=Path, nargs="+", help="import these files")

    parser.add_argument("--pattern", default="*.csv", help="glob for --dir (*.csv)")
    parser.add_argument(
        "--exchange",
        required=True,
        choices=[e.name for e in supported_exchanges()],
        help="market the files belong to (SEHK for HK, SMART for US)",
    )
    parser.add_argument(
        "--interval",
        required=True,
        choices=[i.value for i in Interval if i is not Interval.TICK],
        help="bar interval as stored by vnpy",
    )
    parser.add_argument(
        "--label",
        required=True,
        choices=[label.value for label in BarLabel],
        help="does the CSV timestamp mark the bar's open or its close",
    )
    parser.add_argument(
        "--symbol",
        help="fixed symbol for every row (use with --file for a single symbol)",
    )
    parser.add_argument(
        "--symbol-column",
        help="read the symbol from this CSV column instead of the filename",
    )
    parser.add_argument(
        "--bar-span-seconds",
        type=int,
        help="explicit bar width for --label close (e.g. 300 for 5-minute bars)",
    )
    parser.add_argument("--datetime-column", default="datetime")
    parser.add_argument("--open-column", default="open")
    parser.add_argument("--high-column", default="high")
    parser.add_argument("--low-column", default="low")
    parser.add_argument("--close-column", default="close")
    parser.add_argument("--volume-column", default="volume")
    parser.add_argument("--turnover-column", default="turnover")
    parser.add_argument("--open-interest-column", default="open_interest")
    parser.add_argument(
        "--datetime-format", help="strptime format; default is ISO 8601 parsing"
    )
    parser.add_argument("--encoding", default="utf-8-sig")
    parser.add_argument(
        "--ambiguous",
        choices=[p.value for p in AmbiguousPolicy],
        default=AmbiguousPolicy.REJECT.value,
        help="DST fall-back repeated hour policy (default reject)",
    )
    parser.add_argument(
        "--close-shift",
        choices=[s.value for s in CloseShift],
        default=CloseShift.SESSION_GRID.value,
        help=(
            "how --label close maps to the bar open: session_grid (default, "
            "session-aligned vendors, handles bars truncated by a break) or "
            "fixed_span (wall-clock-aligned vendors)"
        ),
    )
    parser.add_argument(
        "--auction",
        choices=[p.value for p in AuctionPolicy],
        default=AuctionPolicy.WINDOW_START.value,
        help="close-labelled bars inside an auction window (default window_start)",
    )
    parser.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK_SIZE)
    parser.add_argument(
        "--max-reject-samples", type=int, default=DEFAULT_MAX_REJECT_SAMPLES
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="parse and validate everything, write nothing",
    )
    parser.add_argument(
        "--require-dedup",
        action="store_true",
        help="refuse to write unless the table's DEDUP UPSERT KEYS are in place",
    )
    parser.add_argument(
        "--quiet", action="store_true", help="suppress per-file progress lines"
    )
    return parser


def main(argv: Sequence[str] | None = None, stream: IO[str] | None = None) -> int:
    """CLI entry point. Returns a process exit code, never raises on data errors."""
    args = _build_parser().parse_args(argv)
    out = stream if stream is not None else sys.stdout

    columns = ColumnMap(
        datetime=args.datetime_column,
        open=args.open_column,
        high=args.high_column,
        low=args.low_column,
        close=args.close_column,
        volume=args.volume_column,
        turnover=args.turnover_column,
        open_interest=args.open_interest_column,
        symbol=args.symbol_column,
    )
    exchange = Exchange[args.exchange]
    interval = Interval(args.interval)
    label = BarLabel(args.label)
    ambiguous = AmbiguousPolicy(args.ambiguous)
    close_shift = CloseShift(args.close_shift)
    auction = AuctionPolicy(args.auction)
    bar_span = (
        timedelta(seconds=args.bar_span_seconds)
        if args.bar_span_seconds is not None
        else None
    )

    try:
        if args.dir is not None:
            fixed_symbol = args.symbol
            tasks = tasks_from_directory(
                args.dir,
                exchange,
                interval,
                label,
                pattern=args.pattern,
                # --symbol pins every file to one symbol; otherwise the stem
                # rule applies. A --symbol-column wins over both, and
                # tasks_from_directory handles that itself.
                symbol_from_path=(
                    (lambda _path: fixed_symbol) if fixed_symbol else None
                ),
                columns=columns,
                datetime_format=args.datetime_format,
                bar_span=bar_span,
                encoding=args.encoding,
                ambiguous=ambiguous,
                close_shift=close_shift,
                auction=auction,
            )
        else:
            tasks = [
                ImportTask(
                    path=path,
                    exchange=exchange,
                    interval=interval,
                    label=label,
                    symbol=None if args.symbol_column else (args.symbol or path.stem),
                    columns=columns,
                    datetime_format=args.datetime_format,
                    bar_span=bar_span,
                    encoding=args.encoding,
                    ambiguous=ambiguous,
                    close_shift=close_shift,
                    auction=auction,
                )
                for path in args.file
            ]
    except (ValueError, NotADirectoryError) as exc:
        print(f"error: {exc}", file=out)
        return _EXIT_FAILED_FILES

    if not tasks:
        print("error: no files matched", file=out)
        return _EXIT_FAILED_FILES

    def on_progress(event: ProgressEvent) -> None:
        if args.quiet or not event.done:
            return
        print(
            f"({event.file_index + 1}/{event.file_total}) {event.path.name}: "
            f"read={event.rows_read} ok={event.accepted} rejected={event.rejected}",
            file=out,
        )

    try:
        report = bulk_import(
            tasks,
            progress=on_progress,
            chunk_size=args.chunk_size,
            max_reject_samples=args.max_reject_samples,
            dry_run=args.dry_run,
            require_dedup=args.require_dedup,
        )
    except RuntimeError as exc:  # require_dedup refusal
        print(f"error: {exc}", file=out)
        return _EXIT_FAILED_FILES

    print(report.format_text(), file=out)
    for file_report in report.files:
        for sample in file_report.reject_samples:
            print(f"  {file_report.path.name} {sample}", file=out)
        if file_report.samples_truncated:
            print(
                f"  {file_report.path.name}: further rejected rows omitted "
                f"(counts above are complete)",
                file=out,
            )

    if report.failed_files:
        return _EXIT_FAILED_FILES
    if report.rejected:
        return _EXIT_REJECTED_ROWS
    return _EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
