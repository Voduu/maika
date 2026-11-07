"""Open the SQLite database and make safe backups of it.

The conventions are in ``docs/design.md`` section 9.1, and the backup
rules are in section 5, "Database backups".
"""

import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

MEMORY_DATABASE = ":memory:"

# guild_id 0 means everywhere (design section 9.1).
GLOBAL_GUILD_ID = 0

WAL_MODE = "wal"

# PRAGMA integrity_check returns a single row with this text when
# the database has no problems.
INTEGRITY_OK = "ok"


class BackupError(Exception):
    """Raised when a backup fails its integrity check."""


class JournalModeError(Exception):
    """Raised when a file database cannot be put in WAL mode."""


class NestedTransactionError(Exception):
    """Raised when a transaction starts inside another transaction."""


@contextmanager
def transaction(connection: sqlite3.Connection) -> Generator[None]:
    """Run the ``with`` block in one transaction.

    The transaction starts with ``BEGIN IMMEDIATE``, which takes the
    write lock at once, so reads made inside the block cannot be
    changed by another writer before the block writes. It commits
    when the block finishes. If the block or the commit raises, the
    transaction is rolled back and the exception is raised again.
    That includes KeyboardInterrupt and task cancellation, so a
    transaction is never left open.

    SQLite has no nested transactions, so starting one while the
    connection is already in a transaction raises
    NestedTransactionError and changes nothing.
    """
    if connection.in_transaction:
        raise NestedTransactionError(
            "A transaction is already open on this connection. "
            "Transactions cannot be nested; do all the writes inside "
            "the outer transaction."
        )
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield
        connection.execute("COMMIT")
    except BaseException:
        # SQLite rolls back by itself after some errors, so check
        # before rolling back.
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise


def _set_wal_mode(connection: sqlite3.Connection) -> str:
    """Ask for WAL journal mode and return the mode SQLite reports.

    SQLite answers the pragma with the mode now in use, which is not
    WAL when the file system cannot support it.
    """
    row = connection.execute("PRAGMA journal_mode = WAL").fetchone()
    return str(row[0]).lower()


def _integrity_check(connection: sqlite3.Connection) -> list[str]:
    """Run ``PRAGMA integrity_check`` and return its result lines."""
    rows = connection.execute("PRAGMA integrity_check").fetchall()
    return [str(row[0]) for row in rows]


def open_database(path: Path | str) -> sqlite3.Connection:
    """Open the database at ``path`` and return the connection.

    The connection is set up as the design requires:

    - ``isolation_level=None``: the sqlite3 module never opens a
      transaction by itself, so every transaction is written out
      explicitly with ``BEGIN`` and ``COMMIT``.
    - ``row_factory`` is ``sqlite3.Row``, so columns can be read by
      name.
    - Foreign keys are on.
    - File databases use WAL journal mode. ``":memory:"`` has no
      file, so WAL is skipped there. If SQLite reports any other mode
      after the request, the connection is closed and
      JournalModeError is raised.

    The parent folder of a file database is created if it is missing.

    The connection is used from the event loop thread only. sqlite3
    refuses calls from other threads (``check_same_thread`` is left
    at its default of True).
    """
    is_memory = str(path) == MEMORY_DATABASE
    if not is_memory:
        Path(path).parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(path, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    if not is_memory:
        mode = _set_wal_mode(connection)
        if mode != WAL_MODE:
            connection.close()
            raise JournalModeError(
                f"Database {path} could not be put in WAL mode. "
                f"SQLite reports journal mode {mode!r}."
            )
    return connection


def backup_database(
    source: sqlite3.Connection, destination: Path | str
) -> None:
    """Copy the database behind ``source`` to ``destination``.

    The copy uses SQLite's backup function, never a file copy. A file
    copy of a live WAL database can miss the recent writes that are
    still in the separate ``-wal`` file, or produce an unreadable
    copy. The backup function reads through the connection, so those
    writes are included.

    The parent folder of ``destination`` is created if it is missing.
    After the copy, ``PRAGMA integrity_check`` runs on the new file.
    Any result other than "ok" deletes the new file and raises
    BackupError, so a bad copy is never mistaken for a good one.
    """
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)

    target = sqlite3.connect(destination)
    try:
        source.backup(target)
        results = _integrity_check(target)
    finally:
        target.close()

    if results != [INTEGRITY_OK]:
        destination.unlink(missing_ok=True)
        problems = "; ".join(results)
        raise BackupError(
            f"Integrity check failed for backup {destination}: {problems}"
        )
