"""Connect discord.py slash-command interactions to the command lane.

Every slash-command callback calls ``handle_command``, which builds a
CommandContext from the interaction, runs the command through
``run_command`` (the permission check and usage log), and sends the
result as a flat reply. Reply visibility follows design section 1.5:
a refusal is always ephemeral, and every other result keeps the
visibility of its command.

No reply ever pings anyone: mentions are turned off on every send.
"""

import logging
import sqlite3
from collections.abc import Callable

import discord

from maika import usage_log
from maika.command_runner import (
    FAILURE_MESSAGE,
    CommandContext,
    run_command,
)
from maika.commands import CommandDefinition, CommandRegistry, Visibility
from maika.database import GLOBAL_GUILD_ID
from maika.results import CommandResult, FailureKind

logger = logging.getLogger(__name__)


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


async def send_result(
    interaction: discord.Interaction,
    definition: CommandDefinition,
    result: CommandResult,
) -> None:
    """Send a result's message as the reply to an interaction.

    The first reply uses the interaction response; once that is done,
    the reply goes through the followup webhook. A failed send is
    logged and not raised.
    """
    ephemeral = is_ephemeral(definition, result)
    allowed_mentions = discord.AllowedMentions.none()
    try:
        if interaction.response.is_done():
            await interaction.followup.send(
                result.message,
                ephemeral=ephemeral,
                allowed_mentions=allowed_mentions,
            )
        else:
            await interaction.response.send_message(
                result.message,
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
) -> None:
    """Run one slash command and send its reply.

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
        log_usage=usage_log.log_usage,
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
