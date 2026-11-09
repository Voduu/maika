"""The /perm commands: level, show, grant, deny and reset.

See ``docs/design.md`` sections 1.2 (keys, the check order, wildcards
and ``/perm``), 2.3 (levels), 9.2 and 9.3 (the users and
permission_overrides tables) and 9.5 (the audit log). Every /perm
command is owner-only and fixed (section 1.2). Changes use the
``permission_store`` functions, which write each row and its audit
entry in one transaction, and are announced through a callable (the
bot posts them to ``#permissions``).
"""

import sqlite3

import discord

from maika import reply_helpers
from maika.commands import CommandCategory, CommandDefinition
from maika.database import GLOBAL_GUILD_ID
from maika.permission_store import (
    AREA_WILDCARD_PATTERN,
    OverrideChange,
    PermissionChangeError,
    find_key_overrides,
    reset_override,
    set_level,
    set_override,
)
from maika.permissions import (
    EFFECT_ALLOW,
    EFFECT_DENY,
    KEY_PATTERN,
    KEY_SEPARATOR,
    Level,
    PermissionKey,
    PermissionRegistry,
    UnknownPermissionKeyError,
    check_permission,
    get_level,
)
from maika.reply_helpers import (
    SCOPE_EVERYWHERE,
    SCOPE_OPTION_EVERYWHERE,
    SCOPE_OPTION_SERVER,
    SCOPE_SERVER,
    AnnounceFunction,
)
from maika.results import CommandResult, FailureKind

PERM_LEVEL_NAME = "perm level"
PERM_SHOW_NAME = "perm show"
PERM_GRANT_NAME = "perm grant"
PERM_DENY_NAME = "perm deny"
PERM_RESET_NAME = "perm reset"

PERM_LEVEL_KEY = PermissionKey("perm.level", Level.ADMIN, owner_only=True)

PERM_LEVEL_COMMAND = CommandDefinition(
    name=PERM_LEVEL_NAME,
    description="Set a person's permission level",
    category=CommandCategory.OWNER,
    key=PERM_LEVEL_KEY,
    help_text=(
        "Sets a person's level: Everyone (0), Member (1), Trusted (2) or "
        "Admin (3). Levels apply in every server, and each change is "
        "logged. Example: /perm level user:@Friend level:Trusted"
    ),
    example="/perm level user:@Friend level:Trusted",
)

PERM_SHOW_KEY = PermissionKey("perm.show", Level.ADMIN, owner_only=True)

PERM_SHOW_COMMAND = CommandDefinition(
    name=PERM_SHOW_NAME,
    description="Show what a person may use, and why",
    category=CommandCategory.OWNER,
    key=PERM_SHOW_KEY,
    help_text=(
        "Shows a person's level and, for every permission key, whether "
        "they may use it and what decided it, using the same check as "
        "the commands. Example: /perm show user:@Friend"
    ),
    example="/perm show user:@Friend",
)

PERM_GRANT_KEY = PermissionKey("perm.grant", Level.ADMIN, owner_only=True)

PERM_GRANT_COMMAND = CommandDefinition(
    name=PERM_GRANT_NAME,
    description="Allow a person a key, whatever their level",
    category=CommandCategory.OWNER,
    key=PERM_GRANT_KEY,
    help_text=(
        "Allows a person a permission key (an exact key, area.* or *), "
        "everywhere or in this server. A wildcard replaces the "
        "exceptions beneath it. Example: /perm grant user:@Friend "
        "key:feed.subscribe"
    ),
    example="/perm grant user:@Friend key:feed.subscribe",
)

PERM_DENY_KEY = PermissionKey("perm.deny", Level.ADMIN, owner_only=True)

PERM_DENY_COMMAND = CommandDefinition(
    name=PERM_DENY_NAME,
    description="Deny a person a key, whatever their level",
    category=CommandCategory.OWNER,
    key=PERM_DENY_KEY,
    help_text=(
        "Denies a person a permission key (an exact key, area.* or *), "
        "everywhere or in this server. A wildcard replaces the "
        "exceptions beneath it. Example: /perm deny user:@Friend "
        "key:feed.* scope:This server"
    ),
    example="/perm deny user:@Friend key:feed.* scope:This server",
)

