import asyncio
import datetime
import logging
import os
import shutil
from pathlib import Path

import pytest

from maika import offsite_copy
from maika.bot_state import get_state, set_state
from maika.daily_backup import (
    CHECK_INTERVAL_SECONDS,
    RETRY_AFTER_FAILURE_SECONDS,
    SETTINGS,
    daily_backup_dir,
)
from maika.log_router import LogLevel, LogStream
from maika.offsite_copy import (
    LAST_SUCCESS_STATE_KEY,
    OffsiteCopyResult,
    RcloneError,
    RcloneResult,
    apply_retention,
    is_offsite_copy_due,
    newest_daily_backup,
    remote_file_path,
    run_offsite_cycle,
    run_offsite_loop,
    run_rclone,
    scheduled_date,
    upload_backup,
)
from maika.settings_store import set_value

SUNDAY = datetime.date(2026, 11, 8)
MONDAY = datetime.date(2026, 11, 9)
TUESDAY = datetime.date(2026, 11, 10)
SATURDAY = datetime.date(2026, 11, 14)
NEXT_SUNDAY = datetime.date(2026, 11, 15)
FOUR_AM = datetime.time(4, 0)
REMOTE = "gdrive:maika-backups"
NOW_TS = 1_800_000_000
BACKUP_PATH = Path("backups") / "daily" / "maika-2026-11-09.db"


def at(day, hour, minute=0):
    return datetime.datetime.combine(day, datetime.time(hour, minute))


class FakeRouter:
    def __init__(self):
        self.posts = []

    def post(self, stream, level, text):
        self.posts.append((stream, level, text))


class FakeRunner:
    """Answers each rclone command by its first argument."""

    def __init__(self, listing="", fail=None):
        self.calls = []
        self.listing = listing
        self.fail = fail or {}

    async def __call__(self, arguments, timeout_seconds):
        self.calls.append((list(arguments), timeout_seconds))
        command = arguments[0]
        if command in self.fail:
            return RcloneResult(
                returncode=1, stdout="", stderr=self.fail[command]
            )
        if command == "lsf":
            return RcloneResult(returncode=0, stdout=self.listing, stderr="")
        return RcloneResult(returncode=0, stdout="", stderr="")

    def commands(self):
        return [arguments for arguments, _ in self.calls]


def touch(folder, name):
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(b"")
    return path


def enable(connection, **values):
    settings = {"backups.offsite_enabled": "true", **values}
    for key, text in settings.items():
        set_value(connection, SETTINGS, key, text, actor_id=1, now=NOW_TS)


@pytest.fixture
def database_path(tmp_path):
    """A database path with two daily backups beside it."""
    path = tmp_path / "maika.db"
    folder = daily_backup_dir(path)
    touch(folder, "maika-2026-11-08.db")
    touch(folder, "maika-2026-11-09.db")
    return path


# Scheduled date


def test_scheduled_date_is_today_on_the_day():
    assert scheduled_date(SUNDAY, "sunday") == SUNDAY


def test_scheduled_date_goes_back_to_last_weekday():
    assert scheduled_date(TUESDAY, "sunday") == SUNDAY
    assert scheduled_date(SATURDAY, "sunday") == SUNDAY
    assert scheduled_date(MONDAY, "tuesday") == datetime.date(2026, 11, 3)


# The due check


def test_not_due_before_backup_time():
    assert not is_offsite_copy_due(
        at(SUNDAY, 3, 59), FOUR_AM, "sunday", None, todays_backup_exists=True
    )


def test_due_exactly_at_backup_time():
    assert is_offsite_copy_due(
        at(SUNDAY, 4), FOUR_AM, "sunday", None, todays_backup_exists=True
    )


def test_not_due_without_todays_daily_backup():
    # Yesterday's backup alone does not count: the input is False.
    assert not is_offsite_copy_due(
        at(SUNDAY, 9), FOUR_AM, "sunday", None, todays_backup_exists=False
    )


def test_due_when_never_copied_even_off_day():
    assert is_offsite_copy_due(
        at(TUESDAY, 9), FOUR_AM, "sunday", None, todays_backup_exists=True
    )


def test_not_due_when_copied_on_scheduled_date():
    assert not is_offsite_copy_due(
        at(SUNDAY, 9), FOUR_AM, "sunday", SUNDAY, todays_backup_exists=True
    )


def test_not_due_later_in_week_after_copy():
    assert not is_offsite_copy_due(
        at(SATURDAY, 9), FOUR_AM, "sunday", SUNDAY, todays_backup_exists=True
    )


