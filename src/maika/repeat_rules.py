"""Repeat rules for /remind: build the rule text, find the next time.

See ``docs/design.md`` sections 2.2 (Repeating reminders) and 9.10
(the ``repeat_rule`` and ``timezone`` columns). A rule is RFC 5545
recurrence text with no DTSTART. Every rule names its clock time
(BYHOUR, BYMINUTE, BYSECOND=0), so a reminder moved by daylight saving
returns to its clock time on the next occurrence.

Occurrences are computed with python-dateutil on local wall times in
the reminder's timezone, then turned into UTC with
``maika.local_time.local_to_utc``.

Nothing here reads the clock or touches the database: the caller
passes the times in UTC Unix seconds.
"""

import datetime
import enum
import zoneinfo

from dateutil import rrule

from maika.local_time import local_to_utc

# RFC 5545 weekday codes, in Python's weekday order (Monday is 0).
WEEKDAY_CODES = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")
WORKDAY_CODES = WEEKDAY_CODES[:5]

# Days 1 to 28 exist in every month. A later day falls back to the
# last day of the months that lack it.
LAST_DAY_IN_EVERY_MONTH = 28
LEAP_DAY_MONTH = 2
LEAP_DAY = 29

BIWEEKLY_INTERVAL = 2

RULE_SEPARATOR = ";"

# How far before ``after`` the search starts, as local wall time. A
# local wall time and its UTC moment can be out of step by the change
# in UTC offset (an hour for daylight saving, a day at most when a
# zone moves across the date line), so starting two days early never
# skips an occurrence. The work is bounded by the occurrences in this
# window, not by how far ``after`` is past ``current_due``.
SEARCH_MARGIN = datetime.timedelta(days=2)

TIMEZONE_UNAVAILABLE_MESSAGE = "That reminder's timezone could not be loaded."
INVALID_RULE_MESSAGE = "That reminder's repeat rule could not be read."
NO_NEXT_OCCURRENCE_MESSAGE = "That reminder's repeat rule has no next time."


class RepeatChoice(enum.Enum):
    """The repeat patterns offered in the /remind dropdown."""

    DAILY = "daily"
    WEEKDAYS = "weekdays"
    WEEKLY = "weekly"
    BIWEEKLY = "biweekly"
    MONTHLY = "monthly"
    LAST_DAY = "last_day"
    YEARLY = "yearly"


REPEAT_CHOICE_LABELS = {
    RepeatChoice.DAILY: "Daily",
    RepeatChoice.WEEKDAYS: "Weekdays (Mon to Fri)",
    RepeatChoice.WEEKLY: "Weekly",
    RepeatChoice.BIWEEKLY: "Every 2 weeks",
    RepeatChoice.MONTHLY: "Monthly",
    RepeatChoice.LAST_DAY: "Last day of the month",
    RepeatChoice.YEARLY: "Yearly",
}


class RepeatRuleReason(enum.Enum):
    """Why a repeat rule could not be built or followed."""

    INVALID_RULE = "invalid_rule"
    TIMEZONE_UNAVAILABLE = "timezone_unavailable"


class RepeatRuleError(Exception):
    """Raised when a repeat rule cannot be built or followed.

    ``message`` is a plain sentence a command can show the person, and
    ``reason`` says which rule failed.
    """

    def __init__(self, reason: RepeatRuleReason, message: str) -> None:
        """Store the reason and the message."""
        self.reason = reason
        self.message = message
        super().__init__(message)


def _load_zone(name: str) -> zoneinfo.ZoneInfo:
    """Load a reminder's timezone, or raise TIMEZONE_UNAVAILABLE."""
    try:
        return zoneinfo.ZoneInfo(name)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError) as error:
        raise RepeatRuleError(
            RepeatRuleReason.TIMEZONE_UNAVAILABLE,
            TIMEZONE_UNAVAILABLE_MESSAGE,
        ) from error


def _local_wall_time(
    seconds: int, zone: zoneinfo.ZoneInfo
) -> datetime.datetime:
    """Return UTC seconds as a local wall time with no timezone."""
    local = datetime.datetime.fromtimestamp(seconds, tz=zone)
    return local.replace(tzinfo=None)


def _time_parts(local: datetime.datetime) -> list[str]:
    """Return the BYHOUR, BYMINUTE and BYSECOND parts for a time."""
    return [f"BYHOUR={local.hour}", f"BYMINUTE={local.minute}", "BYSECOND=0"]


def _day_or_last_day(day: int) -> list[str]:
    """Return the BYMONTHDAY parts for a day of the month.

    Days 1 to 28 are plain. A later day also names the last day (-1)
    and keeps the first of the two in each period (BYSETPOS=1), so a
    month without that day uses its last day instead.
    """
    if day <= LAST_DAY_IN_EVERY_MONTH:
        return [f"BYMONTHDAY={day}"]
    return [f"BYMONTHDAY={day},-1", "BYSETPOS=1"]


