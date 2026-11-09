"""The read-only /perm commands: history and keys.

See ``docs/design.md`` sections 1.2 (the registry, keys and
``/perm keys``) and 9.5 (the audit log and ``/perm history``). Both
commands are owner-only and fixed (section 1.2) and write nothing.
"""

import sqlite3

import discord

from maika import reply_helpers
from maika.audit import AuditEntry, count_target_entries, find_target_entries
from maika.commands import CommandCategory, CommandDefinition, CommandRegistry
from maika.perm_commands import level_name
from maika.permission_store import (
    AUDIT_KIND_BLOCK,
    AUDIT_KIND_LEVEL,
    AUDIT_KIND_OVERRIDE,
)
from maika.permissions import (
    DEFAULT_LEVEL,
    KEY_SEPARATOR,
    Level,
    PermissionKey,
)
from maika.results import CommandResult, FailureKind

PERM_HISTORY_NAME = "perm history"
PERM_KEYS_NAME = "perm keys"

PERM_HISTORY_KEY = PermissionKey("perm.history", Level.ADMIN, owner_only=True)

PERM_HISTORY_COMMAND = CommandDefinition(
    name=PERM_HISTORY_NAME,
    description="List the permission changes made to one person",
    category=CommandCategory.OWNER,
    key=PERM_HISTORY_KEY,
    help_text=(
        "Lists the level, exception and block changes made to a person, "
        "newest first (20 by default, up to 100), with who made each. "
        "Example: /perm history user:@Friend count:50"
    ),
    example="/perm history user:@Friend count:50",
)

PERM_KEYS_KEY = PermissionKey("perm.keys", Level.ADMIN, owner_only=True)

PERM_KEYS_COMMAND = CommandDefinition(
    name=PERM_KEYS_NAME,
    description="List the permission keys and who may use them",
    category=CommandCategory.OWNER,
    key=PERM_KEYS_KEY,
    help_text=(
        "Lists every permission key, or the keys in one area, with the "
        "level it needs (or owner-only or protected) and what it "
        "covers. Example: /perm keys area:memory"
    ),
    example="/perm keys area:memory",
)

# /perm history
HISTORY_DEFAULT_COUNT = 20
HISTORY_MIN_COUNT = 1
HISTORY_MAX_COUNT = 100

HISTORY_HEADER = "{mention}, newest first ({shown} of {total}):"
HISTORY_LINE = "{time} {kind_text}: {old} to {new} by {actor}"
HISTORY_TIME = "<t:{logged_at}:f>"
HISTORY_KIND_WITH_KEY = "{kind} `{key}`"
HISTORY_KIND_WITH_SCOPE = "{kind_text} ({scope})"
HISTORY_NOTE_PART = " ({note})"
NO_HISTORY_MESSAGE = "No changes recorded for {mention}."
# How a NULL old or new value reads (no row before or after).
NO_VALUE_TEXT = "none"

# /perm keys
KEYS_HEADER_ONE = "1 key."
KEYS_HEADER_MANY = "{count} keys."
KEYS_IN_AREA_HEADER_ONE = "1 key in `{area}`."
KEYS_IN_AREA_HEADER_MANY = "{count} keys in `{area}`."
KEY_LINE = "`{key}`: {level_text}."
KEY_DESCRIPTION_PART = " {description}"
LEVEL_TEXT = "level {number} ({name})"
OWNER_ONLY_TEXT = "owner-only"
PROTECTED_TEXT = "protected (everyone)"
UNKNOWN_AREA_MESSAGE = "I don't know a permission area called `{area}`."
BACKTICK = "`"

LINE_SEPARATOR = "\n"


def _level_value_text(value: str | None) -> str:
    """Return a stored level as its name.

    A NULL level value means "no users row", which is level 0, so it
    reads as that level's name (Everyone) rather than "none".
    """
    if value is None:
        return level_name(DEFAULT_LEVEL)
    return level_name(Level(int(value)))


def _value_text(entry: AuditEntry, value: str | None) -> str:
    """Return how an old or new value reads in a history line."""
    if entry.kind == AUDIT_KIND_LEVEL:
        return _level_value_text(value)
    if value is None:
        return NO_VALUE_TEXT
    return value


def _kind_text(entry: AuditEntry, command_guild: int) -> str:
    """Return the kind part of a history line.

    An override adds its key, and an override or a block adds its
    scope ("everywhere", "this server" or "server <id>").
    """
    kind_text = entry.kind
    if entry.kind == AUDIT_KIND_OVERRIDE:
        kind_text = HISTORY_KIND_WITH_KEY.format(
            kind=entry.kind, key=entry.key
        )
    if entry.kind in (AUDIT_KIND_OVERRIDE, AUDIT_KIND_BLOCK):
        kind_text = HISTORY_KIND_WITH_SCOPE.format(
            kind_text=kind_text,
            scope=reply_helpers.row_scope_text(entry.guild_id, command_guild),
        )
    return kind_text


