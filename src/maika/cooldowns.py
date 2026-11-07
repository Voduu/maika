"""Time-based cooldowns stored in the cooldowns table.

See ``docs/design.md`` section 9.7. A row means "this behavior is
blocked for this scope until this time". A NULL ``expires_at`` never
expires. A cooldown whose ``expires_at`` equals ``now`` has expired.

``try_start_cooldown`` checks and starts a cooldown in one statement,
so two simultaneous triggers cannot both get the slot.

Nothing here reads the clock: the caller passes ``now`` in UTC Unix
seconds. Each function is a single statement and never starts a
transaction, so it can run inside the caller's ``transaction()``.
"""

import enum
import sqlite3

# GLOBAL cooldowns use scope_id 0 (design section 9.7).
GLOBAL_SCOPE_ID = 0

# The statement from design section 9.7. The insert happens when
# there is no row. With a row, the update runs only when the old
# cooldown has an expiry at or before now, so an active cooldown
# changes no row.
TRY_START_SQL = (
    "INSERT INTO cooldowns (scope, scope_id, key, expires_at) "
    "VALUES (:scope, :scope_id, :key, :new_expiry) "
    "ON CONFLICT (scope, scope_id, key) DO UPDATE "
    "SET expires_at = excluded.expires_at "
    "WHERE cooldowns.expires_at IS NOT NULL "
    "AND cooldowns.expires_at <= :now"
)

IS_ACTIVE_SQL = (
    "SELECT 1 FROM cooldowns "
    "WHERE scope = :scope AND scope_id = :scope_id AND key = :key "
    "AND (expires_at IS NULL OR expires_at > :now)"
)

DELETE_EXPIRED_SQL = (
    "DELETE FROM cooldowns WHERE expires_at IS NOT NULL AND expires_at <= :now"
)


class CooldownScope(enum.Enum):
    """What a cooldown applies to. The value is the stored text."""

    USER = "user"
    CHANNEL = "channel"
    SERVER = "server"
    GLOBAL = "global"


def _check_target(scope: CooldownScope, scope_id: int, key: str) -> None:
    """Raise ValueError when the scope, scope ID or key is invalid.

    GLOBAL requires scope_id 0, and every other scope a positive ID.
    The key must not be empty.
    """
    if not isinstance(scope, CooldownScope):
        raise ValueError(f"scope must be a CooldownScope, not {scope!r}.")
    if not isinstance(scope_id, int) or isinstance(scope_id, bool):
        raise ValueError(f"scope_id must be a whole number, not {scope_id!r}.")
    if scope is CooldownScope.GLOBAL:
        if scope_id != GLOBAL_SCOPE_ID:
            raise ValueError(
                f"A global cooldown must use scope_id "
                f"{GLOBAL_SCOPE_ID}, not {scope_id}."
            )
    elif scope_id <= 0:
        raise ValueError(
            f"A {scope.value} cooldown needs a positive scope_id, not "
            f"{scope_id}."
        )
    if not isinstance(key, str) or key.strip() == "":
        raise ValueError("A cooldown key must be non-empty text.")


def _check_duration(duration_seconds: int | None) -> None:
    """Raise ValueError unless the duration is None or above 0."""
    if duration_seconds is None:
        return
    if (
        not isinstance(duration_seconds, int)
        or isinstance(duration_seconds, bool)
        or duration_seconds <= 0
    ):
        raise ValueError(
            "duration_seconds must be a whole number above 0, or None "
            f"for a cooldown that never expires, not {duration_seconds!r}."
        )


def try_start_cooldown(
    connection: sqlite3.Connection,
    scope: CooldownScope,
    scope_id: int,
    key: str,
    duration_seconds: int | None,
    now: int,
) -> bool:
    """Start a cooldown unless one is active. Return True on success.

    The new expiry is ``now + duration_seconds``, or None (never
    expires) when ``duration_seconds`` is None. True means one row was
    inserted or updated and the caller got the slot. False means the
    cooldown was still active and nothing changed.
    """
    _check_target(scope, scope_id, key)
    _check_duration(duration_seconds)

    new_expiry = None
    if duration_seconds is not None:
        new_expiry = now + duration_seconds

    cursor = connection.execute(
        TRY_START_SQL,
        {
            "scope": scope.value,
            "scope_id": scope_id,
            "key": key,
            "new_expiry": new_expiry,
            "now": now,
        },
    )
    return cursor.rowcount == 1


def is_on_cooldown(
    connection: sqlite3.Connection,
    scope: CooldownScope,
    scope_id: int,
    key: str,
    now: int,
) -> bool:
    """Return True when the cooldown is active at ``now``.

    Active means a row exists and its ``expires_at`` is NULL or
    later than ``now``. This matches ``try_start_cooldown``, which
    treats ``expires_at == now`` as expired.
    """
    _check_target(scope, scope_id, key)
    row = connection.execute(
        IS_ACTIVE_SQL,
        {
            "scope": scope.value,
            "scope_id": scope_id,
            "key": key,
            "now": now,
        },
    ).fetchone()
    return row is not None


def delete_expired_cooldowns(connection: sqlite3.Connection, now: int) -> int:
    """Delete cooldowns that expired at or before ``now``.

    Rows with a NULL ``expires_at`` never expire and are kept. Return
    the number of rows deleted.
    """
    cursor = connection.execute(DELETE_EXPIRED_SQL, {"now": now})
    return cursor.rowcount
