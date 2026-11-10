import asyncio
import datetime
import logging
import sqlite3

import pytest

from maika import daily_backup
from maika.daily_backup import (
    DailyBackupResult,
    daily_backup_dir,
    daily_backup_name,
    format_size,
    is_backup_due,
    make_daily_backup,
    prune_daily_backups,
    run_backup_cycle,
    run_backup_loop,
)
from maika.database import open_database
from maika.log_router import LogLevel, LogStream
from maika.settings_store import set_value

TODAY = datetime.date(2026, 11, 9)
FOUR_AM = datetime.time(4, 0)
NOW_TS = 1_800_000_000


class FakeRouter:
    def __init__(self):
        self.posts = []

    def post(self, stream, level, text):
        self.posts.append((stream, level, text))


@pytest.fixture
def live_db(tmp_path):
    """A WAL file database with rows, one still only in the WAL."""
    path = tmp_path / "maika.db"
    connection = open_database(path)
    # Stop SQLite from moving the WAL into the main file on its own.
    connection.execute("PRAGMA wal_autocheckpoint = 0")
    connection.execute(
        "CREATE TABLE notes (id INTEGER PRIMARY KEY, text TEXT)"
    )
    connection.execute("INSERT INTO notes (text) VALUES ('first')")
    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    connection.execute("INSERT INTO notes (text) VALUES ('in the wal')")
    yield path, connection
    connection.close()


def read_notes(path):
    copy = sqlite3.connect(path)
    try:
        return [row[0] for row in copy.execute("SELECT text FROM notes")]
    finally:
        copy.close()


def at(hour, minute, second=0, day=TODAY):
    return datetime.datetime.combine(day, datetime.time(hour, minute, second))


def touch(folder, name):
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(b"")
    return path


# The backup


def test_backup_copies_rows_including_wal_row(live_db):
    path, _ = live_db
    wal_file = path.with_name(path.name + "-wal")
    assert wal_file.stat().st_size > 0
    backup_dir = daily_backup_dir(path)

    result = make_daily_backup(path, backup_dir, TODAY)

    assert result.ok
    assert result.path == backup_dir / "maika-2026-11-09.db"
    assert result.path.exists()
    assert result.size_bytes == result.path.stat().st_size
    assert read_notes(result.path) == ["first", "in the wal"]
    copy = sqlite3.connect(result.path)
    try:
        assert copy.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
    finally:
        copy.close()


def test_backup_folder_is_backups_daily_beside_database(tmp_path):
    assert daily_backup_dir(tmp_path / "maika.db") == (
        tmp_path / "backups" / "daily"
    )


def test_backup_creates_missing_folder(live_db):
    path, _ = live_db
    backup_dir = path.parent / "missing" / "daily"

    result = make_daily_backup(path, backup_dir, TODAY)

    assert result.ok
    assert backup_dir.is_dir()


def test_backup_leaves_only_final_file(live_db):
    path, _ = live_db
    backup_dir = daily_backup_dir(path)

    make_daily_backup(path, backup_dir, TODAY)

    assert [p.name for p in backup_dir.iterdir()] == ["maika-2026-11-09.db"]


def test_failed_integrity_check_leaves_no_file(live_db, monkeypatch):
    path, _ = live_db
    backup_dir = daily_backup_dir(path)
    monkeypatch.setattr(
        "maika.database._integrity_check",
        lambda connection: [
            "*** in database main ***",
            "Page 5 is never used",
        ],
    )

    result = make_daily_backup(path, backup_dir, TODAY)

    assert not result.ok
    assert result.path == backup_dir / "maika-2026-11-09.db"
    assert "Page 5 is never used" in result.problem
    assert list(backup_dir.iterdir()) == []


def test_missing_database_is_not_created(tmp_path):
    path = tmp_path / "missing.db"

    with pytest.raises(sqlite3.OperationalError):
        make_daily_backup(path, daily_backup_dir(path), TODAY)

    assert not path.exists()
    assert list(daily_backup_dir(path).iterdir()) == []


def test_leftover_temporary_file_is_replaced(live_db):
    path, _ = live_db
    backup_dir = daily_backup_dir(path)
    touch(backup_dir, ".maika-2026-11-09.db.tmp").write_bytes(b"junk")

    result = make_daily_backup(path, backup_dir, TODAY)

    assert result.ok
    assert [p.name for p in backup_dir.iterdir()] == ["maika-2026-11-09.db"]


def test_backup_name():
    assert daily_backup_name(datetime.date(2026, 1, 5)) == (
        "maika-2026-01-05.db"
    )


# The due check


