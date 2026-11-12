import asyncio
import datetime
import logging
import zoneinfo

import pytest

from maika.permission_store import set_block
from maika.permissions import BLOCK_KIND_ALL, BLOCK_KIND_CHAT
from maika.reminder_delivery import (
    DEFAULT_BATCH_SIZE,
    LATE_AFTER_SECONDS,
    ChannelAccess,
    DeliverySummary,
    SendOutcome,
    build_block_check,
    compose_text,
    deliver_due_reminders,
    is_blocked,
)
from maika.reminders import create_reminder
from maika.repeat_rules import RepeatChoice, build_repeat_rule, next_occurrence

LOS_ANGELES_NAME = "America/Los_Angeles"
LOS_ANGELES = zoneinfo.ZoneInfo(LOS_ANGELES_NAME)

# Wednesday 2026-11-11 19:42 in Los Angeles.
NOW = int(
    datetime.datetime(2026, 11, 11, 19, 42, tzinfo=LOS_ANGELES).timestamp()
)
MINUTE = 60
DAY = 86400

OWNER = 1
ALEX = 100
SAM = 200
BLOCKED = 300
GUILD = 5000
OTHER_GUILD = 6000
CHANNEL = 7000
OTHER_CHANNEL = 7100

SENT = SendOutcome.SENT
REFUSED = SendOutcome.REFUSED
RETRY = SendOutcome.RETRY

CAN_VIEW = ChannelAccess(server_id=GUILD, creator_can_view=True)
CANNOT_VIEW = ChannelAccess(server_id=GUILD, creator_can_view=False)
CHANNEL_GONE = ChannelAccess(server_id=None, creator_can_view=False)

NOTICE = "<@100> I couldn't DM you a reminder. Check `/remind list`."

MESSAGE = "water the secret plants"


class FakeSender:
    """Records every call and returns scripted outcomes.

    ``access`` is one ChannelAccess (or None) for every channel, or a
    dict by channel id. ``raise_for`` makes every call for that user
    raise RuntimeError.
    """

    def __init__(
        self,
        connection,
        *,
        dm=SENT,
        post=SENT,
        notice=SENT,
        access=CAN_VIEW,
        raise_for=None,
    ):
        self.connection = connection
        self.dm = dm
        self.post = post
        self.notice = notice
        self.access = access
        self.raise_for = raise_for
        self.calls = []

    def _called(self, call, user_id):
        assert self.connection.in_transaction is False
        self.calls.append(call)
        if user_id == self.raise_for:
            raise RuntimeError(f"boom while sending {MESSAGE}")

    async def send_dm(self, user_id, text):
        self._called(("dm", user_id, text), user_id)
        return self.dm

    async def post_to_channel(self, channel_id, user_id, text):
        self._called(("post", channel_id, user_id, text), user_id)
        return self.post

    async def post_notice(self, channel_id, user_id, text):
        self._called(("notice", channel_id, user_id, text), user_id)
        return self.notice

    async def channel_access(self, channel_id, user_id):
        self._called(("access", channel_id, user_id), user_id)
        if isinstance(self.access, dict):
            return self.access.get(channel_id)
        return self.access

    def kinds(self):
        return [call[0] for call in self.calls]


class FakeBlocks:
    """A fake is_blocked: ``all`` blocks as (user, server) pairs.

    A server of None is a global block.
    """

    def __init__(self, *blocks):
        self.blocks = set(blocks)

    def __call__(self, user_id, server_id):
        if (user_id, None) in self.blocks:
            return True
        return server_id is not None and (user_id, server_id) in self.blocks


def add(
    connection,
    *,
    user_id=ALEX,
    message=MESSAGE,
    due_at=NOW - MINUTE,
    delivery="dm",
    channel_id=CHANNEL,
    repeat_rule=None,
    timezone=None,
    status="pending",
):
    reminder_id = create_reminder(
        connection,
        user_id=user_id,
        message=message,
        due_at=due_at,
        delivery=delivery,
        channel_id=channel_id,
        repeat_rule=repeat_rule,
        timezone=timezone,
        now=NOW - 7 * DAY,
    )
    connection.execute(
        "UPDATE reminders SET status = ? WHERE id = ?", (status, reminder_id)
    )
    return reminder_id


