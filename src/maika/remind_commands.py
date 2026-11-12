"""The /remind commands. This slice adds /remind add.

See ``docs/design.md`` sections 1.2 (the ``remind.create`` key), 1.5
(reply visibility), 2.2 (reminders, time forms, repeats) and 9.10
(the ``reminders`` table). The command is level 0, replies
ephemerally and changes only the person's own reminders, so nothing
is written to the audit log.

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
    count_pending,
    create_reminder,
)
from maika.repeat_rules import (
    REPEAT_CHOICE_LABELS,
    RepeatChoice,
    RepeatRuleError,
    build_repeat_rule,
)
from maika.reply_helpers import REPLY_PART_SEPARATOR, join_parts
from maika.results import CommandResult, FailureKind
from maika.settings_store import get_value
from maika.time_parser import WhenParseError, parse_when
from maika.user_settings import TIMEZONE_KEY, get_user_setting

SETTINGS = build_foundation_registry()

REMIND_ADD_NAME = "remind add"

REMIND_CREATE_KEY = PermissionKey("remind.create", Level.EVERYONE)

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
