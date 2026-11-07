"""Read and write the small values the bot keeps about itself.

See ``docs/design.md`` section 9.8. Values are stored and returned as
text; callers convert them. Changes are not audited: these are values
the bot writes for itself (a heartbeat every minute, for example), not
owner changes, so audit entries would only add noise.

Each function is a single statement and never starts a transaction,
so it can run inside the caller's ``transaction()`` block.
"""

import sqlite3
from dataclasses import dataclass

SELECT_ENTRY_SQL = "SELECT key, value, updated_at FROM bot_state WHERE key = ?"

UPSERT_SQL = (
    "INSERT INTO bot_state (key, value, updated_at) "
    "VALUES (:key, :value, :updated_at) "
    "ON CONFLICT (key) DO UPDATE SET "
    "value = excluded.value, "
    "updated_at = excluded.updated_at"
)

DELETE_SQL = "DELETE FROM bot_state WHERE key = ?"


@dataclass(frozen=True)
class StateEntry:
    """One bot_state row. ``updated_at`` is UTC Unix seconds."""

    key: str
    value: str
    updated_at: int


def _check_key(key: object) -> None:
    """Raise when the key is not non-empty text."""
    if not isinstance(key, str):
        raise TypeError(
            f"A bot_state key must be text, not {type(key).__name__}."
        )
    if key.strip() == "":
        raise ValueError("A bot_state key must not be empty.")


def get_entry(connection: sqlite3.Connection, key: str) -> StateEntry | None:
    """Return the row for a key, or None when there is none."""
    row = connection.execute(SELECT_ENTRY_SQL, (key,)).fetchone()
    if row is None:
        return None
    return StateEntry(
        key=row["key"], value=row["value"], updated_at=row["updated_at"]
    )


def get_state(connection: sqlite3.Connection, key: str) -> str | None:
    """Return the stored text for a key, or None when there is none."""
    entry = get_entry(connection, key)
    if entry is None:
        return None
    return entry.value


def set_state(
    connection: sqlite3.Connection, key: str, value: str, now: int
) -> None:
    """Store a value for a key, replacing any earlier value.

    One upsert statement writes the row with ``updated_at = now``.
    Raise TypeError when the key or value is not text, and
    ValueError when the key is empty.
    """
    _check_key(key)
    if not isinstance(value, str):
        raise TypeError(
            f"The bot_state value for {key} must be text, not "
            f"{type(value).__name__}. Convert it before storing."
        )
    connection.execute(
        UPSERT_SQL, {"key": key, "value": value, "updated_at": now}
    )


def delete_state(connection: sqlite3.Connection, key: str) -> bool:
    """Delete the row for a key. Return True when a row was removed."""
    cursor = connection.execute(DELETE_SQL, (key,))
    return cursor.rowcount == 1
