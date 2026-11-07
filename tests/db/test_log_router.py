import asyncio
import logging

import pytest

from maika.foundation_settings import build_foundation_registry
from maika.log_router import (
    DISCORD_MESSAGE_LIMIT,
    LOG_QUEUE_LIMIT,
    TRUNCATION_MARK,
    LogLevel,
    LogRouter,
    LogStream,
    parse_log_level,
)
from maika.settings_store import set_value

SETTINGS = build_foundation_registry()

OWNER_ID = 100
NOW = 1_800_000_000

ENV_CHANNEL = 1111
BOT_LOG_CHANNEL = 2222
ERRORS_CHANNEL = 3333


class FakeSend:
    def __init__(self, fail_on=()):
        self.sent = []
        self.attempts = []
        self.fail_on = set(fail_on)

    async def __call__(self, channel_id, text):
        self.attempts.append((channel_id, text))
        if text in self.fail_on:
            raise RuntimeError("send failed")
        self.sent.append((channel_id, text))


def set_setting(connection, key, text):
    set_value(connection, SETTINGS, key, text, actor_id=OWNER_ID, now=NOW)


def run_router(router, before_close=None):
    """Start run(), let it work, then close the router."""

    async def scenario():
        task = asyncio.create_task(router.run())
        await asyncio.sleep(0)
        if before_close is not None:
            before_close()
        await router.close()
        await task

    asyncio.run(scenario())


def make_router(connection, send, fallback=ENV_CHANNEL):
    return LogRouter(connection, fallback, send)


# parse_log_level


@pytest.mark.parametrize(
    ("text", "level"),
    [
        ("debug", LogLevel.DEBUG),
        ("info", LogLevel.INFO),
        ("warning", LogLevel.WARNING),
        ("error", LogLevel.ERROR),
        (" INFO ", LogLevel.INFO),
    ],
)
def test_parse_log_level(text, level):
    assert parse_log_level(text) is level


def test_parse_log_level_rejects_unknown_text():
    with pytest.raises(ValueError, match="debug, info, warning, error"):
        parse_log_level("verbose")


def test_levels_are_ordered_by_severity():
    assert LogLevel.DEBUG < LogLevel.INFO < LogLevel.WARNING < LogLevel.ERROR


def test_stream_values_match_setting_names():
    for stream in LogStream:
        assert f"log.channel.{stream}" in SETTINGS.keys()
        assert f"log.level.{stream}" in SETTINGS.keys()
    assert len(LogStream) == 8


# Level filtering


@pytest.mark.parametrize(
    ("setting", "passing"),
    [
        ("debug", {"DEBUG", "INFO", "WARNING", "ERROR"}),
        ("info", {"INFO", "WARNING", "ERROR"}),
        ("warning", {"WARNING", "ERROR"}),
        ("error", {"ERROR"}),
    ],
)
def test_level_filtering(migrated_db, setting, passing):
    set_setting(migrated_db, "log.level.feeds", setting)
    send = FakeSend()
    router = make_router(migrated_db, send)
    for level in LogLevel:
        router.post(LogStream.FEEDS, level, level.name)
    run_router(router)
    assert {text for _, text in send.sent} == passing


def test_default_level_is_info(migrated_db):
    send = FakeSend()
    router = make_router(migrated_db, send)
    router.post(LogStream.BOT_LOG, LogLevel.DEBUG, "hidden")
    router.post(LogStream.BOT_LOG, LogLevel.INFO, "shown")
    run_router(router)
    assert send.sent == [(ENV_CHANNEL, "shown")]


def test_level_setting_is_per_stream(migrated_db):
    set_setting(migrated_db, "log.level.errors", "debug")
    send = FakeSend()
    router = make_router(migrated_db, send)
    router.post(LogStream.ERRORS, LogLevel.DEBUG, "errors debug")
    router.post(LogStream.BOT_LOG, LogLevel.DEBUG, "bot_log debug")
    run_router(router)
    assert send.sent == [(ENV_CHANNEL, "errors debug")]


# Channel fallback


def test_stream_channel_setting_wins(migrated_db):
    set_setting(migrated_db, "log.channel.errors", str(ERRORS_CHANNEL))
    set_setting(migrated_db, "log.channel.bot_log", str(BOT_LOG_CHANNEL))
    send = FakeSend()
    router = make_router(migrated_db, send)
    router.post(LogStream.ERRORS, LogLevel.ERROR, "boom")
    run_router(router)
    assert send.sent == [(ERRORS_CHANNEL, "boom")]


def test_falls_back_to_bot_log_setting(migrated_db):
    set_setting(migrated_db, "log.channel.bot_log", str(BOT_LOG_CHANNEL))
    send = FakeSend()
    router = make_router(migrated_db, send)
    router.post(LogStream.ERRORS, LogLevel.ERROR, "boom")
    run_router(router)
    assert send.sent == [(BOT_LOG_CHANNEL, "boom")]


def test_falls_back_to_env_channel(migrated_db):
    send = FakeSend()
    router = make_router(migrated_db, send)
    router.post(LogStream.ERRORS, LogLevel.ERROR, "boom")
    router.post(LogStream.BOT_LOG, LogLevel.INFO, "hello")
    run_router(router)
    assert send.sent == [(ENV_CHANNEL, "boom"), (ENV_CHANNEL, "hello")]


def test_no_channel_sends_nothing(migrated_db, caplog):
    send = FakeSend()
    router = make_router(migrated_db, send, fallback=None)
    with caplog.at_level(logging.DEBUG, logger="maika"):
        router.post(LogStream.ERRORS, LogLevel.ERROR, "boom")
    run_router(router)
    assert send.attempts == []
    records = [r for r in caplog.records if "No log channel" in r.message]
    assert len(records) == 1
    assert records[0].levelno == logging.DEBUG
    assert records[0].name == "maika.log_router"