def add_daily(connection, *, due_at=NOW - MINUTE, **changes):
    rule = build_repeat_rule(
        RepeatChoice.DAILY, first_due=due_at, timezone=LOS_ANGELES_NAME
    )
    return add(
        connection,
        due_at=due_at,
        repeat_rule=rule,
        timezone=LOS_ANGELES_NAME,
        **changes,
    )


def row(connection, reminder_id):
    return dict(
        connection.execute(
            "SELECT * FROM reminders WHERE id = ?", (reminder_id,)
        ).fetchone()
    )


def deliver(connection, sender, blocks=None, *, now=NOW, **options):
    if blocks is None:
        blocks = FakeBlocks()
    return asyncio.run(
        deliver_due_reminders(connection, sender, blocks, now=now, **options)
    )


def body(message=MESSAGE):
    return f"Reminder: {message}"


def audit_count(connection):
    return connection.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]


def assert_unchanged(before, after):
    assert after == before
    assert after["status"] == "pending"


# Message text


def test_compose_text_on_time():
    assert compose_text("tea", due_at=NOW, now=NOW) == "Reminder: tea"


def test_late_after_constant():
    assert LATE_AFTER_SECONDS == 300
    assert DEFAULT_BATCH_SIZE == 50


@pytest.mark.parametrize(
    ("late_seconds", "has_late_line"),
    [(6 * MINUTE, True), (4 * MINUTE, False), (300, False), (301, True)],
)
def test_late_line(migrated_db, late_seconds, has_late_line):
    due_at = NOW - late_seconds
    add(migrated_db, due_at=due_at)
    sender = FakeSender(migrated_db)

    deliver(migrated_db, sender)

    ((_, _, text),) = sender.calls
    if has_late_line:
        assert text == f"{body()}\nThis was due <t:{due_at}:R>."
    else:
        assert text == body()


# One-time DM


def test_one_time_dm_sent(migrated_db):
    reminder_id = add(migrated_db)
    sender = FakeSender(migrated_db)

    summary = deliver(migrated_db, sender)

    assert sender.calls == [("dm", ALEX, body())]
    stored = row(migrated_db, reminder_id)
    assert stored["status"] == "delivered"
    assert stored["delivered_at"] == NOW
    assert stored["delivered_via"] == "dm"
    assert stored["due_at"] == NOW - MINUTE
    assert summary == DeliverySummary(delivered=1)


def test_one_time_dm_refused_posts_notice(migrated_db):
    reminder_id = add(migrated_db)
    sender = FakeSender(migrated_db, dm=REFUSED)

    summary = deliver(migrated_db, sender)

    assert sender.calls == [
        ("dm", ALEX, body()),
        ("access", CHANNEL, ALEX),
        ("notice", CHANNEL, ALEX, NOTICE),
    ]
    stored = row(migrated_db, reminder_id)
    assert stored["status"] == "failed"
    assert stored["delivered_at"] is None
    assert stored["delivered_via"] is None
    assert summary == DeliverySummary(failed=1)


def test_notice_never_contains_reminder_text(migrated_db):
    add(migrated_db)
    sender = FakeSender(migrated_db, dm=REFUSED)

    deliver(migrated_db, sender)

    notice = sender.calls[-1]
    assert notice[0] == "notice"
    assert MESSAGE not in notice[3]


def test_dm_refused_without_channel_has_no_notice(migrated_db):
    reminder_id = add(migrated_db, channel_id=None)
    sender = FakeSender(migrated_db, dm=REFUSED)

    deliver(migrated_db, sender)

    assert sender.calls == [("dm", ALEX, body())]
    assert row(migrated_db, reminder_id)["status"] == "failed"


