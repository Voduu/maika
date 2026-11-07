"""Change permission levels and blocks, with their audit entries.

See ``docs/design.md`` sections 1.2 (blocks), 2.3 (levels), 9.2 (the
users table), 9.4 (the blocks table) and 9.5 (the audit log). Each
function runs in its own transaction, reads the current row inside
it, and writes the row and its audit entry together, so a failure in
either write leaves both tables unchanged. A change that would leave
the row as it is writes nothing.

Nothing here reads the clock: the caller passes ``now`` in UTC Unix
seconds. Checks on the actor (the owner-only ``perm.*`` keys) and on
bot accounts belong to the command layer.
"""

import sqlite3
from dataclasses import dataclass

from maika.audit import write_audit_entry
from maika.database import GLOBAL_GUILD_ID, transaction
from maika.permissions import (
    BLOCK_STRICTNESS,
    DEFAULT_LEVEL,
    Level,
    find_block,
    get_level,
)

AUDIT_KIND_LEVEL = "level"
AUDIT_KIND_BLOCK = "block"

UPSERT_LEVEL_SQL = (
    "INSERT INTO users (discord_id, level, updated_at) "
    "VALUES (:user_id, :level, :now) "
    "ON CONFLICT (discord_id) DO UPDATE "
    "SET level = excluded.level, updated_at = excluded.updated_at"
)

DELETE_LEVEL_SQL = "DELETE FROM users WHERE discord_id = :user_id"

UPSERT_BLOCK_SQL = (
    "INSERT INTO blocks (user_id, guild_id, kind, set_by, set_at) "
    "VALUES (:user_id, :guild_id, :kind, :set_by, :set_at) "
    "ON CONFLICT (user_id, guild_id) DO UPDATE "
    "SET kind = excluded.kind, set_by = excluded.set_by, "
    "set_at = excluded.set_at"
)

DELETE_BLOCK_SQL = (
    "DELETE FROM blocks WHERE user_id = :user_id AND guild_id = :guild_id"
)


class PermissionChangeError(ValueError):
    """Raised when a permission change is refused.

    The message is readable and can be shown to the owner.
    """


@dataclass(frozen=True)
class LevelChange:
    """The result of ``set_level``.

    ``changed`` is False when the person already had the level and
    nothing was written.
    """

    user_id: int
    old_level: Level
    new_level: Level
    changed: bool


@dataclass(frozen=True)
class BlockChange:
    """The result of ``set_block`` or ``remove_block``.

    ``old_kind`` and ``new_kind`` are the block kind text, or None for
    no row. ``changed`` is False when nothing was written.
    """

    user_id: int
    guild_id: int
    old_kind: str | None
    new_kind: str | None
    changed: bool


def _refuse_owner(user_id: int, owner_id: int, what: str) -> None:
    """Raise PermissionChangeError when ``user_id`` is the owner."""
    if user_id == owner_id:
        raise PermissionChangeError(
            f"The owner cannot be given {what}. The owner passes every "
            "check and is never stored."
        )


def _level_text(level: Level) -> str | None:
    """Return a level as audit text, or None for level 0 (no row)."""
    if level == DEFAULT_LEVEL:
        return None
    return str(level.value)


def set_level(
    connection: sqlite3.Connection,
    *,
    user_id: int,
    level: Level,
    owner_id: int,
    actor_id: int,
    now: int,
) -> LevelChange:
    """Set a person's level and write a ``level`` audit entry.

    Level EVERYONE deletes the users row; any other level inserts or
    replaces it with ``updated_at = now``. When the person already
    has the level, nothing is written and ``changed`` is False.

    Raise PermissionChangeError for the owner, who is never stored,
    and ValueError when ``level`` is not a Level.
    """
    _refuse_owner(user_id, owner_id, "a level")
    if not isinstance(level, Level):
        raise ValueError(f"level must be a Level, not {level!r}.")

    with transaction(connection):
        old_level = get_level(connection, user_id)
        if old_level == level:
            return LevelChange(user_id, old_level, level, changed=False)

        if level == DEFAULT_LEVEL:
            connection.execute(DELETE_LEVEL_SQL, {"user_id": user_id})
        else:
            connection.execute(
                UPSERT_LEVEL_SQL,
                {"user_id": user_id, "level": level.value, "now": now},
            )
        write_audit_entry(
            connection,
            kind=AUDIT_KIND_LEVEL,
            actor_id=actor_id,
            now=now,
            target_id=user_id,
            old_value=_level_text(old_level),
            new_value=_level_text(level),
        )
    return LevelChange(user_id, old_level, level, changed=True)


def set_block(
    connection: sqlite3.Connection,
    *,
    user_id: int,
    kind: str,
    guild_id: int = GLOBAL_GUILD_ID,
    owner_id: int,
    actor_id: int,
    now: int,
) -> BlockChange:
    """Block a person in one guild and write a ``block`` audit entry.

    ``guild_id`` 0 sets the global block. An existing row for the
    same person and guild is replaced, with ``set_by = actor_id`` and
    ``set_at = now``. When the row already has ``kind``, nothing is
    written and ``changed`` is False.

    Raise PermissionChangeError for the owner, and ValueError for an
    unknown kind or a negative ``guild_id``.
    """
    _refuse_owner(user_id, owner_id, "a block")
    # BLOCK_STRICTNESS holds every block kind the check knows.
    if kind not in BLOCK_STRICTNESS:
        raise ValueError(
            f"Unknown block kind {kind!r}. Known kinds: "
            f"{', '.join(BLOCK_STRICTNESS)}."
        )
    if guild_id < GLOBAL_GUILD_ID:
        raise ValueError(
            f"guild_id must be {GLOBAL_GUILD_ID} (global) or a server "
            f"ID, not {guild_id}."
        )

    with transaction(connection):
        old_kind = find_block(connection, user_id, guild_id)
        if old_kind == kind:
            return BlockChange(user_id, guild_id, old_kind, kind, False)

        connection.execute(
            UPSERT_BLOCK_SQL,
            {
                "user_id": user_id,
                "guild_id": guild_id,
                "kind": kind,
                "set_by": actor_id,
                "set_at": now,
            },
        )
        write_audit_entry(
            connection,
            kind=AUDIT_KIND_BLOCK,
            actor_id=actor_id,
            now=now,
            target_id=user_id,
            guild_id=guild_id,
            old_value=old_kind,
            new_value=kind,
        )
    return BlockChange(user_id, guild_id, old_kind, kind, changed=True)


def remove_block(
    connection: sqlite3.Connection,
    *,
    user_id: int,
    guild_id: int = GLOBAL_GUILD_ID,
    actor_id: int,
    now: int,
) -> BlockChange:
    """Remove a person's block in one guild, with an audit entry.

    Only the row for ``guild_id`` is removed; a block in another
    guild, or the global one, stays. With no row, nothing is written
    and ``changed`` is False.
    """
    with transaction(connection):
        old_kind = find_block(connection, user_id, guild_id)
        if old_kind is None:
            return BlockChange(user_id, guild_id, None, None, False)

        connection.execute(
            DELETE_BLOCK_SQL, {"user_id": user_id, "guild_id": guild_id}
        )
        write_audit_entry(
            connection,
            kind=AUDIT_KIND_BLOCK,
            actor_id=actor_id,
            now=now,
            target_id=user_id,
            guild_id=guild_id,
            old_value=old_kind,
            new_value=None,
        )
    return BlockChange(user_id, guild_id, old_kind, None, changed=True)