PERM_RESET_KEY = PermissionKey("perm.reset", Level.ADMIN, owner_only=True)

PERM_RESET_COMMAND = CommandDefinition(
    name=PERM_RESET_NAME,
    description="Remove a person's exception for a key",
    category=CommandCategory.OWNER,
    key=PERM_RESET_KEY,
    help_text=(
        "Removes a person's grant or deny for a key, returning it to "
        "their level. A wildcard also removes the exceptions beneath "
        "it. Example: /perm reset user:@Friend key:feed.subscribe"
    ),
    example="/perm reset user:@Friend key:feed.subscribe",
)

LEVEL_SET_MESSAGE = "{mention} is now level {number} ({name})."
LEVEL_UNCHANGED_MESSAGE = "{mention} is already level {number} ({name})."
BOT_TARGET_MESSAGE = "Bots can't be given a level."
OWNER_TARGET_MESSAGE = "The owner is above the levels."
LEVEL_ANNOUNCE_LINE = (
    "Level changed: {target} {old_name} to {new_name} by {actor}."
)

SHOW_HEADER = "{mention}: level {number} ({name}), {scope}."
SHOW_OWNER_HEADER = "{mention}: the owner, above the levels, {scope}."
ALLOWED_LINE = "`{key}`: allowed ({source})"
ALLOWED_DETAIL_LINE = "`{key}`: allowed ({source}: {detail})"
DENIED_LINE = "`{key}`: denied ({source}: {detail})"
LINE_SEPARATOR = "\n"

# How the scope option of grant, deny and reset reads in a reply.
SCOPE_TEXT = {
    SCOPE_OPTION_EVERYWHERE: SCOPE_EVERYWHERE,
    SCOPE_OPTION_SERVER: SCOPE_SERVER,
}

EFFECT_WORDS = {EFFECT_ALLOW: "allowed", EFFECT_DENY: "denied"}

OVERRIDE_BOT_TARGET_MESSAGE = "Bots can't have permission exceptions."
OVERRIDE_OWNER_TARGET_MESSAGE = "The owner passes every check."
SERVER_SCOPE_IN_DM_MESSAGE = "Use this in a server, or choose Everywhere."
UNKNOWN_KEY_MESSAGE = "I don't know a permission key called `{key}`."
OVERRIDE_SET_MESSAGE = "{mention}: `{key}` is now {word} ({scope})."
OVERRIDE_UNCHANGED_MESSAGE = "{mention}: `{key}` is already {word} ({scope})."
OVERRIDE_REMOVED_MESSAGE = "{mention}: `{key}` exception removed ({scope})."
NO_OVERRIDE_MESSAGE = "{mention}: `{key}` has no exception ({scope})."
CLEARED_PART = " Cleared {count} {noun}: {rows}."
CLEARED_ROW = "`{key}` {effect}"
CLEARED_SEPARATOR = ", "
CLEARED_NOUN_ONE = "exception"
CLEARED_NOUN_MANY = "exceptions"
OVERRIDE_SET_ANNOUNCE = (
    "Override set: {mention} `{key}` {effect} ({scope}) by {actor}."
)
OVERRIDE_REMOVED_ANNOUNCE = (
    "Override removed: {mention} `{key}` ({scope}) by {actor}."
)
OTHER_SCOPE_NOTE = " Note: `{key}` is still {word} ({scope})."


def level_name(level: Level) -> str:
    """Return a level's display name, such as "Trusted"."""
    return level.name.title()


