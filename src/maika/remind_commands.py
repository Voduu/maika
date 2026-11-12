"""The /remind commands: add, list and cancel.

See ``docs/design.md`` sections 1.2 (the ``remind.create``,
``remind.list`` and ``remind.cancel`` keys), 1.5 (reply visibility),
2.2 (reminders, time forms, repeats) and 9.10 (the ``reminders``
table). The commands are level 0, reply ephemerally and read or
change only the person's own reminders, so nothing is written to the
audit log.

Nothing here reads the clock or touches Discord: the caller passes
``now`` in UTC Unix seconds, the channel, and whether the bot may
post there.
"""

import sqlite3

from maika.commands import CommandCategory, CommandDefinition
from maika.database import transaction
from maika.foundation_settings import (
    MAX_PENDING_REMINDERS_KEY,
    REMINDER_DEFAULT_TIME_KEY,
    build_foundation_registry,
)
from maika.permissions import Level, PermissionKey
from maika.reminders import (
    DELIVERY_CHANNEL,
    DELIVERY_DM,
    CancelOutcome,
    cancel_reminder,
    count_pending,
    create_reminder,
    list_pending,
    list_recently_failed,
)
from maika.repeat_rules import (
    REPEAT_CHOICE_LABELS,
    RepeatChoice,
    RepeatRuleError,
    build_repeat_rule,
    describe_repeat_rule,
)
from maika.reply_helpers import REPLY_PART_SEPARATOR, join_parts
from maika.results import CommandResult, FailureKind
from maika.settings_store import get_value
from maika.time_parser import WhenParseError, parse_when
from maika.user_settings import TIMEZONE_KEY, get_user_setting

SETTINGS = build_foundation_registry()

REMIND_ADD_NAME = "remind add"
REMIND_LIST_NAME = "remind list"
REMIND_CANCEL_NAME = "remind cancel"

REMIND_CREATE_KEY = PermissionKey("remind.create", Level.EVERYONE)
REMIND_LIST_KEY = PermissionKey("remind.list", Level.EVERYONE)
REMIND_CANCEL_KEY = PermissionKey("remind.cancel", Level.EVERYONE)

REMIND_ADD_EXAMPLE = "/remind add when:2h message:submit assignment"

REMIND_ADD_COMMAND = CommandDefinition(
    name=REMIND_ADD_NAME,
    description="Set a reminder",
    category=CommandCategory.REMINDERS_AND_TIME,
    key=REMIND_CREATE_KEY,
    help_text=(
        "Sets a reminder, sent by DM or in this channel, once or on "
        "a repeat. The time can be a duration such as 2h or a clock "
        "time such as tomorrow 9am, read in your timezone. "
        f"Example: {REMIND_ADD_EXAMPLE}"
    ),
    example=REMIND_ADD_EXAMPLE,
)

REMIND_LIST_EXAMPLE = "/remind list"

REMIND_LIST_COMMAND = CommandDefinition(
    name=REMIND_LIST_NAME,
    description="List your reminders",
    category=CommandCategory.REMINDERS_AND_TIME,
    key=REMIND_LIST_KEY,
    help_text=(
        "Lists your pending reminders, and the ones that could not "
        "be delivered in the last 7 days. "
        f"Example: {REMIND_LIST_EXAMPLE}"
    ),
    example=REMIND_LIST_EXAMPLE,
)

REMIND_CANCEL_EXAMPLE = "/remind cancel reminder:12"

REMIND_CANCEL_COMMAND = CommandDefinition(
    name=REMIND_CANCEL_NAME,
    description="Cancel one of your reminders",
    category=CommandCategory.REMINDERS_AND_TIME,
    key=REMIND_CANCEL_KEY,
    help_text=(
        "Cancels one of your pending reminders. Start typing its "
        "text or number to pick it. "
        f"Example: {REMIND_CANCEL_EXAMPLE}"
    ),
    example=REMIND_CANCEL_EXAMPLE,
)

# The longest reminder text, in characters.
MESSAGE_LIMIT = 500

EMPTY_MESSAGE_MESSAGE = (
    f"Say what to remind you about, for example {REMIND_ADD_EXAMPLE}."
)
MESSAGE_TOO_LONG_MESSAGE = (
    f"That reminder is longer than {MESSAGE_LIMIT} characters."
)
NO_SERVER_CHANNEL_MESSAGE = "Channel delivery needs a server channel."
CANNOT_POST_MESSAGE = "I can't post in this channel."
REPEAT_NEEDS_TIMEZONE_MESSAGE = "Set your timezone first with /timezone set."
PENDING_LIMIT_MESSAGE = (
    "You have {count} pending reminders, and the limit is {limit}. "
    "Cancel some of them with /remind cancel to add another."
)

