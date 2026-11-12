"""Deliver due reminders and record what happened.

See ``docs/design.md`` sections 2.2 (Reminders: delivery, blocks,
repeats) and 9.10 (the ``reminders`` table). Each check reads the
pending reminders that are due, sends each one through a
``ReminderSender``, and records the outcome. The Discord sender and
the background loop that calls ``deliver_due_reminders`` every
``reminders.check_seconds`` live elsewhere; this module never touches
Discord or reads the clock, so tests drive it with fakes and a fixed
``now``.

The rules, per reminder:

- A person with a global ``all`` block is skipped: nothing is sent
  and the row stays pending and due, so it is delivered once the
  block is removed.
- A DM reminder goes by DM. If the DM is refused, a short notice
  without the reminder text is posted in the channel where it was
  set, when the person can see that channel and is not blocked in
  that server.
- A channel reminder is posted in its channel only while its creator
  is a member of that server, can view the channel and is not
  blocked there. Otherwise, or when the post is refused, it goes by
  DM, with no notice.
- Only a permanent refusal fails a delivery. A temporary problem
  (RETRY) leaves the row unchanged, so the next check starts it again
  from the beginning.
- A one-time reminder becomes ``delivered`` or ``failed``. A
  repeating reminder moves to its next time and stays ``pending``
  either way; it becomes ``failed`` only when its next time cannot be
  calculated.

Send first, then record. Each reminder is sent with no database
transaction open, and each outcome is recorded in its own short
transaction afterwards. A crash between the send and the record can
deliver the same reminder again after a restart; that rare duplicate
is accepted.

Log lines name the reminder by number and never contain its text.
Nothing is written to the audit log.
"""

import enum
import logging
import sqlite3
import traceback
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from maika.database import GLOBAL_GUILD_ID, transaction
from maika.permissions import BLOCK_KIND_ALL, find_block
from maika.reminders import (
    DELIVERY_CHANNEL,
    DELIVERY_DM,
    list_due,
    mark_delivered,
    mark_failed,
    record_delivery,
    reschedule,
)
from maika.repeat_rules import (
    TIMEZONE_UNAVAILABLE_MESSAGE,
    RepeatRuleError,
    RepeatRuleReason,
    next_occurrence,
)
from maika.reply_helpers import REPLY_PART_SEPARATOR, join_parts, mention

# A child of the "maika" logger, so a warning or error logged here
# reaches the console and, through RouterLogHandler, #errors.
logger = logging.getLogger(__name__)

# The most reminders one check processes. Skipped reminders do not
# count, so a blocked person's rows cannot hold back everyone else's.
DEFAULT_BATCH_SIZE = 50

# A reminder sent more than this many seconds after its due time says
# when it was due.
LATE_AFTER_SECONDS = 300

REMINDER_TEXT = "Reminder: {message}"
LATE_LINE = "This was due <t:{due_at}:R>."
CHANNEL_TEXT = "{mention} {body}"
DM_FAILED_NOTICE = (
    "{mention} I couldn't DM you a reminder. Check `/remind list`."
)


class SendOutcome(enum.Enum):
    """What happened to one message the sender tried to send."""

    SENT = "sent"
    # A permanent refusal: DMs closed or the bot blocked, the channel
    # gone, the bot lost permission, the account gone.
    REFUSED = "refused"
    # A temporary problem: a Discord outage, a network error.
    RETRY = "retry"


@dataclass(frozen=True)
class ChannelAccess:
    """Where a channel is, and whether a person can see it.

    ``server_id`` is None when the channel or its server cannot be
    reached (the bot left, or the channel was deleted).
    ``creator_can_view`` is True only when the person is a member of
    that server and can view that channel.
    """

    server_id: int | None
    creator_can_view: bool


class ReminderSender(Protocol):
    """What delivery needs from Discord.

    Every method reports a temporary problem as RETRY (or None for
    ``channel_access``) instead of raising.
    """

    async def send_dm(self, user_id: int, text: str) -> SendOutcome:
        """Send a DM to the person."""
        ...

    async def post_to_channel(
        self, channel_id: int, user_id: int, text: str
    ) -> SendOutcome:
        """Post in a channel, allowing a mention of ``user_id`` only."""
        ...

    async def post_notice(
        self, channel_id: int, user_id: int, text: str
    ) -> SendOutcome:
        """Post a failure notice that mentions ``user_id`` only."""
        ...

    async def channel_access(
        self, channel_id: int, user_id: int
    ) -> ChannelAccess | None:
        """Return the channel's access, or None if it can't be read."""
        ...


