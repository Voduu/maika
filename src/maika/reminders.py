"""Read and write rows of the reminders table.

See ``docs/design.md`` sections 2.2 and 9.10. Creating or cancelling
a reminder is a person's own action, and delivering one is the bot's
routine work, so nothing is written to the audit log.

The delivery updates change only a row that is still pending, so a
reminder cancelled while it was being sent stays cancelled.

Each function is a single statement, except ``cancel_reminder``,
which reads the row after a refused update to say why. No function
starts a transaction, so each can run inside the caller's
``transaction()`` block. Nothing here reads the clock: the caller
passes ``now`` or ``since`` in UTC Unix seconds.
"""

import enum
import sqlite3
from dataclasses import dataclass

DELIVERY_DM = "dm"
DELIVERY_CHANNEL = "channel"

STATUS_PENDING = "pending"
STATUS_DELIVERED = "delivered"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"

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

SELECT_PENDING_SQL = (
    "SELECT * FROM reminders WHERE user_id = ? AND status = ? "
    "ORDER BY due_at, id"
)

SELECT_RECENTLY_FAILED_SQL = (
    "SELECT * FROM reminders "
    "WHERE user_id = ? AND status = ? AND due_at >= ? "
    "ORDER BY due_at, id"
)

CANCEL_SQL = (
    "UPDATE reminders SET status = :cancelled "
    "WHERE id = :id AND user_id = :user_id AND status = :pending "
    "RETURNING message, repeat_rule"
)

SELECT_CANCEL_REFUSAL_SQL = (
    "SELECT message, repeat_rule FROM reminders WHERE id = ? AND user_id = ?"
)

# The reminders_due index on (status, due_at) serves this query. The
# id is the rowid, which the index carries, so the order needs no
# extra sort.
SELECT_DUE_SQL = (
    "SELECT * FROM reminders WHERE status = ? AND due_at <= ? "
    "ORDER BY due_at, id LIMIT ? OFFSET ?"
)

MARK_DELIVERED_SQL = (
    "UPDATE reminders SET status = :delivered, delivered_at = :now, "
    "delivered_via = :via WHERE id = :id AND status = :pending"
)

MARK_FAILED_SQL = (
    "UPDATE reminders SET status = :failed "
    "WHERE id = :id AND status = :pending"
)

RESCHEDULE_SQL = (
    "UPDATE reminders SET due_at = :due_at "
    "WHERE id = :id AND status = :pending"
)

RECORD_DELIVERY_SQL = (
    "UPDATE reminders SET delivered_at = :now, delivered_via = :via "
    "WHERE id = :id AND status = :pending"
)


class CancelOutcome(enum.Enum):
    """What happened when a person tried to cancel a reminder."""

    CANCELLED = "cancelled"
    # No row with that id, or a row owned by someone else.
    NOT_FOUND = "not_found"
    # The person's own row, in a status other than pending.
    NOT_PENDING = "not_pending"


@dataclass(frozen=True)
class CancelResult:
    """The outcome of ``cancel_reminder``.

    ``message`` and ``repeat_rule`` come from the row when it is the
    person's own (CANCELLED or NOT_PENDING). For NOT_FOUND both are
    None, so nothing about another person's reminder leaks out.
    ``repeat_rule`` is also None for a one-time reminder.
    """

    outcome: CancelOutcome
    message: str | None = None
    repeat_rule: str | None = None


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


def list_pending(
    connection: sqlite3.Connection, user_id: int
) -> list[sqlite3.Row]:
    """Return a person's pending reminders, soonest first.

    Rows due at the same time are ordered by id.
    """
    return connection.execute(
        SELECT_PENDING_SQL, (user_id, STATUS_PENDING)
    ).fetchall()


def list_recently_failed(
    connection: sqlite3.Connection, user_id: int, *, since: int
) -> list[sqlite3.Row]:
    """Return a person's failed reminders due at or after ``since``.

    The rows come soonest first, then by id.
    """
    return connection.execute(
        SELECT_RECENTLY_FAILED_SQL, (user_id, STATUS_FAILED, since)
    ).fetchall()


def cancel_reminder(
    connection: sqlite3.Connection, *, reminder_id: int, user_id: int
) -> CancelResult:
    """Cancel a person's own pending reminder.

    The status becomes ``cancelled`` only when the row belongs to
    ``user_id`` and is pending. When nothing was cancelled, the row is
    read to tell a missing or someone else's row (NOT_FOUND) from the
    person's own row in another status (NOT_PENDING). Run it inside
    a ``transaction()`` block so the two statements agree.
    """
    cancelled = connection.execute(
        CANCEL_SQL,
        {
            "cancelled": STATUS_CANCELLED,
            "id": reminder_id,
            "user_id": user_id,
            "pending": STATUS_PENDING,
        },
    ).fetchone()
    if cancelled is not None:
        return CancelResult(
            CancelOutcome.CANCELLED,
            message=cancelled["message"],
            repeat_rule=cancelled["repeat_rule"],
        )

    own_row = connection.execute(
        SELECT_CANCEL_REFUSAL_SQL, (reminder_id, user_id)
    ).fetchone()
    if own_row is None:
        return CancelResult(CancelOutcome.NOT_FOUND)
    return CancelResult(
        CancelOutcome.NOT_PENDING,
        message=own_row["message"],
        repeat_rule=own_row["repeat_rule"],
    )


def list_due(
    connection: sqlite3.Connection, now: int, limit: int, offset: int
) -> list[sqlite3.Row]:
    """Return pending reminders due at or before ``now``.

    The rows come by due time, then by id. ``limit`` and ``offset``
    page through them.
    """
    return connection.execute(
        SELECT_DUE_SQL, (STATUS_PENDING, now, limit, offset)
    ).fetchall()


def mark_delivered(
    connection: sqlite3.Connection, reminder_id: int, *, now: int, via: str
) -> None:
    """Mark a one-time reminder delivered, at ``now`` by ``via``."""
    connection.execute(
        MARK_DELIVERED_SQL,
        {
            "delivered": STATUS_DELIVERED,
            "now": now,
            "via": via,
            "id": reminder_id,
            "pending": STATUS_PENDING,
        },
    )


def mark_failed(connection: sqlite3.Connection, reminder_id: int) -> None:
    """Mark a pending reminder failed."""
    connection.execute(
        MARK_FAILED_SQL,
        {
            "failed": STATUS_FAILED,
            "id": reminder_id,
            "pending": STATUS_PENDING,
        },
    )


def reschedule(
    connection: sqlite3.Connection, reminder_id: int, *, due_at: int
) -> None:
    """Move a pending repeating reminder to its next due time."""
    connection.execute(
        RESCHEDULE_SQL,
        {"due_at": due_at, "id": reminder_id, "pending": STATUS_PENDING},
    )


def record_delivery(
    connection: sqlite3.Connection, reminder_id: int, *, now: int, via: str
) -> None:
    """Record a repeating reminder's latest delivery time and route.

    The status is left as it is.
    """
    connection.execute(
        RECORD_DELIVERY_SQL,
        {"now": now, "via": via, "id": reminder_id, "pending": STATUS_PENDING},
    )
