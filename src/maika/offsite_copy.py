"""The weekly offsite copy of the newest daily backup.

The rules are in ``docs/design.md`` section 5, "Database backups", and
the settings in section 10.15. Once a minute the task reads the
settings. When ``backups.offsite_enabled`` is true and a copy is due,
the newest daily backup is uploaded with ``rclone copyto`` to the
remote in ``backups.offsite_remote`` (Backblaze B2 to start), and the
remote copies beyond ``backups.offsite_copies_kept`` are deleted
permanently, never into a trash or a B2 version history.

The bot knows only the rclone remote path. rclone finds its config
file (which holds the storage keys) through the ``RCLONE_CONFIG``
environment variable, read by rclone itself. Nothing from that file is
read or logged here.

The date of the last full success is kept in ``bot_state``, so a copy
that failed on its scheduled day is tried again on later days.
"""

import asyncio
import datetime
import logging
import os
import sqlite3
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from maika.bot_state import get_state, set_state
from maika.daily_backup import (
    BACKUP_TIME_KEY,
    CHECK_INTERVAL_SECONDS,
    RETRY_AFTER_FAILURE_SECONDS,
    SETTINGS,
    backup_name_date,
    daily_backup_dir,
    daily_backup_files,
    daily_backup_name,
)
from maika.foundation_settings import (
    OFFSITE_DAY_KEY,
    OFFSITE_ENABLED_KEY,
    OFFSITE_REMOTE_KEY,
    WEEKDAY_CHOICES,
)
from maika.log_router import LogLevel, LogRouter, LogStream
from maika.settings_store import get_value

# A child of the "maika" logger, so an error logged here reaches the
# console and, through RouterLogHandler, #errors.
logger = logging.getLogger(__name__)

OFFSITE_COPIES_KEPT_KEY = "backups.offsite_copies_kept"

# bot_state key holding the local date (ISO text) of the last copy
# whose upload and retention step both succeeded.
LAST_SUCCESS_STATE_KEY = "backups.offsite_last_success"

RCLONE_PROGRAM = "rclone"

# The most time one rclone command may take before it is killed.
RCLONE_TIMEOUT_SECONDS = 300

# The most characters of rclone's stderr put in an #errors line.
STDERR_LIMIT = 500

# Flags that make rclone deletefile remove a file for good, whatever
# the remote's config says. Each one applies only to its own backend.
# Google Drive sends a deleted file to the trash by default
# (https://rclone.org/drive/). B2 only hides it and keeps its old
# versions unless hard delete is on (https://rclone.org/b2/).
PERMANENT_DELETE_FLAGS = ("--drive-use-trash=false", "--b2-hard-delete")

# The only environment variables rclone is given. Everything else,
# DISCORD_TOKEN included, stays in the bot. The proxy names are kept
# in both cases, because programs differ in which form they read.
RCLONE_ENV_NAMES = frozenset(
    {
        "PATH",
        "HOME",
        "TZ",
        "LANG",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
    }
)
# Names starting with these are kept too (RCLONE_CONFIG, LC_ALL, ...).
RCLONE_ENV_PREFIXES = ("LC_", "RCLONE_")

# An rclone remote path ends with one of these when it already names
# the place a file name can follow directly ("gdrive:" or "gdrive:x/").
REMOTE_SEPARATORS = (":", "/")

NOT_INSTALLED_MESSAGE = "rclone is not installed (no rclone program on PATH)."
TIMEOUT_MESSAGE = "rclone {command} did not finish within {seconds} seconds."
EXIT_MESSAGE = "rclone {command} exited with code {code}."
NO_STDERR_TEXT = "(no error output)"

SUCCESS_LINE = (
    "Offsite copy saved: {name} to {remote}. {kept} weekly {noun} kept."
)
FAILURE_LINE = "Offsite copy of {name} to {remote} failed. {problem}"


@dataclass(frozen=True)
class RcloneResult:
    """What one finished rclone command returned."""

    returncode: int
    stdout: str
    stderr: str


type RcloneRunner = Callable[[Sequence[str], float], Awaitable[RcloneResult]]


class RcloneError(Exception):
    """Raised when an rclone command fails, with a plain message."""


@dataclass(frozen=True)
class OffsiteCopyResult:
    """The outcome of one offsite copy.

    ``ok`` is True when the upload and the retention step both
    succeeded. ``kept`` is then the number of remote copies left.
    When ``ok`` is False, ``problem`` says what went wrong.
    """

    ok: bool
    file_name: str
    remote: str
    kept: int = 0
    problem: str = ""


def scheduled_date(today: datetime.date, offsite_day: str) -> datetime.date:
    """Return the latest date on or before ``today`` on ``offsite_day``.

    ``offsite_day`` is a lowercase weekday name, such as "sunday".
    """
    target_weekday = WEEKDAY_CHOICES.index(offsite_day)
    days_back = (today.weekday() - target_weekday) % len(WEEKDAY_CHOICES)
    return today - datetime.timedelta(days=days_back)