def test_not_due_before_time():
    assert not is_backup_due(at(3, 59, 59), FOUR_AM, today_file_exists=False)


def test_due_after_time_with_no_file():
    assert is_backup_due(at(9, 30), FOUR_AM, today_file_exists=False)


def test_not_due_after_time_with_todays_file():
    assert not is_backup_due(at(9, 30), FOUR_AM, today_file_exists=True)


def test_due_exactly_at_time():
    assert is_backup_due(at(4, 0), FOUR_AM, today_file_exists=False)


# Pruning


def test_prune_deletes_old_and_keeps_new(tmp_path):
    folder = tmp_path / "daily"
    old = touch(folder, "maika-2026-10-09.db")  # 31 days before
    edge = touch(folder, "maika-2026-10-10.db")  # 30 days before
    recent = touch(folder, "maika-2026-11-01.db")
    new = touch(folder, "maika-2026-11-09.db")

    deleted = prune_daily_backups(folder, TODAY, days_kept=30, keep=new)

    assert deleted == [old]
    assert not old.exists()
    assert edge.exists()
    assert recent.exists()
    assert new.exists()


def test_prune_leaves_other_names(tmp_path):
    folder = tmp_path / "daily"
    others = [
        touch(folder, "notes.txt"),
        touch(folder, "maika-2020-01-01.db.tmp"),
        touch(folder, ".maika-2020-01-01.db.tmp"),
        touch(folder, "maika-20200101-000000-before-0002.db"),
        touch(folder, "other-2020-01-01.db"),
        touch(folder, "maika-2020-13-45.db"),
    ]
    new = touch(folder, "maika-2026-11-09.db")

    deleted = prune_daily_backups(folder, TODAY, days_kept=1, keep=new)

    assert deleted == []
    assert all(path.exists() for path in others)


def test_prune_never_deletes_new_file(tmp_path):
    folder = tmp_path / "daily"
    # Dated well past the limit, but it is the file just made.
    new = touch(folder, "maika-2020-01-01.db")

    deleted = prune_daily_backups(folder, TODAY, days_kept=1, keep=new)

    assert deleted == []
    assert new.exists()


# One cycle


def test_missed_backup_is_made_on_first_check_after_time(migrated_db, live_db):
    path, _ = live_db
    router = FakeRouter()

    result = asyncio.run(
        run_backup_cycle(migrated_db, path, router, at(9, 30))
    )

    assert result is not None and result.ok
    assert result.path.exists()
    [(stream, level, text)] = router.posts
    assert stream is LogStream.BOT_LOG
    assert level is LogLevel.INFO
    assert text.startswith("Daily backup saved: maika-2026-11-09.db (")
    assert text.endswith("). 1 backup kept.")


def test_cycle_does_nothing_before_time(migrated_db, live_db):
    path, _ = live_db
    router = FakeRouter()

    result = asyncio.run(run_backup_cycle(migrated_db, path, router, at(3, 0)))

    assert result is None
    assert router.posts == []
    assert not daily_backup_dir(path).exists()


def test_cycle_does_nothing_when_todays_file_exists(migrated_db, live_db):
    path, _ = live_db
    router = FakeRouter()
    existing = touch(daily_backup_dir(path), "maika-2026-11-09.db")

    result = asyncio.run(
        run_backup_cycle(migrated_db, path, router, at(9, 30))
    )

    assert result is None
    assert router.posts == []
    assert existing.read_bytes() == b""


def test_cycle_reads_backup_time_setting(migrated_db, live_db):
    path, _ = live_db
    set_value(
        migrated_db,
        daily_backup.SETTINGS,
        "backups.time",
        "10:00",
        actor_id=1,
        now=NOW_TS,
    )

    result = asyncio.run(
        run_backup_cycle(migrated_db, path, FakeRouter(), at(9, 30))
    )

    assert result is None


def test_cycle_prunes_with_days_kept_setting(migrated_db, live_db):
    path, _ = live_db
    set_value(
        migrated_db,
        daily_backup.SETTINGS,
        "backups.days_kept",
        "2",
        actor_id=1,
        now=NOW_TS,
    )
    folder = daily_backup_dir(path)
    old = touch(folder, "maika-2026-11-06.db")
    kept = touch(folder, "maika-2026-11-07.db")
    router = FakeRouter()

    asyncio.run(run_backup_cycle(migrated_db, path, router, at(9, 30)))

    assert not old.exists()
    assert kept.exists()
    assert router.posts[0][2].endswith("). 2 backups kept.")