CREATED_MESSAGE = (
    "Okay, I'll remind you on <t:{due}:F> (<t:{due}:R>): {message}"
)
REPEATS_NOTE = "Repeats: {label}."
CHANNEL_NOTE = "I'll post it in <#{channel_id}>."
TIMEZONE_NOTE = "Times are read in {timezone}."

DELIVERY_CHOICES = (DELIVERY_DM, DELIVERY_CHANNEL)

SECONDS_PER_DAY = 24 * 60 * 60
# How far back /remind list shows reminders that were not delivered.
NOT_DELIVERED_DAYS = 7

# A shortened message ends with this mark, counted in its length.
CUT_MARK = "…"
# The longest pending message shown by /remind list, in characters.
LIST_MESSAGE_LIMIT = 100

NO_PENDING_MESSAGE = "You have no pending reminders."
PENDING_HEADING = "Pending reminders"
NOT_DELIVERED_HEADING = f"Not delivered (last {NOT_DELIVERED_DAYS} days)"
SECTION_SEPARATOR = "\n\n"
LINE_SEPARATOR = "\n"
ENTRY_FIELD_SEPARATOR = " · "
ENTRY_NUMBER = "#{id}"
PENDING_TIME = "<t:{due}:f> (<t:{due}:R>)"
NOT_DELIVERED_TIME = "<t:{due}:f>"
DM_ROUTE = "DM"
CHANNEL_ROUTE = "in <#{channel_id}>"
REPEATS_FIELD = "Repeats: {label} ({timezone})"

# The message comes last with no closing period, so a message that
# ends with its own punctuation is not followed by a second one.
CANCELLED_REPLY = "Cancelled reminder #{id}: {message}"
CANCELLED_REPEATING_REPLY = (
    "Cancelled reminder #{id} ({label}), so it will not fire again: {message}"
)
CANCEL_NOT_FOUND_MESSAGE = (
    "I couldn't find a pending reminder with that number."
)
CANCEL_NOT_PENDING_MESSAGE = "That reminder is no longer pending."

# Discord shows at most 25 autocomplete choices, and a choice name is
# at most 100 characters. The message part of a name is shorter, so
# the number always fits.
CHOICE_LIMIT = 25
CHOICE_NAME_LIMIT = 100
CHOICE_MESSAGE_LIMIT = 50
CHOICE_NAME = "#{id} {message}"
NUMBER_PREFIX = "#"


def _check_delivery(
    delivery: str, channel_id: int | None, bot_can_post: bool
) -> CommandResult | None:
    """Return a refusal when the delivery route cannot be used.

    A DM always works here. Channel delivery needs a server channel
    the bot may post in. Raise ValueError for an unknown delivery,
    which is a programming error.
    """
    if delivery not in DELIVERY_CHOICES:
        raise ValueError(
            f"delivery must be {DELIVERY_DM!r} or {DELIVERY_CHANNEL!r}, "
            f"not {delivery!r}."
        )
    if delivery == DELIVERY_DM:
        return None
    if channel_id is None:
        return CommandResult.fail(
            FailureKind.INVALID, NO_SERVER_CHANNEL_MESSAGE
        )
    if not bot_can_post:
        return CommandResult.fail(FailureKind.INVALID, CANNOT_POST_MESSAGE)
    return None


def _repeat_choice(repeat: str | None) -> RepeatChoice | None:
    """Return the RepeatChoice with this name, or None for no repeat.

    Raise ValueError for an unknown name, which is a programming
    error.
    """
    if repeat is None:
        return None
    try:
        return RepeatChoice[repeat]
    except KeyError:
        raise ValueError(f"Unknown repeat choice {repeat!r}.") from None


def _created_reply(
    *,
    message: str,
    due_at: int,
    repeat: RepeatChoice | None,
    delivery: str,
    channel_id: int | None,
    timezone_note: str | None,
) -> str:
    """Return the confirmation, with each note on its own line."""
    notes: list[str] = []
    if repeat is not None:
        notes.append(REPEATS_NOTE.format(label=REPEAT_CHOICE_LABELS[repeat]))
    if delivery == DELIVERY_CHANNEL:
        notes.append(CHANNEL_NOTE.format(channel_id=channel_id))
    if timezone_note is not None:
        notes.append(TIMEZONE_NOTE.format(timezone=timezone_note))
    main = CREATED_MESSAGE.format(due=due_at, message=message)
    return join_parts(main, notes, REPLY_PART_SEPARATOR)


