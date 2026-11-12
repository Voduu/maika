"""The personal settings commands: /timezone and /birthday.

See ``docs/design.md`` sections 1.2 (the ``prefs.view`` and
``prefs.set`` keys), 1.5 (reply visibility), 2.2 (timezones) and 9.11
(``user_settings``). Every command here is level 0, replies
ephemerally and changes only the person's own settings, so nothing is
written to the audit log.

Nothing here reads the clock: the caller passes ``now`` in UTC Unix
seconds.
"""

import calendar
import datetime
import sqlite3
import zoneinfo

from maika.commands import CommandCategory, CommandDefinition
from maika.permissions import Level, PermissionKey
from maika.results import CommandResult, FailureKind
from maika.user_settings import (
    BIRTHDAY_KEY,
    LEAP_YEAR,
    TIMEZONE_KEY,
    UserSettingError,
    get_user_setting,
    set_user_setting,
)

TIMEZONE_SET_NAME = "timezone set"
TIMEZONE_SHOW_NAME = "timezone show"
BIRTHDAY_SET_NAME = "birthday set"

PREFS_VIEW_KEY = PermissionKey("prefs.view", Level.EVERYONE)
PREFS_SET_KEY = PermissionKey("prefs.set", Level.EVERYONE)

TIMEZONE_SET_COMMAND = CommandDefinition(
    name=TIMEZONE_SET_NAME,
    description="Set your timezone",
    category=CommandCategory.REMINDERS_AND_TIME,
    key=PREFS_SET_KEY,
    help_text=(
        "Sets your timezone, used to read clock times such as "
        '"tomorrow 9am" in reminders. Start typing a city to pick '
        "one. Example: /timezone set name:America/Los_Angeles"
    ),
    example="/timezone set name:America/Los_Angeles",
)

TIMEZONE_SHOW_COMMAND = CommandDefinition(
    name=TIMEZONE_SHOW_NAME,
    description="Show your timezone and the time there now",
    category=CommandCategory.REMINDERS_AND_TIME,
    key=PREFS_VIEW_KEY,
    help_text=(
        "Shows the timezone you set and the current time there. "
        "Example: /timezone show"
    ),
    example="/timezone show",
)

BIRTHDAY_SET_COMMAND = CommandDefinition(
    name=BIRTHDAY_SET_NAME,
    description="Set your birthday (month and day)",
    category=CommandCategory.REMINDERS_AND_TIME,
    key=PREFS_SET_KEY,
    help_text=(
        "Sets your birthday as a month and a day, with no year. "
        "Example: /birthday set month:March day:14"
    ),
    example="/birthday set month:March day:14",
)

TIMEZONE_SET_MESSAGE = "Your timezone is set to {name}."
TIMEZONE_SHOW_MESSAGE = "Your timezone is {name}. It is {time} there now."
NO_TIMEZONE_MESSAGE = "You have not set a timezone. Use /timezone set."
BIRTHDAY_SET_MESSAGE = "Your birthday is set to {month_name} {day}."
MONTH_TOO_SHORT_MESSAGE = "{month_name} has only {days} days."
MONTH_RANGE_MESSAGE = "The month must be from 1 to 12."
DAY_RANGE_MESSAGE = "The day must be from 1 to 31."

# In month order, so MONTH_NAMES[month - 1] is the month's name. Fixed
# English names rather than calendar.month_name, which follows the
# machine's locale.
MONTH_NAMES = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)

FIRST_MONTH = 1
LAST_MONTH = len(MONTH_NAMES)
FIRST_DAY = 1
LAST_DAY = 31

MONTH_DAY_SEPARATOR = "-"

# 12-hour clock: "4:23 PM", "12:05 AM".
HOURS_PER_HALF_DAY = 12
NOON_HOUR = 12
AM_TEXT = "AM"
PM_TEXT = "PM"
CLOCK_TEXT = "{hour}:{minute:02d} {period}"

# Discord shows at most 25 autocomplete choices, and a choice name is
# at most 100 characters.
TIMEZONE_CHOICE_LIMIT = 25
TIMEZONE_NAME_LIMIT = 100

# Offered when nothing has been typed yet, in this order.
COMMON_TIMEZONES = (
    "America/Los_Angeles",
    "America/Denver",
    "America/Chicago",
    "America/New_York",
    "Europe/London",
    "Europe/Paris",
    "Asia/Tokyo",
    "Australia/Sydney",
    "UTC",
)

# Autocomplete suggests only names in these regions, plus the names
# in SUGGESTED_EXACT_TIMEZONES. Old aliases such as US/Pacific, the
# Etc/ zones and the posix/ and right/ copies are left out of the
# suggestions, but /timezone set still accepts them.
SUGGESTED_TIMEZONE_PREFIXES = (
    "Africa/",
    "America/",
    "Antarctica/",
    "Arctic/",
    "Asia/",
    "Atlantic/",
    "Australia/",
    "Europe/",
    "Indian/",
    "Pacific/",
)
SUGGESTED_EXACT_TIMEZONES = frozenset({"UTC"})


