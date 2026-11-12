"""Read and write rows of the reminders table.

See ``docs/design.md`` sections 2.2 and 9.10. Creating a reminder is
a person's own action, so nothing is written to the audit log.

Each function is a single statement and never starts a transaction,
so it can run inside the caller's ``transaction()`` block. Nothing
here reads the clock: the caller passes ``now`` in UTC Unix seconds.
"""

import sqlite3

DELIVERY_DM = "dm"
DELIVERY_CHANNEL = "channel"

STATUS_PENDING = "pending"

INSERT_SQL = (
    "INSERT INTO reminders "
    "(user_id, message, due_at, delivery, channel_id, created_at, "
    "repeat_rule, timezone, status) "
    "VALUES (:user_id, :message, :due_at, :delivery, :channel_id, "
    ":created_at, :repeat_rule, :timezone, :status)"
)

COUNT_PENDING_SQL = (
    "SELECT COUNT(*) FROM reminders WHERE user_id = ? AND status = ?"
)

SELECT_REMINDER_SQL = "SELECT * FROM reminders WHERE id = ?"


def create_reminder(
    connection: sqlite3.Connection,
    *,
    user_id: int,
    message: str,
    due_at: int,
    delivery: str,
    channel_id: int | None,
    repeat_rule: str | None,
    timezone: str | None,
    now: int,
) -> int:
    """Insert a pending reminder and return its id.

    ``created_at`` is ``now``. ``channel_id`` is the channel where the
    reminder was set, or None in a DM. ``repeat_rule`` and
    ``timezone`` are None for a one-time reminder.
    """
    cursor = connection.execute(
        INSERT_SQL,
        {
            "user_id": user_id,
            "message": message,
            "due_at": due_at,
            "delivery": delivery,
            "channel_id": channel_id,
            "created_at": now,
            "repeat_rule": repeat_rule,
            "timezone": timezone,
            "status": STATUS_PENDING,
        },
    )
    reminder_id = cursor.lastrowid
    if reminder_id is None:
        raise sqlite3.DatabaseError("The new reminder has no id.")
    return reminder_id


def count_pending(connection: sqlite3.Connection, user_id: int) -> int:
    """Return how many pending reminders a person holds."""
    row = connection.execute(
        COUNT_PENDING_SQL, (user_id, STATUS_PENDING)
    ).fetchone()
    return row[0]


def get_reminder(
    connection: sqlite3.Connection, reminder_id: int
) -> sqlite3.Row | None:
    """Return one reminder row, or None when there is no such id."""
    return connection.execute(SELECT_REMINDER_SQL, (reminder_id,)).fetchone()
