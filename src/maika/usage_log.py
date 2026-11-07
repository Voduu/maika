"""Write one console log line per command call.

See ``docs/design.md`` section 3.18: command usage goes to
``#bot-log``, and usage of protected keys (``/memories``,
``/forget``) is written at DEBUG level so repeated use cannot flood
the channel. ``log_usage`` writes the console line to the ``maika``
logger; ``build_usage_logger`` also posts a Discord form of the line,
with mentions and the server name, to ``#bot-log`` through the log
router.

A line names the command and the IDs involved, never the command's
argument values.
"""

import logging
from collections.abc import Callable

import discord

from maika.command_runner import CommandUsage
from maika.database import GLOBAL_GUILD_ID
from maika.log_names import LOGGER_NAME
from maika.log_router import LogLevel, LogStream

logger = logging.getLogger(LOGGER_NAME)

type PostFunction = Callable[[LogStream, LogLevel, str], None]
type GuildNameFunction = Callable[[int], str | None]

NO_FAILURE_TEXT = "none"


def format_usage_line(usage: CommandUsage) -> str:
    """Return the log line for one command call.

    The decision detail (for example "level 1 (member), feed.subscribe
    needs level 1") follows the decision source in parentheses.
    """
    failure = NO_FAILURE_TEXT
    if usage.failure_kind is not None:
        failure = usage.failure_kind.value
    decided_by = usage.decision_source.value
    if usage.decision_detail is not None:
        decided_by = f"{decided_by} ({usage.decision_detail})"
    return (
        f"Command {usage.command_name} ({usage.key}): "
        f"user {usage.user_id}, guild {usage.guild_id}, "
        f"channel {usage.channel_id}, allowed {usage.allowed}, "
        f"decided by {decided_by}, "
        f"success {usage.success}, failure {failure}"
    )


def _discord_place(usage: CommandUsage, guild_name: str | None) -> str:
    """Return where a command ran, for the Discord usage line.

    A DM gives "in DM". Otherwise the channel mention and the server
    name, escaped so Markdown characters show literally, or
    "server <id>" when the name is not known.
    """
    if usage.guild_id == GLOBAL_GUILD_ID:
        return "in DM"
    if guild_name is None:
        server = f"server {usage.guild_id}"
    else:
        server = discord.utils.escape_markdown(guild_name)
    return f"in <#{usage.channel_id}> in {server}"


def _discord_outcome(usage: CommandUsage) -> str:
    """Return the decision and any failure, for the Discord line.

    A refusal shows the decision detail, escaped so Markdown
    characters (such as ``*`` in a key pattern) show literally. An
    allowed command that did not succeed gets a "failed" suffix.
    """
    source = usage.decision_source.value
    if not usage.allowed:
        if usage.decision_detail is None:
            return f"denied ({source})"
        detail = discord.utils.escape_markdown(usage.decision_detail)
        return f"denied ({source}: {detail})"
    outcome = f"allowed ({source})"
    if not usage.success:
        failure = NO_FAILURE_TEXT
        if usage.failure_kind is not None:
            failure = usage.failure_kind.value
        outcome += f", failed: {failure}"
    return outcome


def format_discord_usage_line(
    usage: CommandUsage, guild_name: str | None
) -> str:
    """Return the #bot-log form of the line for one command call.

    For example "`/config get` by <@1> in <#2> in Maika Test: allowed
    (owner)". The user and channel are mentions outside any code
    span. In a DM the line ends "by <@1> in DM", with no channel or
    server. ``guild_name`` is the server's name, or None when it is
    not known.
    """
    return (
        f"`/{usage.command_name}` by <@{usage.user_id}> "
        f"{_discord_place(usage, guild_name)}: {_discord_outcome(usage)}"
    )


def log_usage(usage: CommandUsage) -> None:
    """Log one command call: INFO, or DEBUG for a protected key."""
    level = logging.DEBUG if usage.protected else logging.INFO
    logger.log(level, "%s", format_usage_line(usage))


def build_usage_logger(
    post: PostFunction,
    guild_name_for: GuildNameFunction,
) -> Callable[[CommandUsage], None]:
    """Return a usage function that logs and posts each line.

    The returned function writes the console line with ``log_usage``
    and posts the Discord form (``format_discord_usage_line``) to
    ``LogStream.BOT_LOG`` through ``post`` (usually
    ``LogRouter.post``): at DEBUG for a protected key and at INFO
    otherwise. ``guild_name_for`` returns a server's name from its
    ID, or None when the name is not known.
    """

    def log_and_post(usage: CommandUsage) -> None:
        """Log one command call and post it to #bot-log."""
        log_usage(usage)
        level = LogLevel.DEBUG if usage.protected else LogLevel.INFO
        guild_name = None
        if usage.guild_id != GLOBAL_GUILD_ID:
            guild_name = guild_name_for(usage.guild_id)
        line = format_discord_usage_line(usage, guild_name)
        post(LogStream.BOT_LOG, level, line)

    return log_and_post