def test_due_when_last_copy_before_scheduled_date():
    assert is_offsite_copy_due(
        at(NEXT_SUNDAY, 9),
        FOUR_AM,
        "sunday",
        SUNDAY,
        todays_backup_exists=True,
    )


def test_sunday_failure_retried_on_tuesday():
    last_week = datetime.date(2026, 11, 1)
    assert is_offsite_copy_due(
        at(TUESDAY, 9),
        FOUR_AM,
        "sunday",
        last_week,
        todays_backup_exists=True,
    )


def test_other_offsite_day():
    # Copied on Tuesday the 3rd; the next scheduled Tuesday is the 10th.
    tuesday_before = datetime.date(2026, 11, 3)
    assert not is_offsite_copy_due(
        at(MONDAY, 9),
        FOUR_AM,
        "tuesday",
        tuesday_before,
        todays_backup_exists=True,
    )
    assert not is_offsite_copy_due(
        at(TUESDAY, 3),
        FOUR_AM,
        "tuesday",
        tuesday_before,
        todays_backup_exists=True,
    )
    assert is_offsite_copy_due(
        at(TUESDAY, 9),
        FOUR_AM,
        "tuesday",
        tuesday_before,
        todays_backup_exists=True,
    )


# Files and paths


def test_newest_daily_backup_by_date_in_name(tmp_path):
    folder = tmp_path / "daily"
    touch(folder, "maika-2026-11-09.db")
    touch(folder, "maika-2026-11-10.db")
    touch(folder, "maika-2025-12-31.db")
    touch(folder, "maika-2099-01-01.db.tmp")
    touch(folder, "zzz.db")

    assert newest_daily_backup(folder) == folder / "maika-2026-11-10.db"


def test_newest_daily_backup_none(tmp_path):
    assert newest_daily_backup(tmp_path / "missing") is None


def test_remote_file_path():
    assert remote_file_path("gdrive:maika-backups", "a.db") == (
        "gdrive:maika-backups/a.db"
    )
    assert remote_file_path("gdrive:", "a.db") == "gdrive:a.db"
    assert remote_file_path("gdrive:folder/", "a.db") == "gdrive:folder/a.db"


# Upload and retention with a fake runner


def test_upload_command(tmp_path):
    runner = FakeRunner()
    backup = tmp_path / "maika-2026-11-09.db"

    asyncio.run(upload_backup(runner, backup, REMOTE))

    assert runner.calls == [
        (
            [
                "copyto",
                str(backup),
                "gdrive:maika-backups/maika-2026-11-09.db",
            ],
            300,
        )
    ]


def test_retention_deletes_beyond_limit_oldest_first():
    listing = "\n".join(
        [
            "maika-2026-10-11.db",
            "maika-2026-11-08.db",
            "maika-2026-10-18.db",
            "maika-2026-11-01.db",
            "maika-2026-10-25.db",
            "maika-2026-10-04.db",
        ]
    )
    runner = FakeRunner(listing=listing + "\n")

    kept = asyncio.run(apply_retention(runner, REMOTE, copies_kept=4))

    assert kept == 4
    assert runner.commands() == [
        ["lsf", REMOTE],
        [
            "deletefile",
            "--drive-use-trash=false",
            "gdrive:maika-backups/maika-2026-10-04.db",
        ],
        [
            "deletefile",
            "--drive-use-trash=false",
            "gdrive:maika-backups/maika-2026-10-11.db",
        ],
    ]


def test_retention_leaves_other_names():
    listing = "\n".join(
        [
            "maika-2026-11-08.db",
            "maika-2020-01-01.db",
            "notes.txt",
            "maika-2019-01-01.db.tmp",
            "maika-2019-13-45.db",
            "old/",
            "maika-2018-01-01.db/",
        ]
    )
    runner = FakeRunner(listing=listing)

    kept = asyncio.run(apply_retention(runner, REMOTE, copies_kept=1))

    assert kept == 1
    deleted = [args[-1] for args in runner.commands() if args[0] != "lsf"]
    assert deleted == ["gdrive:maika-backups/maika-2020-01-01.db"]


def test_retention_within_limit_deletes_nothing():
    runner = FakeRunner(listing="maika-2026-11-08.db\n")

    kept = asyncio.run(apply_retention(runner, REMOTE, copies_kept=4))

    assert kept == 1
    assert runner.commands() == [["lsf", REMOTE]]


