import dataclasses
import datetime
import zoneinfo

import pytest

from maika.time_parser import (
    NEEDS_TIMEZONE_MESSAGE,
    PAST_MESSAGE,
    UNREADABLE_MESSAGE,
    ParsedWhen,
    WhenParseError,
    WhenParseReason,
    parse_when,
)

LOS_ANGELES_NAME = "America/Los_Angeles"
LOS_ANGELES = zoneinfo.ZoneInfo(LOS_ANGELES_NAME)
TOKYO_NAME = "Asia/Tokyo"
DEFAULT_TIME = datetime.time(8, 0)

MINUTE = 60
HOUR = 60 * MINUTE
DAY = 24 * HOUR


def local(year, month, day, hour, minute, zone=LOS_ANGELES, fold=0):
    moment = datetime.datetime(
        year, month, day, hour, minute, tzinfo=zone, fold=fold
    )
    return int(moment.timestamp())


# Wednesday 2026-11-11 19:42 in Los Angeles.
NOW = local(2026, 11, 11, 19, 42)


def parse(text, *, now=NOW, timezone=LOS_ANGELES_NAME):
    return parse_when(
        text, now=now, timezone=timezone, default_time=DEFAULT_TIME
    )


def parse_error(text, *, now=NOW, timezone=LOS_ANGELES_NAME):
    with pytest.raises(WhenParseError) as caught:
        parse(text, now=now, timezone=timezone)
    return caught.value


# Durations


@pytest.mark.parametrize(
    ("text", "seconds"),
    [
        ("2h", 2 * HOUR),
        ("in an hour", HOUR),
        ("1h30m", HOUR + 30 * MINUTE),
        ("2d 4h", 2 * DAY + 4 * HOUR),
        ("90 minutes", 90 * MINUTE),
        ("a day", DAY),
        ("in 2 hours", 2 * HOUR),
        ("30m", 30 * MINUTE),
        ("in a minute", MINUTE),
        ("1 hour and 30 minutes", HOUR + 30 * MINUTE),
        ("2 hrs 15 mins", 2 * HOUR + 15 * MINUTE),
        ("1w", 7 * DAY),
        ("2 wks", 14 * DAY),
        ("a week", 7 * DAY),
        ("  In 2H  ", 2 * HOUR),
    ],
)
def test_durations(text, seconds):
    assert parse(text) == ParsedWhen(due_at=NOW + seconds, is_duration=True)


def test_two_hours_is_21_42_today():
    assert parse("2h").due_at == local(2026, 11, 11, 21, 42)


def test_in_an_hour_is_20_42_today():
    assert parse("in an hour").due_at == local(2026, 11, 11, 20, 42)


@pytest.mark.parametrize("timezone", [LOS_ANGELES_NAME, TOKYO_NAME, None])
def test_duration_is_the_same_in_every_timezone(timezone):
    assert parse("2h", timezone=timezone).due_at == NOW + 2 * HOUR


def test_duration_works_with_a_timezone_that_does_not_load():
    assert parse("2h", timezone="Not/AZone").due_at == NOW + 2 * HOUR


# Clock time alone


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("7pm", (2026, 11, 12, 19, 0)),
        ("9am", (2026, 11, 12, 9, 0)),
        ("9 am", (2026, 11, 12, 9, 0)),
        ("9:30pm", (2026, 11, 11, 21, 30)),
        ("15:30", (2026, 11, 12, 15, 30)),
        ("noon", (2026, 11, 12, 12, 0)),
        ("midnight", (2026, 11, 12, 0, 0)),
        ("12am", (2026, 11, 12, 0, 0)),
        ("12pm", (2026, 11, 12, 12, 0)),
        ("11:59pm", (2026, 11, 11, 23, 59)),
        ("0:05", (2026, 11, 12, 0, 5)),
        ("19:43", (2026, 11, 11, 19, 43)),
        ("19:42", (2026, 11, 12, 19, 42)),
    ],
)
def test_clock_times(text, expected):
    assert parse(text) == ParsedWhen(
        due_at=local(*expected), is_duration=False
    )


@pytest.mark.parametrize(
    "text", ["9am", "9:30pm", "15:30", "noon", "midnight"]
)
def test_at_before_a_clock_time_alone_changes_nothing(text):
    assert parse(f"at {text}") == parse(text)