@pytest.mark.parametrize("access", [CANNOT_VIEW, CHANNEL_GONE, None])
def test_dm_refused_no_notice_when_channel_not_usable(migrated_db, access):
    reminder_id = add(migrated_db)
    sender = FakeSender(migrated_db, dm=REFUSED, access=access)

    deliver(migrated_db, sender)

    assert sender.kinds() == ["dm", "access"]
    assert row(migrated_db, reminder_id)["status"] == "failed"


def test_dm_refused_no_notice_when_blocked_in_that_server(migrated_db):
    reminder_id = add(migrated_db)
    sender = FakeSender(migrated_db, dm=REFUSED)

    deliver(migrated_db, sender, FakeBlocks((ALEX, GUILD)))

    assert sender.kinds() == ["dm", "access"]
    assert row(migrated_db, reminder_id)["status"] == "failed"


def test_dm_reminder_sent_despite_server_block(migrated_db):
    reminder_id = add(migrated_db)
    sender = FakeSender(migrated_db)

    deliver(migrated_db, sender, FakeBlocks((ALEX, GUILD)))

    assert sender.calls == [("dm", ALEX, body())]
    assert row(migrated_db, reminder_id)["status"] == "delivered"


@pytest.mark.parametrize("notice", [REFUSED, RETRY])
def test_failing_notice_is_ignored(migrated_db, notice):
    reminder_id = add(migrated_db)
    sender = FakeSender(migrated_db, dm=REFUSED, notice=notice)

    summary = deliver(migrated_db, sender)

    assert sender.kinds() == ["dm", "access", "notice"]
    assert row(migrated_db, reminder_id)["status"] == "failed"
    assert summary == DeliverySummary(failed=1)


def test_one_time_dm_retry_leaves_row_unchanged(migrated_db):
    reminder_id = add(migrated_db)
    before = row(migrated_db, reminder_id)
    sender = FakeSender(migrated_db, dm=RETRY)

    summary = deliver(migrated_db, sender)

    assert sender.calls == [("dm", ALEX, body())]
    assert_unchanged(before, row(migrated_db, reminder_id))
    assert summary == DeliverySummary(retried=1)


def test_channel_reminder_without_channel_goes_by_dm(migrated_db):
    reminder_id = add(migrated_db, delivery="channel", channel_id=None)
    sender = FakeSender(migrated_db)

    deliver(migrated_db, sender)

    assert sender.calls == [("dm", ALEX, body())]
    assert row(migrated_db, reminder_id)["delivered_via"] == "dm"


# Channel reminders


def test_channel_reminder_posted(migrated_db):
    reminder_id = add(migrated_db, delivery="channel")
    sender = FakeSender(migrated_db)

    summary = deliver(migrated_db, sender)

    assert sender.calls == [
        ("access", CHANNEL, ALEX),
        ("post", CHANNEL, ALEX, f"<@{ALEX}> {body()}"),
    ]
    stored = row(migrated_db, reminder_id)
    assert stored["status"] == "delivered"
    assert stored["delivered_at"] == NOW
    assert stored["delivered_via"] == "channel"
    assert summary == DeliverySummary(delivered=1)


def test_channel_post_late_line_follows_mention(migrated_db):
    due_at = NOW - 10 * MINUTE
    add(migrated_db, delivery="channel", due_at=due_at)
    sender = FakeSender(migrated_db)

    deliver(migrated_db, sender)

    assert sender.calls[-1][3] == (
        f"<@{ALEX}> {body()}\nThis was due <t:{due_at}:R>."
    )


@pytest.mark.parametrize("access", [CANNOT_VIEW, CHANNEL_GONE])
def test_channel_reminder_goes_by_dm_when_creator_cannot_view(
    migrated_db, access
):
    reminder_id = add(migrated_db, delivery="channel")
    sender = FakeSender(migrated_db, access=access)

    deliver(migrated_db, sender)

    assert sender.calls == [("access", CHANNEL, ALEX), ("dm", ALEX, body())]
    stored = row(migrated_db, reminder_id)
    assert stored["status"] == "delivered"
    assert stored["delivered_via"] == "dm"


