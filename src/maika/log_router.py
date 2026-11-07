"""Send log lines to the Discord log channels.

See ``docs/design.md`` section 3.18 ("Log channels") and section
10.15. There are eight log streams. Each has a channel setting
(``log.channel.<stream>``) and a level setting
(``log.level.<stream>``). A stream with no channel falls back to the
``#bot-log`` channel, and that falls back to ``LOG_CHANNEL_ID`` from
``.env``.

``LogRouter.post`` is synchronous and never waits on Discord: it
checks the level, picks the channel and puts the line on a bounded
queue. ``LogRouter.run`` sends the queued lines one at a time. A
failed send is logged on the console only, never posted again to
Discord, so a broken log channel cannot cause a loop of error posts.
"""

import asyncio
import enum
import logging
import sqlite3
from collections import deque
from collections.abc import Awaitable, Callable

from maika.foundation_settings import build_foundation_registry
from maika.log_names import LOGGER_NAME
from maika.settings_store import get_value

logger = logging.getLogger(LOGGER_NAME)

SETTINGS = build_foundation_registry()

CHANNEL_KEY_PREFIX = "log.channel."
LEVEL_KEY_PREFIX = "log.level."

# The most lines waiting to be sent. When the queue is full, the
# oldest line is dropped to make room for the new one.
LOG_QUEUE_LIMIT = 100

# How long close() waits for the queue to empty before it stops run().
LOG_DRAIN_SECONDS = 5

# Discord refuses a message longer than this many characters.
DISCORD_MESSAGE_LIMIT = 2000

TRUNCATION_MARK = "…"

type SendFunction = Callable[[int, str], Awaitable[None]]


class LogStream(enum.StrEnum):
    """The eight log streams, named as in their setting keys."""

    BOT_LOG = "bot_log"
    ERRORS = "errors"
    PERMISSIONS = "permissions"
    FEEDS = "feeds"
    DECISIONS = "decisions"
    PERSONA_LOG = "persona_log"
    EVENTS = "events"
    MEMORY_REVIEW = "memory_review"


class LogLevel(enum.IntEnum):
    """A log line's level, ordered from least to most severe."""

    DEBUG = 10
    INFO = 20
    WARNING = 30
    ERROR = 40


def parse_log_level(text: str) -> LogLevel:
    """Return the LogLevel for level text such as ``"info"``.

    The text is matched without regard to case and surrounding
    spaces. Text that names no level raises ValueError.
    """
    name = text.strip().upper()
    try:
        return LogLevel[name]
    except KeyError:
        allowed = ", ".join(level.name.lower() for level in LogLevel)
        raise ValueError(
            f"Log level must be one of {allowed}, not {text!r}."
        ) from None


def truncate_line(text: str) -> str:
    """Return the text cut to fit one Discord message.

    Text over ``DISCORD_MESSAGE_LIMIT`` characters is cut and ends with
    ``TRUNCATION_MARK``, so the result is exactly the limit long.
    """
    if len(text) <= DISCORD_MESSAGE_LIMIT:
        return text
    kept_length = DISCORD_MESSAGE_LIMIT - len(TRUNCATION_MARK)
    return text[:kept_length] + TRUNCATION_MARK


