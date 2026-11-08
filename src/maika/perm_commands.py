"""The /perm commands: level and show.

See ``docs/design.md`` sections 1.2 (keys and ``/perm``), 2.3
(levels), 9.2 (the users table) and 9.5 (the audit log). Every /perm
command is owner-only and fixed (section 1.2). Level changes use
``permission_store.set_level``, which writes the users row and its
audit entry in one transaction, and are announced through a callable
(the bot posts them to ``#permissions``).
"""

import logging
import sqlite3
from collections.abc import Callable

import discord

from maika.commands import CommandCategory, CommandDefinition
from maika.database import GLOBAL_GUILD_ID
from maika.permission_store import PermissionChangeError, set_level
from maika.permissions import (
    Level,
    PermissionKey,
    PermissionRegistry,
    check_permission,
    get_level,
)
from maika.results import CommandResult, FailureKind

logger = logging.getLogger(__name__)

type AnnounceFunction = Callable[[str], None]

PERM_LEVEL_NAME = "perm level"
PERM_SHOW_NAME = "perm show"

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

MENTION = "<@{user_id}>"
LEVEL_SET_MESSAGE = "{mention} is now level {number} ({name})."
LEVEL_UNCHANGED_MESSAGE = "{mention} is already level {number} ({name})."
BOT_TARGET_MESSAGE = "Bots can't be given a level."
OWNER_TARGET_MESSAGE = "The owner is above the levels."
LEVEL_ANNOUNCE_LINE = (
    "Level changed: {target} {old_name} to {new_name} by {actor}."
)

SHOW_HEADER = "{mention}: level {number} ({name}), {scope}."
SHOW_OWNER_HEADER = "{mention}: the owner, above the levels, {scope}."
SCOPE_SERVER = "this server"
SCOPE_EVERYWHERE = "everywhere"
ALLOWED_LINE = "`{key}`: allowed ({source})"
ALLOWED_DETAIL_LINE = "`{key}`: allowed ({source}: {detail})"
DENIED_LINE = "`{key}`: denied ({source}: {detail})"
LINE_SEPARATOR = "\n"


def level_name(level: Level) -> str:
    """Return a level's display name, such as "Trusted"."""
    return level.name.title()


def _mention(user_id: int) -> str:
    """Return a user mention. Replies are sent with mentions off."""
    return MENTION.format(user_id=user_id)


def _announce(announce: AnnounceFunction, line: str) -> None:
    """Call ``announce``, logging a failure instead of raising.

    The change is already saved, so a failed announcement must not
    turn the reply into a failure.
    """
    try:
        announce(line)
    except Exception:
        logger.exception("Announcing a level change failed.")


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

    mention = _mention(target_id)
    if not change.changed:
        return CommandResult.ok(
            LEVEL_UNCHANGED_MESSAGE.format(
                mention=mention,
                number=level.value,
                name=level_name(level),
            ),
            data=change,
        )

    _announce(
        announce,
        LEVEL_ANNOUNCE_LINE.format(
            target=mention,
            old_name=level_name(change.old_level),
            new_name=level_name(change.new_level),
            actor=_mention(actor_id),
        ),
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
    mention = _mention(target_id)
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
