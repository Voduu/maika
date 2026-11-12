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

A result with a confirmation is sent as an ephemeral prompt with
Confirm and Cancel buttons (``ConfirmView``). Confirm runs the action
through ``run_command``, so the permission is checked again and the
action is logged.
"""

import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass

import discord

from maika.command_runner import (
    FAILURE_MESSAGE,
    CommandContext,
    CommandUsage,
    run_command,
)
from maika.commands import CommandDefinition, CommandRegistry, Visibility
from maika.database import GLOBAL_GUILD_ID
from maika.foundation_settings import (
    CONFIRM_TIMEOUT_KEY,
    build_foundation_registry,
)
from maika.log_router import DISCORD_MESSAGE_LIMIT, truncate_line
from maika.results import CommandResult, Confirmation, FailureKind
from maika.settings_store import get_value

logger = logging.getLogger(__name__)

SETTINGS = build_foundation_registry()

# The most characters one reply message may hold.
REPLY_PART_LIMIT = DISCORD_MESSAGE_LIMIT

LINE_BREAK = "\n"

CONFIRM_LABEL = "Confirm"
CANCEL_LABEL = "Cancel"
NOT_YOUR_BUTTONS_MESSAGE = "These buttons belong to someone else."
CANCELLED_MESSAGE = "Cancelled. Nothing changed."
TIMED_OUT_MESSAGE = "Timed out. Nothing changed."


def guild_id_from_interaction(interaction: discord.Interaction) -> int:
    """Return the interaction's guild ID, or GLOBAL_GUILD_ID in a DM."""
    if interaction.guild_id is None:
        return GLOBAL_GUILD_ID
    return interaction.guild_id


def server_channel_id(interaction: discord.Interaction) -> int | None:
    """Return the interaction's channel ID, or None in a DM."""
    if interaction.guild_id is None:
        return None
    return interaction.channel_id


def bot_can_post(interaction: discord.Interaction) -> bool:
    """Return True when the bot may send messages in the channel.

    ``app_permissions`` holds the bot's permissions in the channel
    where the command ran. A thread needs the permission to send
    messages in threads.
    """
    permissions = interaction.app_permissions
    if isinstance(interaction.channel, discord.Thread):
        return permissions.send_messages_in_threads
    return permissions.send_messages


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


@dataclass(frozen=True)
class ConfirmLane:
    """What the Confirm button needs to run an action.

    These are the ``run_command`` arguments of the original call, so
    the confirmed action goes through the same permission check and
    usage log.
    """

    connection: sqlite3.Connection
    registry: CommandRegistry
    owner_id: int
    command_name: str
    log_usage: Callable[[CommandUsage], None]