# is_blocked(user_id, server_id): True when an ``all`` block stops
# the bot from messaging the person there (see ``is_blocked``).
type BlockCheck = Callable[[int, int | None], bool]


class ReminderResult(enum.Enum):
    """What one check did with one reminder."""

    DELIVERED = "delivered"
    FAILED = "failed"
    RETRIED = "retried"
    RESCHEDULED = "rescheduled"
    SKIPPED = "skipped"
    # An unexpected exception; the row was left unchanged.
    ERROR = "error"


# Results that leave the row pending and due, so it is still in the
# list of due rows after it was handled.
STILL_DUE_RESULTS = frozenset(
    {ReminderResult.RETRIED, ReminderResult.SKIPPED, ReminderResult.ERROR}
)


@dataclass(frozen=True)
class DeliverySummary:
    """How many reminders one check handled, by result.

    ``rescheduled`` counts repeating reminders moved to their next
    time, whether or not this occurrence reached the person.
    ``errors`` counts reminders whose handling raised unexpectedly.
    """

    delivered: int = 0
    failed: int = 0
    retried: int = 0
    rescheduled: int = 0
    skipped: int = 0
    errors: int = 0


@dataclass(frozen=True)
class Attempt:
    """The final outcome of sending one reminder.

    ``via`` is the route that worked (DELIVERY_DM or
    DELIVERY_CHANNEL) when ``outcome`` is SENT, and None otherwise.
    """

    outcome: SendOutcome
    via: str | None = None


def is_blocked(
    connection: sqlite3.Connection, user_id: int, server_id: int | None
) -> bool:
    """Return True when an ``all`` block stops messages to a person.

    A global ``all`` block always counts. With a ``server_id``, an
    ``all`` block scoped to that server counts too; with None only
    the global block is read. A ``chat`` block never counts.
    """
    scopes = [GLOBAL_GUILD_ID]
    if server_id is not None:
        scopes.append(server_id)
    return any(
        find_block(connection, user_id, scope) == BLOCK_KIND_ALL
        for scope in scopes
    )


def build_block_check(connection: sqlite3.Connection) -> BlockCheck:
    """Return ``is_blocked`` bound to a connection."""

    def check(user_id: int, server_id: int | None) -> bool:
        """Return ``is_blocked`` for this connection."""
        return is_blocked(connection, user_id, server_id)

    return check


class _CheckCache:
    """The sender and block check, with answers kept for one check.

    A person's block and a channel's access rarely change within one
    check, so each is asked at most once per check.
    """

    def __init__(
        self, sender: ReminderSender, block_check: BlockCheck
    ) -> None:
        """Store the sender and the block check, with empty caches."""
        self.sender = sender
        self._block_check = block_check
        self._blocks: dict[tuple[int, int | None], bool] = {}
        self._access: dict[tuple[int, int], ChannelAccess | None] = {}

    def blocked(self, user_id: int, server_id: int | None) -> bool:
        """Return the block check's answer, asking it once."""
        key = (user_id, server_id)
        if key not in self._blocks:
            self._blocks[key] = self._block_check(user_id, server_id)
        return self._blocks[key]

    async def channel_access(
        self, channel_id: int, user_id: int
    ) -> ChannelAccess | None:
        """Return the sender's channel access, asking it once."""
        key = (channel_id, user_id)
        if key not in self._access:
            self._access[key] = await self.sender.channel_access(
                channel_id, user_id
            )
        return self._access[key]


def compose_text(message: str, *, due_at: int, now: int) -> str:
    """Return the reminder's text, with a late line when it is late.

    The late line is added when ``now`` is more than
    ``LATE_AFTER_SECONDS`` past ``due_at``.
    """
    body = REMINDER_TEXT.format(message=message)
    if now - due_at <= LATE_AFTER_SECONDS:
        return body
    late = LATE_LINE.format(due_at=due_at)
    return join_parts(body, [late], REPLY_PART_SEPARATOR)


def _may_post_in_channel(
    access: ChannelAccess, user_id: int, cache: _CheckCache
) -> bool:
    """Return True when the person may be messaged in that channel.

    The person must be able to view the channel and must not be
    blocked in its server.
    """
    if not access.creator_can_view:
        return False
    if access.server_id is not None and cache.blocked(
        user_id, access.server_id
    ):
        return False
    return True


