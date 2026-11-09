"""Decide whether a person may use a permission key.

See ``docs/design.md`` sections 1.2 (keys and the check order), 2.3
(levels) and 9.2 to 9.4 (the users, permission_overrides and blocks
tables). This module only reads those tables.

The check order:

1. The owner always passes.
2. An owner-only key is refused to everyone else. No override or
   level is consulted.
3. A protected key passes for everyone.
4. Blocks: a ``chat`` block refuses ``chat.talk``, and an ``all``
   block refuses every key. The server row and the global row both
   apply, and the stricter kind wins.
5. Overrides: the exact key, then ``area.*``, then ``*``. For each
   pattern the server row is checked before the global row. The first
   row found decides.
6. The person's level against the key's default level.

Nothing here reads the clock.
"""

import enum
import re
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass

from maika.database import GLOBAL_GUILD_ID


class Level(enum.IntEnum):
    """Permission levels (design section 2.3).

    The owner sits above the ladder and is not a level.
    """

    EVERYONE = 0
    MEMBER = 1
    TRUSTED = 2
    ADMIN = 3


# A person with no users row has this level.
DEFAULT_LEVEL = Level.EVERYONE

# area.action: lowercase letters, digits and underscores on both
# sides of exactly one dot.
KEY_PATTERN = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")

KEY_SEPARATOR = "."
WILDCARD = "*"

# The key a chat block refuses (design section 1.2).
CHAT_TALK_KEY = "chat.talk"

BLOCK_KIND_CHAT = "chat"
BLOCK_KIND_ALL = "all"

# How strict each block kind is. When a person has a server row and a
# global row, the stricter kind applies.
BLOCK_STRICTNESS = {BLOCK_KIND_CHAT: 1, BLOCK_KIND_ALL: 2}

# "Owner-only and fixed" in design section 1.2: every key in these
# areas, and these exact keys, must be flagged owner_only.
OWNER_ONLY_AREAS = frozenset(
    {"perm", "config", "event", "persona", "sleep", "presence", "say"}
)
OWNER_ONLY_KEYS = frozenset(
    {
        "mood.set",
        "mood.raw",
        "relationship.set",
        "relationship.raw",
        "state.reset",
    }
)

# How a row's scope reads in a decision detail.
DETAIL_SCOPE_GLOBAL = "everywhere"
DETAIL_SCOPE_SERVER = "server {guild_id}"

EFFECT_ALLOW = "allow"
EFFECT_DENY = "deny"

SELECT_LEVEL_SQL = "SELECT level FROM users WHERE discord_id = ?"

SELECT_BLOCK_SQL = "SELECT kind FROM blocks WHERE user_id = ? AND guild_id = ?"

SELECT_OVERRIDE_SQL = (
    "SELECT effect FROM permission_overrides "
    "WHERE user_id = ? AND key = ? AND guild_id = ?"
)


class UnknownPermissionKeyError(Exception):
    """Raised when a key has no entry in the permission registry."""

    def __init__(self, key: str) -> None:
        """Store the key and build the message."""
        self.key = key
        super().__init__(f"There is no permission key named {key}.")


@dataclass(frozen=True)
class PermissionKey:
    """One permission key and its rules.

    ``owner_only`` keys are fixed: only the owner passes, and no
    override or level changes that. ``protected`` keys pass for
    everyone, and no block, override or level removes them. A key
    cannot be both.
    """

    key: str
    default_level: Level
    owner_only: bool = False
    protected: bool = False

    def __post_init__(self) -> None:
        """Raise ValueError when the key's definition is wrong."""
        if WILDCARD in self.key:
            raise ValueError(
                f"Permission key {self.key!r} must not contain "
                f"{WILDCARD!r}. Wildcards belong in override rows only."
            )
        if not KEY_PATTERN.match(self.key):
            raise ValueError(
                f"Permission key {self.key!r} must be lowercase "
                "area.action: letters, digits and underscores on both "
                "sides of exactly one dot."
            )
        if not isinstance(self.default_level, Level):
            raise ValueError(
                f"{self.key}: default_level must be a Level, not "
                f"{self.default_level!r}."
            )
        if self.owner_only and self.protected:
            raise ValueError(
                f"{self.key} cannot be both owner-only and protected."
            )


