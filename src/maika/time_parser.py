"""Read the ``when`` text of /remind as a UTC time.

See ``docs/design.md`` section 2.2 (Time forms and Timezones). The one
public function is ``parse_when``. It accepts durations (``2h``,
``in an hour``), a clock time alone (``9am``), a day word, weekday or
date with an optional clock time (``tomorrow 9am``, ``next friday``,
``nov 20 3pm``). Durations need no timezone; every other form is read
in the person's timezone.

Nothing here reads the clock or touches the database: the caller
passes ``now`` in UTC Unix seconds and the stored timezone name.
"""

import calendar
import datetime
import enum
import re
import zoneinfo
from dataclasses import dataclass

from maika.local_time import local_to_utc
from maika.month_names import MONTH_NAMES, MONTH_TOO_SHORT_MESSAGE
from maika.user_settings import LEAP_YEAR

SECONDS_PER_MINUTE = 60
SECONDS_PER_HOUR = 60 * SECONDS_PER_MINUTE
SECONDS_PER_DAY = 24 * SECONDS_PER_HOUR
DAYS_PER_WEEK = 7
SECONDS_PER_WEEK = DAYS_PER_WEEK * SECONDS_PER_DAY

# A reminder may be due at most this many years after now.
MAX_YEARS_AHEAD = 10

FIRST_MONTH = 1
LAST_MONTH = len(MONTH_NAMES)
FIRST_DAY = 1
LAST_DAY = 31

# 12-hour clock hours run 1 to 12; 24-hour clock hours run 0 to 23.
FIRST_TWELVE_HOUR = 1
LAST_TWELVE_HOUR = 12
HOURS_PER_HALF_DAY = 12
LAST_TWENTY_FOUR_HOUR = 23
LAST_MINUTE = 59

# How many dates a year-less date may try: this year's, then the next
# one that has that day (for February 29, the next leap year).
YEARLESS_CANDIDATES = 2

# Month abbreviations are the first three letters, plus "sept".
MONTH_ABBREVIATION_LENGTH = 3
SEPTEMBER = 9

UNREADABLE_MESSAGE = (
    "I couldn't read that time. Try 2h, tomorrow 9am, friday 3pm, "
    "or 2026-11-20 14:30."
)
NEEDS_TIMEZONE_MESSAGE = "Set your timezone first with /timezone set."
PAST_MESSAGE = "That time has already passed."
TOO_FAR_MESSAGE = (
    f"That is more than {MAX_YEARS_AHEAD} years away. Pick a time "
    f"within {MAX_YEARS_AHEAD} years."
)

LEADING_IN = "in "
LEADING_AT = "at "

# Seconds in one of each duration unit, by every accepted spelling.
DURATION_UNITS = {
    "m": SECONDS_PER_MINUTE,
    "min": SECONDS_PER_MINUTE,
    "mins": SECONDS_PER_MINUTE,
    "minute": SECONDS_PER_MINUTE,
    "minutes": SECONDS_PER_MINUTE,
    "h": SECONDS_PER_HOUR,
    "hr": SECONDS_PER_HOUR,
    "hrs": SECONDS_PER_HOUR,
    "hour": SECONDS_PER_HOUR,
    "hours": SECONDS_PER_HOUR,
    "d": SECONDS_PER_DAY,
    "day": SECONDS_PER_DAY,
    "days": SECONDS_PER_DAY,
    "w": SECONDS_PER_WEEK,
    "wk": SECONDS_PER_WEEK,
    "wks": SECONDS_PER_WEEK,
    "week": SECONDS_PER_WEEK,
    "weeks": SECONDS_PER_WEEK,
}

# The unit words that may follow "a" or "an" ("an hour", "a day").
ARTICLE_UNITS = {
    "minute": SECONDS_PER_MINUTE,
    "hour": SECONDS_PER_HOUR,
    "day": SECONDS_PER_DAY,
    "week": SECONDS_PER_WEEK,
}

# Python's weekday numbers: Monday is 0 and Sunday is 6.
WEEKDAYS = {
    "monday": 0,
    "mon": 0,
    "tuesday": 1,
    "tue": 1,
    "tues": 1,
    "wednesday": 2,
    "wed": 2,
    "thursday": 3,
    "thu": 3,
    "thur": 3,
    "thurs": 3,
    "friday": 4,
    "fri": 4,
    "saturday": 5,
    "sat": 5,
    "sunday": 6,
    "sun": 6,
}