def remind_add(
    connection: sqlite3.Connection,
    *,
    user_id: int,
    when: str,
    message: str,
    delivery: str,
    repeat: str | None,
    channel_id: int | None,
    bot_can_post: bool,
    now: int,
) -> CommandResult:
    """Create a reminder and confirm it.

    ``delivery`` is "dm" or "channel". ``repeat`` is a RepeatChoice
    name, or None for a one-time reminder. ``channel_id`` is the
    channel where the command ran, or None in a DM, and is stored for
    both routes. ``bot_can_post`` says whether the bot may send
    messages in that channel.

    The checks run in order: the message, the delivery route, the
    time, the repeat, then the pending limit. Each refusal is INVALID
    (an empty message is MISSING_ARGUMENT) and saves nothing. The
    limit check and the insert share one transaction. The timezone is
    stored only with a repeat rule.
    """
    text = message.strip()
    if text == "":
        return CommandResult.fail(
            FailureKind.MISSING_ARGUMENT, EMPTY_MESSAGE_MESSAGE
        )
    if len(text) > MESSAGE_LIMIT:
        return CommandResult.fail(
            FailureKind.INVALID, MESSAGE_TOO_LONG_MESSAGE
        )

    refusal = _check_delivery(delivery, channel_id, bot_can_post)
    if refusal is not None:
        return refusal

    timezone = get_user_setting(connection, user_id, TIMEZONE_KEY)
    default_time = get_value(connection, SETTINGS, REMINDER_DEFAULT_TIME_KEY)
    try:
        parsed = parse_when(
            when, now=now, timezone=timezone, default_time=default_time
        )
    except WhenParseError as error:
        return CommandResult.fail(FailureKind.INVALID, error.message)

    choice = _repeat_choice(repeat)
    repeat_rule: str | None = None
    if choice is not None:
        if timezone is None:
            return CommandResult.fail(
                FailureKind.INVALID, REPEAT_NEEDS_TIMEZONE_MESSAGE
            )
        try:
            repeat_rule = build_repeat_rule(
                choice, first_due=parsed.due_at, timezone=timezone
            )
        except RepeatRuleError as error:
            return CommandResult.fail(FailureKind.INVALID, error.message)

    limit = get_value(connection, SETTINGS, MAX_PENDING_REMINDERS_KEY)
    with transaction(connection):
        pending = count_pending(connection, user_id)
        if pending >= limit:
            return CommandResult.fail(
                FailureKind.INVALID,
                PENDING_LIMIT_MESSAGE.format(count=pending, limit=limit),
            )
        create_reminder(
            connection,
            user_id=user_id,
            message=text,
            due_at=parsed.due_at,
            delivery=delivery,
            channel_id=channel_id,
            repeat_rule=repeat_rule,
            timezone=timezone if repeat_rule is not None else None,
            now=now,
        )

    # A clock-based time or a repeat is read in the stored timezone.
    read_in_timezone = not parsed.is_duration or repeat_rule is not None
    return CommandResult.ok(
        _created_reply(
            message=text,
            due_at=parsed.due_at,
            repeat=choice,
            delivery=delivery,
            channel_id=channel_id,
            timezone_note=timezone if read_in_timezone else None,
        )
    )


def _one_line(text: str) -> str:
    """Return text with each run of whitespace made one space."""
    return " ".join(text.split())


def _shorten(text: str, limit: int) -> str:
    """Return text cut to ``limit`` characters, ending with "…"."""
    if len(text) <= limit:
        return text
    return text[: limit - len(CUT_MARK)] + CUT_MARK


def _entry_line(row: sqlite3.Row, *, time_text: str, message: str) -> str:
    """Return one /remind list line for a reminder row.

    The fields are the number, the time, the message, the route, and
    for a repeating reminder its label and timezone.
    """
    if row["delivery"] == DELIVERY_CHANNEL:
        route = CHANNEL_ROUTE.format(channel_id=row["channel_id"])
    else:
        route = DM_ROUTE
    fields = [ENTRY_NUMBER.format(id=row["id"]), time_text, message, route]
    if row["repeat_rule"] is not None:
        fields.append(
            REPEATS_FIELD.format(
                label=describe_repeat_rule(row["repeat_rule"]),
                timezone=row["timezone"],
            )
        )
    return ENTRY_FIELD_SEPARATOR.join(fields)


def _pending_line(row: sqlite3.Row) -> str:
    """Return a pending reminder's line, its message on one line."""
    return _entry_line(
        row,
        time_text=PENDING_TIME.format(due=row["due_at"]),
        message=_shorten(_one_line(row["message"]), LIST_MESSAGE_LIMIT),
    )


