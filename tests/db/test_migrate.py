import re
import sqlite3

import pytest

from maika import database
from maika.database import BackupError, open_database
from maika.migrate import (
    MIGRATION_BACKUPS_KEPT,
    MigrationError,
    apply_migrations,
    find_migrations,
)

# 2027-01-15 08:00:00 UTC
NOW = 1_800_000_000
LATER = NOW + 60


def write(directory, filename, sql=""):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / filename
    path.write_text(sql, encoding="utf-8")
    return path


def table_names(connection):
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ).fetchall()
    return {row[0] for row in rows}


def recorded(connection):
    rows = connection.execute(
        "SELECT version, name, applied_at FROM schema_migrations "
        "ORDER BY version"
    ).fetchall()
    return [tuple(row) for row in rows]


@pytest.fixture
def migrations_dir(tmp_path):
    directory = tmp_path / "migrations"
    directory.mkdir()
    return directory


# find_migrations


def test_find_sorts_by_version(migrations_dir):
    write(migrations_dir, "0003_third.sql")
    write(migrations_dir, "0001_first.sql")
    write(migrations_dir, "0002_second.sql")
    found = find_migrations(migrations_dir)
    assert [m.version for m in found] == [1, 2, 3]
    assert [m.name for m in found] == ["first", "second", "third"]
    assert found[0].path == migrations_dir / "0001_first.sql"


def test_find_ignores_other_files(migrations_dir):
    write(migrations_dir, ".gitkeep")
    write(migrations_dir, "README.md")
    write(migrations_dir, "0001_first.sql")
    found = find_migrations(migrations_dir)
    assert [m.path.name for m in found] == ["0001_first.sql"]


def test_find_empty_directory(migrations_dir):
    write(migrations_dir, ".gitkeep")
    assert find_migrations(migrations_dir) == []


def test_find_duplicate_version(migrations_dir):
    write(migrations_dir, "0001_first.sql")
    write(migrations_dir, "0001_other.sql")
    with pytest.raises(MigrationError, match="0001"):
        find_migrations(migrations_dir)


def test_find_gap(migrations_dir):
    write(migrations_dir, "0001_first.sql")
    write(migrations_dir, "0003_third.sql")
    with pytest.raises(MigrationError, match="0003_third.sql"):
        find_migrations(migrations_dir)


def test_find_must_start_at_one(migrations_dir):
    write(migrations_dir, "0002_second.sql")
    with pytest.raises(MigrationError):
        find_migrations(migrations_dir)


@pytest.mark.parametrize(
    "filename",
    [
        "001_three_digits.sql",
        "00001_five_digits.sql",
        "0001-hyphen.sql",
        "0001_.sql",
        "foundation.sql",
        "0001.sql",
    ],
)
def test_find_misnamed_sql_file(migrations_dir, filename):
    write(migrations_dir, "0001_first.sql")
    write(migrations_dir, filename)
    with pytest.raises(MigrationError, match=re.escape(filename)):
        find_migrations(migrations_dir)


# apply_migrations


def test_fresh_database_applies_all_in_order(migrations_dir):
    write(migrations_dir, "0001_create.sql", "CREATE TABLE a (x INTEGER);")
    write(
        migrations_dir,
        "0002_alter.sql",
        "ALTER TABLE a ADD COLUMN y TEXT;",
    )
    connection = open_database(":memory:")

    applied = apply_migrations(connection, migrations_dir, None, NOW)

    assert [m.version for m in applied] == [1, 2]
    assert recorded(connection) == [(1, "create", NOW), (2, "alter", NOW)]
    columns = [
        row["name"]
        for row in connection.execute("PRAGMA table_info(a)").fetchall()
    ]
    assert columns == ["x", "y"]
    assert not connection.in_transaction


def test_second_run_applies_nothing(migrations_dir):
    write(migrations_dir, "0001_create.sql", "CREATE TABLE a (x INTEGER);")
    connection = open_database(":memory:")
    apply_migrations(connection, migrations_dir, None, NOW)

    assert apply_migrations(connection, migrations_dir, None, LATER) == []
    assert recorded(connection) == [(1, "create", NOW)]


def test_new_file_later_applies_only_it(migrations_dir):
    write(migrations_dir, "0001_create.sql", "CREATE TABLE a (x INTEGER);")
    connection = open_database(":memory:")
    apply_migrations(connection, migrations_dir, None, NOW)

    write(migrations_dir, "0002_more.sql", "CREATE TABLE b (x INTEGER);")
    applied = apply_migrations(connection, migrations_dir, None, LATER)

    assert [m.version for m in applied] == [2]
    assert recorded(connection) == [(1, "create", NOW), (2, "more", LATER)]