# Days after today for each day word.
DAY_WORDS = {"today": 0, "tomorrow": 1}

THIS_WORD = "this"
NEXT_WORD = "next"

NAMED_CLOCK_TIMES = {
    "noon": datetime.time(12, 0),
    "midnight": datetime.time(0, 0),
}
AM_TEXT = "am"
PM_TEXT = "pm"

# One duration part: digits and a unit ("90 minutes", "1h"), or "a"
# or "an" and a unit word ("an hour").
DURATION_PART_PATTERN = re.compile(
    r"(?P<amount>[0-9]+) ?(?P<unit>[a-z]+)|an? (?P<article_unit>[a-z]+)"
)
# Between duration parts: " and ", one space, or nothing.
DURATION_JOINER_PATTERN = re.compile(r" and | ?")

# H am, H:MM pm, H pm with an optional space before am or pm.
TWELVE_HOUR_PATTERN = re.compile(
    r"(?P<hour>[0-9]{1,2})(?::(?P<minute>[0-9]{2}))? ?(?P<period>am|pm)"
)
# H:MM or HH:MM on the 24-hour clock.
TWENTY_FOUR_HOUR_PATTERN = re.compile(
    r"(?P<hour>[0-9]{1,2}):(?P<minute>[0-9]{2})"
)

# A day part, an optional "at", then the clock part: the last word,
# or the last two when the last is am or pm ("friday at 3 pm").
DAY_THEN_CLOCK_PATTERN = re.compile(
    r"(?P<day>.+?)(?: at)? (?P<clock>\S+(?: (?:am|pm))?)"
)

# YYYY-MM-DD or YYYY/MM/DD, with one- or two-digit month and day.
YEAR_FIRST_PATTERN = re.compile(
    r"(?P<year>[0-9]{4})(?P<separator>[-/])"
    r"(?P<month>[0-9]{1,2})(?P=separator)(?P<day>[0-9]{1,2})"
)
# MM-DD-YYYY or MM/DD/YYYY.
YEAR_LAST_PATTERN = re.compile(
    r"(?P<month>[0-9]{1,2})(?P<separator>[-/])"
    r"(?P<day>[0-9]{1,2})(?P=separator)(?P<year>[0-9]{4})"
)
# MM-DD or MM/DD.
NO_YEAR_PATTERN = re.compile(r"(?P<month>[0-9]{1,2})[-/](?P<day>[0-9]{1,2})")
# A month name, a day with an optional ordinal ending, and an
# optional four-digit year: "nov 20", "november 20th 2026".
MONTH_NAME_PATTERN = re.compile(
    r"(?P<month>[a-z]+) (?P<day>[0-9]{1,2})(?:st|nd|rd|th)?"
    r"(?: (?P<year>[0-9]{4}))?"
)


class WhenParseReason(enum.Enum):
    """Why a ``when`` text could not be turned into a time."""

    UNREADABLE = "unreadable"
    NEEDS_TIMEZONE = "needs_timezone"
    PAST = "past"
    INVALID_DATE = "invalid_date"
    TOO_FAR = "too_far"


class WhenParseError(Exception):
    """Raised when a ``when`` text does not give a usable time.

    ``message`` is a plain reply a command can show the person, and
    ``reason`` says which rule failed.
    """

    def __init__(self, reason: WhenParseReason, message: str) -> None:
        """Store the reason and the message."""
        self.reason = reason
        self.message = message
        super().__init__(message)


@dataclass(frozen=True)
class ParsedWhen:
    """The time a ``when`` text names.

    ``due_at`` is in UTC Unix seconds. ``is_duration`` is True for
    the duration forms, which need no timezone.
    """

    due_at: int
    is_duration: bool


@dataclass(frozen=True)
class _RelativeDay:
    """Today or tomorrow, as a number of days after today."""

    days_ahead: int


@dataclass(frozen=True)
class _Weekday:
    """A weekday, either the coming one or the one next week."""

    weekday: int
    following_week: bool


@dataclass(frozen=True)
class _CalendarDate:
    """A month and day, with a year or without one."""

    month: int
    day: int
    year: int | None