@pytest.mark.parametrize(
    "text", ["at", "at tomorrow", "at 9", "at nov 20", "at friday"]
)
def test_at_without_a_clock_time_is_unreadable(text):
    assert parse_error(text).reason is WhenParseReason.UNREADABLE


# Day words


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("tomorrow at 9am", (2026, 11, 12, 9, 0)),
        ("tomorrow 9am", (2026, 11, 12, 9, 0)),
        ("tomorrow", (2026, 11, 12, 8, 0)),
        ("today 9pm", (2026, 11, 11, 21, 0)),
        ("today at 9 pm", (2026, 11, 11, 21, 0)),
        ("tomorrow noon", (2026, 11, 12, 12, 0)),
        ("Tomorrow  at  15:30", (2026, 11, 12, 15, 30)),
    ],
)
def test_day_words(text, expected):
    assert parse(text).due_at == local(*expected)


# Weekdays


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("friday 3pm", (2026, 11, 13, 15, 0)),
        ("this friday 3pm", (2026, 11, 13, 15, 0)),
        ("next friday 3pm", (2026, 11, 20, 15, 0)),
        ("wednesday 9pm", (2026, 11, 11, 21, 0)),
        ("wednesday 9am", (2026, 11, 18, 9, 0)),
        ("fri", (2026, 11, 13, 8, 0)),
        ("friday at 3 pm", (2026, 11, 13, 15, 0)),
        ("wednesday", (2026, 11, 18, 8, 0)),
        ("next wednesday", (2026, 11, 18, 8, 0)),
        ("next monday", (2026, 11, 16, 8, 0)),
        ("next sunday", (2026, 11, 22, 8, 0)),
        ("monday", (2026, 11, 16, 8, 0)),
        ("tues 10am", (2026, 11, 17, 10, 0)),
        ("thurs", (2026, 11, 12, 8, 0)),
        ("sat", (2026, 11, 14, 8, 0)),
    ],
)
def test_weekdays(text, expected):
    assert parse(text).due_at == local(*expected)


# Dates


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("nov 20", (2026, 11, 20, 8, 0)),
        ("11/20 15:30", (2026, 11, 20, 15, 30)),
        ("november 20, 2026 3pm", (2026, 11, 20, 15, 0)),
        ("nov 5", (2027, 11, 5, 8, 0)),
        ("2026-12-25 08:00", (2026, 12, 25, 8, 0)),
        ("12-25-2026", (2026, 12, 25, 8, 0)),
        ("12/25/2026 3pm", (2026, 12, 25, 15, 0)),
        ("2026/12/25", (2026, 12, 25, 8, 0)),
        ("nov 20th", (2026, 11, 20, 8, 0)),
        ("nov 20 2026", (2026, 11, 20, 8, 0)),
        ("november 20", (2026, 11, 20, 8, 0)),
        ("sept 3", (2027, 9, 3, 8, 0)),
        ("1/2", (2027, 1, 2, 8, 0)),
        ("2027-1-2 9am", (2027, 1, 2, 9, 0)),
        ("12-25", (2026, 12, 25, 8, 0)),
        ("11/11 9pm", (2026, 11, 11, 21, 0)),
        ("11/11 9am", (2027, 11, 11, 9, 0)),
        ("dec 25 at 9am", (2026, 12, 25, 9, 0)),
    ],
)
def test_dates(text, expected):
    assert parse(text) == ParsedWhen(
        due_at=local(*expected), is_duration=False
    )


def test_february_29_without_a_year_uses_the_next_leap_year():
    assert parse("feb 29").due_at == local(2028, 2, 29, 8, 0)


def test_february_29_with_a_leap_year():
    assert parse("2028-02-29").due_at == local(2028, 2, 29, 8, 0)


# Daylight saving

BEFORE_SPRING_FORWARD = local(2027, 3, 1, 12, 0)


def test_missing_local_time_moves_forward():
    due_at = parse("2027-03-14 2:30am", now=BEFORE_SPRING_FORWARD).due_at

    moment = datetime.datetime.fromtimestamp(due_at, tz=LOS_ANGELES)
    assert (moment.hour, moment.minute) == (3, 30)
    assert moment.tzname() == "PDT"
    assert due_at == local(2027, 3, 14, 3, 30)