def perm_level(
    connection: sqlite3.Connection,
    *,
    target_id: int,
    target_is_bot: bool,
    level: Level,
    owner_id: int,
    actor_id: int,
    now: int,
    announce: AnnounceFunction,
) -> CommandResult:
    """Set a person's level and announce the change.

    A bot account or the owner as target is INVALID and nothing is
    written. When the person already has the level, nothing is
    written and the reply says so. Otherwise ``set_level`` writes the
    users row (or deletes it for Everyone) and a ``level`` audit
    entry, and ``announce`` receives the change line.
    """
    if target_is_bot:
        return CommandResult.fail(FailureKind.INVALID, BOT_TARGET_MESSAGE)
    if target_id == owner_id:
        return CommandResult.fail(FailureKind.INVALID, OWNER_TARGET_MESSAGE)

    try:
        change = set_level(
            connection,
            user_id=target_id,
            level=level,
            owner_id=owner_id,
            actor_id=actor_id,
            now=now,
        )
    except PermissionChangeError:
        # set_level refuses the owner, which is checked above.
        return CommandResult.fail(FailureKind.INVALID, OWNER_TARGET_MESSAGE)

    mention = reply_helpers.mention(target_id)
    if not change.changed:
        return CommandResult.ok(
            LEVEL_UNCHANGED_MESSAGE.format(
                mention=mention,
                number=level.value,
                name=level_name(level),
            ),
            data=change,
        )

    reply_helpers.announce(
        announce,
        LEVEL_ANNOUNCE_LINE.format(
            target=mention,
            old_name=level_name(change.old_level),
            new_name=level_name(change.new_level),
            actor=reply_helpers.mention(actor_id),
        ),
        "a level change",
    )
    return CommandResult.ok(
        LEVEL_SET_MESSAGE.format(
            mention=mention, number=level.value, name=level_name(level)
        ),
        data=change,
    )


def _show_header(
    connection: sqlite3.Connection,
    *,
    target_id: int,
    owner_id: int,
    guild_id: int,
) -> str:
    """Return the first line of /perm show."""
    mention = reply_helpers.mention(target_id)
    scope = SCOPE_EVERYWHERE if guild_id == GLOBAL_GUILD_ID else SCOPE_SERVER
    if target_id == owner_id:
        return SHOW_OWNER_HEADER.format(mention=mention, scope=scope)
    level = get_level(connection, target_id)
    return SHOW_HEADER.format(
        mention=mention,
        number=level.value,
        name=level_name(level),
        scope=scope,
    )


def perm_show(
    connection: sqlite3.Connection,
    registry: PermissionRegistry,
    *,
    target_id: int,
    owner_id: int,
    guild_id: int,
) -> CommandResult:
    """Show a person's level and the decision for every key.

    The first line names the level and the scope ("this server", or
    "everywhere" for ``guild_id`` 0). Then each key in the registry,
    sorted, gets one line from ``check_permission`` with the same
    arguments the real check uses: "allowed (<source>: <detail>)" or
    "denied (<source>: <detail>)", with the detail escaped so a ``*``
    in a pattern shows literally. An allowed line whose detail only
    repeats the source (the owner) is "allowed (<source>)".
    """
    lines = [
        _show_header(
            connection,
            target_id=target_id,
            owner_id=owner_id,
            guild_id=guild_id,
        )
    ]
    for key in registry.keys():
        decision = check_permission(
            connection,
            registry,
            owner_id=owner_id,
            user_id=target_id,
            key=key,
            guild_id=guild_id,
        )
        source = decision.source.value
        detail = discord.utils.escape_markdown(decision.detail)
        if not decision.allowed:
            line = DENIED_LINE.format(key=key, source=source, detail=detail)
        elif decision.detail == source:
            line = ALLOWED_LINE.format(key=key, source=source)
        else:
            line = ALLOWED_DETAIL_LINE.format(
                key=key, source=source, detail=detail
            )
        lines.append(line)
    return CommandResult.ok(LINE_SEPARATOR.join(lines))


def _names_nothing(registry: PermissionRegistry, pattern: str) -> bool:
    """Return True for a well-formed pattern that matches no key.

    That is an exact key that is not registered, or ``area.*`` for an
    area with no registered key. ``*`` and malformed text return
    False; the store's own checks handle them.
    """
    if AREA_WILDCARD_PATTERN.match(pattern):
        area = pattern.split(KEY_SEPARATOR, maxsplit=1)[0]
        return not registry.has_area(area)
    if KEY_PATTERN.match(pattern):
        try:
            registry.get(pattern)
        except UnknownPermissionKeyError:
            return True
    return False