def test_failed_command_raises_with_stderr_cut():
    runner = FakeRunner(fail={"copyto": "x" * 2000})

    with pytest.raises(RcloneError) as raised:
        asyncio.run(upload_backup(runner, BACKUP_PATH, REMOTE))

    message = str(raised.value)
    assert message.startswith("rclone copyto exited with code 1. ")
    assert message.endswith("x" * 500)
    assert "x" * 501 not in message


# One cycle


def test_disabled_does_nothing(migrated_db, database_path):
    runner = FakeRunner()

    result = asyncio.run(
        run_offsite_cycle(
            migrated_db, database_path, FakeRouter(), at(SUNDAY, 9), runner
        )
    )

    assert result is None
    assert runner.calls == []


def test_cycle_uploads_newest_and_records_success(migrated_db, database_path):
    enable(migrated_db)
    runner = FakeRunner(listing="maika-2026-11-09.db\nmaika-2026-11-01.db\n")
    router = FakeRouter()

    result = asyncio.run(
        run_offsite_cycle(
            migrated_db, database_path, router, at(MONDAY, 9), runner
        )
    )

    assert result == OffsiteCopyResult(
        ok=True, file_name="maika-2026-11-09.db", remote=REMOTE, kept=2
    )
    newest = daily_backup_dir(database_path) / "maika-2026-11-09.db"
    assert runner.commands() == [
        ["copyto", str(newest), "gdrive:maika-backups/maika-2026-11-09.db"],
        ["lsf", REMOTE],
    ]
    assert get_state(migrated_db, LAST_SUCCESS_STATE_KEY) == "2026-11-09"
    assert router.posts == [
        (
            LogStream.BOT_LOG,
            LogLevel.INFO,
            "Offsite copy saved: maika-2026-11-09.db to "
            "gdrive:maika-backups. 2 weekly copies kept.",
        )
    ]


def test_cycle_uses_remote_and_copies_kept_settings(
    migrated_db, database_path
):
    enable(
        migrated_db,
        **{
            "backups.offsite_remote": "other:folder",
            "backups.offsite_copies_kept": "1",
        },
    )
    runner = FakeRunner(listing="maika-2026-11-09.db\nmaika-2026-11-01.db\n")
    router = FakeRouter()

    asyncio.run(
        run_offsite_cycle(
            migrated_db, database_path, router, at(MONDAY, 9), runner
        )
    )

    assert runner.commands()[1:] == [
        ["lsf", "other:folder"],
        [
            "deletefile",
            "--drive-use-trash=false",
            "other:folder/maika-2026-11-01.db",
        ],
    ]
    assert router.posts[0][2].endswith("to other:folder. 1 weekly copy kept.")


def test_cycle_not_due_after_success_this_week(migrated_db, database_path):
    enable(migrated_db)
    set_state(migrated_db, LAST_SUCCESS_STATE_KEY, "2026-11-08", now=NOW_TS)
    runner = FakeRunner()

    result = asyncio.run(
        run_offsite_cycle(
            migrated_db, database_path, FakeRouter(), at(MONDAY, 9), runner
        )
    )

    assert result is None
    assert runner.calls == []


def test_cycle_bad_stored_date_counts_as_never(migrated_db, database_path):
    enable(migrated_db)
    set_state(migrated_db, LAST_SUCCESS_STATE_KEY, "not a date", now=NOW_TS)
    runner = FakeRunner()

    result = asyncio.run(
        run_offsite_cycle(
            migrated_db, database_path, FakeRouter(), at(MONDAY, 9), runner
        )
    )

    assert result.ok


def test_failed_upload_deletes_nothing_and_keeps_state(
    migrated_db, database_path, caplog
):
    enable(migrated_db)
    runner = FakeRunner(
        listing="maika-2020-01-01.db\n", fail={"copyto": "Failed to copy"}
    )
    router = FakeRouter()

    with caplog.at_level(logging.ERROR, logger="maika.offsite_copy"):
        result = asyncio.run(
            run_offsite_cycle(
                migrated_db, database_path, router, at(MONDAY, 9), runner
            )
        )

    assert not result.ok
    assert [args[0] for args in runner.commands()] == ["copyto"]
    assert get_state(migrated_db, LAST_SUCCESS_STATE_KEY) is None
    assert router.posts == []
    [record] = caplog.records
    assert record.getMessage() == (
        "Offsite copy of maika-2026-11-09.db to gdrive:maika-backups "
        "failed. rclone copyto exited with code 1. Failed to copy"
    )


