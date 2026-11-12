import datetime
import zoneinfo
from collections import Counter

import pytest
from dateutil import rrule

from maika import repeat_rules
from maika.repeat_rules import (
    REPEAT_CHOICE_LABELS,
    TIMEZONE_UNAVAILABLE_MESSAGE,
    RepeatChoice,
    RepeatRuleError,
    RepeatRuleReason,
    build_repeat_rule,
    describe_repeat_rule,
    next_occurrence,
)

LOS_ANGELES_NAME = "America/Los_Angeles"
LOS_ANGELES = zoneinfo.ZoneInfo(LOS_ANGELES_NAME)
TOKYO_NAME = "Asia/Tokyo"

TIME_PARTS = "BYHOUR=9;BYMINUTE=30;BYSECOND=0"


def local(year, month, day, hour, minute, second=0, zone=LOS_ANGELES, fold=0):
    moment = datetime.datetime(
        year, month, day, hour, minute, second, tzinfo=zone, fold=fold
    )
    return int(moment.timestamp())


def wall_time(seconds, zone=LOS_ANGELES):
    return datetime.datetime.fromtimestamp(seconds, tz=zone)


def rule_for(choice, first_due, timezone=LOS_ANGELES_NAME):
    return build_repeat_rule(choice, first_due=first_due, timezone=timezone)


def following(rule, current_due, after=None, timezone=LOS_ANGELES_NAME):
    if after is None:
        after = current_due
    return next_occurrence(
        rule, current_due=current_due, after=after, timezone=timezone
    )


def chain(choice, first_due, count):
    rule = rule_for(choice, first_due)
    results = []
    current = first_due
    for _ in range(count):
        current = following(rule, current)
        results.append(current)
    return results


# Tuesday 2026-11-17 09:30 in Los Angeles.
TUESDAY = local(2026, 11, 17, 9, 30)


# Labels


def test_every_choice_has_a_label():
    assert REPEAT_CHOICE_LABELS == {
        RepeatChoice.DAILY: "Daily",
        RepeatChoice.WEEKDAYS: "Weekdays (Mon to Fri)",
        RepeatChoice.WEEKLY: "Weekly",
        RepeatChoice.BIWEEKLY: "Every 2 weeks",
        RepeatChoice.MONTHLY: "Monthly",
        RepeatChoice.LAST_DAY: "Last day of the month",
        RepeatChoice.YEARLY: "Yearly",
    }


# build_repeat_rule


@pytest.mark.parametrize(
    ("choice", "first_due", "expected"),
    [
        (RepeatChoice.DAILY, TUESDAY, f"FREQ=DAILY;{TIME_PARTS}"),
        (
            RepeatChoice.WEEKDAYS,
            TUESDAY,
            f"FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;{TIME_PARTS}",
        ),
        (RepeatChoice.WEEKLY, TUESDAY, f"FREQ=WEEKLY;BYDAY=TU;{TIME_PARTS}"),
        (
            RepeatChoice.BIWEEKLY,
            TUESDAY,
            f"FREQ=WEEKLY;INTERVAL=2;BYDAY=TU;{TIME_PARTS}",
        ),
        (
            RepeatChoice.MONTHLY,
            TUESDAY,
            f"FREQ=MONTHLY;BYMONTHDAY=17;{TIME_PARTS}",
        ),
        (
            RepeatChoice.MONTHLY,
            local(2026, 12, 15, 9, 30),
            f"FREQ=MONTHLY;BYMONTHDAY=15;{TIME_PARTS}",
        ),
        (
            RepeatChoice.MONTHLY,
            local(2026, 12, 28, 9, 30),
            f"FREQ=MONTHLY;BYMONTHDAY=28;{TIME_PARTS}",
        ),
        (
            RepeatChoice.MONTHLY,
            local(2026, 12, 29, 9, 30),
            f"FREQ=MONTHLY;BYMONTHDAY=29,-1;BYSETPOS=1;{TIME_PARTS}",
        ),
        (
            RepeatChoice.MONTHLY,
            local(2026, 12, 30, 9, 30),
            f"FREQ=MONTHLY;BYMONTHDAY=30,-1;BYSETPOS=1;{TIME_PARTS}",
        ),
        (
            RepeatChoice.MONTHLY,
            local(2026, 12, 31, 9, 30),
            f"FREQ=MONTHLY;BYMONTHDAY=31,-1;BYSETPOS=1;{TIME_PARTS}",
        ),
        (
            RepeatChoice.LAST_DAY,
            TUESDAY,
            f"FREQ=MONTHLY;BYMONTHDAY=-1;{TIME_PARTS}",
        ),
        (
            RepeatChoice.YEARLY,
            TUESDAY,
            f"FREQ=YEARLY;BYMONTH=11;BYMONTHDAY=17;{TIME_PARTS}",
        ),
        (
            RepeatChoice.YEARLY,
            local(2028, 2, 29, 9, 30),
            f"FREQ=YEARLY;BYMONTH=2;BYMONTHDAY=29,-1;BYSETPOS=1;{TIME_PARTS}",
        ),
        (
            RepeatChoice.YEARLY,
            local(2027, 2, 28, 9, 30),
            f"FREQ=YEARLY;BYMONTH=2;BYMONTHDAY=28;{TIME_PARTS}",
        ),
    ],
)
def test_build_repeat_rule(choice, first_due, expected):
    assert rule_for(choice, first_due) == expected