def test_channel_post_refused_falls_back_to_dm(migrated_db):
    reminder_id = add(migrated_db, delivery="channel")
    sender = FakeSender(migrated_db, post=REFUSED)

    deliver(migrated_db, sender)

    assert sender.kinds() == ["access", "post", "dm"]
    assert sender.calls[-1] == ("dm", ALEX, body())
    assert row(migrated_db, reminder_id)["delivered_via"] == "dm"


def test_channel_post_and_dm_refused_fails_without_notice(migrated_db):
    reminder_id = add(migrated_db, delivery="channel")
    sender = FakeSender(migrated_db, post=REFUSED, dm=REFUSED)

    summary = deliver(migrated_db, sender)

    assert sender.kinds() == ["access", "post", "dm"]
    assert row(migrated_db, reminder_id)["status"] == "failed"
    assert summary == DeliverySummary(failed=1)


def test_channel_dm_fallback_retry_leaves_row_unchanged(migrated_db):
    reminder_id = add(migrated_db, delivery="channel")
    before = row(migrated_db, reminder_id)
    sender = FakeSender(migrated_db, post=REFUSED, dm=RETRY)

    deliver(migrated_db, sender)

    assert_unchanged(before, row(migrated_db, reminder_id))


def test_channel_access_unknown_leaves_row_unchanged(migrated_db):
    reminder_id = add(migrated_db, delivery="channel")
    before = row(migrated_db, reminder_id)
    sender = FakeSender(migrated_db, access=None)

    summary = deliver(migrated_db, sender)

    assert sender.kinds() == ["access"]
    assert_unchanged(before, row(migrated_db, reminder_id))
    assert summary == DeliverySummary(retried=1)


def test_channel_post_retry_leaves_row_unchanged(migrated_db):
    reminder_id = add(migrated_db, delivery="channel")
    before = row(migrated_db, reminder_id)
    sender = FakeSender(migrated_db, post=RETRY)

    deliver(migrated_db, sender)

    assert sender.kinds() == ["access", "post"]
    assert_unchanged(before, row(migrated_db, reminder_id))


def test_server_block_sends_channel_reminder_by_dm(migrated_db):
    reminder_id = add(migrated_db, delivery="channel")
    sender = FakeSender(migrated_db)

    deliver(migrated_db, sender, FakeBlocks((ALEX, GUILD)))

    assert sender.calls == [("access", CHANNEL, ALEX), ("dm", ALEX, body())]
    assert row(migrated_db, reminder_id)["delivered_via"] == "dm"


def test_block_in_another_server_does_not_stop_post(migrated_db):
    reminder_id = add(migrated_db, delivery="channel")
    sender = FakeSender(migrated_db)

    deliver(migrated_db, sender, FakeBlocks((ALEX, OTHER_GUILD)))

    assert sender.kinds() == ["access", "post"]
    assert row(migrated_db, reminder_id)["delivered_via"] == "channel"


def test_channel_access_asked_once_per_channel_and_person(migrated_db):
    add(migrated_db, delivery="channel")
    add(migrated_db, delivery="channel")
    sender = FakeSender(migrated_db)

    summary = deliver(migrated_db, sender)

    assert sender.kinds() == ["access", "post", "post"]
    assert summary == DeliverySummary(delivered=2)


# Global blocks


def test_global_block_skips_every_kind(migrated_db):
    ids = [
        add(migrated_db, user_id=BLOCKED),
        add(migrated_db, user_id=BLOCKED, delivery="channel"),
        add_daily(migrated_db, user_id=BLOCKED),
    ]
    before = [row(migrated_db, reminder_id) for reminder_id in ids]
    sender = FakeSender(migrated_db)

    summary = deliver(migrated_db, sender, FakeBlocks((BLOCKED, None)))

    assert sender.calls == []
    assert [row(migrated_db, reminder_id) for reminder_id in ids] == before
    assert summary == DeliverySummary(skipped=3)