def _history_line(entry: AuditEntry, command_guild: int) -> str:
    """Return one history line, with the note when there is one."""
    line = HISTORY_LINE.format(
        time=HISTORY_TIME.format(logged_at=entry.logged_at),
        kind_text=_kind_text(entry, command_guild),
        old=_value_text(entry, entry.old_value),
        new=_value_text(entry, entry.new_value),
        actor=reply_helpers.mention(entry.actor_id),
    )
    if entry.note:
        note = discord.utils.escape_markdown(entry.note)
        line += HISTORY_NOTE_PART.format(note=note)
    return line


def perm_history(
    connection: sqlite3.Connection,
    *,
    target_id: int,
    count: int,
    guild_id: int,
) -> CommandResult:
    """List the newest ``count`` permission changes made to a person.

    The first line is "<@id>, newest first (<shown> of <total>):",
    where total counts every level, override and block entry stored
    for the person. Each later line is "<t:unix:f> <kind>: <old> to
    <new> by <@actor>", with the key for an override, the scope for
    an override or a block (relative to ``guild_id``, the server
    where the command ran), and the escaped note in parentheses.
    A level reads as its name; any other NULL value reads "none".
    With no entries the reply is "No changes recorded for <@id>."

    Raise ValueError when ``count`` is outside HISTORY_MIN_COUNT to
    HISTORY_MAX_COUNT.
    """
    if not HISTORY_MIN_COUNT <= count <= HISTORY_MAX_COUNT:
        raise ValueError(
            f"count must be {HISTORY_MIN_COUNT} to {HISTORY_MAX_COUNT}, "
            f"not {count}."
        )
    mention = reply_helpers.mention(target_id)
    entries = find_target_entries(connection, target_id=target_id, limit=count)
    if not entries:
        return CommandResult.ok(NO_HISTORY_MESSAGE.format(mention=mention))

    total = count_target_entries(connection, target_id=target_id)
    lines = [
        HISTORY_HEADER.format(mention=mention, shown=len(entries), total=total)
    ]
    lines.extend(_history_line(entry, guild_id) for entry in entries)
    return CommandResult.ok(LINE_SEPARATOR.join(lines))


def _key_level_text(entry: PermissionKey) -> str:
    """Return who may use a key: a level, owner-only or protected."""
    if entry.owner_only:
        return OWNER_ONLY_TEXT
    if entry.protected:
        return PROTECTED_TEXT
    return LEVEL_TEXT.format(
        number=entry.default_level.value,
        name=level_name(entry.default_level),
    )


def _keys_header(count: int, area: str | None) -> str:
    """Return the first line of /perm keys."""
    if area is None:
        if count == 1:
            return KEYS_HEADER_ONE
        return KEYS_HEADER_MANY.format(count=count)
    if count == 1:
        return KEYS_IN_AREA_HEADER_ONE.format(area=area)
    return KEYS_IN_AREA_HEADER_MANY.format(count=count, area=area)


def perm_keys(registry: CommandRegistry, *, area: str | None) -> CommandResult:
    """List every permission key, or the keys in one area.

    The first line is "<n> keys." or "<n> keys in `<area>`.", then
    one line per key, sorted: "`<key>`: <level text>. <description>",
    where the level text is "level <n> (<name>)", "owner-only" or
    "protected (everyone)". ``area`` is matched without regard to
    case and surrounding spaces, and an empty area lists every key.
    An area with no key is NOT_FOUND.
    """
    permission_registry = registry.permission_registry()
    wanted = None
    if area is not None and area.strip() != "":
        wanted = area.strip().lower()
        if not permission_registry.has_area(wanted):
            # A backtick would end the code span early, so it is left
            # out of the echoed text.
            shown = wanted.replace(BACKTICK, "")
            return CommandResult.fail(
                FailureKind.NOT_FOUND, UNKNOWN_AREA_MESSAGE.format(area=shown)
            )

    keys = [
        key
        for key in permission_registry.keys()
        if wanted is None or key.split(KEY_SEPARATOR, maxsplit=1)[0] == wanted
    ]
    lines = [_keys_header(len(keys), wanted)]
    for key in keys:
        line = KEY_LINE.format(
            key=key, level_text=_key_level_text(permission_registry.get(key))
        )
        description = registry.key_description(key)
        if description:
            line += KEY_DESCRIPTION_PART.format(description=description)
        lines.append(line)
    return CommandResult.ok(LINE_SEPARATOR.join(lines))
