"""The /block and /unblock commands.

See ``docs/design.md`` sections 1.2 (the check order and blocks), 9.4
(the blocks table) and 9.5 (the audit log). Both commands are
owner-only and fixed under ``perm.*`` (section 1.2). Changes use the
``permission_store`` block functions, which write each row and its
audit entry in one transaction, and are announced through a callable
(the bot posts them to ``#permissions``). A block never touches
override rows, so ``/unblock`` restores the person exactly as they
were.
"""

import sqlite3

from maika import reply_helpers
from maika.commands import CommandCategory, CommandDefinition
from maika.perm_commands import SCOPE_TEXT, SERVER_SCOPE_IN_DM_MESSAGE
from maika.permission_store import (
    BlockChange,
    PermissionChangeError,
    find_user_blocks,
    remove_block,
    set_block,
)
from maika.permissions import (
    BLOCK_KIND_ALL,
    BLOCK_KIND_CHAT,
    Level,
    PermissionKey,
)
from maika.reply_helpers import AnnounceFunction
from maika.results import CommandResult, FailureKind

BLOCK_NAME = "block"
UNBLOCK_NAME = "unblock"

BLOCK_KEY = PermissionKey("perm.block", Level.ADMIN, owner_only=True)

BLOCK_COMMAND = CommandDefinition(
    name=BLOCK_NAME,
    description="Block a person from chat, or from chat and commands",
    category=CommandCategory.OWNER,
    key=BLOCK_KEY,
    help_text=(
        "Blocks a person from chat, or from chat and commands, "
        "everywhere or in this server. Protected keys such as /forget "
        "still work, and their permission exceptions are kept for "
        "/unblock. Example: /block user:@Friend kind:Chat and commands"
    ),
    example="/block user:@Friend kind:Chat and commands",
)

UNBLOCK_KEY = PermissionKey("perm.unblock", Level.ADMIN, owner_only=True)

UNBLOCK_COMMAND = CommandDefinition(
    name=UNBLOCK_NAME,
    description="Remove a person's block",
    category=CommandCategory.OWNER,
    key=UNBLOCK_KEY,
    help_text=(
        "Removes a person's block, everywhere or in this server, and "
        "lists any block that remains in another scope. Protected keys "
        "such as /forget work even while a block is in force. Example: "
        "/unblock user:@Friend scope:This server"
    ),
    example="/unblock user:@Friend scope:This server",
)

# How each block kind reads in a reply.
KIND_WORDS = {
    BLOCK_KIND_CHAT: "chat",
    BLOCK_KIND_ALL: "chat and commands",
}

BOT_TARGET_MESSAGE = "Bots can't be blocked."
OWNER_TARGET_MESSAGE = "The owner can't be blocked."
BLOCK_SET_MESSAGE = "{mention} is now blocked from {word} ({scope})."
BLOCK_UNCHANGED_MESSAGE = "{mention} is already blocked from {word} ({scope})."
BLOCK_REMOVED_MESSAGE = "{mention} is no longer blocked ({scope})."
NO_BLOCK_MESSAGE = "{mention} has no block ({scope})."
BLOCK_OTHER_SCOPE_NOTE = (
    "Note: {mention} is also blocked from {word} ({scope})."
)
UNBLOCK_REMAINING_NOTE = "Still blocked: {word} ({scope})."
BLOCK_SET_ANNOUNCE = "Block set: {mention} {kind} ({scope}) by {actor}."
BLOCK_KIND_CHANGE = "{old_kind} to {new_kind}"
BLOCK_REMOVED_ANNOUNCE = "Block removed: {mention} ({scope}) by {actor}."
ANNOUNCE_SUBJECT = "a block change"


def _check_target(
    *, target_id: int, target_is_bot: bool, owner_id: int
) -> CommandResult | None:
    """Return an INVALID result for a bot or the owner, else None."""
    if target_is_bot:
        return CommandResult.fail(FailureKind.INVALID, BOT_TARGET_MESSAGE)
    if target_id == owner_id:
        return CommandResult.fail(FailureKind.INVALID, OWNER_TARGET_MESSAGE)
    return None


def _other_block_notes(
    connection: sqlite3.Connection,
    *,
    target_id: int,
    template: str,
    changed_guild: int,
    command_guild: int,
) -> list[str]:
    """Return a note for each of the person's blocks in another scope.

    ``template`` is BLOCK_OTHER_SCOPE_NOTE or UNBLOCK_REMAINING_NOTE.
    The row in ``changed_guild`` (the scope the command used) is left
    out. The global row comes first, then servers by ID. With no such
    row the result is [].
    """
    return [
        template.format(
            mention=reply_helpers.mention(target_id),
            word=KIND_WORDS[row.kind],
            scope=reply_helpers.row_scope_text(row.guild_id, command_guild),
        )
        for row in find_user_blocks(connection, user_id=target_id)
        if row.guild_id != changed_guild
    ]


def _reply(
    message: str, notes: list[str], change: BlockChange
) -> CommandResult:
    """Return a success with each note on its own line."""
    return CommandResult.ok(
        reply_helpers.join_parts(
            message, notes, reply_helpers.REPLY_PART_SEPARATOR
        ),
        data=change,
    )