def _key_area(key: str) -> str:
    """Return the part of a key before the dot."""
    return key.split(KEY_SEPARATOR, maxsplit=1)[0]


def _design_requires_owner_only(key: str) -> bool:
    """Return True when design section 1.2 makes a key owner-only."""
    return _key_area(key) in OWNER_ONLY_AREAS or key in OWNER_ONLY_KEYS


class PermissionRegistry:
    """All permission keys, looked up by key."""

    def __init__(self, keys: Iterable[PermissionKey]) -> None:
        """Index the keys, refusing a duplicate.

        A key in an owner-only area (OWNER_ONLY_AREAS) or in
        OWNER_ONLY_KEYS must be flagged owner_only, so a new key in
        those areas can never be granted by mistake. Other keys may
        also be flagged owner_only.
        """
        self._keys: dict[str, PermissionKey] = {}
        for entry in keys:
            if entry.key in self._keys:
                raise ValueError(
                    f"Permission key {entry.key} is defined more than once."
                )
            if _design_requires_owner_only(entry.key) and not entry.owner_only:
                raise ValueError(
                    f"Permission key {entry.key} must be flagged "
                    "owner_only: the design makes it owner-only "
                    "(section 1.2)."
                )
            self._keys[entry.key] = entry

    def get(self, key: str) -> PermissionKey:
        """Return the entry for a key.

        Raise UnknownPermissionKeyError when there is none.
        """
        try:
            return self._keys[key]
        except KeyError:
            raise UnknownPermissionKeyError(key) from None

    def has_area(self, area: str) -> bool:
        """Return True when at least one key is in ``area``."""
        return any(_key_area(key) == area for key in self._keys)

    def keys(self) -> tuple[str, ...]:
        """Return every key, sorted."""
        return tuple(sorted(self._keys))


class DecisionSource(enum.Enum):
    """Which step of the check decided."""

    OWNER = "owner"
    OWNER_ONLY_KEY = "owner_only_key"
    PROTECTED = "protected"
    BLOCK = "block"
    OVERRIDE = "override"
    LEVEL = "level"


@dataclass(frozen=True)
class Decision:
    """The result of a permission check.

    ``detail`` is a short readable note of what decided, for
    ``/perm show`` and the logs.
    """

    allowed: bool
    source: DecisionSource
    detail: str


def override_patterns(key: str) -> tuple[str, str, str]:
    """Return the override patterns for a key, in check order.

    For ``feed.remove`` that is ``("feed.remove", "feed.*", "*")``.
    """
    area = _key_area(key)
    return (key, f"{area}{KEY_SEPARATOR}{WILDCARD}", WILDCARD)


def _guilds_to_check(guild_id: int) -> tuple[int, ...]:
    """Return the guilds to look in: the server first, then global.

    With ``guild_id`` 0 only the global rows apply.
    """
    if guild_id == GLOBAL_GUILD_ID:
        return (GLOBAL_GUILD_ID,)
    return (guild_id, GLOBAL_GUILD_ID)


def _detail_scope(guild_id: int) -> str:
    """Return "everywhere" for the global row, else "server <id>"."""
    if guild_id == GLOBAL_GUILD_ID:
        return DETAIL_SCOPE_GLOBAL
    return DETAIL_SCOPE_SERVER.format(guild_id=guild_id)


def get_level(connection: sqlite3.Connection, user_id: int) -> Level:
    """Return a person's level, or DEFAULT_LEVEL with no users row."""
    row = connection.execute(SELECT_LEVEL_SQL, (user_id,)).fetchone()
    if row is None:
        return DEFAULT_LEVEL
    return Level(row["level"])