def test_failed_retention_keeps_state(migrated_db, database_path, caplog):
    enable(migrated_db)
    runner = FakeRunner(fail={"lsf": "directory not found"})

    with caplog.at_level(logging.ERROR, logger="maika.offsite_copy"):
        result = asyncio.run(
            run_offsite_cycle(
                migrated_db, database_path, FakeRouter(), at(MONDAY, 9), runner
            )
        )

    assert not result.ok
    assert "rclone lsf exited with code 1." in result.problem
    assert get_state(migrated_db, LAST_SUCCESS_STATE_KEY) is None
    assert len(caplog.records) == 1


def test_failed_delete_keeps_state(migrated_db, database_path):
    enable(migrated_db, **{"backups.offsite_copies_kept": "1"})
    runner = FakeRunner(
        listing="maika-2026-11-09.db\nmaika-2026-11-01.db\n",
        fail={"deletefile": "permission denied"},
    )

    result = asyncio.run(
        run_offsite_cycle(
            migrated_db, database_path, FakeRouter(), at(MONDAY, 9), runner
        )
    )

    assert not result.ok
    assert get_state(migrated_db, LAST_SUCCESS_STATE_KEY) is None


def test_no_daily_backup_does_nothing(migrated_db, tmp_path):
    enable(migrated_db)
    runner = FakeRunner()

    result = asyncio.run(
        run_offsite_cycle(
            migrated_db,
            tmp_path / "maika.db",
            FakeRouter(),
            at(MONDAY, 9),
            runner,
        )
    )

    assert result is None
    assert runner.calls == []


def test_yesterdays_backup_only_waits(migrated_db, tmp_path):
    enable(migrated_db)
    path = tmp_path / "maika.db"
    folder = daily_backup_dir(path)
    touch(folder, "maika-2026-11-08.db")
    runner = FakeRunner()

    def cycle():
        return asyncio.run(
            run_offsite_cycle(
                migrated_db, path, FakeRouter(), at(MONDAY, 9), runner
            )
        )

    assert cycle() is None
    assert runner.calls == []

    touch(folder, "maika-2026-11-09.db")
    result = cycle()

    assert result.ok
    assert result.file_name == "maika-2026-11-09.db"
    assert runner.commands()[0][1] == str(folder / "maika-2026-11-09.db")


# The real runner with a fake rclone program