def _not_delivered_line(row: sqlite3.Row) -> str:
    """Return a not-delivered reminder's line, with the full message.

    The text is shown whole because this list is the only place a
    person can read a reminder that never reached them.
    """
    return _entry_line(
        row,
        time_text=NOT_DELIVERED_TIME.format(due=row["due_at"]),
        message=row["message"],
    )


def _section(heading: str, lines: list[str]) -> str:
    """Return a heading followed by one line per entry."""
    return LINE_SEPARATOR.join([heading, *lines])


def remind_list(
    connection: sqlite3.Connection, *, user_id: int, now: int
) -> CommandResult:
    """List a person's pending and recently undelivered reminders.

    The reply has a "Pending reminders" section and a "Not delivered
    (last 7 days)" section, each left out when empty and each soonest
    first. With no pending reminders the reply starts with "You have
    no pending reminders." A long reply is split into several
    messages when it is sent.
    """
    pending = list_pending(connection, user_id)
    since = now - NOT_DELIVERED_DAYS * SECONDS_PER_DAY
    failed = list_recently_failed(connection, user_id, since=since)

    sections: list[str] = []
    if pending:
        sections.append(
            _section(PENDING_HEADING, [_pending_line(row) for row in pending])
        )
    else:
        sections.append(NO_PENDING_MESSAGE)
    if failed:
        sections.append(
            _section(
                NOT_DELIVERED_HEADING,
                [_not_delivered_line(row) for row in failed],
            )
        )
    return CommandResult.ok(SECTION_SEPARATOR.join(sections))


def remind_cancel(
    connection: sqlite3.Connection,
    *,
    user_id: int,
    reminder_id: int,
    now: int,
) -> CommandResult:
    """Cancel one of a person's own pending reminders.

    A missing reminder or someone else's is NOT_FOUND, with the same
    message, so the reply says nothing about other people's
    reminders. The person's own reminder in another status is
    INVALID. There is no confirm step and no audit row. ``now`` is
    unused for now and kept so every /remind command has the same
    shape.
    """
    with transaction(connection):
        cancelled = cancel_reminder(
            connection, reminder_id=reminder_id, user_id=user_id
        )

    match cancelled.outcome:
        case CancelOutcome.NOT_FOUND:
            return CommandResult.fail(
                FailureKind.NOT_FOUND, CANCEL_NOT_FOUND_MESSAGE
            )
        case CancelOutcome.NOT_PENDING:
            return CommandResult.fail(
                FailureKind.INVALID, CANCEL_NOT_PENDING_MESSAGE
            )
        case CancelOutcome.CANCELLED:
            if cancelled.repeat_rule is None:
                return CommandResult.ok(
                    CANCELLED_REPLY.format(
                        id=reminder_id, message=cancelled.message
                    )
                )
            return CommandResult.ok(
                CANCELLED_REPEATING_REPLY.format(
                    id=reminder_id,
                    label=describe_repeat_rule(cancelled.repeat_rule),
                    message=cancelled.message,
                )
            )


def _matches(row: sqlite3.Row, text: str) -> bool:
    """Return True when a reminder matches the typed text.

    The text matches the message without regard to case. Digits,
    with or without a leading "#", also match a number that starts
    with them.
    """
    if text in _one_line(row["message"]).lower():
        return True
    digits = text.removeprefix(NUMBER_PREFIX)
    return digits.isdecimal() and str(row["id"]).startswith(digits)


def _choice_name(row: sqlite3.Row) -> str:
    """Return "#<id> <message>", the message cut to 50 characters."""
    message = _shorten(_one_line(row["message"]), CHOICE_MESSAGE_LIMIT)
    name = CHOICE_NAME.format(id=row["id"], message=message)
    return _shorten(name, CHOICE_NAME_LIMIT)


def reminder_choices(
    connection: sqlite3.Connection, *, user_id: int, current: str
) -> list[tuple[str, int]]:
    """Return autocomplete choices for a person's pending reminders.

    Each choice is a (name, reminder id) pair, soonest first, at most
    25. ``current`` is the typed text; with nothing typed every
    pending reminder is offered (up to the limit). Only the person's
    own rows are read, and nothing is written.
    """
    text = _one_line(current).lower()
    rows = list_pending(connection, user_id)
    if text != "":
        rows = [row for row in rows if _matches(row, text)]
    return [(_choice_name(row), row["id"]) for row in rows[:CHOICE_LIMIT]]