def _pattern_parts(
    choice: RepeatChoice, local: datetime.datetime
) -> list[str]:
    """Return the rule parts before the time parts for a choice."""
    weekday = WEEKDAY_CODES[local.weekday()]
    match choice:
        case RepeatChoice.DAILY:
            # FREQ=DAILY
            return ["FREQ=DAILY"]
        case RepeatChoice.WEEKDAYS:
            # FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR
            return ["FREQ=WEEKLY", f"BYDAY={','.join(WORKDAY_CODES)}"]
        case RepeatChoice.WEEKLY:
            # FREQ=WEEKLY;BYDAY=<weekday>
            return ["FREQ=WEEKLY", f"BYDAY={weekday}"]
        case RepeatChoice.BIWEEKLY:
            # FREQ=WEEKLY;INTERVAL=2;BYDAY=<weekday>
            return [
                "FREQ=WEEKLY",
                f"INTERVAL={BIWEEKLY_INTERVAL}",
                f"BYDAY={weekday}",
            ]
        case RepeatChoice.MONTHLY:
            # FREQ=MONTHLY;BYMONTHDAY=<day>, or for days 29 to 31
            # FREQ=MONTHLY;BYMONTHDAY=<day>,-1;BYSETPOS=1
            return ["FREQ=MONTHLY", *_day_or_last_day(local.day)]
        case RepeatChoice.LAST_DAY:
            # FREQ=MONTHLY;BYMONTHDAY=-1
            return ["FREQ=MONTHLY", "BYMONTHDAY=-1"]
        case RepeatChoice.YEARLY:
            # FREQ=YEARLY;BYMONTH=<month>;BYMONTHDAY=<day>, or on
            # February 29
            # FREQ=YEARLY;BYMONTH=2;BYMONTHDAY=29,-1;BYSETPOS=1
            if (local.month, local.day) == (LEAP_DAY_MONTH, LEAP_DAY):
                day_parts = _day_or_last_day(local.day)
            else:
                day_parts = [f"BYMONTHDAY={local.day}"]
            return ["FREQ=YEARLY", f"BYMONTH={local.month}", *day_parts]


def build_repeat_rule(
    choice: RepeatChoice, *, first_due: int, timezone: str
) -> str:
    """Return the stored rule text for a dropdown choice.

    The weekday, day of the month, month and clock time come from
    ``first_due`` (UTC seconds) read in ``timezone``. Seconds are
    always zero in the rule. Raise TIMEZONE_UNAVAILABLE when the
    timezone does not load.
    """
    zone = _load_zone(timezone)
    local = _local_wall_time(first_due, zone)
    parts = _pattern_parts(choice, local) + _time_parts(local)
    return RULE_SEPARATOR.join(parts)


def _read_rule(rule: str, start: datetime.datetime) -> rrule.rrulebase:
    """Return the dateutil rule for the text, starting at start.

    Raise INVALID_RULE when dateutil cannot read the text.
    """
    try:
        return rrule.rrulestr(rule, dtstart=start)
    except ValueError as error:
        raise RepeatRuleError(
            RepeatRuleReason.INVALID_RULE, INVALID_RULE_MESSAGE
        ) from error


def next_occurrence(
    rule: str, *, current_due: int, after: int, timezone: str
) -> int:
    """Return the first occurrence strictly after ``after``.

    ``current_due`` is the occurrence just delivered, in UTC seconds.
    Read as a local wall time in ``timezone``, it is the rule's start,
    so an every-2-weeks rule keeps its weeks. Each local occurrence is
    turned into UTC with the daylight saving rules of local_to_utc.

    After downtime, ``after`` may be far past ``current_due``. The
    search then starts just before ``after`` with dateutil's own
    search, so only a few occurrences are checked here, and an
    overdue reminder skips to its next future time.

    Raise INVALID_RULE when dateutil cannot read the rule or it has no
    later occurrence, and TIMEZONE_UNAVAILABLE when the timezone does
    not load.
    """
    zone = _load_zone(timezone)
    start = _local_wall_time(current_due, zone)
    recurrence = _read_rule(rule, start)
    search_from = _local_wall_time(after, zone) - SEARCH_MARGIN
    for occurrence in recurrence.xafter(search_from):
        due_at = local_to_utc(occurrence.date(), occurrence.time(), zone)
        if due_at > after:
            return due_at
    raise RepeatRuleError(
        RepeatRuleReason.INVALID_RULE, NO_NEXT_OCCURRENCE_MESSAGE
    )