def test_rule_parts_come_from_the_local_time():
    # 09:30 Tuesday in Los Angeles is 02:30 Wednesday in Tokyo.
    assert (
        rule_for(RepeatChoice.WEEKLY, TUESDAY, timezone=TOKYO_NAME)
        == "FREQ=WEEKLY;BYDAY=WE;BYHOUR=2;BYMINUTE=30;BYSECOND=0"
    )


def test_seconds_are_always_zero():
    first_due = local(2026, 11, 17, 9, 30, 45)
    rule = rule_for(RepeatChoice.DAILY, first_due)

    assert rule == f"FREQ=DAILY;{TIME_PARTS}"
    for due_at in chain(RepeatChoice.DAILY, first_due, 3):
        assert wall_time(due_at).second == 0


# One occurrence per month for the fallback forms


@pytest.mark.parametrize(
    "rule",
    [
        "FREQ=MONTHLY;BYMONTHDAY=29,-1;BYSETPOS=1;" + TIME_PARTS,
        "FREQ=MONTHLY;BYMONTHDAY=30,-1;BYSETPOS=1;" + TIME_PARTS,
        "FREQ=MONTHLY;BYMONTHDAY=31,-1;BYSETPOS=1;" + TIME_PARTS,
        "FREQ=MONTHLY;BYMONTHDAY=-1;" + TIME_PARTS,
    ],
)
def test_monthly_fallback_gives_one_occurrence_per_month(rule):
    start = datetime.datetime(2027, 1, 1)
    end = datetime.datetime(2029, 1, 1)
    occurrences = rrule.rrulestr(rule, dtstart=start).between(start, end)

    months = Counter((moment.year, moment.month) for moment in occurrences)
    assert len(months) == 24
    assert set(months.values()) == {1}


def test_yearly_leap_day_gives_one_occurrence_per_year():
    rule = "FREQ=YEARLY;BYMONTH=2;BYMONTHDAY=29,-1;BYSETPOS=1;" + TIME_PARTS
    start = datetime.datetime(2028, 1, 1)
    end = datetime.datetime(2036, 1, 1)
    occurrences = rrule.rrulestr(rule, dtstart=start).between(start, end)

    assert [moment.year for moment in occurrences] == list(range(2028, 2036))


# next_occurrence


@pytest.mark.parametrize(
    ("choice", "first_due", "expected"),
    [
        (
            RepeatChoice.DAILY,
            TUESDAY,
            [(2026, 11, 18, 9, 30), (2026, 11, 19, 9, 30)],
        ),
        (
            # Friday 2026-11-20 gives Monday 2026-11-23.
            RepeatChoice.WEEKDAYS,
            local(2026, 11, 20, 9, 30),
            [(2026, 11, 23, 9, 30), (2026, 11, 24, 9, 30)],
        ),
        (
            RepeatChoice.WEEKLY,
            TUESDAY,
            [(2026, 11, 24, 9, 30), (2026, 12, 1, 9, 30)],
        ),
        (
            RepeatChoice.BIWEEKLY,
            TUESDAY,
            [(2026, 12, 1, 9, 30), (2026, 12, 15, 9, 30)],
        ),
        (
            RepeatChoice.MONTHLY,
            local(2026, 11, 15, 9, 30),
            [(2026, 12, 15, 9, 30), (2027, 1, 15, 9, 30)],
        ),
        (
            RepeatChoice.MONTHLY,
            local(2027, 1, 31, 9, 30),
            [(2027, 2, 28, 9, 30), (2027, 3, 31, 9, 30), (2027, 4, 30, 9, 30)],
        ),
        (
            RepeatChoice.MONTHLY,
            local(2028, 1, 29, 9, 30),
            [(2028, 2, 29, 9, 30), (2028, 3, 29, 9, 30)],
        ),
        (
            RepeatChoice.MONTHLY,
            local(2027, 1, 30, 9, 30),
            [(2027, 2, 28, 9, 30), (2027, 3, 30, 9, 30)],
        ),
        (
            RepeatChoice.LAST_DAY,
            local(2027, 1, 31, 9, 30),
            [(2027, 2, 28, 9, 30), (2027, 3, 31, 9, 30)],
        ),
        (
            RepeatChoice.LAST_DAY,
            local(2028, 1, 31, 9, 30),
            [(2028, 2, 29, 9, 30), (2028, 3, 31, 9, 30)],
        ),
        (
            RepeatChoice.YEARLY,
            TUESDAY,
            [(2027, 11, 17, 9, 30), (2028, 11, 17, 9, 30)],
        ),
        (
            RepeatChoice.YEARLY,
            local(2028, 2, 29, 9, 30),
            [
                (2029, 2, 28, 9, 30),
                (2030, 2, 28, 9, 30),
                (2031, 2, 28, 9, 30),
                (2032, 2, 29, 9, 30),
            ],
        ),
    ],
)
def test_next_occurrence(choice, first_due, expected):
    results = chain(choice, first_due, len(expected))
    assert results == [local(*parts) for parts in expected]