def test_delivered_after_unblock_with_late_line(migrated_db):
    due_at = NOW - 2 * DAY
    one_time = add(migrated_db, user_id=BLOCKED, due_at=due_at)
    daily = add_daily(migrated_db, user_id=BLOCKED, due_at=due_at)
    blocked = FakeBlocks((BLOCKED, None))
    deliver(migrated_db, FakeSender(migrated_db), blocked)
    sender = FakeSender(migrated_db)

    summary = deliver(migrated_db, sender, FakeBlocks())

    late_text = f"{body()}\nThis was due <t:{due_at}:R>."
    assert sender.calls == [
        ("dm", BLOCKED, late_text),
        ("dm", BLOCKED, late_text),
    ]
    assert row(migrated_db, one_time)["status"] == "delivered"
    stored_daily = row(migrated_db, daily)
    assert stored_daily["status"] == "pending"
    assert stored_daily["due_at"] == due_at + 3 * DAY
    assert stored_daily["due_at"] > NOW
    assert summary == DeliverySummary(delivered=1, rescheduled=1)


def test_starvation_blocked_rows_do_not_use_the_batch(migrated_db):
    blocked_ids = [
        add(migrated_db, user_id=BLOCKED, due_at=NOW - DAY + number)
        for number in range(60)
    ]
    other_ids = [
        add(migrated_db, user_id=user_id, due_at=NOW - MINUTE)
        for user_id in (ALEX, SAM, ALEX, SAM, ALEX)
    ]
    sender = FakeSender(migrated_db)

    summary = deliver(migrated_db, sender, FakeBlocks((BLOCKED, None)))

    assert summary == DeliverySummary(delivered=5, skipped=60)
    assert all(
        row(migrated_db, reminder_id)["status"] == "delivered"
        for reminder_id in other_ids
    )
    assert all(
        row(migrated_db, reminder_id)["status"] == "pending"
        for reminder_id in blocked_ids
    )


# The real block check


def block(connection, kind, guild_id):
    set_block(
        connection,
        user_id=ALEX,
        kind=kind,
        guild_id=guild_id,
        owner_id=OWNER,
        actor_id=OWNER,
        now=NOW,
    )


def test_is_blocked_without_blocks(migrated_db):
    assert is_blocked(migrated_db, ALEX, None) is False
    assert is_blocked(migrated_db, ALEX, GUILD) is False


def test_is_blocked_global_all(migrated_db):
    block(migrated_db, BLOCK_KIND_ALL, 0)

    assert is_blocked(migrated_db, ALEX, None) is True
    assert is_blocked(migrated_db, ALEX, GUILD) is True
    assert is_blocked(migrated_db, SAM, None) is False


def test_is_blocked_server_all(migrated_db):
    block(migrated_db, BLOCK_KIND_ALL, GUILD)

    assert is_blocked(migrated_db, ALEX, None) is False
    assert is_blocked(migrated_db, ALEX, GUILD) is True
    assert is_blocked(migrated_db, ALEX, OTHER_GUILD) is False


@pytest.mark.parametrize("guild_id", [0, GUILD])
def test_is_blocked_ignores_chat_block(migrated_db, guild_id):
    block(migrated_db, BLOCK_KIND_CHAT, guild_id)

    assert is_blocked(migrated_db, ALEX, None) is False
    assert is_blocked(migrated_db, ALEX, GUILD) is False


def test_chat_block_does_not_skip(migrated_db):
    block(migrated_db, BLOCK_KIND_CHAT, 0)
    reminder_id = add(migrated_db)
    sender = FakeSender(migrated_db)

    deliver(migrated_db, sender, build_block_check(migrated_db))

    assert sender.calls == [("dm", ALEX, body())]
    assert row(migrated_db, reminder_id)["status"] == "delivered"


def test_real_global_all_block_skips(migrated_db):
    block(migrated_db, BLOCK_KIND_ALL, 0)
    reminder_id = add(migrated_db)
    before = row(migrated_db, reminder_id)
    sender = FakeSender(migrated_db)

    deliver(migrated_db, sender, build_block_check(migrated_db))

    assert sender.calls == []
    assert row(migrated_db, reminder_id) == before


# Repeating reminders


