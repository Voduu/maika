"""The daily database backup task.

The rules are in ``docs/design.md`` section 5, "Database backups",
and the settings ``backups.time`` and ``backups.days_kept`` in section
10.15. Once a minute the task checks whether today's backup is due.
When it is, the database is copied with SQLite's backup function to
``backups/daily/maika-YYYY-MM-DD.db`` beside the database, and copies
older than ``backups.days_kept`` days are deleted.

The copy is written to a temporary name in the same folder and
renamed to the final name only after ``PRAGMA integrity_check``
returns "ok", so a file with the final name is always a checked copy.
The copy runs in a worker thread with its own connections, so the
event loop is not blocked.

The backup time is local time on the machine running the bot. A bot
that was down at backup time makes the missing backup on its first
check after it starts.
"""

import asyncio
import datetime
import logging
import re
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from maika.database import BackupError, backup_database
from maika.foundation_settings import build_foundation_registry
from maika.log_router import LogLevel, LogRouter, LogStream
from maika.settings_store import get_value

# A child of the "maika" logger, so an error logged here reaches the
# console and, through RouterLogHandler, #errors.
logger = logging.getLogger(__name__)

SETTINGS = build_foundation_registry()

BACKUP_TIME_KEY = "backups.time"
DAYS_KEPT_KEY = "backups.days_kept"

# Daily backups go here, relative to the database's folder.
DAILY_BACKUP_SUBDIR = Path("backups") / "daily"

DAILY_BACKUP_PREFIX = "maika-"
DAILY_BACKUP_SUFFIX = ".db"
DAILY_BACKUP_DATE_FORMAT = "%Y-%m-%d"

# Only files matching this pattern are ever deleted by the prune. The
# group holds the date written in the name.
DAILY_BACKUP_PATTERN = re.compile(r"^maika-(\d{4}-\d{2}-\d{2})\.db$")

# The copy is written under this name first. The leading dot and the
# extra suffix keep it from matching DAILY_BACKUP_PATTERN.
TEMPORARY_PREFIX = "."
TEMPORARY_SUFFIX = ".tmp"

# How often the task checks whether a backup is due.
CHECK_INTERVAL_SECONDS = 60

# The wait after a failed cycle, so a failure that keeps happening
# posts one error an hour instead of one a minute.
SECONDS_PER_HOUR = 60 * 60
RETRY_AFTER_FAILURE_SECONDS = SECONDS_PER_HOUR

BYTES_PER_KIB = 1024
SIZE_UNITS = ("bytes", "KB", "MB", "GB")

SUCCESS_LINE = "Daily backup saved: {name} ({size}). {kept} {noun} kept."
FAILURE_LINE = "Daily backup {name} failed and was not saved. {problem}"

type SleepFunction = Callable[[float], Awaitable[None]]
type CycleFunction = Callable[[], Awaitable[DailyBackupResult | None]]


@dataclass(frozen=True)
class DailyBackupResult:
    """The outcome of one daily backup.

    ``ok`` is True when the copy passed its integrity check and has
    its final name. ``size_bytes`` is the size of that file. When
    ``ok`` is False, no file was kept and ``problem`` holds the
    check's output.
    """

    ok: bool
    path: Path
    size_bytes: int = 0
    problem: str = ""


def daily_backup_dir(database_path: Path) -> Path:
    """Return the daily backup folder for a database file."""
    return database_path.parent / DAILY_BACKUP_SUBDIR


def daily_backup_name(day: datetime.date) -> str:
    """Return the daily backup file name for a date."""
    date_text = day.strftime(DAILY_BACKUP_DATE_FORMAT)
    return f"{DAILY_BACKUP_PREFIX}{date_text}{DAILY_BACKUP_SUFFIX}"


def is_backup_due(
    now: datetime.datetime,
    backup_time: datetime.time,
    today_file_exists: bool,
) -> bool:
    """Return True when today's daily backup should be made now.

    ``now`` is the current local time. A backup is due when the time
    of day is at or after ``backup_time`` and today's file does not
    exist yet.
    """
    if today_file_exists:
        return False
    return now.time() >= backup_time


def make_daily_backup(
    database_path: Path, backup_dir: Path, day: datetime.date
) -> DailyBackupResult:
    """Back up the database to the daily file for ``day``.

    This blocks, so the task runs it with ``asyncio.to_thread``. Both
    connections are opened here, in the thread that uses them. The
    source is opened without the create flag, so a missing database
    file raises instead of being created empty.

    The copy is made with ``backup_database`` (SQLite's backup
    function and an integrity check) under a temporary name in
    ``backup_dir``, then renamed to the final name. A failed check
    deletes the temporary file and returns a result with the check's
    output. Any other error deletes the temporary file and is raised.
    ``backup_dir`` is created if it is missing.
    """
    final_path = backup_dir / daily_backup_name(day)
    temporary_path = backup_dir / (
        f"{TEMPORARY_PREFIX}{final_path.name}{TEMPORARY_SUFFIX}"
    )
    backup_dir.mkdir(parents=True, exist_ok=True)
    # A leftover from an earlier run that stopped part way.
    temporary_path.unlink(missing_ok=True)

    source_uri = f"{database_path.resolve().as_uri()}?mode=rw"
    source = sqlite3.connect(source_uri, uri=True)
    try:
        backup_database(source, temporary_path)
        temporary_path.replace(final_path)
    except BackupError as error:
        return DailyBackupResult(ok=False, path=final_path, problem=str(error))
    finally:
        source.close()
        # Nothing is left here after a rename. This removes a partial
        # copy when the copy or the rename raised part way.
        temporary_path.unlink(missing_ok=True)
    return DailyBackupResult(
        ok=True, path=final_path, size_bytes=final_path.stat().st_size
    )


