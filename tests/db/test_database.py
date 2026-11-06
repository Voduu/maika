import sqlite3

import pytest

from maika import database
from maika.database import (
    BackupError,
    JournalModeError,
    backup_database,
    open_database,
)


def test_memory_database_opens():
    connection = open_database(":memory:")
    assert connection.execute("SELECT 1").fetchone()[0] == 1


def test_foreign_keys_are_on():
    connection = open_database(":memory:")
    assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_file_database_uses_wal(tmp_path):
    connection = open_database(tmp_path / "maika.db")
    mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode == "wal"


def test_file_database_creates_parent_folder(tmp_path):
    path = tmp_path / "nested" / "folder" / "maika.db"
    connection = open_database(path)
    connection.close()
    assert path.exists()


def test_rows_read_by_column_name():
    connection = open_database(":memory:")
    connection.execute("CREATE TABLE things (id INTEGER, label TEXT)")
    connection.execute("INSERT INTO things VALUES (7, 'cat')")
    row = connection.execute("SELECT id, label FROM things").fetchone()
    assert row["id"] == 7
    assert row["label"] == "cat"


def test_autocommit_without_explicit_transaction():
    connection = open_database(":memory:")
    connection.execute("CREATE TABLE things (id INTEGER)")
    connection.execute("INSERT INTO things VALUES (1)")
    assert not connection.in_transaction


def test_backup_creates_parent_folder(tmp_path):
    connection = open_database(":memory:")
    destination = tmp_path / "backups" / "copy.db"
    backup_database(connection, destination)
    assert destination.exists()


def test_backup_includes_rows_still_in_wal(tmp_path):
    db_path = tmp_path / "maika.db"
    connection = open_database(db_path)
    connection.execute("CREATE TABLE things (id INTEGER)")
    for number in range(5):
        connection.execute("INSERT INTO things VALUES (?)", (number,))

    wal_path = tmp_path / "maika.db-wal"
    assert wal_path.exists()
    assert wal_path.stat().st_size > 0

    destination = tmp_path / "backup.db"
    backup_database(connection, destination)

    copy = sqlite3.connect(destination)
    count = copy.execute("SELECT COUNT(*) FROM things").fetchone()[0]
    copy.close()
    assert count == 5


def test_failed_integrity_check_deletes_backup(tmp_path, monkeypatch):
    monkeypatch.setattr(
        database,
        "_integrity_check",
        lambda connection: ["row 3 missing from index", "page 4 is bad"],
    )
    connection = open_database(":memory:")
    destination = tmp_path / "backups" / "copy.db"

    with pytest.raises(BackupError, match="page 4 is bad"):
        backup_database(connection, destination)

    assert not destination.exists()


def test_journal_mode_other_than_wal_raises(tmp_path, monkeypatch):
    opened = []

    def report_delete_mode(connection):
        opened.append(connection)
        return "delete"

    monkeypatch.setattr(database, "_set_wal_mode", report_delete_mode)
    path = tmp_path / "maika.db"

    with pytest.raises(JournalModeError) as caught:
        open_database(path)

    message = str(caught.value)
    assert str(path) in message
    assert "'delete'" in message
    with pytest.raises(sqlite3.ProgrammingError):
        opened[0].execute("SELECT 1")