def test_repeating_sent_moves_to_next_time(migrated_db):
    due_at = NOW - MINUTE
    reminder_id = add_daily(migrated_db, due_at=due_at)
    rule = row(migrated_db, reminder_id)["repeat_rule"]
    sender = FakeSender(migrated_db)

    summary = deliver(migrated_db, sender)

    expected = next_occurrence(
        rule, current_due=due_at, after=NOW, timezone=LOS_ANGELES_NAME
    )
    stored = row(migrated_db, reminder_id)
    assert sender.calls == [("dm", ALEX, body())]
    assert stored["status"] == "pending"
    assert stored["due_at"] == expected == due_at + DAY
    assert stored["delivered_at"] == NOW
    assert stored["delivered_via"] == "dm"
    assert summary == DeliverySummary(rescheduled=1)


def test_daily_three_days_overdue_sent_once(migrated_db):
    due_at = NOW - 3 * DAY - MINUTE
    reminder_id = add_daily(migrated_db, due_at=due_at)
    sender = FakeSender(migrated_db)

    deliver(migrated_db, sender)

    assert sender.kinds() == ["dm"]
    stored = row(migrated_db, reminder_id)
    assert stored["due_at"] == NOW - MINUTE + DAY
    assert stored["status"] == "pending"

    second = FakeSender(migrated_db)
    deliver(migrated_db, second)
    assert second.calls == []


def test_repeating_dm_refused_posts_notice_and_moves_on(migrated_db):
    due_at = NOW - MINUTE
    reminder_id = add_daily(migrated_db, due_at=due_at)
    migrated_db.execute(
        "UPDATE reminders SET delivered_at = ?, delivered_via = 'dm' "
        "WHERE id = ?",
        (NOW - DAY, reminder_id),
    )
    sender = FakeSender(migrated_db, dm=REFUSED)

    summary = deliver(migrated_db, sender)

    assert sender.calls[-1] == ("notice", CHANNEL, ALEX, NOTICE)
    stored = row(migrated_db, reminder_id)
    assert stored["status"] == "pending"
    assert stored["due_at"] == due_at + DAY
    assert stored["delivered_at"] == NOW - DAY
    assert stored["delivered_via"] == "dm"
    assert summary == DeliverySummary(rescheduled=1)


def test_repeating_channel_both_refused_moves_on(migrated_db):
    due_at = NOW - MINUTE
    reminder_id = add_daily(migrated_db, due_at=due_at, delivery="channel")
    sender = FakeSender(migrated_db, post=REFUSED, dm=REFUSED)

    deliver(migrated_db, sender)

    assert sender.kinds() == ["access", "post", "dm"]
    stored = row(migrated_db, reminder_id)
    assert stored["status"] == "pending"
    assert stored["due_at"] == due_at + DAY
    assert stored["delivered_at"] is None
    assert stored["delivered_via"] is None


def test_repeating_channel_sent_records_channel(migrated_db):
    reminder_id = add_daily(migrated_db, delivery="channel")
    sender = FakeSender(migrated_db)

    deliver(migrated_db, sender)

    assert row(migrated_db, reminder_id)["delivered_via"] == "channel"


def test_repeating_retry_leaves_row_unchanged(migrated_db):
    reminder_id = add_daily(migrated_db)
    before = row(migrated_db, reminder_id)
    sender = FakeSender(migrated_db, dm=RETRY)

    deliver(migrated_db, sender)

    assert_unchanged(before, row(migrated_db, reminder_id))


@pytest.mark.parametrize("timezone", ["Not/AZone", None])
def test_repeating_with_unloadable_timezone(migrated_db, caplog, timezone):
    reminder_id = add_daily(migrated_db)
    migrated_db.execute(
        "UPDATE reminders SET timezone = ? WHERE id = ?",
        (timezone, reminder_id),
    )
    sender = FakeSender(migrated_db)

    with caplog.at_level(logging.WARNING, logger="maika.reminder_delivery"):
        summary = deliver(migrated_db, sender)

    assert sender.calls == [("dm", ALEX, body())]
    stored = row(migrated_db, reminder_id)
    assert stored["status"] == "failed"
    assert stored["delivered_at"] == NOW
    assert stored["delivered_via"] == "dm"
    assert summary == DeliverySummary(failed=1)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert f"#{reminder_id}" in warnings[0].getMessage()
    assert "timezone_unavailable" in warnings[0].getMessage()