def _is_suggested(name: str) -> bool:
    """Return True when autocomplete may suggest a timezone name."""
    if len(name) >= TIMEZONE_NAME_LIMIT:
        return False
    return name in SUGGESTED_EXACT_TIMEZONES or name.startswith(
        SUGGESTED_TIMEZONE_PREFIXES
    )


# Built once: reading the timezone list scans the tzdata files.
_SUGGESTED_TIMEZONES = tuple(
    sorted(
        name for name in zoneinfo.available_timezones() if _is_suggested(name)
    )
)


def clock_text(moment: datetime.datetime) -> str:
    """Return a time on the 12-hour clock, such as "4:23 PM"."""
    period = AM_TEXT if moment.hour < NOON_HOUR else PM_TEXT
    hour = moment.hour % HOURS_PER_HALF_DAY
    if hour == 0:
        hour = HOURS_PER_HALF_DAY
    return CLOCK_TEXT.format(hour=hour, minute=moment.minute, period=period)


def timezone_name_matches(current: str) -> list[str]:
    """Return up to 25 timezone names that match the typed text.

    Only suggested names are considered (``_is_suggested``). The text
    is matched anywhere in the name, without regard to capitalization
    or surrounding spaces. Names that start with the text come first,
    then the other matches, each group sorted. With nothing typed,
    the common zones are returned.
    """
    text = current.strip().lower()
    if text == "":
        known = set(_SUGGESTED_TIMEZONES)
        return [name for name in COMMON_TIMEZONES if name in known]

    starts: list[str] = []
    contains: list[str] = []
    for name in _SUGGESTED_TIMEZONES:
        lowered = name.lower()
        if lowered.startswith(text):
            starts.append(name)
        elif text in lowered:
            contains.append(name)
    return (starts + contains)[:TIMEZONE_CHOICE_LIMIT]


def timezone_set(
    connection: sqlite3.Connection, *, user_id: int, name: str, now: int
) -> CommandResult:
    """Store the person's timezone under its canonical IANA name.

    An unknown or empty name is INVALID, with the plain message from
    the user_settings store.
    """
    try:
        stored = set_user_setting(connection, user_id, TIMEZONE_KEY, name, now)
    except UserSettingError as error:
        return CommandResult.fail(FailureKind.INVALID, error.message)
    return CommandResult.ok(TIMEZONE_SET_MESSAGE.format(name=stored))


def timezone_show(
    connection: sqlite3.Connection, *, user_id: int, now: int
) -> CommandResult:
    """Show the person's timezone and the current time there.

    With no timezone stored, the reply says how to set one. That is
    a success, not a failure.
    """
    name = get_user_setting(connection, user_id, TIMEZONE_KEY)
    if name is None:
        return CommandResult.ok(NO_TIMEZONE_MESSAGE)
    local = datetime.datetime.fromtimestamp(now, tz=zoneinfo.ZoneInfo(name))
    return CommandResult.ok(
        TIMEZONE_SHOW_MESSAGE.format(name=name, time=clock_text(local))
    )


def birthday_set(
    connection: sqlite3.Connection,
    *,
    user_id: int,
    month: int,
    day: int,
    now: int,
) -> CommandResult:
    """Store the person's birthday as MM-DD text.

    The reply names the month and day, with no year. An impossible
    date is INVALID: a day past the end of the month replies, for
    example, "February has only 29 days." (February counts as 29).
    A month or day outside the option ranges, which Discord does not
    send, is INVALID with a plain range message.
    """
    if not FIRST_MONTH <= month <= LAST_MONTH:
        return CommandResult.fail(FailureKind.INVALID, MONTH_RANGE_MESSAGE)
    if day < FIRST_DAY:
        return CommandResult.fail(FailureKind.INVALID, DAY_RANGE_MESSAGE)
    month_name = MONTH_NAMES[month - 1]
    _, days_in_month = calendar.monthrange(LEAP_YEAR, month)
    if day > days_in_month:
        return CommandResult.fail(
            FailureKind.INVALID,
            MONTH_TOO_SHORT_MESSAGE.format(
                month_name=month_name, days=days_in_month
            ),
        )

    text = f"{month:02d}{MONTH_DAY_SEPARATOR}{day:02d}"
    try:
        stored = set_user_setting(connection, user_id, BIRTHDAY_KEY, text, now)
    except UserSettingError as error:
        return CommandResult.fail(FailureKind.INVALID, error.message)
    month_text, day_text = stored.split(MONTH_DAY_SEPARATOR)
    return CommandResult.ok(
        BIRTHDAY_SET_MESSAGE.format(
            month_name=MONTH_NAMES[int(month_text) - 1], day=int(day_text)
        )
    )