def test_repeated_local_time_uses_the_first_occurrence():
    due_at = parse("2027-11-07 1:30am", now=BEFORE_SPRING_FORWARD).due_at

    moment = datetime.datetime.fromtimestamp(due_at, tz=LOS_ANGELES)
    assert (moment.hour, moment.minute) == (1, 30)
    assert moment.tzname() == "PDT"
    assert due_at == local(2027, 11, 7, 1, 30, fold=0)
    assert due_at < local(2027, 11, 7, 1, 30, fold=1)


# Timezones


@pytest.mark.parametrize(
    "text", ["tomorrow 9am", "9am", "friday 3pm", "2026-12-25 08:00"]
)
def test_clock_form_is_read_in_the_given_timezone(text):
    in_los_angeles = parse(text).due_at
    in_tokyo = parse(text, timezone=TOKYO_NAME).due_at
    assert in_los_angeles != in_tokyo


def test_tokyo_reads_tomorrow_9am_in_tokyo():
    tokyo = zoneinfo.ZoneInfo(TOKYO_NAME)
    # NOW is already Thursday 2026-11-12 12:42 in Tokyo.
    assert parse("tomorrow 9am", timezone=TOKYO_NAME).due_at == local(
        2026, 11, 13, 9, 0, zone=tokyo
    )


@pytest.mark.parametrize("timezone", [None, "Not/AZone", "", "../etc"])
@pytest.mark.parametrize(
    "text", ["9am", "tomorrow", "friday 3pm", "nov 20", "2026-12-25"]
)
def test_clock_form_needs_a_timezone(text, timezone):
    error = parse_error(text, timezone=timezone)
    assert error.reason is WhenParseReason.NEEDS_TIMEZONE
    assert error.message == NEEDS_TIMEZONE_MESSAGE
    assert error.message == "Set your timezone first with /timezone set."


# Errors


@pytest.mark.parametrize(
    "text",
    [
        "9",
        "15",
        "15pm",
        "13am",
        "0am",
        "9:60pm",
        "24:00",
        "9:5",
        "1.5h",
        "0h",
        "0m 0h",
        "25/12/2026",
        "12/25/26",
        "13/1",
        "nov 32",
        "tomorrowish",
        "sometime tomorrow",
        "tomorrow 9",
        "today at",
        "3pm friday",
        "next week",
        "eod",
        "2h and",
        "a m",
        "",
        "in",
        "   ",
        "next",
        "this tomorrow",
        "smarch 3",
        "0000-01-01",
    ],
)
def test_unreadable(text):
    error = parse_error(text)
    assert error.reason is WhenParseReason.UNREADABLE
    assert error.message == UNREADABLE_MESSAGE
    assert error.message == (
        "I couldn't read that time. Try 2h, tomorrow 9am, friday 3pm, "
        "or 2026-11-20 14:30."
    )


def test_unreadable_wins_over_missing_timezone():
    error = parse_error("15pm", timezone=None)
    assert error.reason is WhenParseReason.UNREADABLE


@pytest.mark.parametrize(
    "text", ["today 9am", "today", "2026-11-01", "11/11/2026 7:41pm"]
)
def test_past(text):
    error = parse_error(text)
    assert error.reason is WhenParseReason.PAST
    assert error.message == PAST_MESSAGE
    assert error.message == "That time has already passed."


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("2026-02-30", "February has only 28 days."),
        ("april 31", "April has only 30 days."),
        ("feb 30", "February has only 29 days."),
        ("2027-02-29", "February has only 28 days."),
        ("6/31 9am", "June has only 30 days."),
    ],
)
def test_invalid_date(text, message):
    error = parse_error(text)
    assert error.reason is WhenParseReason.INVALID_DATE
    assert error.message == message


def test_invalid_date_wins_over_missing_timezone():
    error = parse_error("april 31", timezone=None)
    assert error.reason is WhenParseReason.INVALID_DATE


@pytest.mark.parametrize("text", ["2040-01-01", "600w", "99999999999d"])
def test_too_far(text):
    error = parse_error(text)
    assert error.reason is WhenParseReason.TOO_FAR
    assert "10 years" in error.message


def test_ten_years_ahead_is_the_limit():
    assert parse("2036-11-11 7pm").due_at == local(2036, 11, 11, 19, 0)
    assert parse_error("2036-11-12").reason is WhenParseReason.TOO_FAR


def test_result_is_frozen():
    result = parse("2h")
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.due_at = 0


def test_error_reason_values():
    assert [reason.value for reason in WhenParseReason] == [
        "unreadable",
        "needs_timezone",
        "past",
        "invalid_date",
        "too_far",
    ]