_DayForm = _RelativeDay | _Weekday | _CalendarDate


def _build_month_numbers() -> dict[str, int]:
    """Map each month name and abbreviation to its month number."""
    numbers: dict[str, int] = {}
    for number, name in enumerate(MONTH_NAMES, start=FIRST_MONTH):
        lowered = name.lower()
        numbers[lowered] = number
        numbers[lowered[:MONTH_ABBREVIATION_LENGTH]] = number
    numbers["sept"] = SEPTEMBER
    return numbers


MONTH_NUMBERS = _build_month_numbers()


def _unreadable() -> WhenParseError:
    """Return the error for text that matches no form."""
    return WhenParseError(WhenParseReason.UNREADABLE, UNREADABLE_MESSAGE)


def _normalize(text: str) -> str:
    """Return the text lowercased, without commas or a leading "in".

    Runs of spaces become one space. The word "at" is handled where
    it may appear: between a day and a time (DAY_THEN_CLOCK_PATTERN)
    and in front of a clock time alone (_parse_clock_form).
    """
    normalized = " ".join(text.lower().replace(",", " ").split())
    if normalized.startswith(LEADING_IN):
        normalized = normalized.removeprefix(LEADING_IN)
    return normalized


# Grammar: part (joiner part)*, where a part is NUMBER UNIT or
# a/an UNIT-WORD, and a joiner is " and ", a space or nothing.
def _parse_duration(text: str) -> int | None:
    """Return a duration's length in seconds, or None if it is not one.

    A zero length is returned as 0, so the caller can refuse it.
    """
    position = 0
    total = 0
    parts = 0
    while position < len(text):
        if parts > 0:
            joiner = DURATION_JOINER_PATTERN.match(text, position)
            if joiner is not None:
                position = joiner.end()
        part = DURATION_PART_PATTERN.match(text, position)
        if part is None:
            return None
        if part["amount"] is not None:
            unit_seconds = DURATION_UNITS.get(part["unit"])
            amount = int(part["amount"])
        else:
            unit_seconds = ARTICLE_UNITS.get(part["article_unit"])
            amount = 1
        if unit_seconds is None:
            return None
        total += amount * unit_seconds
        position = part.end()
        parts += 1
    if parts == 0:
        return None
    return total


# Grammar: noon | midnight | H[:MM] [ ]am|pm (H 1 to 12)
# | H:MM (H 0 to 23).
def _parse_clock(text: str) -> datetime.time | None:
    """Return the clock time the text names, or None if it names none.

    12am is midnight and 12pm is noon. A bare number is not a clock
    time.
    """
    named = NAMED_CLOCK_TIMES.get(text)
    if named is not None:
        return named

    twelve_hour = TWELVE_HOUR_PATTERN.fullmatch(text)
    if twelve_hour is not None:
        hour = int(twelve_hour["hour"])
        minute = int(twelve_hour["minute"] or 0)
        if not FIRST_TWELVE_HOUR <= hour <= LAST_TWELVE_HOUR:
            return None
        if minute > LAST_MINUTE:
            return None
        hour = hour % HOURS_PER_HALF_DAY
        if twelve_hour["period"] == PM_TEXT:
            hour += HOURS_PER_HALF_DAY
        return datetime.time(hour, minute)

    twenty_four_hour = TWENTY_FOUR_HOUR_PATTERN.fullmatch(text)
    if twenty_four_hour is not None:
        hour = int(twenty_four_hour["hour"])
        minute = int(twenty_four_hour["minute"])
        if hour > LAST_TWENTY_FOUR_HOUR or minute > LAST_MINUTE:
            return None
        return datetime.time(hour, minute)

    return None


# Grammar: today | tomorrow.
def _parse_day_word(text: str) -> _RelativeDay | None:
    """Return today or tomorrow, or None for any other text."""
    days_ahead = DAY_WORDS.get(text)
    if days_ahead is None:
        return None
    return _RelativeDay(days_ahead)


# Grammar: [this | next] WEEKDAY, with full or short weekday names.
def _parse_weekday(text: str) -> _Weekday | None:
    """Return the weekday the text names, or None if it names none."""
    words = text.split(" ")
    following_week = False
    if len(words) == 2 and words[0] in (THIS_WORD, NEXT_WORD):
        following_week = words[0] == NEXT_WORD
        words = words[1:]
    if len(words) != 1:
        return None
    weekday = WEEKDAYS.get(words[0])
    if weekday is None:
        return None
    return _Weekday(weekday, following_week)