# Truncation


def test_long_text_is_cut_with_ellipsis(migrated_db):
    send = FakeSend()
    router = make_router(migrated_db, send)
    router.post(LogStream.BOT_LOG, LogLevel.INFO, "x" * 3000)
    run_router(router)
    [(_, text)] = send.sent
    assert len(text) == DISCORD_MESSAGE_LIMIT
    assert text.endswith(TRUNCATION_MARK)
    assert text[:-1] == "x" * (DISCORD_MESSAGE_LIMIT - 1)


def test_text_at_limit_is_unchanged(migrated_db):
    send = FakeSend()
    router = make_router(migrated_db, send)
    text = "y" * DISCORD_MESSAGE_LIMIT
    router.post(LogStream.BOT_LOG, LogLevel.INFO, text)
    run_router(router)
    assert send.sent == [(ENV_CHANNEL, text)]


# Queue overflow


def test_overflow_drops_oldest_and_warns_once(migrated_db, caplog):
    send = FakeSend()
    router = make_router(migrated_db, send)
    extra = 5
    with caplog.at_level(logging.WARNING, logger="maika"):
        for number in range(LOG_QUEUE_LIMIT + extra):
            router.post(LogStream.BOT_LOG, LogLevel.INFO, f"line {number}")
    run_router(router)
    texts = [text for _, text in send.sent]
    assert texts == [
        f"line {number}" for number in range(extra, LOG_QUEUE_LIMIT + extra)
    ]
    warnings = [r for r in caplog.records if "log queue is full" in r.message]
    assert len(warnings) == 1


def test_overflow_warns_again_after_queue_empties(migrated_db, caplog):
    send = FakeSend()
    router = make_router(migrated_db, send)

    def fill():
        for number in range(LOG_QUEUE_LIMIT + 1):
            router.post(LogStream.BOT_LOG, LogLevel.INFO, f"line {number}")

    async def scenario():
        fill()
        task = asyncio.create_task(router.run())
        # Let run() empty the queue, then overflow it a second time.
        while len(send.sent) < LOG_QUEUE_LIMIT:
            await asyncio.sleep(0)
        fill()
        await router.close()
        await task

    with caplog.at_level(logging.WARNING, logger="maika"):
        asyncio.run(scenario())
    warnings = [r for r in caplog.records if "log queue is full" in r.message]
    assert len(warnings) == 2


# Failing send


def test_failing_send_does_not_stop_later_lines(migrated_db, caplog):
    send = FakeSend(fail_on={"bad"})
    router = make_router(migrated_db, send)
    router.post(LogStream.BOT_LOG, LogLevel.INFO, "first")
    router.post(LogStream.BOT_LOG, LogLevel.INFO, "bad")
    router.post(LogStream.BOT_LOG, LogLevel.INFO, "last")
    with caplog.at_level(logging.ERROR, logger="maika"):
        run_router(router)
    assert send.sent == [(ENV_CHANNEL, "first"), (ENV_CHANNEL, "last")]
    # The failed line was tried once and nothing else was posted.
    assert send.attempts == [
        (ENV_CHANNEL, "first"),
        (ENV_CHANNEL, "bad"),
        (ENV_CHANNEL, "last"),
    ]
    failures = [r for r in caplog.records if "failed" in r.message]
    assert len(failures) == 1
    assert failures[0].exc_info is not None


# close and run


def test_close_drains_pending_lines(migrated_db):
    send = FakeSend()
    router = make_router(migrated_db, send)

    def post_lines():
        router.post(LogStream.BOT_LOG, LogLevel.INFO, "one")
        router.post(LogStream.BOT_LOG, LogLevel.INFO, "two")

    run_router(router, before_close=post_lines)
    assert send.sent == [(ENV_CHANNEL, "one"), (ENV_CHANNEL, "two")]


def test_lines_after_close_are_ignored(migrated_db):
    send = FakeSend()
    router = make_router(migrated_db, send)
    run_router(router)
    router.post(LogStream.BOT_LOG, LogLevel.INFO, "late")
    assert send.attempts == []


def test_lines_posted_before_run_are_sent(migrated_db):
    send = FakeSend()
    router = make_router(migrated_db, send)
    router.post(LogStream.BOT_LOG, LogLevel.INFO, "early")

    async def scenario():
        task = asyncio.create_task(router.run())
        while not send.sent:
            await asyncio.sleep(0)
        assert send.sent == [(ENV_CHANNEL, "early")]
        await router.close()
        await task

    asyncio.run(scenario())


def test_close_stops_a_stuck_send(migrated_db, monkeypatch, caplog):
    monkeypatch.setattr("maika.log_router.LOG_DRAIN_SECONDS", 0.01)
    started = []

    async def stuck_send(channel_id, text):
        started.append(text)
        await asyncio.Event().wait()

    router = make_router(migrated_db, stuck_send)
    router.post(LogStream.BOT_LOG, LogLevel.INFO, "stuck")
    router.post(LogStream.BOT_LOG, LogLevel.INFO, "waiting")

    async def scenario():
        task = asyncio.create_task(router.run())
        await asyncio.sleep(0)
        await router.close()
        assert task.done()

    with caplog.at_level(logging.WARNING, logger="maika"):
        asyncio.run(scenario())
    assert started == ["stuck"]
    assert any("1 queued lines" in r.message for r in caplog.records)
