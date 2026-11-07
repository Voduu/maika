import dataclasses
import logging

import pytest

from maika.command_runner import CommandUsage
from maika.database import GLOBAL_GUILD_ID
from maika.log_router import LogLevel, LogStream
from maika.permissions import DecisionSource
from maika.results import FailureKind
from maika.usage_log import (
    build_usage_logger,
    format_discord_usage_line,
    format_usage_line,
    log_usage,
)


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


# format_discord_usage_line


def config_get_usage(**changes):
    base = usage(detail=None)
    fields = {
        "command_name": "config get",
        "key": "config.get",
        "user_id": 1,
        "guild_id": 9000,
        "channel_id": 2,
        "decision_source": DecisionSource.OWNER,
    }
    fields.update(changes)
    return dataclasses.replace(base, **fields)


def test_discord_line_allowed():
    line = format_discord_usage_line(config_get_usage(), "Maika Test")
    assert (
        line == "`/config get` by <@1> in <#2> in Maika Test: allowed (owner)"
    )


def test_discord_line_denied_with_detail():
    record = config_get_usage(
        user_id=3,
        channel_id=4,
        allowed=False,
        decision_source=DecisionSource.OWNER_ONLY_KEY,
        decision_detail="config.get is owner-only",
        success=False,
        failure_kind=FailureKind.DENIED,
    )
    line = format_discord_usage_line(record, "Friends")
    assert line == (
        "`/config get` by <@3> in <#4> in Friends: "
        "denied (owner_only_key: config.get is owner-only)"
    )


def test_discord_line_allowed_but_failed():
    record = config_get_usage(
        success=False, failure_kind=FailureKind.NOT_FOUND
    )
    line = format_discord_usage_line(record, "Maika Test")
    assert line == (
        "`/config get` by <@1> in <#2> in Maika Test: "
        "allowed (owner), failed: not_found"
    )


def test_discord_line_dm():
    record = config_get_usage(
        command_name="remind",
        key="remind.use",
        user_id=3,
        guild_id=GLOBAL_GUILD_ID,
        channel_id=4,
        decision_source=DecisionSource.LEVEL,
    )
    line = format_discord_usage_line(record, None)
    assert line == "`/remind` by <@3> in DM: allowed (level)"


def test_discord_line_dm_ignores_guild_name():
    record = config_get_usage(guild_id=GLOBAL_GUILD_ID)
    line = format_discord_usage_line(record, "Should Not Show")
    assert line == "`/config get` by <@1> in DM: allowed (owner)"


def test_discord_line_unknown_server():
    line = format_discord_usage_line(config_get_usage(), None)
    assert line == (
        "`/config get` by <@1> in <#2> in server 9000: allowed (owner)"
    )


def test_discord_line_escapes_asterisk_in_detail():
    record = config_get_usage(
        user_id=3,
        allowed=False,
        decision_source=DecisionSource.OVERRIDE,
        decision_detail="deny override on config.*, server 9000",
        success=False,
        failure_kind=FailureKind.DENIED,
    )
    line = format_discord_usage_line(record, "Friends")
    assert line.endswith(
        "denied (override: deny override on config.\\*, server 9000)"
    )


def test_discord_line_escapes_asterisks_in_server_name():
    line = format_discord_usage_line(config_get_usage(), "*Cat* Club")
    assert line == (
        "`/config get` by <@1> in <#2> in \\*Cat\\* Club: allowed (owner)"
    )


def test_discord_line_refusal_has_no_failed_suffix():
    record = config_get_usage(
        allowed=False,
        decision_source=DecisionSource.BLOCK,
        decision_detail="blocked",
        success=False,
        failure_kind=FailureKind.DENIED,
    )
    line = format_discord_usage_line(record, "Friends")
    assert "failed" not in line


def test_discord_line_has_no_argument_values():
    line = format_discord_usage_line(usage(), "Friends")
    assert line == (
        "`/feed subscribe` by <@100> in <#7000> in Friends: allowed (level)"
    )


# build_usage_logger


@pytest.mark.parametrize(
    ("protected", "console_level", "post_level"),
    [
        (False, logging.INFO, LogLevel.INFO),
        (True, logging.DEBUG, LogLevel.DEBUG),
    ],
)
def test_usage_logger_logs_and_posts(
    caplog, protected, console_level, post_level
):
    posts = []
    asked = []

    def guild_name_for(guild_id):
        asked.append(guild_id)
        return "Friends"

    log_and_post = build_usage_logger(
        lambda stream, level, text: posts.append((stream, level, text)),
        guild_name_for,
    )
    record = usage(protected=protected)

    with caplog.at_level(logging.DEBUG, logger="maika"):
        log_and_post(record)

    assert asked == [5000]
    assert posts == [
        (
            LogStream.BOT_LOG,
            post_level,
            format_discord_usage_line(record, "Friends"),
        )
    ]
    (console,) = caplog.records
    assert console.name == "maika"
    assert console.levelno == console_level
    assert console.getMessage() == format_usage_line(record)
    assert console.getMessage().startswith("Command feed subscribe")


def test_usage_logger_unknown_server():
    posts = []
    log_and_post = build_usage_logger(
        lambda stream, level, text: posts.append(text),
        lambda guild_id: None,
    )

    log_and_post(usage())

    assert posts == [
        "`/feed subscribe` by <@100> in <#7000> in server 5000: "
        "allowed (level)"
    ]


def test_usage_logger_dm_does_not_look_up_a_server():
    posts = []
    asked = []
    log_and_post = build_usage_logger(
        lambda stream, level, text: posts.append(text),
        lambda guild_id: asked.append(guild_id),
    )

    log_and_post(dataclasses.replace(usage(), guild_id=GLOBAL_GUILD_ID))

    assert asked == []
    assert posts == ["`/feed subscribe` by <@100> in DM: allowed (level)"]
