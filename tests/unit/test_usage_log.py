import logging

import pytest

from maika.command_runner import CommandUsage
from maika.permissions import DecisionSource
from maika.results import FailureKind
from maika.usage_log import format_usage_line, log_usage


def usage(
    protected=False,
    allowed=True,
    source=DecisionSource.LEVEL,
    success=True,
    failure_kind=None,
    detail="level 1 (member), feed.subscribe needs level 1",
):
    return CommandUsage(
        command_name="feed subscribe",
        key="feed.subscribe",
        protected=protected,
        user_id=100,
        guild_id=5000,
        channel_id=7000,
        now=1_800_000_000,
        allowed=allowed,
        decision_source=source,
        decision_detail=detail,
        success=success,
        failure_kind=failure_kind,
    )


def test_line_for_success():
    assert format_usage_line(usage()) == (
        "Command feed subscribe (feed.subscribe): user 100, guild 5000, "
        "channel 7000, allowed True, decided by level (level 1 "
        "(member), feed.subscribe needs level 1), success True, "
        "failure none"
    )


def test_line_without_detail():
    assert format_usage_line(usage(detail=None)) == (
        "Command feed subscribe (feed.subscribe): user 100, guild 5000, "
        "channel 7000, allowed True, decided by level, success True, "
        "failure none"
    )


def test_line_with_override_detail():
    line = format_usage_line(
        usage(
            allowed=False,
            source=DecisionSource.OVERRIDE,
            success=False,
            failure_kind=FailureKind.DENIED,
            detail="deny override on feed.*, server 5000",
        )
    )
    assert "decided by override (deny override on feed.*, server 5000)," in (
        line
    )


def test_line_for_denied():
    line = format_usage_line(
        usage(
            allowed=False,
            source=DecisionSource.BLOCK,
            success=False,
            failure_kind=FailureKind.DENIED,
        )
    )
    assert "allowed False" in line
    assert "decided by block" in line
    assert line.endswith("success False, failure denied")


@pytest.mark.parametrize(
    ("protected", "level"),
    [(False, logging.INFO), (True, logging.DEBUG)],
)
def test_log_level(caplog, protected, level):
    with caplog.at_level(logging.DEBUG, logger="maika"):
        log_usage(usage(protected=protected))

    (record,) = caplog.records
    assert record.name == "maika"
    assert record.levelno == level
    assert record.getMessage() == format_usage_line(usage(protected=protected))


def test_protected_usage_hidden_at_info(caplog):
    with caplog.at_level(logging.INFO, logger="maika"):
        log_usage(usage(protected=True))
    assert caplog.records == []