def test_every_two_weeks_keeps_its_weeks():
    rule = rule_for(RepeatChoice.BIWEEKLY, TUESDAY)
    after = local(2026, 12, 20, 12, 0)

    assert following(rule, TUESDAY, after) == local(2026, 12, 29, 9, 30)


def test_strictly_after_an_occurrence():
    rule = rule_for(RepeatChoice.DAILY, TUESDAY)
    occurrence = local(2026, 11, 19, 9, 30)

    assert following(rule, TUESDAY, occurrence) == local(2026, 11, 20, 9, 30)
    assert following(rule, TUESDAY, occurrence - 1) == occurrence


@pytest.mark.parametrize(
    ("after", "expected"),
    [
        (local(2026, 11, 20, 12, 0), local(2026, 11, 21, 9, 30)),
        (local(2026, 11, 20, 8, 0), local(2026, 11, 20, 9, 30)),
    ],
)
def test_downtime_skips_to_the_first_future_occurrence(after, expected):
    rule = rule_for(RepeatChoice.DAILY, TUESDAY)
    # TUESDAY is three days before after.
    assert following(rule, TUESDAY, after) == expected


def test_far_behind_checks_only_a_few_occurrences(monkeypatch):
    calls = []
    real_local_to_utc = repeat_rules.local_to_utc

    def counting_local_to_utc(date, clock, zone):
        calls.append(date)
        return real_local_to_utc(date, clock, zone)

    monkeypatch.setattr(repeat_rules, "local_to_utc", counting_local_to_utc)
    rule = rule_for(RepeatChoice.DAILY, TUESDAY)
    after = local(2029, 11, 20, 12, 0)

    assert following(rule, TUESDAY, after) == local(2029, 11, 21, 9, 30)
    assert len(calls) <= 5


# Daylight saving


def test_daily_keeps_its_clock_time_across_spring_forward():
    first_due = local(2027, 3, 13, 9, 0)
    results = chain(RepeatChoice.DAILY, first_due, 2)

    assert results == [local(2027, 3, 14, 9, 0), local(2027, 3, 15, 9, 0)]
    assert [wall_time(due_at).hour for due_at in results] == [9, 9]
    # The day of the change is an hour shorter in UTC.
    assert results[0] - first_due == 23 * 60 * 60


def test_missing_time_moves_forward_then_returns():
    first_due = local(2027, 3, 13, 2, 30)
    rule = rule_for(RepeatChoice.DAILY, first_due)

    moved = following(rule, first_due)
    assert moved == local(2027, 3, 14, 3, 30)
    assert wall_time(moved).tzname() == "PDT"

    assert following(rule, moved) == local(2027, 3, 15, 2, 30)


def test_repeated_time_uses_the_first_occurrence():
    first_due = local(2027, 11, 6, 1, 30)
    rule = rule_for(RepeatChoice.DAILY, first_due)

    on_the_change = following(rule, first_due)
    assert on_the_change == local(2027, 11, 7, 1, 30, fold=0)
    assert wall_time(on_the_change).tzname() == "PDT"

    next_day = following(rule, on_the_change)
    assert next_day == local(2027, 11, 8, 1, 30)
    assert wall_time(next_day).tzname() == "PST"


def test_repeated_time_fires_once_that_day():
    first_due = local(2027, 11, 6, 1, 30)
    rule = rule_for(RepeatChoice.DAILY, first_due)
    second_one_thirty = local(2027, 11, 7, 1, 30, fold=1)

    assert following(rule, first_due, second_one_thirty - 1) == local(
        2027, 11, 8, 1, 30
    )


# Timezones


def test_the_same_rule_in_tokyo_gives_different_times():
    rule = f"FREQ=DAILY;{TIME_PARTS}"
    in_los_angeles = following(rule, TUESDAY)
    in_tokyo = following(rule, TUESDAY, timezone=TOKYO_NAME)

    assert in_los_angeles != in_tokyo
    assert wall_time(in_tokyo, zoneinfo.ZoneInfo(TOKYO_NAME)).hour == 9


