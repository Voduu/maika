import sqlite3

import pytest

from maika.database import (
    NestedTransactionError,
    open_database,
    transaction,
)

NOW = 1_800_000_000


def insert_state(connection, key):
    connection.execute(
        "INSERT INTO bot_state (key, value, updated_at) VALUES (?, ?, ?)",
        (key, "1", NOW),
    )


def state_keys(connection):
    rows = connection.execute("SELECT key FROM bot_state ORDER BY key")
    return [row[0] for row in rows.fetchall()]


def test_commits_when_block_finishes(migrated_db):
    with transaction(migrated_db):
        assert migrated_db.in_transaction
        insert_state(migrated_db, "first")
        insert_state(migrated_db, "second")

    assert not migrated_db.in_transaction
    assert state_keys(migrated_db) == ["first", "second"]


def test_rolls_back_and_reraises(migrated_db):
    with pytest.raises(ValueError, match="stop"):
        with transaction(migrated_db):
            insert_state(migrated_db, "first")
            raise ValueError("stop")

    assert not migrated_db.in_transaction
    assert state_keys(migrated_db) == []


def test_rolls_back_on_keyboard_interrupt(migrated_db):
    with pytest.raises(KeyboardInterrupt):
        with transaction(migrated_db):
            insert_state(migrated_db, "first")
            raise KeyboardInterrupt

    assert not migrated_db.in_transaction
    assert state_keys(migrated_db) == []


def test_rolls_back_after_sql_error(migrated_db):
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        with transaction(migrated_db):
            insert_state(migrated_db, "first")
            insert_state(migrated_db, "first")

    assert not migrated_db.in_transaction
    assert state_keys(migrated_db) == []


def test_refuses_nesting(migrated_db):
    with transaction(migrated_db):
        insert_state(migrated_db, "outer")
        with pytest.raises(NestedTransactionError):
            with transaction(migrated_db):
                insert_state(migrated_db, "inner")
        # The refusal changed nothing: the outer transaction is open.
        assert migrated_db.in_transaction

    assert state_keys(migrated_db) == ["outer"]


def test_refuses_manual_transaction(migrated_db):
    migrated_db.execute("BEGIN")
    with pytest.raises(NestedTransactionError):
        with transaction(migrated_db):
            pass
    assert migrated_db.in_transaction
    migrated_db.execute("ROLLBACK")


def test_uses_begin_immediate(tmp_path):
    path = tmp_path / "maika.db"
    writer = open_database(path)
    other = open_database(path)
    other.execute("PRAGMA busy_timeout = 0")
    writer.execute("CREATE TABLE t (x INTEGER)")

    # BEGIN IMMEDIATE takes the write lock at once, before any write
    # in the block, so a second writer is refused straight away.
    with transaction(writer):
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            other.execute("BEGIN IMMEDIATE")

    writer.close()
    other.close()