def _cleared_part(change: OverrideChange) -> str:
    """Return " Cleared N exception(s): ..." or "" when none were."""
    if not change.cleared:
        return ""
    count = len(change.cleared)
    noun = CLEARED_NOUN_ONE if count == 1 else CLEARED_NOUN_MANY
    rows = CLEARED_SEPARATOR.join(
        CLEARED_ROW.format(key=cleared.key, effect=cleared.effect)
        for cleared in change.cleared
    )
    return CLEARED_PART.format(count=count, noun=noun, rows=rows)


def _other_scope_notes(
    connection: sqlite3.Connection,
    *,
    target_id: int,
    pattern: str,
    changed_guild: int,
    command_guild: int,
) -> str:
    """Return a note for each of the person's rows in another scope.

    Only rows for exactly ``pattern`` count, and not the row in
    ``changed_guild`` (the scope the command used). The global row
    comes first, then servers by ID. With no such row the result is
    "".
    """
    notes = [
        OTHER_SCOPE_NOTE.format(
            key=pattern,
            word=EFFECT_WORDS[row.effect],
            scope=reply_helpers.row_scope_text(row.guild_id, command_guild),
        )
        for row in find_key_overrides(
            connection, user_id=target_id, key=pattern
        )
        if row.guild_id != changed_guild
    ]
    return "".join(notes)


def _change_override(
    connection: sqlite3.Connection,
    registry: PermissionRegistry,
    *,
    effect: str | None,
    target_id: int,
    target_is_bot: bool,
    key: str,
    scope: str,
    owner_id: int,
    actor_id: int,
    guild_id: int,
    now: int,
    announce: AnnounceFunction,
) -> CommandResult:
    """Set (``effect`` allow or deny) or reset (None) an override.

    The checks, replies and announce line are described in
    ``perm_grant``, ``perm_deny`` and ``perm_reset``.
    """
    if target_is_bot:
        return CommandResult.fail(
            FailureKind.INVALID, OVERRIDE_BOT_TARGET_MESSAGE
        )
    if target_id == owner_id:
        return CommandResult.fail(
            FailureKind.INVALID, OVERRIDE_OWNER_TARGET_MESSAGE
        )
    row_guild = reply_helpers.row_guild(scope, guild_id)
    if row_guild is None:
        return CommandResult.fail(
            FailureKind.INVALID, SERVER_SCOPE_IN_DM_MESSAGE
        )

    # Keys are lowercase, so typed text is matched without regard to
    # case or surrounding spaces. Text that reaches a code span below
    # has passed the store's pattern check, so it cannot hold a
    # backtick.
    pattern = key.strip().lower()
    if _names_nothing(registry, pattern):
        return CommandResult.fail(
            FailureKind.NOT_FOUND, UNKNOWN_KEY_MESSAGE.format(key=pattern)
        )

    try:
        if effect is None:
            change = reset_override(
                connection,
                registry,
                user_id=target_id,
                pattern=pattern,
                guild_id=row_guild,
                owner_id=owner_id,
                actor_id=actor_id,
                now=now,
            )
        else:
            change = set_override(
                connection,
                registry,
                user_id=target_id,
                pattern=pattern,
                effect=effect,
                guild_id=row_guild,
                owner_id=owner_id,
                actor_id=actor_id,
                now=now,
            )
    except PermissionChangeError as error:
        return CommandResult.fail(
            FailureKind.INVALID, discord.utils.escape_markdown(str(error))
        )

    mention = reply_helpers.mention(target_id)
    scope_text = SCOPE_TEXT[scope]
    # Rows for the same pattern in other scopes stay in force, so the
    # reply (not the announce line) names them.
    notes = _other_scope_notes(
        connection,
        target_id=target_id,
        pattern=pattern,
        changed_guild=row_guild,
        command_guild=guild_id,
    )
    if not change.changed:
        if effect is None:
            message = NO_OVERRIDE_MESSAGE.format(
                mention=mention, key=pattern, scope=scope_text
            )
        else:
            message = OVERRIDE_UNCHANGED_MESSAGE.format(
                mention=mention,
                key=pattern,
                word=EFFECT_WORDS[effect],
                scope=scope_text,
            )
        return CommandResult.ok(message + notes, data=change)

    cleared = _cleared_part(change)
    actor = reply_helpers.mention(actor_id)
    if effect is None:
        message = OVERRIDE_REMOVED_MESSAGE.format(
            mention=mention, key=pattern, scope=scope_text
        )
        line = OVERRIDE_REMOVED_ANNOUNCE.format(
            mention=mention, key=pattern, scope=scope_text, actor=actor
        )
    else:
        message = OVERRIDE_SET_MESSAGE.format(
            mention=mention,
            key=pattern,
            word=EFFECT_WORDS[effect],
            scope=scope_text,
        )
        line = OVERRIDE_SET_ANNOUNCE.format(
            mention=mention,
            key=pattern,
            effect=effect,
            scope=scope_text,
            actor=actor,
        )
    reply_helpers.announce(announce, line + cleared, "an override change")
    return CommandResult.ok(message + cleared + notes, data=change)