def _calendar_date(
    month: int, day: int, year: int | None
) -> _CalendarDate | None:
    """Return a calendar date, or None for a month or day out of range.

    A month above 12 means the text was not month-first, so it is
    unreadable rather than an invalid date. Whether the day exists
    in that month is checked later, by _check_real_date.
    """
    if not FIRST_MONTH <= month <= LAST_MONTH:
        return None
    if not FIRST_DAY <= day <= LAST_DAY:
        return None
    if year is not None and year < datetime.MINYEAR:
        return None
    return _CalendarDate(month, day, year)


# Grammar: YYYY-M-D | YYYY/M/D | M-D-YYYY | M/D/YYYY | M-D | M/D.
def _parse_numeric_date(text: str) -> _CalendarDate | None:
    """Return the date written in digits, or None if there is none."""
    for pattern in (YEAR_FIRST_PATTERN, YEAR_LAST_PATTERN):
        match = pattern.fullmatch(text)
        if match is not None:
            return _calendar_date(
                int(match["month"]), int(match["day"]), int(match["year"])
            )
    match = NO_YEAR_PATTERN.fullmatch(text)
    if match is not None:
        return _calendar_date(int(match["month"]), int(match["day"]), None)
    return None


# Grammar: MONTH D[st|nd|rd|th] [YYYY], with full or short month names.
def _parse_month_name_date(text: str) -> _CalendarDate | None:
    """Return the date with a month name, or None if there is none."""
    match = MONTH_NAME_PATTERN.fullmatch(text)
    if match is None:
        return None
    month = MONTH_NUMBERS.get(match["month"])
    if month is None:
        return None
    year = int(match["year"]) if match["year"] is not None else None
    return _calendar_date(month, int(match["day"]), year)


def _parse_day(text: str) -> _DayForm | None:
    """Return the day form the text names, or None if it names none."""
    for parser in (
        _parse_day_word,
        _parse_weekday,
        _parse_numeric_date,
        _parse_month_name_date,
    ):
        form = parser(text)
        if form is not None:
            return form
    return None


def _parse_clock_form(
    text: str,
) -> tuple[_DayForm | None, datetime.time | None]:
    """Split a clock-based text into its day form and clock time.

    A clock time alone, with or without a leading "at" ("at 9am"),
    gives no day form. A day with no time gives no clock time. Raise
    UNREADABLE when the text matches no form.
    """
    clock = _parse_clock(text.removeprefix(LEADING_AT))
    if clock is not None:
        return None, clock

    split = DAY_THEN_CLOCK_PATTERN.fullmatch(text)
    if split is not None:
        clock = _parse_clock(split["clock"])
        day = _parse_day(split["day"])
        if clock is not None and day is not None:
            return day, clock

    day = _parse_day(text)
    if day is not None:
        return day, None
    raise _unreadable()


def _days_in_month(year: int, month: int) -> int:
    """Return the number of days in a month of a year."""
    _, days = calendar.monthrange(year, month)
    return days


def _check_real_date(form: _DayForm | None) -> None:
    """Raise INVALID_DATE when a calendar date does not exist.

    A date without a year is checked against a leap year, so
    February 29 passes.
    """
    if not isinstance(form, _CalendarDate):
        return
    year = form.year if form.year is not None else LEAP_YEAR
    days = _days_in_month(year, form.month)
    if form.day > days:
        raise WhenParseError(
            WhenParseReason.INVALID_DATE,
            MONTH_TOO_SHORT_MESSAGE.format(
                month_name=MONTH_NAMES[form.month - 1], days=days
            ),
        )


def _load_zone(name: str | None) -> zoneinfo.ZoneInfo:
    """Load the person's timezone, or raise NEEDS_TIMEZONE."""
    if name is None:
        raise WhenParseError(
            WhenParseReason.NEEDS_TIMEZONE, NEEDS_TIMEZONE_MESSAGE
        )
    try:
        return zoneinfo.ZoneInfo(name)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError) as error:
        raise WhenParseError(
            WhenParseReason.NEEDS_TIMEZONE, NEEDS_TIMEZONE_MESSAGE
        ) from error