async def _post_dm_failed_notice(row: sqlite3.Row, cache: _CheckCache) -> None:
    """Post the "couldn't DM you" notice where the reminder was set.

    Nothing is posted for a reminder set in a DM, when the channel
    cannot be checked, or when the person may not be messaged there.
    The notice's own outcome is ignored.
    """
    channel_id = row["channel_id"]
    if channel_id is None:
        return
    user_id = row["user_id"]
    access = await cache.channel_access(channel_id, user_id)
    if access is None or not _may_post_in_channel(access, user_id, cache):
        return
    notice = DM_FAILED_NOTICE.format(mention=mention(user_id))
    await cache.sender.post_notice(channel_id, user_id, notice)


async def _send_by_dm(
    row: sqlite3.Row, text: str, cache: _CheckCache, *, notify: bool
) -> Attempt:
    """Send the reminder by DM.

    With ``notify``, a refused DM is followed by the failure notice.
    """
    outcome = await cache.sender.send_dm(row["user_id"], text)
    if outcome is SendOutcome.REFUSED and notify:
        await _post_dm_failed_notice(row, cache)
    if outcome is SendOutcome.SENT:
        return Attempt(outcome, via=DELIVERY_DM)
    return Attempt(outcome)


async def _send_in_channel(
    row: sqlite3.Row, text: str, cache: _CheckCache
) -> Attempt:
    """Post the reminder in its channel, or fall back to a DM.

    An unreachable channel check is RETRY. The post is skipped for a
    person who cannot view the channel or is blocked in its server,
    and a refused post falls back to the DM. No notice follows a
    refused DM here.
    """
    channel_id = row["channel_id"]
    user_id = row["user_id"]
    access = await cache.channel_access(channel_id, user_id)
    if access is None:
        return Attempt(SendOutcome.RETRY)

    if _may_post_in_channel(access, user_id, cache):
        post = CHANNEL_TEXT.format(mention=mention(user_id), body=text)
        outcome = await cache.sender.post_to_channel(channel_id, user_id, post)
        if outcome is SendOutcome.SENT:
            return Attempt(outcome, via=DELIVERY_CHANNEL)
        if outcome is SendOutcome.RETRY:
            return Attempt(outcome)

    return await _send_by_dm(row, text, cache, notify=False)


async def _send(row: sqlite3.Row, text: str, cache: _CheckCache) -> Attempt:
    """Send one reminder by its route.

    A channel reminder with no channel is sent as a DM reminder.
    """
    if row["delivery"] == DELIVERY_CHANNEL and row["channel_id"] is not None:
        return await _send_in_channel(row, text, cache)
    return await _send_by_dm(row, text, cache, notify=True)


def _next_due(row: sqlite3.Row, now: int) -> int | RepeatRuleError:
    """Return a repeating reminder's first occurrence after ``now``.

    When it cannot be calculated, including for a row with no stored
    timezone, the RepeatRuleError is returned instead of raised, so
    the reminder can still be sent before it is marked failed.
    """
    timezone = row["timezone"]
    if timezone is None:
        return RepeatRuleError(
            RepeatRuleReason.TIMEZONE_UNAVAILABLE,
            TIMEZONE_UNAVAILABLE_MESSAGE,
        )
    try:
        return next_occurrence(
            row["repeat_rule"],
            current_due=row["due_at"],
            after=now,
            timezone=timezone,
        )
    except RepeatRuleError as error:
        return error


def _record_one_time(
    connection: sqlite3.Connection,
    reminder_id: int,
    attempt: Attempt,
    *,
    now: int,
) -> ReminderResult:
    """Record a one-time reminder as delivered or failed."""
    with transaction(connection):
        if attempt.via is not None:
            mark_delivered(connection, reminder_id, now=now, via=attempt.via)
        else:
            mark_failed(connection, reminder_id)
    if attempt.via is not None:
        logger.info("Reminder #%d delivered by %s.", reminder_id, attempt.via)
        return ReminderResult.DELIVERED
    logger.warning("Reminder #%d could not be delivered.", reminder_id)
    return ReminderResult.FAILED


