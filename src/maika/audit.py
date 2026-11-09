"""Write and read entries in the permanent audit log.

See ``docs/design.md`` section 9.5. The bot only adds rows. Each
change and its audit entry are written in one transaction, which the
caller owns: this module never starts or commits one. The reads
write nothing.
"""

import sqlite3
from dataclasses import dataclass

from maika.database import GLOBAL_GUILD_ID

# The kinds listed in design section 9.5. The kind column has no CHECK
# rule (SQLite cannot change one without rebuilding the table), so the
# code checks kinds against this tuple. A new kind is added here.
AUDIT_KINDS = (
    "level",
    "override",
    "block",
    "setting",
    "photo",
    "memory",
    "relationship",
    "nickname",
    "say",
)

# The kinds that record a change to one person's permissions, which
# /perm history lists.
PERMISSION_AUDIT_KINDS = ("level", "override", "block")


class AuditError(RuntimeError):
    """Raised when an audit entry is written outside a transaction."""


INSERT_AUDIT_ENTRY_SQL = (
    "INSERT INTO audit_log ("
    "logged_at, kind, actor_id, target_id, key, guild_id, "
    "old_value, new_value, note"
    ") VALUES ("
    ":logged_at, :kind, :actor_id, :target_id, :key, :guild_id, "
    ":old_value, :new_value, :note"
    ")"
)

# One placeholder per permission kind, for the IN list below.
_PERMISSION_KIND_PLACEHOLDERS = ", ".join("?" for _ in PERMISSION_AUDIT_KINDS)

# Both reads filter on target_id, so they use the audit_log_target
# index (target_id, logged_at). The id breaks ties between entries
# written in the same second, so the newest written comes first.
SELECT_TARGET_ENTRIES_SQL = (
    "SELECT logged_at, kind, actor_id, target_id, key, guild_id, "
    "old_value, new_value, note FROM audit_log "
    f"WHERE target_id = ? AND kind IN ({_PERMISSION_KIND_PLACEHOLDERS}) "
    "ORDER BY logged_at DESC, id DESC LIMIT ?"
)

COUNT_TARGET_ENTRIES_SQL = (
    "SELECT COUNT(*) AS total FROM audit_log "
    f"WHERE target_id = ? AND kind IN ({_PERMISSION_KIND_PLACEHOLDERS})"
)


@dataclass(frozen=True)
class AuditEntry:
    """One audit_log row. None means NULL (for values, "no row")."""

    logged_at: int
    kind: str
    actor_id: int
    target_id: int | None
    key: str | None
    guild_id: int
    old_value: str | None
    new_value: str | None
    note: str | None


def find_target_entries(
    connection: sqlite3.Connection, *, target_id: int, limit: int
) -> tuple[AuditEntry, ...]:
    """Return a person's permission entries, newest first.

    Only the kinds in PERMISSION_AUDIT_KINDS (level, override and
    block) count, and at most ``limit`` entries are returned. Nothing
    is written.
    """
    rows = connection.execute(
        SELECT_TARGET_ENTRIES_SQL,
        (target_id, *PERMISSION_AUDIT_KINDS, limit),
    ).fetchall()
    return tuple(
        AuditEntry(
            logged_at=row["logged_at"],
            kind=row["kind"],
            actor_id=row["actor_id"],
            target_id=row["target_id"],
            key=row["key"],
            guild_id=row["guild_id"],
            old_value=row["old_value"],
            new_value=row["new_value"],
            note=row["note"],
        )
        for row in rows
    )


def count_target_entries(
    connection: sqlite3.Connection, *, target_id: int
) -> int:
    """Return how many permission entries a person has in total.

    The kinds are those of ``find_target_entries``. Nothing is
    written.
    """
    row = connection.execute(
        COUNT_TARGET_ENTRIES_SQL, (target_id, *PERMISSION_AUDIT_KINDS)
    ).fetchone()
    return row["total"]


def write_audit_entry(
    connection: sqlite3.Connection,
    *,
    kind: str,
    actor_id: int,
    now: int,
    target_id: int | None = None,
    key: str | None = None,
    guild_id: int = GLOBAL_GUILD_ID,
    old_value: str | None = None,
    new_value: str | None = None,
    note: str | None = None,
) -> None:
    """Insert one audit_log row, inside the caller's transaction.

    ``now`` is stored as ``logged_at`` (UTC Unix seconds). None for
    ``old_value`` or ``new_value`` means "no row" and is stored as
    NULL, as are the other optional fields left at None.

    Raise ValueError when ``kind`` is not in AUDIT_KINDS, and
    AuditError when the connection is not inside a transaction: a
    change and its audit entry must be written together.
    """
    if kind not in AUDIT_KINDS:
        raise ValueError(
            f"Unknown audit kind {kind!r}. Known kinds: "
            f"{', '.join(AUDIT_KINDS)}."
        )
    if not connection.in_transaction:
        raise AuditError(
            "write_audit_entry was called outside a transaction. The "
            "caller must start one (for example with "
            "database.transaction) so the change and its audit entry "
            "are written together."
        )
    connection.execute(
        INSERT_AUDIT_ENTRY_SQL,
        {
            "logged_at": now,
            "kind": kind,
            "actor_id": actor_id,
            "target_id": target_id,
            "key": key,
            "guild_id": guild_id,
            "old_value": old_value,
            "new_value": new_value,
            "note": note,
        },
    )
