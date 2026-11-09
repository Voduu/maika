"""The /perm commands: level, show, grant, deny and reset.

See ``docs/design.md`` sections 1.2 (keys, the check order, wildcards
and ``/perm``), 2.3 (levels), 9.2 and 9.3 (the users and
permission_overrides tables) and 9.5 (the audit log). Every /perm
command is owner-only and fixed (section 1.2). Changes use the
``permission_store`` functions, which write each row and its audit
entry in one transaction, and are announced through a callable (the
bot posts them to ``#permissions``).

A wildcard grant, deny or reset that would clear existing rows asks
first: it returns a ``CommandResult.ask`` that lists those rows, and
the write runs only on Confirm.
"""

import sqlite3

import discord

from maika import reply_helpers
from maika.commands import CommandCategory, CommandDefinition
from maika.database import GLOBAL_GUILD_ID
from maika.permission_store import (
    AREA_WILDCARD_PATTERN,
    ClearedOverride,
    OverrideChange,
    PermissionChangeError,
    find_covered_in_other_scopes,
    find_covered_overrides,
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
    DecisionSource,
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

# Sources whose detail only restates the source, so an allowed line
# in /perm show names the source alone.
SHORT_ALLOWED_SOURCES = frozenset(
    {DecisionSource.OWNER, DecisionSource.PROTECTED}
)

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
CLEARED_PART = "Cleared {count} {noun}: {rows}."
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
OTHER_SCOPE_NOTE = "Note: `{key}` is still {word} ({scope})."
# The note for rows beneath a wildcard that stay in other scopes.
COVERED_ELSEWHERE_NOTE = (
    "Note: {count} {noun} beneath it {verb} still in effect in "
    "{where}: {rows}."
)
COVERED_ELSEWHERE_ROW = "`{key}` {effect} ({scope})"
# Ends a row list cut at CONFIRM_ROW_LIMIT.
LIST_MORE_PART = "and {count} more"
COVERED_ELSEWHERE_VERB_ONE = "is"
COVERED_ELSEWHERE_VERB_MANY = "are"
COVERED_ELSEWHERE_ONE_SCOPE = "another scope"
COVERED_ELSEWHERE_MANY_SCOPES = "other scopes"

# The confirmation prompt of a wildcard command that clears rows.
CONFIRM_SET_PROMPT = "{verb} {mention} `{key}` ({scope})?"
CONFIRM_RESET_PROMPT = "Remove {mention}'s `{key}` exception ({scope})?"
CONFIRM_VERBS = {EFFECT_ALLOW: "Grant", EFFECT_DENY: "Deny"}
CONFIRM_CLEARS_LINE = "This also clears {count} {noun} beneath it:"
CONFIRM_ROW_LINE = "- `{key}` {effect}"
CONFIRM_MORE_LINE = "- and {count} more"
# At most this many rows are listed in a prompt, a cleared list or an
# other-scope note, each key cut to CONFIRM_KEY_LIMIT characters. The
# longest reply (removed message, capped cleared list, capped
# other-scope note, same-key notes) is then about
# 20 * CONFIRM_KEY_LIMIT + 695 + 77 per same-key note, assuming
# 20-digit IDs and a pattern of at most 20 characters. With 40 that
# stays under Discord's 2000-character limit for up to 6 same-key
# notes.
CONFIRM_ROW_LIMIT = 10
CONFIRM_KEY_LIMIT = 40
KEY_CUT_MARK = "…"


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
    arguments the real check uses, plus ``server_label`` "this
    server", so a deciding server row reads "this server" rather than
    its ID: "allowed (<source>: <detail>)" or
    "denied (<source>: <detail>)", with the detail escaped so a ``*``
    in a pattern shows literally. An allowed line whose detail only
    restates the source (SHORT_ALLOWED_SOURCES: the owner and
    protected keys) is "allowed (<source>)".
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
            server_label=SCOPE_SERVER,
        )
        source = decision.source.value
        detail = discord.utils.escape_markdown(decision.detail)
        if not decision.allowed:
            line = DENIED_LINE.format(key=key, source=source, detail=detail)
        elif decision.source in SHORT_ALLOWED_SOURCES:
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


