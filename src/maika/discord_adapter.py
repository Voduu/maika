"""Connect discord.py slash-command interactions to the command lane.

Every slash-command callback calls ``handle_command``, which builds a
CommandContext from the interaction, runs the command through
``run_command`` (the permission check and usage log), and sends the
result as a flat reply. Reply visibility follows design section 1.5:
a refusal is always ephemeral, and every other result keeps the
visibility of its command.

No reply ever pings anyone: mentions are turned off on every send. A
reply longer than Discord's message limit is sent as several messages,
split at line boundaries.
"""

import logging
import sqlite3
from collections.abc import Callable

import discord

from maika.command_runner import (
    FAILURE_MESSAGE,
    CommandContext,
    CommandUsage,
    run_command,
)
from maika.commands import CommandDefinition, CommandRegistry, Visibility
from maika.database import GLOBAL_GUILD_ID
from maika.log_router import DISCORD_MESSAGE_LIMIT, truncate_line
from maika.results import CommandResult, FailureKind

logger = logging.getLogger(__name__)

# The most characters one reply message may hold.
REPLY_PART_LIMIT = DISCORD_MESSAGE_LIMIT

LINE_BREAK = "\n"


def guild_id_from_interaction(interaction: discord.Interaction) -> int:
    """Return the interaction's guild ID, or GLOBAL_GUILD_ID in a DM."""
    if interaction.guild_id is None:
        return GLOBAL_GUILD_ID
    return interaction.guild_id


def context_from_interaction(
    interaction: discord.Interaction,
) -> CommandContext:
    """Return the CommandContext for an interaction.

    A direct message has no guild, so its ``guild_id`` is
    GLOBAL_GUILD_ID. ``now`` is the interaction's creation time in
    UTC Unix seconds, so tests control it.

    Raise ValueError when the interaction has no channel ID.
    """
    if interaction.channel_id is None:
        raise ValueError("The interaction has no channel ID.")
    return CommandContext(
        user_id=interaction.user.id,
        guild_id=guild_id_from_interaction(interaction),
        channel_id=interaction.channel_id,
        now=int(interaction.created_at.timestamp()),
    )


def is_ephemeral(definition: CommandDefinition, result: CommandResult) -> bool:
    """Return True when the reply should be seen by the user only.

    A refusal is always ephemeral. Every other result, failures
    included, keeps the visibility of the command (design 1.5).
    """
    if result.failure_kind is FailureKind.DENIED:
        return True
    return definition.visibility is Visibility.EPHEMERAL


def split_message(text: str) -> list[str]:
    """Split a reply into parts that each fit one Discord message.

    Text within ``REPLY_PART_LIMIT`` is one part. Longer text is split
    at line breaks, keeping as many whole lines in each part as fit.
    A single line longer than the limit is cut and ends with an
    ellipsis (``log_router.truncate_line``).
    """
    if len(text) <= REPLY_PART_LIMIT:
        return [text]

    parts: list[str] = []
    current: str | None = None
    for line in text.split(LINE_BREAK):
        line = truncate_line(line)
        if current is None:
            current = line
        elif len(current) + len(LINE_BREAK) + len(line) <= REPLY_PART_LIMIT:
            current += LINE_BREAK + line
        else:
            parts.append(current)
            current = line
    if current is not None:
        parts.append(current)
    return parts


async def send_result(
    interaction: discord.Interaction,
    definition: CommandDefinition,
    result: CommandResult,
) -> None:
    """Send a result's message as the reply to an interaction.

    The first part uses the interaction response; once that is done
    (or for every later part of a long message), it goes through the
    followup webhook. Every part has the same ephemeral flag and no
    mentions. A failed send is logged and not raised, and the parts
    after it are not sent.
    """
    ephemeral = is_ephemeral(definition, result)
    allowed_mentions = discord.AllowedMentions.none()
    first, *rest = split_message(result.message)
    try:
        if interaction.response.is_done():
            await interaction.followup.send(
                first,
                ephemeral=ephemeral,
                allowed_mentions=allowed_mentions,
            )
        else:
            await interaction.response.send_message(
                first,
                ephemeral=ephemeral,
                allowed_mentions=allowed_mentions,
            )
        for part in rest:
            await interaction.followup.send(
                part,
                ephemeral=ephemeral,
                allowed_mentions=allowed_mentions,
            )
    except discord.HTTPException:
        logger.exception("Sending the reply to %s failed.", definition.name)


async def handle_command(
    interaction: discord.Interaction,
    *,
    connection: sqlite3.Connection,
    registry: CommandRegistry,
    owner_id: int,
    command_name: str,
    function: Callable[[], CommandResult],
    log_usage: Callable[[CommandUsage], None],
) -> None:
    """Run one slash command and send its reply.

    ``log_usage`` receives the usage record (see ``run_command``).
    Every slash-command callback calls this. ``run_command`` is
    synchronous and blocks the event loop while it runs, so it must
    stay fast: Discord requires a reply within 3 seconds.
    """
    context = context_from_interaction(interaction)
    result = run_command(
        connection,
        registry,
        owner_id=owner_id,
        command_name=command_name,
        context=context,
        function=function,
        log_usage=log_usage,
    )
    definition = registry.get(command_name)
    await send_result(interaction, definition, result)


async def handle_tree_error(
    interaction: discord.Interaction, error: Exception
) -> None:
    """Log an error the command tree caught, and answer if needed.

    When the interaction has no reply yet, an ephemeral "Something
    went wrong." is sent, so Discord does not show "The application
    did not respond". A failed send is logged and not raised.
    """
    logger.exception(
        "A slash-command interaction raised an error.", exc_info=error
    )
    if interaction.response.is_done():
        return
    try:
        await interaction.response.send_message(
            FAILURE_MESSAGE,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except discord.HTTPException:
        logger.exception("Sending the error reply failed.")