def is_offsite_copy_due(
    now: datetime.datetime,
    backup_time: datetime.time,
    offsite_day: str,
    last_success: datetime.date | None,
    todays_backup_exists: bool,
) -> bool:
    """Return True when the weekly offsite copy should run now.

    ``now`` is the current local time. A copy is due when a daily
    backup dated today (local date) exists, the time of day is at or
    after ``backup_time``, and the last success is None or earlier
    than the scheduled date (the latest ``offsite_day`` on or before
    today). So the copy waits for that morning's daily backup, a copy
    that failed on its day is tried again on later days, and the
    first copy after the feature is turned on runs at once.
    """
    if not todays_backup_exists:
        return False
    if now.time() < backup_time:
        return False
    if last_success is None:
        return True
    return last_success < scheduled_date(now.date(), offsite_day)


def read_last_success(connection: sqlite3.Connection) -> datetime.date | None:
    """Return the date of the last full success, or None.

    A stored value that is not an ISO date counts as None, so the next
    copy runs and writes a good value.
    """
    text = get_state(connection, LAST_SUCCESS_STATE_KEY)
    if text is None:
        return None
    try:
        return datetime.date.fromisoformat(text)
    except ValueError:
        return None


def newest_daily_backup(backup_dir: Path) -> Path | None:
    """Return the daily backup with the latest date in its name."""
    files = daily_backup_files(backup_dir)
    if not files:
        return None
    return max(files, key=lambda path: backup_name_date(path.name))


def remote_file_path(remote: str, file_name: str) -> str:
    """Return the rclone path of a file inside the remote folder."""
    if remote.endswith(REMOTE_SEPARATORS):
        return f"{remote}{file_name}"
    return f"{remote}/{file_name}"


def cut_stderr(text: str) -> str:
    """Return rclone's stderr trimmed and cut to ``STDERR_LIMIT``."""
    stripped = text.strip()
    if not stripped:
        return NO_STDERR_TEXT
    return stripped[:STDERR_LIMIT]


def rclone_environment(environ: Mapping[str, str]) -> dict[str, str]:
    """Return the variables of ``environ`` that rclone may see.

    Only the names in ``RCLONE_ENV_NAMES`` and those starting with
    one of ``RCLONE_ENV_PREFIXES`` are kept.
    """
    return {
        name: value
        for name, value in environ.items()
        if name in RCLONE_ENV_NAMES or name.startswith(RCLONE_ENV_PREFIXES)
    }


async def run_rclone(
    arguments: Sequence[str], timeout_seconds: float
) -> RcloneResult:
    """Run rclone with ``arguments``; return its exit code and output.

    The program is started directly, never through a shell, with the
    limited environment from ``rclone_environment``. Standard input
    is empty, so rclone cannot wait for a typed answer. A
    missing rclone program and a command that runs past
    ``timeout_seconds`` raise RcloneError; the timed-out process is
    killed. The process is also killed if this task is cancelled.
    """
    command = arguments[0] if arguments else ""
    try:
        process = await asyncio.create_subprocess_exec(
            RCLONE_PROGRAM,
            *arguments,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=rclone_environment(os.environ),
        )
    except FileNotFoundError:
        raise RcloneError(NOT_INSTALLED_MESSAGE) from None

    try:
        async with asyncio.timeout(timeout_seconds):
            stdout, stderr = await process.communicate()
    except TimeoutError:
        raise RcloneError(
            TIMEOUT_MESSAGE.format(command=command, seconds=timeout_seconds)
        ) from None
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()

    return RcloneResult(
        returncode=process.returncode,
        stdout=stdout.decode("utf-8", errors="replace"),
        stderr=stderr.decode("utf-8", errors="replace"),
    )


async def _run_checked(
    runner: RcloneRunner, arguments: Sequence[str]
) -> RcloneResult:
    """Run one rclone command and raise RcloneError if it failed."""
    result = await runner(arguments, RCLONE_TIMEOUT_SECONDS)
    if result.returncode != 0:
        message = EXIT_MESSAGE.format(
            command=arguments[0], code=result.returncode
        )
        raise RcloneError(f"{message} {cut_stderr(result.stderr)}")
    return result


async def upload_backup(
    runner: RcloneRunner, backup_path: Path, remote: str
) -> None:
    """Upload one daily backup into the remote folder, same name.

    ``rclone copyto`` checks the file's hash after the upload by
    default. A failure raises RcloneError.
    """
    await _run_checked(
        runner,
        [
            "copyto",
            str(backup_path),
            remote_file_path(remote, backup_path.name),
        ],
    )