@pytest.fixture
def fake_rclone(tmp_path, monkeypatch):
    """Write an "rclone" shell script and put only it on PATH."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    programs = {"sleep": shutil.which("sleep"), "env": shutil.which("env")}
    monkeypatch.setenv("PATH", str(bin_dir))

    def write(body):
        script = bin_dir / "rclone"
        script.write_text("#!/bin/sh\n" + body.format(**programs) + "\n")
        script.chmod(0o755)

    return write


def test_real_runner_passes_limited_environment(fake_rclone, monkeypatch):
    monkeypatch.setenv("HOME", "/home/maika")
    monkeypatch.setenv("TZ", "America/Los_Angeles")
    monkeypatch.setenv("RCLONE_CONFIG", "/appdata/rclone.conf")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy:3128")
    monkeypatch.setenv("LC_ALL", "C.UTF-8")
    monkeypatch.setenv("DISCORD_TOKEN", "secret-token")
    monkeypatch.setenv("SECRET_X", "hidden")
    monkeypatch.setenv("UNLISTED_VARIABLE", "nope")
    fake_rclone("exec {env}")

    result = asyncio.run(run_rclone(["version"], 5))

    child = dict(
        line.split("=", 1)
        for line in result.stdout.splitlines()
        if "=" in line
    )
    assert "DISCORD_TOKEN" not in child
    assert "SECRET_X" not in child
    assert "UNLISTED_VARIABLE" not in child
    assert "secret-token" not in result.stdout
    assert child["PATH"] == os.environ["PATH"]
    assert child["HOME"] == "/home/maika"
    assert child["TZ"] == "America/Los_Angeles"
    assert child["RCLONE_CONFIG"] == "/appdata/rclone.conf"
    assert child["HTTPS_PROXY"] == "http://proxy:3128"
    assert child["LC_ALL"] == "C.UTF-8"


def test_rclone_environment_keeps_only_listed_names():
    environ = {
        "PATH": "/bin",
        "HOME": "/h",
        "TZ": "UTC",
        "LANG": "C",
        "LC_TIME": "C",
        "RCLONE_CONFIG": "/c",
        "RCLONE_DRIVE_USE_TRASH": "false",
        "SSL_CERT_FILE": "/f",
        "SSL_CERT_DIR": "/d",
        "HTTP_PROXY": "a",
        "HTTPS_PROXY": "b",
        "NO_PROXY": "c",
        "http_proxy": "a",
        "https_proxy": "b",
        "no_proxy": "c",
        "DISCORD_TOKEN": "t",
        "OWNER_ID": "1",
        "XRCLONE_CONFIG": "x",
        "rclone_config": "x",
        "PATHX": "x",
    }

    kept = offsite_copy.rclone_environment(environ)

    assert set(kept) == set(environ) - {
        "DISCORD_TOKEN",
        "OWNER_ID",
        "XRCLONE_CONFIG",
        "rclone_config",
        "PATHX",
    }


def test_real_runner_success(fake_rclone):
    fake_rclone('echo "args: $*"')

    result = asyncio.run(run_rclone(["lsf", "gdrive:x"], 5))

    assert result.returncode == 0
    assert result.stdout == "args: lsf gdrive:x\n"
    assert result.stderr == ""


def test_real_runner_nonzero_exit_with_stderr(fake_rclone):
    fake_rclone('echo "Failed to copy: quota exceeded" >&2\nexit 3')

    result = asyncio.run(run_rclone(["copyto", "a", "b"], 5))

    assert result.returncode == 3
    assert result.stderr == "Failed to copy: quota exceeded\n"

    with pytest.raises(RcloneError) as raised:
        asyncio.run(upload_backup(run_rclone, BACKUP_PATH, REMOTE))
    assert str(raised.value) == (
        "rclone copyto exited with code 3. Failed to copy: quota exceeded"
    )


def test_real_runner_timeout_kills_process(fake_rclone, tmp_path):
    pid_file = tmp_path / "pid"
    fake_rclone(f"echo $$ > {pid_file}\nexec {{sleep}} 30")

    with pytest.raises(RcloneError) as raised:
        asyncio.run(run_rclone(["copyto", "a", "b"], 0.5))

    assert str(raised.value) == (
        "rclone copyto did not finish within 0.5 seconds."
    )
    pid = int(pid_file.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_real_runner_without_rclone(tmp_path, monkeypatch):
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))

    with pytest.raises(RcloneError) as raised:
        asyncio.run(run_rclone(["lsf", "gdrive:x"], 5))

    assert str(raised.value) == (
        "rclone is not installed (no rclone program on PATH)."
    )


# The loop


SAVED = OffsiteCopyResult(ok=True, file_name="a.db", remote=REMOTE, kept=1)
FAILED = OffsiteCopyResult(
    ok=False, file_name="a.db", remote=REMOTE, problem="boom"
)


def waits_after(results):
    """Run the loop over cycle results; return every wait asked for."""
    remaining = list(results)
    sleeps = []

    async def cycle():
        result = remaining.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        if not remaining:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(run_offsite_loop(cycle, sleep=fake_sleep))
    return sleeps


def test_loop_reuses_daily_backup_delays():
    assert CHECK_INTERVAL_SECONDS == 60
    assert RETRY_AFTER_FAILURE_SECONDS == 3600


def test_wait_after_success():
    assert waits_after([SAVED]) == [60, 60]


def test_wait_after_nothing_to_do():
    assert waits_after([None]) == [60, 60]


def test_wait_after_failure():
    assert waits_after([FAILED]) == [60, 3600]


def test_wait_after_exception_and_loop_continues(caplog):
    with caplog.at_level(logging.ERROR, logger="maika.offsite_copy"):
        waits = waits_after([RuntimeError("boom"), None])

    assert waits == [60, 3600, 60]
    [record] = caplog.records
    assert record.getMessage() == "The offsite copy check failed."
    assert record.exc_info[0] is RuntimeError


def test_failed_cycle_posts_one_error(migrated_db, database_path):
    enable(migrated_db)
    runner = FakeRunner(fail={"copyto": "nope"})
    records = []
    handler = logging.Handler(level=logging.WARNING)
    handler.emit = records.append
    logger = logging.getLogger("maika")
    logger.addHandler(handler)
    sleeps = []

    async def cycle():
        return await run_offsite_cycle(
            migrated_db, database_path, FakeRouter(), at(MONDAY, 9), runner
        )

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) > 1:
            raise asyncio.CancelledError

    try:
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(offsite_copy.run_offsite_loop(cycle, sleep=fake_sleep))
    finally:
        logger.removeHandler(handler)

    assert sleeps == [60, 3600]
    assert len(records) == 1
