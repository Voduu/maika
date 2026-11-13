import asyncio
from pathlib import Path
from unittest import mock

import pytest

from maika.bot import MaikaClient
from maika.command_catalog import build_command_registry
from maika.config import Config, Environment
from maika.discord_reminder_sender import DiscordReminderSender
from maika.foundation_settings import build_foundation_registry
from maika.permission_store import set_block
from maika.permissions import BLOCK_KIND_ALL
from maika.reminder_delivery import DeliverySummary, SendOutcome
from maika.reminder_loop import run_reminder_cycle
from maika.reminders import create_reminder
from maika.settings_store import set_value

NOW = 1_800_000_000
MINUTE = 60

OWNER = 1
ALEX = 100
BLOCKED = 300
CHANNEL = 7000

SETTINGS = build_foundation_registry()


class FakeSender:
    def __init__(self, connection, *, dm=SendOutcome.SENT):
        self.connection = connection
        self.dm = dm
        self.calls = []

    async def send_dm(self, user_id, text):
        assert self.connection.in_transaction is False
        self.calls.append(("dm", user_id, text))
        if isinstance(self.dm, BaseException):
            raise self.dm
        return self.dm

    async def post_to_channel(self, channel_id, user_id, text):
        raise AssertionError("not used")

    async def post_notice(self, channel_id, user_id, text):
        self.calls.append(("notice", channel_id, user_id, text))
        return SendOutcome.SENT

    async def channel_access(self, channel_id, user_id):
        raise AssertionError("not used")


def add(connection, user_id=ALEX):
    return create_reminder(
        connection,
        user_id=user_id,
        message="water plants",
        due_at=NOW - MINUTE,
        delivery="dm",
        channel_id=None,
        repeat_rule=None,
        timezone=None,
        now=NOW - 7 * 86400,
    )


def row(connection, reminder_id):
    return dict(
        connection.execute(
            "SELECT * FROM reminders WHERE id = ?", (reminder_id,)
        ).fetchone()
    )


# run_reminder_cycle


def test_cycle_delivers_due_reminder(migrated_db):
    reminder_id = add(migrated_db)
    sender = FakeSender(migrated_db)

    summary = asyncio.run(run_reminder_cycle(migrated_db, sender, NOW))

    assert summary == DeliverySummary(delivered=1)
    assert sender.calls == [("dm", ALEX, "Reminder: water plants")]
    stored = row(migrated_db, reminder_id)
    assert stored["status"] == "delivered"
    assert stored["delivered_at"] == NOW


def test_cycle_skips_globally_blocked_person(migrated_db):
    set_block(
        migrated_db,
        user_id=BLOCKED,
        kind=BLOCK_KIND_ALL,
        guild_id=0,
        owner_id=OWNER,
        actor_id=OWNER,
        now=NOW,
    )
    blocked = add(migrated_db, user_id=BLOCKED)
    before = row(migrated_db, blocked)
    sender = FakeSender(migrated_db)

    summary = asyncio.run(run_reminder_cycle(migrated_db, sender, NOW))

    assert summary == DeliverySummary(skipped=1)
    assert sender.calls == []
    assert row(migrated_db, blocked) == before


def test_cancellation_inside_send_leaves_row_pending(migrated_db):
    reminder_id = add(migrated_db)
    before = row(migrated_db, reminder_id)
    sender = FakeSender(migrated_db, dm=asyncio.CancelledError())

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(run_reminder_cycle(migrated_db, sender, NOW))

    assert row(migrated_db, reminder_id) == before
    assert before["status"] == "pending"
    assert migrated_db.in_transaction is False


# The bot's reminder methods


def make_client(connection):
    config = Config(
        environment=Environment.TEST,
        discord_token="unused-token",
        owner_id=OWNER,
        listen_channels=(),
        log_channel_id=None,
        database_path=Path("unused.db"),
    )
    return MaikaClient(
        config,
        connection=connection,
        command_registry=build_command_registry(),
    )


def test_bot_reads_check_seconds(migrated_db):
    client = make_client(migrated_db)

    assert client._read_reminder_interval() == 1

    set_value(
        migrated_db,
        SETTINGS,
        "reminders.check_seconds",
        "30",
        actor_id=OWNER,
        now=NOW,
    )
    assert client._read_reminder_interval() == 30


def test_bot_cycle_waits_for_ready_then_delivers(migrated_db, monkeypatch):
    client = make_client(migrated_db)
    events = []

    async def wait_until_ready():
        events.append("ready")

    async def fake_cycle(connection, sender, now):
        events.append(("cycle", connection, sender, now))
        return DeliverySummary()

    monkeypatch.setattr(client, "wait_until_ready", wait_until_ready)
    monkeypatch.setattr("maika.reminder_loop.run_reminder_cycle", fake_cycle)
    monkeypatch.setattr(
        "maika.bot.time.time", mock.Mock(return_value=NOW + 0.7)
    )

    summary = asyncio.run(client._run_reminder_cycle())

    assert summary == DeliverySummary()
    assert events[0] == "ready"
    _, connection, sender, now = events[1]
    assert connection is migrated_db
    assert isinstance(sender, DiscordReminderSender)
    assert sender.client is client
    assert now == NOW