def _capped_rows(parts: list[str]) -> str:
    """Join the first ``CONFIRM_ROW_LIMIT`` rows, then "and N more"."""
    shown = parts[:CONFIRM_ROW_LIMIT]
    hidden = len(parts) - CONFIRM_ROW_LIMIT
    if hidden > 0:
        shown.append(LIST_MORE_PART.format(count=hidden))
    return CLEARED_SEPARATOR.join(shown)


def _cleared_parts(change: OverrideChange) -> list[str]:
    """Return ["Cleared N exception(s): ..."], or [] when none were.

    N is the total. At most ``CONFIRM_ROW_LIMIT`` rows are named, each
    key cut to ``CONFIRM_KEY_LIMIT``, then "and N more". The audit log
    keeps one entry per cleared row.
    """
    if not change.cleared:
        return []
    count = len(change.cleared)
    noun = CLEARED_NOUN_ONE if count == 1 else CLEARED_NOUN_MANY
    rows = _capped_rows(
        [
            CLEARED_ROW.format(
                key=_cut_key(cleared.key), effect=cleared.effect
            )
            for cleared in change.cleared
        ]
    )
    return [CLEARED_PART.format(count=count, noun=noun, rows=rows)]


def _other_scope_notes(
    connection: sqlite3.Connection,
    *,
    target_id: int,
    pattern: str,
    changed_guild: int,
    command_guild: int,
) -> list[str]:
    """Return a note for each of the person's rows in another scope.

    Only rows for exactly ``pattern`` count, and not the row in
    ``changed_guild`` (the scope the command used). The global row
    comes first, then servers by ID. With no such row the result is
    [].
    """
    return [
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


def _covered_elsewhere_notes(
    connection: sqlite3.Connection,
    *,
    target_id: int,
    pattern: str,
    changed_guild: int,
    command_guild: int,
) -> list[str]:
    """Return the note for rows beneath a wildcard in other scopes.

    Those rows are what the wildcard would clear in ``changed_guild``
    but sit in another scope, so they still decide their keys there.
    At most ``CONFIRM_ROW_LIMIT`` rows are named, then "and N more".
    An exact key, or no such row, gives [].
    """
    rows = find_covered_in_other_scopes(
        connection, user_id=target_id, pattern=pattern, guild_id=changed_guild
    )
    if not rows:
        return []
    count = len(rows)
    many_scopes = len({row.guild_id for row in rows}) > 1
    parts = [
        COVERED_ELSEWHERE_ROW.format(
            key=_cut_key(row.key),
            effect=row.effect,
            scope=reply_helpers.row_scope_text(row.guild_id, command_guild),
        )
        for row in rows
    ]
    note = COVERED_ELSEWHERE_NOTE.format(
        count=count,
        noun=CLEARED_NOUN_ONE if count == 1 else CLEARED_NOUN_MANY,
        verb=(
            COVERED_ELSEWHERE_VERB_ONE
            if count == 1
            else COVERED_ELSEWHERE_VERB_MANY
        ),
        where=(
            COVERED_ELSEWHERE_MANY_SCOPES
            if many_scopes
            else COVERED_ELSEWHERE_ONE_SCOPE
        ),
        rows=_capped_rows(parts),
    )
    return [note]


def _cut_key(key: str) -> str:
    """Return a key cut to ``CONFIRM_KEY_LIMIT`` with a mark."""
    if len(key) <= CONFIRM_KEY_LIMIT:
        return key
    kept_length = CONFIRM_KEY_LIMIT - len(KEY_CUT_MARK)
    return key[:kept_length] + KEY_CUT_MARK


def _confirm_prompt(
    *,
    effect: str | None,
    target_id: int,
    pattern: str,
    scope: str,
    covered: tuple[ClearedOverride, ...],
) -> str:
    """Return the prompt asking to confirm a wildcard command.

    The first line names the command, the person, the pattern and the
    scope. Then come the rows it would clear, at most
    ``CONFIRM_ROW_LIMIT`` of them, with "and N more" for the rest.
    """
    mention = reply_helpers.mention(target_id)
    scope_text = SCOPE_TEXT[scope]
    if effect is None:
        question = CONFIRM_RESET_PROMPT.format(
            mention=mention, key=pattern, scope=scope_text
        )
    else:
        question = CONFIRM_SET_PROMPT.format(
            verb=CONFIRM_VERBS[effect],
            mention=mention,
            key=pattern,
            scope=scope_text,
        )
    count = len(covered)
    noun = CLEARED_NOUN_ONE if count == 1 else CLEARED_NOUN_MANY
    lines = [question, CONFIRM_CLEARS_LINE.format(count=count, noun=noun)]
    for cleared in covered[:CONFIRM_ROW_LIMIT]:
        lines.append(
            CONFIRM_ROW_LINE.format(
                key=_cut_key(cleared.key), effect=cleared.effect
            )
        )
    hidden = count - CONFIRM_ROW_LIMIT
    if hidden > 0:
        lines.append(CONFIRM_MORE_LINE.format(count=hidden))
    return LINE_SEPARATOR.join(lines)


def _refusal(error: PermissionChangeError) -> CommandResult:
    """Return the INVALID result for a refused change."""
    return CommandResult.fail(
        FailureKind.INVALID, discord.utils.escape_markdown(str(error))
    )


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
    ``perm_grant``, ``perm_deny`` and ``perm_reset``. A wildcard that
    would clear existing rows in the chosen scope returns an ask
    result listing them, and the write runs only on Confirm. With
    nothing to clear, the write runs at once.
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

    def write() -> CommandResult:
        """Make the change and return its reply."""
        return _write_override(
            connection,
            registry,
            effect=effect,
            target_id=target_id,
            pattern=pattern,
            scope=scope,
            row_guild=row_guild,
            owner_id=owner_id,
            actor_id=actor_id,
            guild_id=guild_id,
            now=now,
            announce=announce,
        )

    try:
        covered = find_covered_overrides(
            connection, user_id=target_id, pattern=pattern, guild_id=row_guild
        )
    except PermissionChangeError as error:
        return _refusal(error)
    if not covered:
        return write()
    # The write reads the covered rows again in its transaction, so a
    # row added before the press is cleared and listed in the reply.
    prompt = _confirm_prompt(
        effect=effect,
        target_id=target_id,
        pattern=pattern,
        scope=scope,
        covered=covered,
    )
    return CommandResult.ask(prompt, write)


def _write_override(
    connection: sqlite3.Connection,
    registry: PermissionRegistry,
    *,
    effect: str | None,
    target_id: int,
    pattern: str,
    scope: str,
    row_guild: int,
    owner_id: int,
    actor_id: int,
    guild_id: int,
    now: int,
    announce: AnnounceFunction,
) -> CommandResult:
    """Write a checked override change and return its reply.

    ``pattern`` is the stripped, lowercased key and ``row_guild`` the
    guild of the row. The store refuses an owner-only or protected
    pattern, which becomes INVALID with its reason.
    """
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
        return _refusal(error)

    mention = reply_helpers.mention(target_id)
    scope_text = SCOPE_TEXT[scope]
    # Rows for the same pattern, and rows beneath a wildcard, in other
    # scopes stay in force, so the reply (not the announce line) names
    # them.
    notes = _other_scope_notes(
        connection,
        target_id=target_id,
        pattern=pattern,
        changed_guild=row_guild,
        command_guild=guild_id,
    ) + _covered_elsewhere_notes(
        connection,
        target_id=target_id,
        pattern=pattern,
        changed_guild=row_guild,
        command_guild=guild_id,
    )
    reply_separator = reply_helpers.REPLY_PART_SEPARATOR
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
        return CommandResult.ok(
            reply_helpers.join_parts(message, notes, reply_separator),
            data=change,
        )

    cleared = _cleared_parts(change)
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
    reply_helpers.announce(
        announce,
        reply_helpers.join_parts(
            line, cleared, reply_helpers.ANNOUNCE_PART_SEPARATOR
        ),
        "an override change",
    )
    return CommandResult.ok(
        reply_helpers.join_parts(message, cleared + notes, reply_separator),
        data=change,
    )


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
    reply and the announce line list them. When it would clear rows,
    the result is an ask that lists them (at most
    ``CONFIRM_ROW_LIMIT``), and the change is made on Confirm with
    the same transaction, audit entries and reply. When nothing
    changed the reply says so and ``announce`` is not called. Either
    way the reply
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

    A wildcard also removes the rows beneath it, and asks first when
    there are any, as in ``perm_grant``. The target, scope and
    pattern checks are those of ``perm_grant``. An owner-only or
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
