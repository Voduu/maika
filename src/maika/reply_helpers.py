"""Shared reply, scope and announce helpers for the owner commands."""

import logging
from collections.abc import Callable, Iterable

from maika.database import GLOBAL_GUILD_ID

logger = logging.getLogger(__name__)

type AnnounceFunction = Callable[[str], None]

MENTION = "<@{user_id}>"

SCOPE_SERVER = "this server"
SCOPE_EVERYWHERE = "everywhere"
OTHER_SERVER_SCOPE = "server {guild_id}"

# The scope option of the commands that write per-scope rows.
SCOPE_OPTION_EVERYWHERE = "everywhere"
SCOPE_OPTION_SERVER = "server"

# A reply puts each extra part (what a command cleared, a note) on its
# own line after the main sentence. An announce line to a log channel
# stays on one line.
REPLY_PART_SEPARATOR = "\n"
ANNOUNCE_PART_SEPARATOR = " "


def join_parts(main: str, parts: Iterable[str], separator: str) -> str:
    """Return the main sentence followed by each extra part.

    ``separator`` is REPLY_PART_SEPARATOR for a reply and
    ANNOUNCE_PART_SEPARATOR for an announce line. With no parts the
    result is ``main`` unchanged.
    """
    return separator.join([main, *parts])


def mention(user_id: int) -> str:
    """Return a user mention. Replies are sent with mentions off."""
    return MENTION.format(user_id=user_id)


def announce(function: AnnounceFunction, line: str, subject: str) -> None:
    """Call ``function`` with ``line``, logging a failure.

    The change is already saved, so a failed announcement must not
    turn the reply into a failure. ``subject`` names the change in
    the console line, for example "a level change".
    """
    try:
        function(line)
    except Exception:
        logger.exception("Announcing %s failed.", subject)


def row_guild(scope: str, guild_id: int) -> int | None:
    """Return the guild_id the row uses, or None for server in a DM.

    Raise ValueError for a scope that is not a known option.
    """
    if scope == SCOPE_OPTION_EVERYWHERE:
        return GLOBAL_GUILD_ID
    if scope == SCOPE_OPTION_SERVER:
        if guild_id == GLOBAL_GUILD_ID:
            return None
        return guild_id
    raise ValueError(
        f"scope must be {SCOPE_OPTION_EVERYWHERE!r} or "
        f"{SCOPE_OPTION_SERVER!r}, not {scope!r}."
    )


def row_scope_text(stored_guild: int, command_guild: int) -> str:
    """Return how a stored row's scope reads in a note.

    "everywhere" for the global row, "this server" for the server
    where the command ran, and "server <id>" for any other server.
    """
    if stored_guild == GLOBAL_GUILD_ID:
        return SCOPE_EVERYWHERE
    if stored_guild == command_guild:
        return SCOPE_SERVER
    return OTHER_SERVER_SCOPE.format(guild_id=stored_guild)