# Selection


def test_rows_processed_in_due_order(migrated_db):
    third = add(migrated_db, message="third", due_at=NOW - MINUTE)
    first = add(migrated_db, message="first", due_at=NOW - 3 * MINUTE)
    second_a = add(migrated_db, message="second a", due_at=NOW - 2 * MINUTE)
    second_b = add(migrated_db, message="second b", due_at=NOW - 2 * MINUTE)
    sender = FakeSender(migrated_db)

    deliver(migrated_db, sender)

    assert [text for _, _, text in sender.calls] == [
        body("first"),
        body("second a"),
        body("second b"),
        body("third"),
    ]
    assert first < second_a < second_b and third < first


def test_only_due_pending_rows_are_processed(migrated_db):
    due = add(migrated_db)
    at_now = add(migrated_db, due_at=NOW)
    future = add(migrated_db, due_at=NOW + 1)
    others = [
        add(migrated_db, status=status)
        for status in ("cancelled", "delivered", "failed")
    ]
    before = {
        reminder_id: row(migrated_db, reminder_id)
        for reminder_id in [future, *others]
    }
    sender = FakeSender(migrated_db)

    summary = deliver(migrated_db, sender)

    assert summary == DeliverySummary(delivered=2)
    assert row(migrated_db, due)["status"] == "delivered"
    assert row(migrated_db, at_now)["status"] == "delivered"
    for reminder_id, stored in before.items():
        assert row(migrated_db, reminder_id) == stored


def test_batch_size_limits_one_check(migrated_db):
    for number in range(60):
        add(migrated_db, due_at=NOW - DAY + number)

    first = deliver(migrated_db, FakeSender(migrated_db), batch_size=50)
    pending = migrated_db.execute(
        "SELECT COUNT(*) FROM reminders WHERE status = 'pending'"
    ).fetchone()[0]
    second = deliver(migrated_db, FakeSender(migrated_db), batch_size=50)

    assert first == DeliverySummary(delivered=50)
    assert pending == 10
    assert second == DeliverySummary(delivered=10)


def test_default_batch_size_is_50(migrated_db):
    for number in range(60):
        add(migrated_db, due_at=NOW - DAY + number)

    summary = deliver(migrated_db, FakeSender(migrated_db))

    assert summary == DeliverySummary(delivered=50)


def test_retried_rows_count_toward_batch_and_are_not_repeated(migrated_db):
    for number in range(5):
        add(migrated_db, due_at=NOW - DAY + number)
    sender = FakeSender(migrated_db, dm=RETRY)

    summary = deliver(migrated_db, sender, batch_size=3)

    assert summary == DeliverySummary(retried=3)
    assert len(sender.calls) == 3


def test_nothing_due(migrated_db):
    sender = FakeSender(migrated_db)
    assert deliver(migrated_db, sender) == DeliverySummary()
    assert sender.calls == []


# Unexpected exceptions, logs and audit


def test_exception_for_one_reminder_does_not_stop_others(migrated_db, caplog):
    first = add(migrated_db, due_at=NOW - 3 * MINUTE)
    broken = add(migrated_db, user_id=SAM, due_at=NOW - 2 * MINUTE)
    last = add(migrated_db, due_at=NOW - MINUTE)
    before = row(migrated_db, broken)
    sender = FakeSender(migrated_db, raise_for=SAM)

    with caplog.at_level(logging.ERROR, logger="maika.reminder_delivery"):
        summary = deliver(migrated_db, sender)

    assert summary == DeliverySummary(delivered=2, errors=1)
    assert row(migrated_db, first)["status"] == "delivered"
    assert row(migrated_db, last)["status"] == "delivered"
    assert_unchanged(before, row(migrated_db, broken))
    (record,) = caplog.records
    text = record.getMessage()
    assert record.levelno == logging.ERROR
    assert text.startswith(
        f"Delivering reminder #{broken} raised RuntimeError."
    )
    # The frames: file, line number and function, from the delivery
    # code down to the fake sender that raised.
    assert 'reminder_delivery.py", line ' in text
    assert "in _deliver_one" in text
    assert 'test_reminder_delivery.py", line ' in text
    assert "in _called" in text
    # The exception's message, which holds the reminder text, is not.
    # (The frame's source line shows the f-string template that built
    # it, which is code, not the message.)
    assert f"boom while sending {MESSAGE}" not in text
    assert MESSAGE not in text
    assert record.exc_info is None