def _announce_kind(change: BlockChange) -> str:
    """Return the kind for the announce line, with the old kind if any.

    "all" for a new row, and "chat to all" when the kind changed.
    """
    if change.old_kind is None:
        return change.new_kind
    return BLOCK_KIND_CHANGE.format(
        old_kind=change.old_kind, new_kind=change.new_kind
    )


def block_user(
    connection: sqlite3.Connection,
    *,
    target_id: int,
    target_is_bot: bool,
    kind: str,
    scope: str,
    owner_id: int,
    actor_id: int,
    guild_id: int,
    now: int,
    announce: AnnounceFunction,
) -> CommandResult:
    """Block a person from chat (``kind`` chat) or everything (all).

    ``scope`` is SCOPE_OPTION_EVERYWHERE (a global row) or
    SCOPE_OPTION_SERVER (a row for ``guild_id``, the server where the
    command ran; INVALID in a DM). A bot or the owner as target is
    INVALID and nothing is written. A row with another kind is
    overwritten. When the row already has ``kind``, nothing is written,
    the reply says so and ``announce`` is not called. Either way the
    reply ends with a note for each of the person's blocks in another
    scope, which stay in force.

    Raise ValueError for an unknown kind or scope.
    """
    refusal = _check_target(
        target_id=target_id, target_is_bot=target_is_bot, owner_id=owner_id
    )
    if refusal is not None:
        return refusal
    row_guild = reply_helpers.row_guild(scope, guild_id)
    if row_guild is None:
        return CommandResult.fail(
            FailureKind.INVALID, SERVER_SCOPE_IN_DM_MESSAGE
        )

    try:
        change = set_block(
            connection,
            user_id=target_id,
            kind=kind,
            guild_id=row_guild,
            owner_id=owner_id,
            actor_id=actor_id,
            now=now,
        )
    except PermissionChangeError:
        # set_block refuses the owner, which is checked above.
        return CommandResult.fail(FailureKind.INVALID, OWNER_TARGET_MESSAGE)

    mention = reply_helpers.mention(target_id)
    scope_text = SCOPE_TEXT[scope]
    notes = _other_block_notes(
        connection,
        target_id=target_id,
        template=BLOCK_OTHER_SCOPE_NOTE,
        changed_guild=row_guild,
        command_guild=guild_id,
    )
    if not change.changed:
        message = BLOCK_UNCHANGED_MESSAGE.format(
            mention=mention, word=KIND_WORDS[kind], scope=scope_text
        )
        return _reply(message, notes, change)

    reply_helpers.announce(
        announce,
        BLOCK_SET_ANNOUNCE.format(
            mention=mention,
            kind=_announce_kind(change),
            scope=scope_text,
            actor=reply_helpers.mention(actor_id),
        ),
        ANNOUNCE_SUBJECT,
    )
    message = BLOCK_SET_MESSAGE.format(
        mention=mention, word=KIND_WORDS[kind], scope=scope_text
    )
    return _reply(message, notes, change)


def unblock_user(
    connection: sqlite3.Connection,
    *,
    target_id: int,
    target_is_bot: bool,
    scope: str,
    owner_id: int,
    actor_id: int,
    guild_id: int,
    now: int,
    announce: AnnounceFunction,
) -> CommandResult:
    """Remove a person's block in one scope.

    The target and scope checks are those of ``block_user``. Only the
    row for the chosen scope is removed. With no row there, nothing is
    written, the reply says so and ``announce`` is not called. Either
    way the reply ends with a "Still blocked" part for each of the
    person's blocks in another scope.

    Raise ValueError for an unknown scope.
    """
    refusal = _check_target(
        target_id=target_id, target_is_bot=target_is_bot, owner_id=owner_id
    )
    if refusal is not None:
        return refusal
    row_guild = reply_helpers.row_guild(scope, guild_id)
    if row_guild is None:
        return CommandResult.fail(
            FailureKind.INVALID, SERVER_SCOPE_IN_DM_MESSAGE
        )

    change = remove_block(
        connection,
        user_id=target_id,
        guild_id=row_guild,
        actor_id=actor_id,
        now=now,
    )

    mention = reply_helpers.mention(target_id)
    scope_text = SCOPE_TEXT[scope]
    notes = _other_block_notes(
        connection,
        target_id=target_id,
        template=UNBLOCK_REMAINING_NOTE,
        changed_guild=row_guild,
        command_guild=guild_id,
    )
    if not change.changed:
        message = NO_BLOCK_MESSAGE.format(mention=mention, scope=scope_text)
        return _reply(message, notes, change)

    reply_helpers.announce(
        announce,
        BLOCK_REMOVED_ANNOUNCE.format(
            mention=mention,
            scope=scope_text,
            actor=reply_helpers.mention(actor_id),
        ),
        ANNOUNCE_SUBJECT,
    )
    message = BLOCK_REMOVED_MESSAGE.format(mention=mention, scope=scope_text)
    return _reply(message, notes, change)