def daily_backup_files(backup_dir: Path) -> list[Path]:
    """Return the files in ``backup_dir`` named like daily backups."""
    if not backup_dir.is_dir():
        return []
    return sorted(
        path
        for path in backup_dir.iterdir()
        if path.is_file() and backup_name_date(path.name) is not None
    )


def prune_daily_backups(
    backup_dir: Path,
    today: datetime.date,
    days_kept: int,
    keep: Path,
) -> list[Path]:
    """Delete daily backups dated more than ``days_kept`` days ago.

    The date is read from the file name, never the file's times. Only
    files named ``maika-YYYY-MM-DD.db`` with a real date are touched,
    and ``keep`` (the file just created) is never deleted. Returns the
    deleted paths, oldest first.
    """
    cutoff = today - datetime.timedelta(days=days_kept)
    deleted = []
    for path in daily_backup_files(backup_dir):
        if path == keep:
            continue
        file_date = backup_name_date(path.name)
        if file_date is not None and file_date < cutoff:
            path.unlink()
            deleted.append(path)
    return deleted


def format_size(size_bytes: int) -> str:
    """Return a size such as "512 bytes", "4.0 KB" or "2.3 MB"."""
    if size_bytes < BYTES_PER_KIB:
        return f"{size_bytes} bytes"
    size = size_bytes / BYTES_PER_KIB
    for unit in SIZE_UNITS[1:-1]:
        if size < BYTES_PER_KIB:
            return f"{size:.1f} {unit}"
        size /= BYTES_PER_KIB
    return f"{size:.1f} {SIZE_UNITS[-1]}"


def format_success_line(result: DailyBackupResult, kept: int) -> str:
    """Return the #bot-log line for a saved backup."""
    noun = "backup" if kept == 1 else "backups"
    return SUCCESS_LINE.format(
        name=result.path.name,
        size=format_size(result.size_bytes),
        kept=kept,
        noun=noun,
    )


def format_failure_line(result: DailyBackupResult) -> str:
    """Return the #errors line for a backup that failed its check."""
    return FAILURE_LINE.format(name=result.path.name, problem=result.problem)


async def run_backup_cycle(
    connection: sqlite3.Connection,
    database_path: Path,
    router: LogRouter,
    now: datetime.datetime,
) -> DailyBackupResult | None:
    """Make today's backup and prune old ones, if a backup is due.

    ``now`` is the current local time. The settings are read on every
    call, so a changed time or limit applies at the next check.
    Returns None when no backup was due, otherwise the result.

    A saved backup is followed by the prune and one line to #bot-log
    with the file name, its size and the number of backups kept. A
    failed integrity check is logged as an error, which reaches
    #errors, and nothing is pruned.
    """
    backup_time = get_value(connection, SETTINGS, BACKUP_TIME_KEY)
    days_kept = get_value(connection, SETTINGS, DAYS_KEPT_KEY)
    backup_dir = daily_backup_dir(database_path)
    today = now.date()
    today_file_exists = (backup_dir / daily_backup_name(today)).exists()
    if not is_backup_due(now, backup_time, today_file_exists):
        return None

    result = await asyncio.to_thread(
        make_daily_backup, database_path, backup_dir, today
    )
    if not result.ok:
        logger.error(format_failure_line(result))
        return result

    prune_daily_backups(backup_dir, today, days_kept, keep=result.path)
    kept = len(daily_backup_files(backup_dir))
    line = format_success_line(result, kept)
    logger.info(line)
    router.post(LogStream.BOT_LOG, LogLevel.INFO, line)
    return result


def cycle_failed(result: DailyBackupResult | None) -> bool:
    """Return True when a cycle's result is a failed backup.

    None means nothing was due, which is not a failure.
    """
    return result is not None and not result.ok


async def run_backup_loop(
    cycle: CycleFunction,
    *,
    interval_seconds: float = CHECK_INTERVAL_SECONDS,
    retry_seconds: float = RETRY_AFTER_FAILURE_SECONDS,
    sleep: SleepFunction = asyncio.sleep,
) -> None:
    """Wait, run ``cycle``, and repeat until the task is cancelled.

    The first check comes ``interval_seconds`` after the start, so
    the bot has connected before a backup runs. After a cycle that
    saved a backup or found nothing due, the next wait is
    ``interval_seconds``. After a failed cycle (a failed integrity
    check, or any exception) it is ``retry_seconds``.

    The cycle itself logs a failed check. An exception is logged here.
    Either way each failed cycle posts one error to #errors, and the
    loop goes on.
    """
    delay = interval_seconds
    while True:
        await sleep(delay)
        # Any exception is caught on purpose: one failed check must
        # not stop the daily backups.
        try:
            result = await cycle()
        except Exception:
            logger.exception("The daily backup check failed.")
            delay = retry_seconds
            continue
        if cycle_failed(result):
            delay = retry_seconds
        else:
            delay = interval_seconds


def backup_name_date(name: str) -> datetime.date | None:
    """Return the date in a daily backup name, or None if not one.

    The offsite copy uses this for the remote file names too.
    """
    match = DAILY_BACKUP_PATTERN.match(name)
    if match is None:
        return None
    try:
        return datetime.date.fromisoformat(match.group(1))
    except ValueError:
        return None