def _yearless_dates(
    month: int, day: int, first_year: int
) -> list[datetime.date]:
    """Return the first dates on or after first_year with this day.

    Every year has the day except for February 29, which only leap
    years have.
    """
    dates: list[datetime.date] = []
    year = first_year
    while len(dates) < YEARLESS_CANDIDATES:
        if day <= _days_in_month(year, month):
            dates.append(datetime.date(year, month, day))
        year += 1
    return dates


def _candidate_dates(
    form: _DayForm | None, today: datetime.date
) -> list[datetime.date]:
    """Return the local dates a form may mean, earliest first.

    The first date whose moment is still ahead of now is used. A form
    with only one date is past when that moment has passed.
    """
    one_day = datetime.timedelta(days=1)
    one_week = datetime.timedelta(days=DAYS_PER_WEEK)
    if form is None:
        # A clock time alone: today, or tomorrow if it has passed.
        return [today, today + one_day]
    if isinstance(form, _RelativeDay):
        return [today + form.days_ahead * one_day]
    if isinstance(form, _Weekday):
        if form.following_week:
            # Weeks start on Monday.
            next_monday = today + (DAYS_PER_WEEK - today.weekday()) * one_day
            return [next_monday + form.weekday * one_day]
        days_ahead = (form.weekday - today.weekday()) % DAYS_PER_WEEK
        coming = today + days_ahead * one_day
        return [coming, coming + one_week]
    if form.year is not None:
        return [datetime.date(form.year, form.month, form.day)]
    return _yearless_dates(form.month, form.day, today.year)


def _first_moment_after(
    dates: list[datetime.date],
    clock: datetime.time,
    zone: zoneinfo.ZoneInfo,
    now: int,
) -> int:
    """Return the first candidate moment after now, or raise PAST."""
    for date in dates:
        due_at = local_to_utc(date, clock, zone)
        if due_at > now:
            return due_at
    raise WhenParseError(WhenParseReason.PAST, PAST_MESSAGE)


def _latest_due_at(now: int) -> int:
    """Return the UTC seconds MAX_YEARS_AHEAD years after now.

    From February 29, the limit falls on February 28.
    """
    start = datetime.datetime.fromtimestamp(now, tz=datetime.UTC)
    year = start.year + MAX_YEARS_AHEAD
    day = min(start.day, _days_in_month(year, start.month))
    return int(start.replace(year=year, day=day).timestamp())


def _checked(due_at: int, *, now: int, is_duration: bool) -> ParsedWhen:
    """Return the result after the past and too-far checks."""
    if due_at <= now:
        raise WhenParseError(WhenParseReason.PAST, PAST_MESSAGE)
    if due_at > _latest_due_at(now):
        raise WhenParseError(WhenParseReason.TOO_FAR, TOO_FAR_MESSAGE)
    return ParsedWhen(due_at=due_at, is_duration=is_duration)


def parse_when(
    text: str,
    *,
    now: int,
    timezone: str | None,
    default_time: datetime.time,
) -> ParsedWhen:
    """Return the UTC time that a ``when`` text names.

    ``now`` is in UTC seconds and ``timezone`` is the person's stored
    IANA name, or None. ``default_time`` is the clock time for a form
    that gives a day but no time. Durations ignore the timezone.

    Raise WhenParseError when the text matches no form (UNREADABLE),
    names a date that does not exist (INVALID_DATE), needs a timezone
    that is missing or does not load (NEEDS_TIMEZONE), is not after
    now (PAST), or is more than 10 years ahead (TOO_FAR).
    """
    normalized = _normalize(text)

    seconds = _parse_duration(normalized)
    if seconds is not None:
        if seconds == 0:
            raise _unreadable()
        return _checked(now + seconds, now=now, is_duration=True)

    form, clock = _parse_clock_form(normalized)
    _check_real_date(form)
    zone = _load_zone(timezone)
    today = datetime.datetime.fromtimestamp(now, tz=zone).date()
    due_at = _first_moment_after(
        _candidate_dates(form, today),
        clock if clock is not None else default_time,
        zone,
        now,
    )
    return _checked(due_at, now=now, is_duration=False)
