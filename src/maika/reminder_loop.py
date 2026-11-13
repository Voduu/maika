"""The background loop that delivers due reminders.

See ``docs/design.md`` section 2.2 (Delivery). Every
``reminders.check_seconds`` the loop runs one check, which hands the
due reminders to ``maika.reminder_delivery.deliver_due_reminders``.
The first check comes one interval after the start, so the bot has
connected. The interval is read again before every wait, so a changed
setting applies at the next wait.

A check that raises is logged, and the wait doubles after each one,
up to 15 minutes, so a lasting problem does not post an error every
minute. A successful check returns the wait to the interval. A check
that retried any reminder (a temporary Discord problem) is followed
by a wait of at least 15 seconds, so an outage is not hit every
second. Errors
for one reminder are handled inside the check and never reach here,
so the logged exception never carries reminder text.

The loop runs until its task is cancelled. The bot cancels it when it
shuts down (``MaikaClient.close``).
"""

import asyncio
import logging
import sqlite3
from collections.abc import Awaitable, Callable

from maika.foundation_settings import REMINDER_CHECK_SECONDS_DEFAULT
from maika.reminder_delivery import (
    DeliverySummary,
    ReminderSender,
    build_block_check,
    deliver_due_reminders,
)

# A child of the "maika" logger, so an error logged here reaches the
# console and, through RouterLogHandler, #errors.
logger = logging.getLogger(__name__)

# The longest wait after failed checks: 15 minutes.
MAX_BACKOFF_SECONDS = 15 * 60

# Each failed check doubles the wait.
BACKOFF_FACTOR = 2

# The shortest wait after a check that retried any reminder.
RETRY_WAIT_SECONDS = 15

type SleepFunction = Callable[[float], Awaitable[None]]
type CycleFunction = Callable[[], Awaitable[DeliverySummary]]
type IntervalFunction = Callable[[], int]


async def run_reminder_cycle(
    connection: sqlite3.Connection, sender: ReminderSender, now: int
) -> DeliverySummary:
    """Deliver the reminders due at ``now``, with the real block check.

    ``now`` is UTC Unix seconds.
    """
    return await deliver_due_reminders(
        connection, sender, build_block_check(connection), now=now
    )


def _read(read_interval: IntervalFunction, fallback: float) -> float:
    """Return the check interval, or ``fallback`` if it can't be read.

    A failed read is logged and must not end the loop, or reminders
    would stop until the bot restarts.
    """
    try:
        return read_interval()
    except Exception:
        logger.exception("Reading the reminder check interval failed.")
        return fallback


async def run_reminder_loop(
    cycle: CycleFunction,
    *,
    read_interval: IntervalFunction,
    max_backoff_seconds: float = MAX_BACKOFF_SECONDS,
    sleep: SleepFunction = asyncio.sleep,
) -> None:
    """Wait, run ``cycle``, and repeat until the task is cancelled.

    ``read_interval`` is called before every wait. After a cycle that
    raises, the wait is double the larger of the last wait and the
    interval, up to ``max_backoff_seconds``. After a successful cycle
    it is the interval again, or at least ``RETRY_WAIT_SECONDS`` when
    the cycle retried any reminder. Cancelling the task ends the loop:
    the CancelledError is not caught.
    """
    delay = _read(read_interval, REMINDER_CHECK_SECONDS_DEFAULT)
    while True:
        await sleep(delay)
        # Any exception is caught on purpose: one failed check must
        # not stop reminders. CancelledError is not an Exception, so
        # it still ends the loop.
        try:
            summary = await cycle()
        except Exception:
            logger.exception("The reminder check failed.")
            interval = _read(read_interval, delay)
            delay = min(
                max(delay, interval) * BACKOFF_FACTOR, max_backoff_seconds
            )
            continue
        delay = _read(read_interval, delay)
        if summary.retried > 0:
            delay = max(delay, RETRY_WAIT_SECONDS)