def find_block(
    connection: sqlite3.Connection, user_id: int, guild_id: int
) -> str | None:
    """Return the block kind for a person in one guild, or None."""
    row = connection.execute(SELECT_BLOCK_SQL, (user_id, guild_id)).fetchone()
    if row is None:
        return None
    return row["kind"]


def find_override(
    connection: sqlite3.Connection, user_id: int, pattern: str, guild_id: int
) -> str | None:
    """Return the override effect for one pattern and guild, or None."""
    row = connection.execute(
        SELECT_OVERRIDE_SQL, (user_id, pattern, guild_id)
    ).fetchone()
    if row is None:
        return None
    return row["effect"]


def _check_block(
    connection: sqlite3.Connection, user_id: int, key: str, guild_id: int
) -> Decision | None:
    """Return a refusal when a block applies to the key, else None.

    The server row and the global row both apply, and the stricter
    kind wins (``all`` over ``chat``), so a server row never loosens
    a global row. With equal kinds the server row is named. A
    ``chat`` block applies only to ``chat.talk``; when the winning
    block does not apply, the check continues past blocks.
    """
    strictest_kind = None
    strictest_guild = None
    # The server row is read first, so on equal kinds it is kept.
    for lookup_guild in _guilds_to_check(guild_id):
        kind = find_block(connection, user_id, lookup_guild)
        if kind is None:
            continue
        if (
            strictest_kind is None
            or BLOCK_STRICTNESS[kind] > BLOCK_STRICTNESS[strictest_kind]
        ):
            strictest_kind = kind
            strictest_guild = lookup_guild

    if strictest_kind is None:
        return None
    applies = strictest_kind == BLOCK_KIND_ALL or (
        strictest_kind == BLOCK_KIND_CHAT and key == CHAT_TALK_KEY
    )
    if not applies:
        return None
    return Decision(
        allowed=False,
        source=DecisionSource.BLOCK,
        detail=f"{strictest_kind} block, {_detail_scope(strictest_guild)}",
    )


def _check_overrides(
    connection: sqlite3.Connection, user_id: int, key: str, guild_id: int
) -> Decision | None:
    """Return the decision of the first override row found, or None."""
    for pattern in override_patterns(key):
        for lookup_guild in _guilds_to_check(guild_id):
            effect = find_override(connection, user_id, pattern, lookup_guild)
            if effect is None:
                continue
            return Decision(
                allowed=effect == EFFECT_ALLOW,
                source=DecisionSource.OVERRIDE,
                detail=(
                    f"{effect} override on {pattern}, "
                    f"{_detail_scope(lookup_guild)}"
                ),
            )
    return None


def check_permission(
    connection: sqlite3.Connection,
    registry: PermissionRegistry,
    *,
    owner_id: int,
    user_id: int,
    key: str,
    guild_id: int,
) -> Decision:
    """Decide whether a person may use a permission key.

    The steps run in the order given in the module docstring, and
    the first step that decides returns. ``guild_id`` is the server
    where the key is used, or 0 outside a server.

    An unknown key raises UnknownPermissionKeyError, for the owner
    too, so a typo in a key is never silently allowed.
    """
    entry = registry.get(key)

    if user_id == owner_id:
        return Decision(True, DecisionSource.OWNER, "owner")

    if entry.owner_only:
        return Decision(
            False, DecisionSource.OWNER_ONLY_KEY, f"{key} is owner-only"
        )

    if entry.protected:
        return Decision(True, DecisionSource.PROTECTED, f"{key} is protected")

    block = _check_block(connection, user_id, key, guild_id)
    if block is not None:
        return block

    override = _check_overrides(connection, user_id, key, guild_id)
    if override is not None:
        return override

    level = get_level(connection, user_id)
    required = entry.default_level
    return Decision(
        allowed=level >= required,
        source=DecisionSource.LEVEL,
        detail=(
            f"level {level.value} ({level.name.lower()}), {key} needs "
            f"level {required.value}"
        ),
    )
