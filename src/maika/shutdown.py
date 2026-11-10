"""Run the client and close it cleanly on SIGTERM or SIGINT.

``docker stop`` sends SIGTERM, and as PID 1 in a container Python
ignores it unless a handler is installed. Ctrl+C sends SIGINT. Both
start the same shutdown: ``client.close()`` stops the background
tasks, posts the shutdown line, sends the queued log lines for at most
``LOG_DRAIN_SECONDS`` (log_router), and closes the Discord connection.
``client.start`` then returns, and the caller closes the database and
exits with code 0.

Docker waits 10 seconds after SIGTERM before it kills the container,
and the log drain is the only step that waits on Discord for long, so
the shutdown fits well inside that time.
"""

import asyncio
import logging
import signal
from collections.abc import Awaitable, Callable
from typing import Protocol

# A child of the "maika" logger, so the line reaches the console.
logger = logging.getLogger(__name__)

SHUTDOWN_SIGNALS = (signal.SIGTERM, signal.SIGINT)

SHUTDOWN_LINE = "Shutting down (%s)."


class RunnableClient(Protocol):
    """The part of the Discord client that ``run_client`` uses."""

    async def __aenter__(self) -> object:
        """Prepare the client to run."""
        ...

    async def __aexit__(self, *exc_info: object) -> None:
        """Close the client if it is still open."""
        ...

    async def start(self, token: str) -> None:
        """Log in and run until the client closes."""
        ...

    async def close(self) -> None:
        """Close the client."""
        ...


class ShutdownHandler:
    """Start one shutdown on the first stop signal; ignore the rest."""

    def __init__(self, close: Callable[[], Awaitable[None]]) -> None:
        """Store the function that closes the client."""
        self._close = close
        # The shutdown task. Holding it keeps it from being garbage
        # collected, and it marks a shutdown as started.
        self._task: asyncio.Task[None] | None = None

    @property
    def started(self) -> bool:
        """Return True once a stop signal has started the shutdown."""
        return self._task is not None

    def install(self, loop: asyncio.AbstractEventLoop) -> None:
        """Route SIGTERM and SIGINT to ``request`` on this loop."""
        for signum in SHUTDOWN_SIGNALS:
            loop.add_signal_handler(signum, self.request, signum)

    def remove(self, loop: asyncio.AbstractEventLoop) -> None:
        """Give SIGTERM and SIGINT back their usual handling."""
        for signum in SHUTDOWN_SIGNALS:
            loop.remove_signal_handler(signum)

    def request(self, signum: int) -> None:
        """Start the shutdown, unless one has already started.

        One console line names the signal. A second signal while the
        shutdown runs changes nothing.
        """
        if self._task is not None:
            return
        logger.info(SHUTDOWN_LINE, signal.Signals(signum).name)
        self._task = asyncio.create_task(self._close())

    async def wait(self) -> None:
        """Wait for a started shutdown to finish."""
        if self._task is not None:
            await self._task


async def run_client(client: RunnableClient, token: str) -> None:
    """Run the client until it closes, with the stop signals handled.

    This replaces ``discord.Client.run`` so the handlers can be
    installed in the running loop. A rejected token raises from
    ``client.start`` as before. When ``start`` returns or raises, a
    shutdown already under way is awaited, then the handlers are
    removed.
    """
    loop = asyncio.get_running_loop()
    handler = ShutdownHandler(client.close)
    handler.install(loop)
    try:
        async with client:
            await client.start(token)
    finally:
        await handler.wait()
        handler.remove(loop)