# Errors


@pytest.mark.parametrize(
    "rule", ["garbage", "", "FREQ=DAILY;BYHOUR=99", "FREQ=SOMETIMES"]
)
def test_invalid_rule(rule):
    with pytest.raises(RepeatRuleError) as caught:
        following(rule, TUESDAY)
    assert caught.value.reason is RepeatRuleReason.INVALID_RULE


def test_rule_with_no_later_occurrence_is_invalid():
    with pytest.raises(RepeatRuleError) as caught:
        following(f"FREQ=DAILY;COUNT=1;{TIME_PARTS}", TUESDAY)
    assert caught.value.reason is RepeatRuleReason.INVALID_RULE


@pytest.mark.parametrize("timezone", ["", "Not/AZone", "../etc"])
def test_timezone_unavailable(timezone):
    with pytest.raises(RepeatRuleError) as caught:
        following(f"FREQ=DAILY;{TIME_PARTS}", TUESDAY, timezone=timezone)
    assert caught.value.reason is RepeatRuleReason.TIMEZONE_UNAVAILABLE
    assert caught.value.message == TIMEZONE_UNAVAILABLE_MESSAGE
    assert caught.value.message == (
        "That reminder's timezone could not be loaded."
    )

    with pytest.raises(RepeatRuleError) as caught:
        rule_for(RepeatChoice.DAILY, TUESDAY, timezone=timezone)
    assert caught.value.reason is RepeatRuleReason.TIMEZONE_UNAVAILABLE


def test_reason_values():
    assert [reason.value for reason in RepeatRuleReason] == [
        "invalid_rule",
        "timezone_unavailable",
    ]


# describe_repeat_rule


@pytest.mark.parametrize("choice", list(RepeatChoice))
def test_describe_gives_back_every_label(choice):
    rule = rule_for(choice, TUESDAY)
    assert describe_repeat_rule(rule) == REPEAT_CHOICE_LABELS[choice]


@pytest.mark.parametrize(
    ("choice", "first_due"),
    [
        (RepeatChoice.MONTHLY, local(2026, 12, 31, 9, 30)),
        (RepeatChoice.MONTHLY, local(2026, 12, 29, 9, 30)),
        (RepeatChoice.MONTHLY, local(2026, 12, 1, 9, 30)),
        (RepeatChoice.YEARLY, local(2028, 2, 29, 9, 30)),
        (RepeatChoice.YEARLY, local(2027, 12, 31, 23, 59)),
        (RepeatChoice.WEEKLY, local(2026, 11, 22, 0, 0)),
        (RepeatChoice.BIWEEKLY, local(2026, 11, 21, 18, 0)),
    ],
)
def test_describe_special_days(choice, first_due):
    rule = rule_for(choice, first_due)
    assert describe_repeat_rule(rule) == REPEAT_CHOICE_LABELS[choice]


def test_describe_rule_built_in_tokyo():
    rule = rule_for(RepeatChoice.WEEKDAYS, TUESDAY, timezone=TOKYO_NAME)
    assert describe_repeat_rule(rule) == "Weekdays (Mon to Fri)"


@pytest.mark.parametrize(
    "rule",
    [
        "garbage",
        "",
        "FREQ=SOMETIMES",
        "FREQ=HOURLY;BYMINUTE=0",
        "FREQ=DAILY;INTERVAL=3",
        "FREQ=DAILY;FREQ=DAILY",
        "FREQ=DAILY;;BYHOUR=9",
        "FREQ=WEEKLY",
        "FREQ=WEEKLY;BYDAY=XX",
        "FREQ=WEEKLY;BYDAY=TU,TH",
        "FREQ=WEEKLY;INTERVAL=3;BYDAY=TU",
        "FREQ=WEEKLY;INTERVAL=2;BYDAY=MO,TU,WE,TH,FR",
        "FREQ=MONTHLY",
        "FREQ=MONTHLY;BYMONTHDAY=32",
        "FREQ=MONTHLY;BYMONTHDAY=0",
        "FREQ=MONTHLY;BYMONTHDAY=31,-2",
        "FREQ=MONTHLY;BYMONTHDAY=abc",
        "FREQ=MONTHLY;INTERVAL=2;BYMONTHDAY=5",
        "FREQ=YEARLY;BYDAY=MO",
        "freq=daily",
        "FREQ=DAILY;BYHOUR=9;BYMINUTE",
    ],
)
def test_describe_unrecognized_rule(rule):
    assert describe_repeat_rule(rule) == "Custom repeat"


def test_describe_daily_with_interval_one():
    assert describe_repeat_rule("FREQ=DAILY;INTERVAL=1") == "Daily"