def test_cycle_failure_goes_to_errors_and_skips_prune(
    migrated_db, live_db, monkeypatch, caplog
):
    path, _ = live_db
    old = touch(daily_backup_dir(path), "maika-2020-01-01.db")
    monkeypatch.setattr(
        "maika.database._integrity_check",
        lambda connection: ["Page 5 is never used"],
    )
    router = FakeRouter()

    with caplog.at_level(logging.ERROR, logger="maika.daily_backup"):
        result = asyncio.run(
            run_backup_cycle(migrated_db, path, router, at(9, 30))
        )

    assert result is not None and not result.ok
    assert router.posts == []
    assert old.exists()
    [record] = caplog.records
    assert record.levelno == logging.ERROR
    assert record.getMessage().startswith(
        "Daily backup maika-2026-11-09.db failed and was not saved. "
    )
    assert "Page 5 is never used" in record.getMessage()


# The loop


def test_exception_in_one_cycle_does_not_stop_loop(caplog):
    calls = []
    sleeps = []

    async def cycle():
        calls.append(len(calls))
        if len(calls) == 1:
            raise RuntimeError("disk on fire")

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) > 3:
            raise asyncio.CancelledError

    with caplog.at_level(logging.ERROR, logger="maika.daily_backup"):
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(run_backup_loop(cycle, sleep=fake_sleep))

    assert len(calls) == 3
    assert sleeps == [60, 3600, 60, 60]
    [record] = caplog.records
    assert record.getMessage() == "The daily backup check failed."
    assert record.exc_info[0] is RuntimeError


SAVED = DailyBackupResult(ok=True, path=None, size_bytes=1)
FAILED = DailyBackupResult(ok=False, path=None, problem="bad page")


def waits_after(results):
    """Run the loop over cycle results; return every wait asked for."""
    remaining = list(results)
    sleeps = []

    async def cycle():
        return remaining.pop(0)

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        if not remaining:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(run_backup_loop(cycle, sleep=fake_sleep))
    return sleeps


def test_retry_delay_is_one_hour():
    assert daily_backup.RETRY_AFTER_FAILURE_SECONDS == 3600
    assert daily_backup.CHECK_INTERVAL_SECONDS == 60


def test_wait_after_failed_cycle_is_retry_delay():
    assert waits_after([FAILED]) == [60, 3600]


def test_wait_after_saved_cycle_is_one_minute():
    assert waits_after([SAVED]) == [60, 60]


def test_wait_after_nothing_due_is_one_minute():
    assert waits_after([None]) == [60, 60]


def test_wait_returns_to_one_minute_after_recovery():
    assert waits_after([FAILED, FAILED, SAVED, None]) == [
        60,
        3600,
        3600,
        60,
        60,
    ]


def test_failed_cycle_posts_one_error(migrated_db, live_db, monkeypatch):
    path, _ = live_db
    monkeypatch.setattr(
        "maika.database._integrity_check",
        lambda connection: ["Page 5 is never used"],
    )
    records = []
    handler = logging.Handler(level=logging.WARNING)
    handler.emit = records.append
    logger = logging.getLogger("maika")
    logger.addHandler(handler)
    sleeps = []

    async def cycle():
        return await run_backup_cycle(
            migrated_db, path, FakeRouter(), at(9, 30)
        )

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) > 1:
            raise asyncio.CancelledError

    try:
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(run_backup_loop(cycle, sleep=fake_sleep))
    finally:
        logger.removeHandler(handler)

    assert sleeps == [60, 3600]
    [record] = records
    assert "failed and was not saved" in record.getMessage()


def test_exception_in_settings_read_uses_retry_delay(
    migrated_db, live_db, monkeypatch
):
    path, _ = live_db

    def broken_get_value(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr("maika.daily_backup.get_value", broken_get_value)
    sleeps = []

    async def cycle():
        return await run_backup_cycle(
            migrated_db, path, FakeRouter(), at(9, 30)
        )

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) > 1:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(run_backup_loop(cycle, sleep=fake_sleep))

    assert sleeps == [60, 3600]


def test_loop_waits_before_first_check():
    events = []

    async def cycle():
        events.append("cycle")

    async def fake_sleep(seconds):
        events.append("sleep")
        if len(events) > 2:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(run_backup_loop(cycle, sleep=fake_sleep))

    assert events == ["sleep", "cycle", "sleep"]


# Formatting


@pytest.mark.parametrize(
    ("size", "text"),
    [
        (0, "0 bytes"),
        (1023, "1023 bytes"),
        (1024, "1.0 KB"),
        (1536, "1.5 KB"),
        (5 * 1024 * 1024, "5.0 MB"),
        (3 * 1024**3, "3.0 GB"),
        (2048 * 1024**3, "2048.0 GB"),
    ],
)
def test_format_size(size, text):
    assert format_size(size) == text