def test_failing_file_rolls_back_completely(migrations_dir):
    write(migrations_dir, "0001_good.sql", "CREATE TABLE a (x INTEGER);")
    write(
        migrations_dir,
        "0002_bad.sql",
        "CREATE TABLE b (x INTEGER);\nINSERT INTO missing VALUES (1);\n",
    )
    write(migrations_dir, "0003_later.sql", "CREATE TABLE c (x INTEGER);")
    connection = open_database(":memory:")

    with pytest.raises(MigrationError, match="0002") as caught:
        apply_migrations(connection, migrations_dir, None, NOW)

    assert "missing" in str(caught.value)
    assert isinstance(caught.value.__cause__, sqlite3.Error)
    tables = table_names(connection)
    assert "a" in tables
    assert "b" not in tables
    assert "c" not in tables
    assert recorded(connection) == [(1, "good", NOW)]
    assert not connection.in_transaction


def test_file_with_own_commit_is_rejected(migrations_dir):
    write(
        migrations_dir,
        "0001_commit.sql",
        "CREATE TABLE a (x INTEGER);\nCOMMIT;\n",
    )
    connection = open_database(":memory:")

    with pytest.raises(MigrationError, match="0001"):
        apply_migrations(connection, migrations_dir, None, NOW)
    assert recorded(connection) == []


def test_applied_version_without_file(migrations_dir):
    write(migrations_dir, "0001_create.sql", "CREATE TABLE a (x INTEGER);")
    write(migrations_dir, "0002_more.sql", "CREATE TABLE b (x INTEGER);")
    connection = open_database(":memory:")
    apply_migrations(connection, migrations_dir, None, NOW)

    (migrations_dir / "0002_more.sql").unlink()
    with pytest.raises(MigrationError, match="0002"):
        apply_migrations(connection, migrations_dir, None, LATER)


# Pre-migration backups


@pytest.fixture
def file_database(tmp_path, migrations_dir):
    """A file database with 0001 applied and 0002 pending."""
    write(
        migrations_dir,
        "0001_create.sql",
        "CREATE TABLE a (x INTEGER);\nINSERT INTO a VALUES (1);\n",
    )
    connection = open_database(tmp_path / "data" / "maika.db")
    apply_migrations(connection, migrations_dir, None, NOW)
    write(
        migrations_dir,
        "0002_more.sql",
        "CREATE TABLE b (x INTEGER);\nINSERT INTO a VALUES (2);\n",
    )
    return connection


def test_backup_before_pending_migration(
    tmp_path, migrations_dir, file_database
):
    backup_dir = tmp_path / "backups"

    apply_migrations(file_database, migrations_dir, backup_dir, NOW)

    expected = backup_dir / "maika-20270115-080000-before-0002.db"
    assert [p.name for p in backup_dir.iterdir()] == [expected.name]
    copy = sqlite3.connect(expected)
    assert recorded(copy) == [(1, "create", NOW)]
    assert "b" not in table_names(copy)
    assert copy.execute("SELECT x FROM a").fetchall() == [(1,)]
    copy.close()


def test_no_backup_when_nothing_pending(tmp_path, migrations_dir):
    write(migrations_dir, "0001_create.sql", "CREATE TABLE a (x INTEGER);")
    connection = open_database(tmp_path / "maika.db")
    apply_migrations(connection, migrations_dir, None, NOW)
    backup_dir = tmp_path / "backups"

    apply_migrations(connection, migrations_dir, backup_dir, LATER)

    assert not backup_dir.exists()


def test_no_backup_for_new_database(tmp_path, migrations_dir):
    write(migrations_dir, "0001_create.sql", "CREATE TABLE a (x INTEGER);")
    connection = open_database(tmp_path / "maika.db")
    backup_dir = tmp_path / "backups"

    apply_migrations(connection, migrations_dir, backup_dir, NOW)

    assert not backup_dir.exists()


def test_no_backup_without_backup_dir(tmp_path, migrations_dir, file_database):
    applied = apply_migrations(file_database, migrations_dir, None, NOW)

    assert [m.version for m in applied] == [2]
    assert not (tmp_path / "backups").exists()


def test_pruning_keeps_newest_ten(tmp_path, migrations_dir, file_database):
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    old_names = [
        f"maika-20260101-0000{second:02d}-before-0001.db"
        for second in range(12)
    ]
    for name in old_names:
        (backup_dir / name).write_bytes(b"")
    unrelated = ["notes.txt", "maika-manual.db", "maika-daily.db"]
    for name in unrelated:
        (backup_dir / name).write_bytes(b"")

    apply_migrations(file_database, migrations_dir, backup_dir, NOW)

    new_name = "maika-20270115-080000-before-0002.db"
    kept_old = old_names[-(MIGRATION_BACKUPS_KEPT - 1) :]
    remaining = {p.name for p in backup_dir.iterdir()}
    assert remaining == set(kept_old) | {new_name} | set(unrelated)


def test_failed_backup_stops_all_migrations(
    tmp_path, migrations_dir, file_database, monkeypatch
):
    monkeypatch.setattr(
        database, "_integrity_check", lambda connection: ["page 2 is bad"]
    )
    backup_dir = tmp_path / "backups"

    with pytest.raises(BackupError):
        apply_migrations(file_database, migrations_dir, backup_dir, NOW)

    assert list(backup_dir.iterdir()) == []
    assert recorded(file_database) == [(1, "create", NOW)]
    assert "b" not in table_names(file_database)
