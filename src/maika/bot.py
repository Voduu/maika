"""The Discord client: commands, log router, startup and shutdown.

The slash commands are added to the command tree and synced once in
``setup_hook``, which also starts the log router and attaches the
handler that forwards warnings and errors to ``#errors``. The
startup, shutdown and command-usage lines go to ``#bot-log`` through
the router (design section 3.18). Posting a log line is best effort:
a missing channel or a failed post is logged to the console and never
stops the bot.
"""

import asyncio
import logging
import sqlite3

import discord
from discord import app_commands

import maika
from maika import usage_log
from maika.commands import CommandRegistry
from maika.config import Config
from maika.discord_adapter import handle_tree_error
from maika.log_names import LOGGER_NAME
from maika.log_router import LogLevel, LogRouter, LogStream, RouterLogHandler
from maika.slash_commands import build_config_group

logger = logging.getLogger(__name__)

# The most time a log line post may take. Without a limit a slow or
# rate-limited request could hold up the queue.
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
        self.log_router = LogRouter(
            connection, config.log_channel_id, self._send_log_line
        )
        self.log_usage = usage_log.build_usage_logger(
            self.log_router.post, self._guild_name_for
        )
        # Set in setup_hook. The task reference keeps the router's
        # run() from being garbage collected while it waits.
        self._log_router_task: asyncio.Task[None] | None = None
        self._log_handler: RouterLogHandler | None = None
        # Set by the first ready event. It keeps the startup line to
        # one post per run, and close() reads it to skip the shutdown
        # line when the bot never got that far.
        self._reached_first_ready = False
        self._shutdown_line_sent = False

    async def setup_hook(self) -> None:
        """Start the log router, then add and sync the slash commands.

        The router's run() starts as a background task, and a
        RouterLogHandler on the ``maika`` logger forwards warnings and
        errors to #errors. A failed sync is logged and the bot keeps
        running; Discord keeps the commands from the last successful
        sync.
        """
        self._start_log_router()
        self.tree.add_command(
            build_config_group(
                connection=self.connection,
                registry=self.command_registry,
                owner_id=self.config.owner_id,
                log_usage=self.log_usage,
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
        post per run. Before the line is posted, a console warning is
        logged when no log channel is set.
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
        self.log_router.warn_if_no_channel()
        self.log_router.post(LogStream.BOT_LOG, LogLevel.INFO, line)

    async def close(self) -> None:
        """Post the shutdown line, drain the log router, then close.

        ``close`` can be called more than once (for example by
        ``Client.run`` after an earlier call), so the line is posted
        only on the first call. Nothing is posted when the bot never
        reached its first ready event (for example after a rejected
        token). The router gets its drain time before the Discord
        connection closes, so queued lines can still be sent.
        """
        if (
            self._reached_first_ready
            and not self._shutdown_line_sent
            and not self.is_closed()
        ):
            self._shutdown_line_sent = True
            line = format_shutdown_line(self.config.environment.value)
            self.log_router.post(LogStream.BOT_LOG, LogLevel.INFO, line)
        await self.log_router.close()
        self._stop_log_handler()
        await super().close()

    def _start_log_router(self) -> None:
        """Start the router task and attach the #errors handler."""
        if self._log_router_task is None:
            self._log_router_task = asyncio.create_task(self.log_router.run())
        if self._log_handler is None:
            self._log_handler = RouterLogHandler(
                self.log_router, secrets=(self.config.discord_token,)
            )
            logging.getLogger(LOGGER_NAME).addHandler(self._log_handler)

    def _stop_log_handler(self) -> None:
        """Detach the #errors handler, if it is attached."""
        if self._log_handler is not None:
            logging.getLogger(LOGGER_NAME).removeHandler(self._log_handler)
            self._log_handler = None

    def _guild_name_for(self, guild_id: int) -> str | None:
        """Return a cached server's name, or None if it is unknown."""
        guild = self.get_guild(guild_id)
        if guild is None:
            return None
        return guild.name

    async def _send_log_line(self, channel_id: int, text: str) -> None:
        """Send one log line to a channel, with mentions turned off.

        A failure or timeout raises, and the router logs it on the
        console.
        """
        channel = self.get_partial_messageable(channel_id)
        async with asyncio.timeout(LOG_POST_TIMEOUT_SECONDS):
            await channel.send(
                text, allowed_mentions=discord.AllowedMentions.none()
            )