def _record_repeating(
    connection: sqlite3.Connection,
    reminder_id: int,
    attempt: Attempt,
    *,
    now: int,
    next_due: int | RepeatRuleError,
) -> ReminderResult:
    """Record a repeating reminder and move it to its next time.

    The delivery time and route are updated only when it was sent.
    When ``next_due`` is an error, the delivery is still recorded,
    then the reminder is marked failed, because it has no next time.
    """
    with transaction(connection):
        if attempt.via is not None:
            record_delivery(connection, reminder_id, now=now, via=attempt.via)
        if isinstance(next_due, RepeatRuleError):
            mark_failed(connection, reminder_id)
        else:
            reschedule(connection, reminder_id, due_at=next_due)

    if attempt.via is not None:
        logger.info("Reminder #%d delivered by %s.", reminder_id, attempt.via)
    else:
        logger.warning("Reminder #%d could not be delivered.", reminder_id)

    if isinstance(next_due, RepeatRuleError):
        logger.warning(
            "Reminder #%d was marked failed: its next time could not be "
            "calculated (%s).",
            reminder_id,
            next_due.reason.value,
        )
        return ReminderResult.FAILED
    return ReminderResult.RESCHEDULED


async def _deliver_one(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    cache: _CheckCache,
    *,
    now: int,
) -> ReminderResult:
    """Send one due reminder and record the outcome.

    A repeating reminder's next time is calculated before sending, so
    a problem with it can never cause a resend at every check.
    """
    reminder_id = row["id"]
    if cache.blocked(row["user_id"], None):
        logger.debug(
            "Reminder #%d skipped: the person is blocked.", reminder_id
        )
        return ReminderResult.SKIPPED

    next_due: int | RepeatRuleError | None = None
    if row["repeat_rule"] is not None:
        next_due = _next_due(row, now)

    text = compose_text(row["message"], due_at=row["due_at"], now=now)
    attempt = await _send(row, text, cache)

    if attempt.outcome is SendOutcome.RETRY:
        logger.info("Reminder #%d will be tried again.", reminder_id)
        return ReminderResult.RETRIED
    if next_due is None:
        return _record_one_time(connection, reminder_id, attempt, now=now)
    return _record_repeating(
        connection, reminder_id, attempt, now=now, next_due=next_due
    )


async def _deliver_guarded(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    cache: _CheckCache,
    *,
    now: int,
) -> ReminderResult:
    """Run ``_deliver_one``, turning an unexpected exception into ERROR.

    The log line names the reminder, the exception type and the
    traceback frames (file, line number, function, source line). The
    exception's own message is left out, because it could contain the
    reminder text. The row stays unchanged.
    """
    try:
        return await _deliver_one(connection, row, cache, now=now)
    except Exception as error:
        # format_tb gives the frames only; the message line that
        # format_exception would add at the end is never built.
        frames = "".join(traceback.format_tb(error.__traceback__))
        logger.error(
            "Delivering reminder #%d raised %s. Traceback, message left "
            "out:\n%s",
            row["id"],
            type(error).__name__,
            frames.rstrip(),
        )
        return ReminderResult.ERROR


def _summary(results: Counter[ReminderResult]) -> DeliverySummary:
    """Return the summary for the results of one check."""
    return DeliverySummary(
        delivered=results[ReminderResult.DELIVERED],
        failed=results[ReminderResult.FAILED],
        retried=results[ReminderResult.RETRIED],
        rescheduled=results[ReminderResult.RESCHEDULED],
        skipped=results[ReminderResult.SKIPPED],
        errors=results[ReminderResult.ERROR],
    )


async def deliver_due_reminders(
    connection: sqlite3.Connection,
    sender: ReminderSender,
    is_blocked: BlockCheck,
    *,
    now: int,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> DeliverySummary:
    """Deliver the reminders due at ``now`` and return a summary.

    Due rows are read a page at a time, soonest first, until
    ``batch_size`` reminders were processed or no due rows remain.
    Skipped reminders do not count toward ``batch_size``. A handled
    row that is still due (skipped, retried, or an error) stays in
    the list, so the next page starts past those rows.
    """
    cache = _CheckCache(sender, is_blocked)
    results: Counter[ReminderResult] = Counter()
    seen: set[int] = set()
    processed = 0
    still_due = 0

    while processed < batch_size:
        rows = list_due(connection, now, batch_size - processed, still_due)
        if not rows:
            break
        for row in rows:
            if processed >= batch_size:
                break
            if row["id"] in seen:
                # Already handled in this check and still due; it
                # shows up again only if another writer shifted the
                # list, so step past it.
                still_due += 1
                continue
            seen.add(row["id"])
            result = await _deliver_guarded(connection, row, cache, now=now)
            results[result] += 1
            if result is not ReminderResult.SKIPPED:
                processed += 1
            if result in STILL_DUE_RESULTS:
                still_due += 1

    return _summary(results)