class ConfirmView(discord.ui.View):
    """Confirm and Cancel buttons under a confirmation prompt.

    Only the user who ran the command can press them, and only the
    first press counts. Build it inside the running event loop.
    """

    def __init__(
        self,
        *,
        interaction: discord.Interaction,
        confirmation: Confirmation,
        lane: ConfirmLane,
        timeout: float,
    ) -> None:
        """Add the two buttons to a view that stops after ``timeout``.

        ``interaction`` is the command's interaction, whose user owns
        the buttons and whose response holds the prompt.
        """
        super().__init__(timeout=timeout)
        self.interaction = interaction
        self.confirmation = confirmation
        self.lane = lane
        # Set when the prompt went out as a followup instead of the
        # interaction response.
        self.message: discord.WebhookMessage | None = None
        self.pressed = False

        self.confirm_button: discord.ui.Button = discord.ui.Button(
            label=CONFIRM_LABEL, style=discord.ButtonStyle.success
        )
        self.confirm_button.callback = self.confirm
        self.cancel_button: discord.ui.Button = discord.ui.Button(
            label=CANCEL_LABEL, style=discord.ButtonStyle.secondary
        )
        self.cancel_button.callback = self.cancel
        self.add_item(self.confirm_button)
        self.add_item(self.cancel_button)

    async def _accept_press(self, interaction: discord.Interaction) -> bool:
        """Return True when this press should act.

        A press by anyone but the command's user gets an ephemeral
        refusal. Every press after the first is ignored.
        """
        if interaction.user.id != self.interaction.user.id:
            try:
                await interaction.response.send_message(
                    NOT_YOUR_BUTTONS_MESSAGE,
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException:
                logger.exception(
                    "Refusing a button press on %s failed.",
                    self.lane.command_name,
                )
            return False
        if self.pressed:
            return False
        self.pressed = True
        self.stop()
        return True

    async def _replace_prompt(
        self, interaction: discord.Interaction, text: str
    ) -> None:
        """Edit the prompt to ``text`` and remove the buttons.

        A failed edit is logged and not raised.
        """
        try:
            await interaction.response.edit_message(
                content=text,
                view=None,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            logger.exception(
                "Editing the confirmation of %s failed.",
                self.lane.command_name,
            )

    async def confirm(self, interaction: discord.Interaction) -> None:
        """Run the confirmed action and show its result."""
        if not await self._accept_press(interaction):
            return
        result = run_command(
            self.lane.connection,
            self.lane.registry,
            owner_id=self.lane.owner_id,
            command_name=self.lane.command_name,
            context=context_from_interaction(interaction),
            function=self.confirmation.on_confirm,
            log_usage=self.lane.log_usage,
        )
        await self._replace_prompt(interaction, result.message)

    async def cancel(self, interaction: discord.Interaction) -> None:
        """Drop the action and say nothing changed."""
        if not await self._accept_press(interaction):
            return
        await self._replace_prompt(interaction, CANCELLED_MESSAGE)

    async def on_timeout(self) -> None:
        """Say the prompt timed out and remove the buttons.

        A failed edit is logged and not raised.
        """
        if self.pressed:
            return
        allowed_mentions = discord.AllowedMentions.none()
        try:
            if self.message is not None:
                await self.message.edit(
                    content=TIMED_OUT_MESSAGE,
                    view=None,
                    allowed_mentions=allowed_mentions,
                )
            else:
                await self.interaction.edit_original_response(
                    content=TIMED_OUT_MESSAGE,
                    view=None,
                    allowed_mentions=allowed_mentions,
                )
        except discord.HTTPException:
            logger.exception(
                "Editing the timed-out confirmation of %s failed.",
                self.lane.command_name,
            )


async def send_confirmation(
    interaction: discord.Interaction,
    confirmation: Confirmation,
    lane: ConfirmLane,
) -> None:
    """Send a confirmation prompt with Confirm and Cancel buttons.

    The prompt is ephemeral and has no mentions. The buttons stop
    working after ``commands.confirm_timeout_seconds``, read when the
    prompt is sent. A failed send is logged and not raised.
    """
    timeout = get_value(lane.connection, SETTINGS, CONFIRM_TIMEOUT_KEY)
    view = ConfirmView(
        interaction=interaction,
        confirmation=confirmation,
        lane=lane,
        timeout=timeout,
    )
    allowed_mentions = discord.AllowedMentions.none()
    try:
        if interaction.response.is_done():
            view.message = await interaction.followup.send(
                confirmation.prompt,
                ephemeral=True,
                view=view,
                allowed_mentions=allowed_mentions,
                wait=True,
            )
        else:
            await interaction.response.send_message(
                confirmation.prompt,
                ephemeral=True,
                view=view,
                allowed_mentions=allowed_mentions,
            )
    except discord.HTTPException:
        view.stop()
        logger.exception(
            "Sending the confirmation of %s failed.", lane.command_name
        )


async def send_result(
    interaction: discord.Interaction,
    definition: CommandDefinition,
    result: CommandResult,
    *,
    lane: ConfirmLane | None = None,
) -> None:
    """Send a result's message as the reply to an interaction.

    The first part uses the interaction response; once that is done
    (or for every later part of a long message), it goes through the
    followup webhook. Every part has the same ephemeral flag and no
    mentions. A failed send is logged and not raised, and the parts
    after it are not sent.

    A result with a confirmation goes to ``send_confirmation``, which
    needs ``lane``. Raise ValueError when it is missing, which is a
    programming error.
    """
    if result.confirmation is not None:
        if lane is None:
            raise ValueError(
                f"The confirmation of {definition.name} needs a lane."
            )
        await send_confirmation(interaction, result.confirmation, lane)
        return

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
    lane = ConfirmLane(
        connection=connection,
        registry=registry,
        owner_id=owner_id,
        command_name=command_name,
        log_usage=log_usage,
    )
    await send_result(interaction, definition, result, lane=lane)


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
