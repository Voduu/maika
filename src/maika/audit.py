"""Write entries to the permanent audit log.

See ``docs/design.md`` section 9.5. The bot only adds rows. Each
change and its audit entry are written in one transaction, which the
caller owns: this module never starts or commits one.
"""

import sqlite3

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