def perm_grant(
    connection: sqlite3.Connection,
    registry: PermissionRegistry,
    *,
    target_id: int,
    target_is_bot: bool,
    key: str,
    scope: str,
    owner_id: int,
    actor_id: int,
    guild_id: int,
    now: int,
    announce: AnnounceFunction,
) -> CommandResult:
    """Allow a person a key, an ``area.*`` wildcard or ``*``.

    ``scope`` is SCOPE_OPTION_EVERYWHERE (a global row) or
    SCOPE_OPTION_SERVER (a row for ``guild_id``, the server where the
    command ran; INVALID in a DM). A bot or the owner as target is
    INVALID. ``key`` is stripped and lowercased before it is checked,
    and the replies and the stored row use that form. A well-formed
    pattern that names no registered key or area is NOT_FOUND, and
    anything else ``set_override`` refuses
    (a malformed pattern, an owner-only or protected key) is INVALID
    with its reason. A wildcard clears the rows beneath it, and the
    reply and the announce line list them. When nothing changed the
    reply says so and ``announce`` is not called. Either way the reply
    ends with a note for each of the person's rows for the same
    pattern in another scope, which stay in force.
    """
    return _change_override(
        connection,
        registry,
        effect=EFFECT_ALLOW,
        target_id=target_id,
        target_is_bot=target_is_bot,
        key=key,
        scope=scope,
        owner_id=owner_id,
        actor_id=actor_id,
        guild_id=guild_id,
        now=now,
        announce=announce,
    )


def perm_deny(
    connection: sqlite3.Connection,
    registry: PermissionRegistry,
    *,
    target_id: int,
    target_is_bot: bool,
    key: str,
    scope: str,
    owner_id: int,
    actor_id: int,
    guild_id: int,
    now: int,
    announce: AnnounceFunction,
) -> CommandResult:
    """Deny a person a key, an ``area.*`` wildcard or ``*``.

    The rules are those of ``perm_grant`` with the deny effect.
    """
    return _change_override(
        connection,
        registry,
        effect=EFFECT_DENY,
        target_id=target_id,
        target_is_bot=target_is_bot,
        key=key,
        scope=scope,
        owner_id=owner_id,
        actor_id=actor_id,
        guild_id=guild_id,
        now=now,
        announce=announce,
    )


def perm_reset(
    connection: sqlite3.Connection,
    registry: PermissionRegistry,
    *,
    target_id: int,
    target_is_bot: bool,
    key: str,
    scope: str,
    owner_id: int,
    actor_id: int,
    guild_id: int,
    now: int,
    announce: AnnounceFunction,
) -> CommandResult:
    """Remove a person's exception for a pattern in one scope.

    A wildcard also removes the rows beneath it. The target, scope
    and pattern checks are those of ``perm_grant``. An owner-only or
    protected key never has a row, so its reset changes nothing.
    """
    return _change_override(
        connection,
        registry,
        effect=None,
        target_id=target_id,
        target_is_bot=target_is_bot,
        key=key,
        scope=scope,
        owner_id=owner_id,
        actor_id=actor_id,
        guild_id=guild_id,
        now=now,
        announce=announce,
    )