class LogRouter:
    """Queue log lines and send them to their log channels."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        fallback_channel_id: int | None,
        send: SendFunction,
    ) -> None:
        """Store the database, the .env channel and the send function.

        ``fallback_channel_id`` is ``LOG_CHANNEL_ID`` from ``.env``, or
        None. ``send`` does the real Discord send for one line, given
        the channel ID and the text.
        """
        self._connection = connection
        self._fallback_channel_id = fallback_channel_id
        self._send = send
        self._lines: deque[tuple[int, str]] = deque()
        # Set when a line is queued or close() is called, so run()
        # wakes up.
        self._wakeup = asyncio.Event()
        # True from the first dropped line until the queue empties,
        # so the overflow warning is logged once per episode.
        self._overflowing = False
        self._closing = False
        self._run_task: asyncio.Task[None] | None = None

    def post(self, stream: LogStream, level: LogLevel, text: str) -> None:
        """Queue a line for its stream's channel, if its level allows.

        Nothing happens when ``level`` is below the stream's level
        setting, when no channel is found, or after ``close`` was
        called. This never waits on Discord.
        """
        if self._closing:
            logger.debug(
                "The log router is closed, so a %s line was not queued.",
                stream,
            )
            return

        if level < self._stream_level(stream):
            return

        channel_id = self._resolve_channel(stream)
        if channel_id is None:
            logger.debug(
                "No log channel is set for %s, so a line was not queued.",
                stream,
            )
            return

        self._enqueue(channel_id, truncate_line(text))

    async def run(self) -> None:
        """Send queued lines one at a time until ``close`` ends this.

        A failed send is logged on the console and the next line is
        sent. Lines posted before this starts are sent first.
        """
        self._run_task = asyncio.current_task()
        try:
            while True:
                if self._lines:
                    await self._send_next()
                    continue
                if self._closing:
                    return
                self._wakeup.clear()
                await self._wakeup.wait()
        finally:
            self._run_task = None

    async def close(self) -> None:
        """Stop taking lines, drain the queue, then end ``run``.

        ``run`` gets up to ``LOG_DRAIN_SECONDS`` to send what is left.
        After that it is cancelled and the unsent lines are counted in
        a console warning.
        """
        self._closing = True
        self._wakeup.set()

        task = self._run_task
        if task is None:
            if self._lines:
                logger.warning(
                    "The log router closed before it ran. %d log lines "
                    "were not sent.",
                    len(self._lines),
                )
            return

        done, _ = await asyncio.wait({task}, timeout=LOG_DRAIN_SECONDS)
        if done:
            return

        logger.warning(
            "Log lines did not finish sending within %d seconds. %d "
            "queued lines were not sent.",
            LOG_DRAIN_SECONDS,
            len(self._lines),
        )
        task.cancel()
        await asyncio.wait({task})

    def _stream_level(self, stream: LogStream) -> LogLevel:
        """Return the lowest level a stream posts, from its setting."""
        text = get_value(self._connection, SETTINGS, LEVEL_KEY_PREFIX + stream)
        return parse_log_level(text)

    def _resolve_channel(self, stream: LogStream) -> int | None:
        """Return the channel for a stream, or None if none is set.

        The order is the stream's channel setting, the #bot-log
        channel setting, then ``LOG_CHANNEL_ID`` from ``.env``.
        """
        lookup_streams = [stream]
        if stream != LogStream.BOT_LOG:
            lookup_streams.append(LogStream.BOT_LOG)

        for lookup_stream in lookup_streams:
            channel_id = get_value(
                self._connection,
                SETTINGS,
                CHANNEL_KEY_PREFIX + lookup_stream,
            )
            if channel_id is not None:
                return channel_id
        return self._fallback_channel_id

    def _enqueue(self, channel_id: int, text: str) -> None:
        """Add a line, dropping the oldest when the queue is full."""
        if len(self._lines) >= LOG_QUEUE_LIMIT:
            self._lines.popleft()
            if not self._overflowing:
                self._overflowing = True
                logger.warning(
                    "The log queue is full (%d lines). The oldest lines "
                    "are being dropped until it empties.",
                    LOG_QUEUE_LIMIT,
                )
        self._lines.append((channel_id, text))
        self._wakeup.set()

    async def _send_next(self) -> None:
        """Send the oldest queued line, logging any failure."""
        channel_id, text = self._lines.popleft()
        if not self._lines:
            self._overflowing = False
        # Any exception is caught on purpose: a failed log post must
        # never stop the router, and it is never posted to Discord
        # again.
        try:
            await self._send(channel_id, text)
        except Exception:
            logger.exception(
                "Sending a log line to channel %d failed.", channel_id
            )
