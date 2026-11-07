"""The Discord client: commands, startup and shutdown lines.

The slash commands are added to the command tree and synced once in
``setup_hook``. The startup and shutdown lines go to ``#bot-log``
(design section 3.18). The channel comes from ``LOG_CHANNEL_ID`` in
``.env`` (design section 5). Posting a log line is best effort: a
missing channel or a failed post is logged to the console and never
stops the bot.
"""

import asyncio
import logging
import sqlite3

import discord
from discord import app_commands

import maika
from maika.commands import CommandRegistry
from maika.config import Config
from maika.discord_adapter import handle_tree_error
from maika.slash_commands import build_config_group

logger = logging.getLogger(__name__)

# The most time a log line post may take. Without a limit a slow or
# rate-limited request could hold up shutdown.
LOG_POST_TIMEOUT_SECONDS = 10


def format_startup_line(
    environment: str, version: str, discord_version: str
) -> str:
    """Return the line posted to #bot-log when the bot comes online."""
    return (
        f"Maika ({environment}) is online. "
        f"Version {version}, discord.py {discord_version}."
    )


def format_shutdown_line(environment: str) -> str:
    """Return the line posted to #bot-log when the bot shuts down."""
    return f"Maika ({environment}) is shutting down."


def build_intents() -> discord.Intents:
    """Return the gateway intents: guilds only, no message content."""
    intents = discord.Intents.none()
    intents.guilds = True
    return intents


class MaikaClient(discord.Client):
    """The Discord client for Maika."""

    def __init__(
        self,
        config: Config,
        *,
        connection: sqlite3.Connection,
        command_registry: CommandRegistry,
    ) -> None:
        """Store the config, database and commands, and set up intents.

        The connection is used only on the event-loop thread. The
        caller owns it and closes it after the client stops.
        """
        super().__init__(intents=build_intents())
        self.config = config
        self.connection = connection
        self.command_registry = command_registry
        self.tree = app_commands.CommandTree(self)
        # Set by the first ready event. It keeps the startup line to
        # one post per run, and close() reads it to skip the shutdown
        # line when the bot never got that far.
        self._reached_first_ready = False
        self._shutdown_line_sent = False

    async def setup_hook(self) -> None:
        """Add the slash commands to the tree and sync them once.

        A failed sync is logged and the bot keeps running; Discord
        keeps the commands from the last successful sync.
        """
        self.tree.add_command(
            build_config_group(
                connection=self.connection,
                registry=self.command_registry,
                owner_id=self.config.owner_id,
            )
        )
        self.tree.error(handle_tree_error)
        try:
            synced = await self.tree.sync()
        except discord.HTTPException:
            logger.exception("Syncing the slash commands failed.")
            return
        count = len(synced)
        noun = "command" if count == 1 else "commands"
        logger.info("Synced %d slash %s.", count, noun)

    async def on_ready(self) -> None:
        """Post the startup line on the first ready event only.

        discord.py fires ``on_ready`` again after a reconnect that
        could not resume the session, so a flag keeps the line to one
        post per run.
        """
        if self._reached_first_ready:
            logger.info("Reconnected to Discord.")
            return
        self._reached_first_ready = True
        logger.info("Connected to Discord as %s.", self.user)
        line = format_startup_line(
            self.config.environment.value,
            maika.__version__,
            discord.__version__,
        )
        await self._post_to_log_channel(line)

    async def close(self) -> None:
        """Post the shutdown line, then close the connection.

        ``close`` can be called more than once (for example by
        ``Client.run`` after an earlier call), so the line is posted
        only on the first call. Nothing is posted when the bot never
        reached its first ready event (for example after a rejected
        token), because the channel cache is empty then.
        """
        if (
            self._reached_first_ready
            and not self._shutdown_line_sent
            and not self.is_closed()
        ):
            self._shutdown_line_sent = True
            line = format_shutdown_line(self.config.environment.value)
            await self._post_to_log_channel(line)
        await super().close()

    async def _post_to_log_channel(self, line: str) -> None:
        """Post a line to the log channel, logging any failure.

        Nothing is posted when ``LOG_CHANNEL_ID`` is not set. A missing
        channel, a failed post or a timeout is logged to the console
        and does not raise.
        """
        channel_id = self.config.log_channel_id
        if channel_id is None:
            logger.warning(
                "LOG_CHANNEL_ID is not set, so this line was not posted: %s",
                line,
            )
            return

        channel = self.get_channel(channel_id)
        if channel is None:
            logger.error(
                "Log channel %d was not found, so this line was not "
                "posted: %s",
                channel_id,
                line,
            )
            return
        if not isinstance(channel, discord.abc.Messageable):
            logger.error(
                "Log channel %d cannot receive messages, so this line "
                "was not posted: %s",
                channel_id,
                line,
            )
            return

        # Any exception is caught on purpose: a failed log post must
        # never stop the bot or interrupt shutdown.
        try:
            async with asyncio.timeout(LOG_POST_TIMEOUT_SECONDS):
                await channel.send(line)
        except Exception:
            logger.exception(
                "Posting to log channel %d failed. Line: %s",
                channel_id,
                line,
            )
