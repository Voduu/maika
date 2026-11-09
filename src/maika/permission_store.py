"""Change levels, blocks and overrides, with their audit entries.

See ``docs/design.md`` sections 1.2 (blocks, wildcards and protected
keys), 2.3 (levels), 9.2 (the users table), 9.3 (the
permission_overrides table), 9.4 (the blocks table) and 9.5 (the
audit log). Each
function runs in its own transaction, reads the current row inside
it, and writes the row and its audit entry together, so a failure in
either write leaves both tables unchanged. A change that would leave
the row as it is writes nothing.

Nothing here reads the clock: the caller passes ``now`` in UTC Unix
seconds. Checks on the actor (the owner-only ``perm.*`` keys) and on
bot accounts belong to the command layer.
"""

import enum
import re
import sqlite3
from dataclasses import dataclass

from maika.audit import write_audit_entry
from maika.database import GLOBAL_GUILD_ID, transaction
from maika.permissions import (
    BLOCK_STRICTNESS,
    DEFAULT_LEVEL,
    EFFECT_ALLOW,
    EFFECT_DENY,
    KEY_PATTERN,
    KEY_SEPARATOR,
    OWNER_ONLY_AREAS,
    WILDCARD,
    Level,
    PermissionKey,
    PermissionRegistry,
    UnknownPermissionKeyError,
    find_block,
    find_override,
    get_level,
)

AUDIT_KIND_LEVEL = "level"
AUDIT_KIND_BLOCK = "block"
AUDIT_KIND_OVERRIDE = "override"

# An area wildcard such as ``feed.*``. The group is the area.
AREA_WILDCARD_PATTERN = re.compile(r"^([a-z0-9_]+)\.\*$")

# The /perm subcommand named in the note of a cleared row's audit
# entry. The keys are also the effects set_override accepts.
COMMAND_FOR_EFFECT = {EFFECT_ALLOW: "grant", EFFECT_DENY: "deny"}
RESET_COMMAND = "reset"
CLEARED_NOTE = "cleared by /perm {command} {pattern}"

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

SELECT_USER_BLOCKS_SQL = (
    "SELECT guild_id, kind FROM blocks WHERE user_id = :user_id "
    "ORDER BY guild_id"
)

SELECT_USER_OVERRIDES_SQL = (
    "SELECT key, effect FROM permission_overrides "
    "WHERE user_id = :user_id AND guild_id = :guild_id"
)

SELECT_OTHER_SCOPE_OVERRIDES_SQL = (
    "SELECT key, effect, guild_id FROM permission_overrides "
    "WHERE user_id = :user_id AND guild_id != :guild_id "
    "ORDER BY guild_id, key"
)

SELECT_KEY_OVERRIDES_SQL = (
    "SELECT guild_id, effect FROM permission_overrides "
    "WHERE user_id = :user_id AND key = :key "
    "ORDER BY guild_id"
)

UPSERT_OVERRIDE_SQL = (
    "INSERT INTO permission_overrides "
    "(user_id, key, effect, guild_id, set_by, set_at) "
    "VALUES (:user_id, :key, :effect, :guild_id, :set_by, :set_at) "
    "ON CONFLICT (user_id, key, guild_id) DO UPDATE "
    "SET effect = excluded.effect, set_by = excluded.set_by, "
    "set_at = excluded.set_at"
)