async def apply_retention(
    runner: RcloneRunner, remote: str, copies_kept: int
) -> int:
    """Delete all but the newest ``copies_kept`` remote copies.

    The remote is listed with ``rclone lsf``. Only names of the form
    ``maika-YYYY-MM-DD.db`` count, ordered by the date in the name,
    and other files are never touched. Each extra copy is deleted
    with ``rclone deletefile`` and ``PERMANENT_DELETE_FLAGS``, oldest
    first. Returns the number of copies left. A failure
    raises RcloneError.
    """
    listing = await _run_checked(runner, ["lsf", remote])
    names = [
        line.strip()
        for line in listing.stdout.splitlines()
        if backup_name_date(line.strip()) is not None
    ]
    newest_first = sorted(names, key=backup_name_date, reverse=True)
    extra = newest_first[copies_kept:]
    for name in reversed(extra):
        await _run_checked(
            runner,
            [
                "deletefile",
                *PERMANENT_DELETE_FLAGS,
                remote_file_path(remote, name),
            ],
        )
    return len(newest_first) - len(extra)


def format_success_line(result: OffsiteCopyResult) -> str:
    """Return the #bot-log line for a saved offsite copy."""
    noun = "copy" if result.kept == 1 else "copies"
    return SUCCESS_LINE.format(
        name=result.file_name,
        remote=result.remote,
        kept=result.kept,
        noun=noun,
    )


def format_failure_line(result: OffsiteCopyResult) -> str:
    """Return the #errors line for a failed offsite copy."""
    return FAILURE_LINE.format(
        name=result.file_name, remote=result.remote, problem=result.problem
    )


async def run_offsite_cycle(
    connection: sqlite3.Connection,
    database_path: Path,
    router: LogRouter,
    now: datetime.datetime,
    runner: RcloneRunner = run_rclone,
) -> OffsiteCopyResult | None:
    """Make the weekly offsite copy, if it is turned on and due.

    ``now`` is the current local time. The settings are read on every
    call. Returns None when the copy is off or not due, otherwise the
    result.

    The upload comes first, then the retention step; nothing is
    deleted after a failed upload. Only when both succeed is today's
    date written to ``bot_state`` and one line posted to #bot-log. A
    failure is logged as an error, which reaches #errors.
    """
    if not get_value(connection, SETTINGS, OFFSITE_ENABLED_KEY):
        return None
    backup_time = get_value(connection, SETTINGS, BACKUP_TIME_KEY)
    offsite_day = get_value(connection, SETTINGS, OFFSITE_DAY_KEY)
    remote = get_value(connection, SETTINGS, OFFSITE_REMOTE_KEY)
    copies_kept = get_value(connection, SETTINGS, OFFSITE_COPIES_KEPT_KEY)

    backup_dir = daily_backup_dir(database_path)
    todays_backup = backup_dir / daily_backup_name(now.date())
    last_success = read_last_success(connection)
    if not is_offsite_copy_due(
        now, backup_time, offsite_day, last_success, todays_backup.exists()
    ):
        return None
    # Today's file exists, so this is it (or a later-dated file, if
    # the clock was moved back).
    newest = newest_daily_backup(backup_dir)

    try:
        await upload_backup(runner, newest, remote)
        kept = await apply_retention(runner, remote, copies_kept)
    except RcloneError as error:
        result = OffsiteCopyResult(
            ok=False, file_name=newest.name, remote=remote, problem=str(error)
        )
        logger.error(format_failure_line(result))
        return result

    set_state(
        connection,
        LAST_SUCCESS_STATE_KEY,
        now.date().isoformat(),
        now=int(now.timestamp()),
    )
    result = OffsiteCopyResult(
        ok=True, file_name=newest.name, remote=remote, kept=kept
    )
    line = format_success_line(result)
    logger.info(line)
    router.post(LogStream.BOT_LOG, LogLevel.INFO, line)
    return result


def cycle_failed(result: OffsiteCopyResult | None) -> bool:
    """Return True when a cycle's result is a failed copy.

    None means the copy was off or not due, which is not a failure.
    """
    return result is not None and not result.ok


type OffsiteCycle = Callable[[], Awaitable[OffsiteCopyResult | None]]
type SleepFunction = Callable[[float], Awaitable[None]]


async def run_offsite_loop(
    cycle: OffsiteCycle,
    *,
    interval_seconds: float = CHECK_INTERVAL_SECONDS,
    retry_seconds: float = RETRY_AFTER_FAILURE_SECONDS,
    sleep: SleepFunction = asyncio.sleep,
) -> None:
    """Wait, run ``cycle``, and repeat until the task is cancelled.

    The waits are the daily backup task's: ``interval_seconds`` after
    a cycle that saved a copy or had nothing to do, and
    ``retry_seconds`` after a failed copy or any exception. The cycle
    logs a failed copy and this logs an exception, so each failed
    cycle posts one error to #errors, and the loop goes on.
    """
    delay = interval_seconds
    while True:
        await sleep(delay)
        # Any exception is caught on purpose: one failed check must
        # not stop the weekly copies.
        try:
            result = await cycle()
        except Exception:
            logger.exception("The offsite copy check failed.")
            delay = retry_seconds
            continue
        if cycle_failed(result):
            delay = retry_seconds
        else:
            delay = interval_seconds