def test_logs_never_contain_reminder_text(migrated_db, caplog):
    add(migrated_db)
    add(migrated_db, user_id=SAM)
    add(migrated_db, delivery="channel")
    add_daily(migrated_db, user_id=SAM)
    add(migrated_db, user_id=BLOCKED)
    broken = add_daily(migrated_db, user_id=SAM, delivery="channel")
    migrated_db.execute(
        "UPDATE reminders SET timezone = 'Not/AZone' WHERE id = ?", (broken,)
    )
    refusing = FakeSender(migrated_db, dm=REFUSED, post=REFUSED)

    with caplog.at_level(logging.DEBUG, logger="maika"):
        deliver(migrated_db, refusing, FakeBlocks((BLOCKED, None)))
        add(migrated_db)
        deliver(migrated_db, FakeSender(migrated_db))
        add(migrated_db)
        deliver(migrated_db, FakeSender(migrated_db, dm=RETRY))
        add(migrated_db, user_id=SAM)
        deliver(migrated_db, FakeSender(migrated_db, raise_for=SAM))

    assert caplog.records
    for record in caplog.records:
        assert MESSAGE not in record.getMessage()
        assert "secret" not in record.getMessage()


def test_info_line_per_delivery(migrated_db, caplog):
    channel = add(migrated_db, delivery="channel")
    dm = add(migrated_db)

    with caplog.at_level(logging.INFO, logger="maika.reminder_delivery"):
        deliver(migrated_db, FakeSender(migrated_db))

    assert [r.getMessage() for r in caplog.records] == [
        f"Reminder #{channel} delivered by channel.",
        f"Reminder #{dm} delivered by dm.",
    ]


def test_warning_per_failure(migrated_db, caplog):
    reminder_id = add(migrated_db, channel_id=None)

    with caplog.at_level(logging.WARNING, logger="maika.reminder_delivery"):
        deliver(migrated_db, FakeSender(migrated_db, dm=REFUSED))

    (record,) = caplog.records
    assert record.levelno == logging.WARNING
    assert record.getMessage() == (
        f"Reminder #{reminder_id} could not be delivered."
    )


def test_no_audit_rows(migrated_db):
    add(migrated_db)
    add(migrated_db, delivery="channel")
    add_daily(migrated_db)
    add(migrated_db, user_id=SAM)

    deliver(migrated_db, FakeSender(migrated_db))
    add(migrated_db)
    deliver(migrated_db, FakeSender(migrated_db, dm=REFUSED))

    assert audit_count(migrated_db) == 0


def test_no_transaction_left_open(migrated_db):
    add(migrated_db)
    add_daily(migrated_db)

    deliver(migrated_db, FakeSender(migrated_db))

    assert migrated_db.in_transaction is False


def test_cancelled_while_sending_stays_cancelled(migrated_db):
    reminder_id = add(migrated_db)

    class CancellingSender(FakeSender):
        async def send_dm(self, user_id, text):
            outcome = await super().send_dm(user_id, text)
            self.connection.execute(
                "UPDATE reminders SET status = 'cancelled' WHERE id = ?",
                (reminder_id,),
            )
            return outcome

    deliver(migrated_db, CancellingSender(migrated_db))

    stored = row(migrated_db, reminder_id)
    assert stored["status"] == "cancelled"
    assert stored["delivered_at"] is None