DELETE_OVERRIDE_SQL = (
    "DELETE FROM permission_overrides "
    "WHERE user_id = :user_id AND key = :key AND guild_id = :guild_id"
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


def _check_guild_id(guild_id: int) -> None:
    """Raise ValueError when ``guild_id`` is negative."""
    if guild_id < GLOBAL_GUILD_ID:
        raise ValueError(
            f"guild_id must be {GLOBAL_GUILD_ID} (global) or a server "
            f"ID, not {guild_id}."
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
    _check_guild_id(guild_id)

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


@dataclass(frozen=True)
class ScopedBlock:
    """One stored block row, in one scope.

    ``guild_id`` is 0 for the global row or a server ID.
    """

    guild_id: int
    kind: str


def find_user_blocks(
    connection: sqlite3.Connection, *, user_id: int
) -> tuple[ScopedBlock, ...]:
    """Return a person's block rows in every scope.

    The global row (``guild_id`` 0) comes first, then servers by ID.
    Nothing is written.
    """
    rows = connection.execute(
        SELECT_USER_BLOCKS_SQL, {"user_id": user_id}
    ).fetchall()
    return tuple(ScopedBlock(row["guild_id"], row["kind"]) for row in rows)


class _PatternShape(enum.Enum):
    """The three forms an override pattern can take."""

    EXACT = "exact"
    AREA = "area"
    ALL = "all"


@dataclass(frozen=True)
class ClearedOverride:
    """One override row that a wildcard command removes."""

    key: str
    effect: str


@dataclass(frozen=True)
class OverrideChange:
    """The result of ``set_override`` or ``reset_override``.

    ``effect`` is the pattern row's new effect, or None for a reset.
    ``old_effect`` is its effect before, or None for no row.
    ``cleared`` lists the rows beneath a wildcard that were removed,
    sorted by key. ``changed`` is True when anything was written or
    deleted.
    """

    pattern: str
    effect: str | None
    old_effect: str | None
    changed: bool
    cleared: tuple[ClearedOverride, ...]


def _pattern_area(pattern: str) -> str:
    """Return the part of a key or area wildcard before the dot."""
    return pattern.split(KEY_SEPARATOR, maxsplit=1)[0]


def _pattern_shape(pattern: str) -> _PatternShape:
    """Return the form of an override pattern.

    Raise PermissionChangeError when the pattern is not an exact
    key, an ``area.*`` wildcard or ``*``.
    """
    if pattern == WILDCARD:
        return _PatternShape.ALL
    if isinstance(pattern, str):
        if AREA_WILDCARD_PATTERN.match(pattern):
            return _PatternShape.AREA
        if KEY_PATTERN.match(pattern):
            return _PatternShape.EXACT
    raise PermissionChangeError(
        f"{pattern!r} is not a permission key (area.action), an area "
        f"wildcard (area{KEY_SEPARATOR}{WILDCARD}) or {WILDCARD}."
    )


def _check_pattern(
    registry: PermissionRegistry, pattern: str
) -> tuple[_PatternShape, PermissionKey | None]:
    """Check a pattern against the registry.

    Return its shape, and the registry entry for an exact key (None
    for a wildcard). Raise PermissionChangeError for a malformed
    pattern, an unknown key, or an area with no registered key.
    """
    shape = _pattern_shape(pattern)
    if shape is _PatternShape.EXACT:
        try:
            return shape, registry.get(pattern)
        except UnknownPermissionKeyError as error:
            raise PermissionChangeError(str(error)) from None
    if shape is _PatternShape.AREA:
        area = _pattern_area(pattern)
        if not registry.has_area(area):
            raise PermissionChangeError(
                f"There is no permission key in the {area} area, so "
                f"{pattern} would match nothing."
            )
    return shape, None


def _fixed_reason(
    shape: _PatternShape, entry: PermissionKey | None, pattern: str
) -> str | None:
    """Return why overrides never apply to a pattern, or None.

    Owner-only keys, owner-only areas and protected keys are decided
    before overrides in the check order, so a row for them would do
    nothing.
    """
    if entry is not None and entry.owner_only:
        return f"{pattern} is owner-only and cannot be granted or denied."
    if entry is not None and entry.protected:
        return (
            f"{pattern} is protected: it is always allowed for "
            "everyone, so it cannot be granted or denied."
        )
    if shape is _PatternShape.AREA:
        area = _pattern_area(pattern)
        if area in OWNER_ONLY_AREAS:
            return (
                f"Keys in the {area} area are owner-only and cannot be "
                "granted or denied."
            )
    return None


def _is_covered(shape: _PatternShape, pattern: str, key: str) -> bool:
    """Return True when a row's key sits beneath a wildcard pattern.

    ``*`` covers every other row, ``area.*`` rows included. ``area.*``
    covers the exact keys in that area. The match is a plain string
    comparison, so an underscore matches only an underscore.
    """
    if key == pattern:
        return False
    if shape is _PatternShape.ALL:
        return True
    if shape is _PatternShape.AREA:
        area_prefix = _pattern_area(pattern) + KEY_SEPARATOR
        return WILDCARD not in key and key.startswith(area_prefix)
    return False


def find_covered_overrides(
    connection: sqlite3.Connection,
    *,
    user_id: int,
    pattern: str,
    guild_id: int = GLOBAL_GUILD_ID,
) -> tuple[ClearedOverride, ...]:
    """Return the rows a wildcard command on ``pattern`` would clear.

    Only rows for the same person and ``guild_id`` count. An exact key
    covers nothing. The result is sorted by key, and nothing is
    written. Raise PermissionChangeError for a malformed pattern.
    """
    shape = _pattern_shape(pattern)
    if shape is _PatternShape.EXACT:
        return ()
    rows = connection.execute(
        SELECT_USER_OVERRIDES_SQL,
        {"user_id": user_id, "guild_id": guild_id},
    ).fetchall()
    covered = [
        ClearedOverride(row["key"], row["effect"])
        for row in rows
        if _is_covered(shape, pattern, row["key"])
    ]
    return tuple(sorted(covered, key=lambda cleared: cleared.key))


@dataclass(frozen=True)
class ScopedCoveredOverride:
    """A row beneath a wildcard, in a scope other than the command's.

    ``guild_id`` is 0 for the global row or a server ID.
    """

    guild_id: int
    key: str
    effect: str


def find_covered_in_other_scopes(
    connection: sqlite3.Connection,
    *,
    user_id: int,
    pattern: str,
    guild_id: int,
) -> tuple[ScopedCoveredOverride, ...]:
    """Return rows ``pattern`` would cover in scopes but ``guild_id``.

    The covering rule is that of ``find_covered_overrides``, applied
    to the person's rows in every other scope. Those rows are not
    cleared by a command in ``guild_id`` and stay in force. An exact
    key covers nothing. The global rows come first, then servers by
    ID, each sorted by key. Nothing is written. Raise
    PermissionChangeError for a malformed pattern.
    """
    shape = _pattern_shape(pattern)
    if shape is _PatternShape.EXACT:
        return ()
    rows = connection.execute(
        SELECT_OTHER_SCOPE_OVERRIDES_SQL,
        {"user_id": user_id, "guild_id": guild_id},
    ).fetchall()
    return tuple(
        ScopedCoveredOverride(row["guild_id"], row["key"], row["effect"])
        for row in rows
        if _is_covered(shape, pattern, row["key"])
    )


@dataclass(frozen=True)
class ScopedOverride:
    """One stored override row for a key, in one scope.

    ``guild_id`` is 0 for the global row or a server ID.
    """

    guild_id: int
    effect: str


def find_key_overrides(
    connection: sqlite3.Connection, *, user_id: int, key: str
) -> tuple[ScopedOverride, ...]:
    """Return a person's rows for one key or pattern, in every scope.

    ``key`` is matched exactly, so ``feed.*`` finds only ``feed.*``
    rows. The global row (``guild_id`` 0) comes first, then servers
    by ID. Nothing is written.
    """
    rows = connection.execute(
        SELECT_KEY_OVERRIDES_SQL, {"user_id": user_id, "key": key}
    ).fetchall()
    return tuple(
        ScopedOverride(row["guild_id"], row["effect"]) for row in rows
    )


def _write_override_audit(
    connection: sqlite3.Connection,
    *,
    actor_id: int,
    now: int,
    user_id: int,
    key: str,
    guild_id: int,
    old_value: str | None,
    new_value: str | None,
    note: str | None = None,
) -> None:
    """Write one ``override`` audit entry for one row."""
    write_audit_entry(
        connection,
        kind=AUDIT_KIND_OVERRIDE,
        actor_id=actor_id,
        now=now,
        target_id=user_id,
        key=key,
        guild_id=guild_id,
        old_value=old_value,
        new_value=new_value,
        note=note,
    )


def _clear_covered(
    connection: sqlite3.Connection,
    *,
    covered: tuple[ClearedOverride, ...],
    user_id: int,
    guild_id: int,
    note: str,
    actor_id: int,
    now: int,
) -> None:
    """Delete each covered row and write its audit entry, by key."""
    for cleared in covered:
        connection.execute(
            DELETE_OVERRIDE_SQL,
            {"user_id": user_id, "key": cleared.key, "guild_id": guild_id},
        )
        _write_override_audit(
            connection,
            actor_id=actor_id,
            now=now,
            user_id=user_id,
            key=cleared.key,
            guild_id=guild_id,
            old_value=cleared.effect,
            new_value=None,
            note=note,
        )


def set_override(
    connection: sqlite3.Connection,
    registry: PermissionRegistry,
    *,
    user_id: int,
    pattern: str,
    effect: str,
    guild_id: int = GLOBAL_GUILD_ID,
    owner_id: int,
    actor_id: int,
    now: int,
) -> OverrideChange:
    """Grant or deny a pattern for a person in one guild.

    ``effect`` is EFFECT_ALLOW (``/perm grant``) or EFFECT_DENY
    (``/perm deny``). A wildcard pattern also removes the rows
    beneath it (see ``find_covered_overrides``), so the latest
    command decides every key it covers. The pattern row is written
    with ``set_by = actor_id`` and ``set_at = now`` when its effect
    changes. Audit entries come in order: the pattern row, then each
    cleared row by key. When the row already has the effect and
    nothing is covered, nothing is written.

    Raise PermissionChangeError for the owner, a malformed or
    unknown pattern, an owner-only key or area, or a protected key.
    Raise ValueError for an unknown effect or a negative guild_id.
    """
    _refuse_owner(user_id, owner_id, "an override")
    if effect not in COMMAND_FOR_EFFECT:
        raise ValueError(
            f"Unknown override effect {effect!r}. Known effects: "
            f"{', '.join(COMMAND_FOR_EFFECT)}."
        )
    _check_guild_id(guild_id)
    shape, entry = _check_pattern(registry, pattern)
    reason = _fixed_reason(shape, entry, pattern)
    if reason is not None:
        raise PermissionChangeError(reason)

    with transaction(connection):
        old_effect = find_override(connection, user_id, pattern, guild_id)
        covered = find_covered_overrides(
            connection, user_id=user_id, pattern=pattern, guild_id=guild_id
        )
        if old_effect == effect and not covered:
            return OverrideChange(pattern, effect, old_effect, False, ())

        if old_effect != effect:
            connection.execute(
                UPSERT_OVERRIDE_SQL,
                {
                    "user_id": user_id,
                    "key": pattern,
                    "effect": effect,
                    "guild_id": guild_id,
                    "set_by": actor_id,
                    "set_at": now,
                },
            )
            _write_override_audit(
                connection,
                actor_id=actor_id,
                now=now,
                user_id=user_id,
                key=pattern,
                guild_id=guild_id,
                old_value=old_effect,
                new_value=effect,
            )
        _clear_covered(
            connection,
            covered=covered,
            user_id=user_id,
            guild_id=guild_id,
            note=CLEARED_NOTE.format(
                command=COMMAND_FOR_EFFECT[effect], pattern=pattern
            ),
            actor_id=actor_id,
            now=now,
        )
    return OverrideChange(pattern, effect, old_effect, True, covered)


def reset_override(
    connection: sqlite3.Connection,
    registry: PermissionRegistry,
    *,
    user_id: int,
    pattern: str,
    guild_id: int = GLOBAL_GUILD_ID,
    owner_id: int,
    actor_id: int,
    now: int,
) -> OverrideChange:
    """Remove a pattern's row, returning those keys to the level.

    A wildcard pattern also removes the rows beneath it. Each removed
    row gets an audit entry: the pattern row first, then each
    cleared row by key. With nothing to remove, nothing is written.
    An owner-only or protected key, or an owner-only area, cannot
    have a row, so its reset changes nothing.

    Raise PermissionChangeError for the owner and for a malformed or
    unknown pattern, and ValueError for a negative guild_id.
    """
    _refuse_owner(user_id, owner_id, "an override")
    _check_guild_id(guild_id)
    shape, entry = _check_pattern(registry, pattern)
    if _fixed_reason(shape, entry, pattern) is not None:
        return OverrideChange(pattern, None, None, False, ())

    with transaction(connection):
        old_effect = find_override(connection, user_id, pattern, guild_id)
        covered = find_covered_overrides(
            connection, user_id=user_id, pattern=pattern, guild_id=guild_id
        )
        if old_effect is None and not covered:
            return OverrideChange(pattern, None, None, False, ())

        if old_effect is not None:
            connection.execute(
                DELETE_OVERRIDE_SQL,
                {"user_id": user_id, "key": pattern, "guild_id": guild_id},
            )
            _write_override_audit(
                connection,
                actor_id=actor_id,
                now=now,
                user_id=user_id,
                key=pattern,
                guild_id=guild_id,
                old_value=old_effect,
                new_value=None,
            )
        _clear_covered(
            connection,
            covered=covered,
            user_id=user_id,
            guild_id=guild_id,
            note=CLEARED_NOTE.format(command=RESET_COMMAND, pattern=pattern),
            actor_id=actor_id,
            now=now,
        )
    return OverrideChange(pattern, None, old_effect, True, covered)
