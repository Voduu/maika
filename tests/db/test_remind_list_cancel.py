import asyncio
import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest

from maika.bot import MaikaClient
from maika.command_catalog import build_command_registry
from maika.command_runner import CommandContext, run_command
from maika.commands import CommandCategory, Visibility
from maika.config import Config, Environment
from maika.discord_adapter import REPLY_PART_LIMIT, split_message
from maika.permission_store import set_block
from maika.permissions import BLOCK_KIND_ALL, BLOCK_KIND_CHAT, Level
from maika.remind_commands import (
    REMIND_CANCEL_NAME,
    REMIND_LIST_NAME,
    remind_cancel,
    remind_list,
    reminder_choices,
)
from maika.reminders import create_reminder
from maika.results import FailureKind
from maika.slash_commands import reminder_autocomplete

NOW = 1_800_000_000
DAY = 86400

OWNER = 1
ALEX = 100
SAM = 200
GUILD = 5000
CHANNEL = 7000

LOS_ANGELES_NAME = "America/Los_Angeles"
WEEKLY_RULE = "FREQ=WEEKLY;BYDAY=TU;BYHOUR=9;BYMINUTE=0;BYSECOND=0"
MONTHLY_RULE = (
    "FREQ=MONTHLY;BYMONTHDAY=31,-1;BYSETPOS=1;BYHOUR=9;BYMINUTE=0;BYSECOND=0"
)

DENIED_TEXT = "You don't have permission for that."
NOT_FOUND_TEXT = "I couldn't find a pending reminder with that number."
NOT_PENDING_TEXT = "That reminder is no longer pending."

REAL_REGISTRY = build_command_registry()


def add(
    connection,
    *,
    user_id=ALEX,
    message="water plants",
    due_at=NOW + DAY,
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
        now=NOW,
    )
    connection.execute(
        "UPDATE reminders SET status = ? WHERE id = ?", (status, reminder_id)
    )
    return reminder_id


def status_of(connection, reminder_id):
    return connection.execute(
        "SELECT status FROM reminders WHERE id = ?", (reminder_id,)
    ).fetchone()[0]


def audit_count(connection):
    return connection.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]


def listing(connection, user_id=ALEX):
    result = remind_list(connection, user_id=user_id, now=NOW)
    assert result.success is True
    return result.message


def pending_line(reminder_id, due, rest):
    return f"#{reminder_id} · <t:{due}:f> (<t:{due}:R>) · {rest}"


def failed_line(reminder_id, due, rest):
    return f"#{reminder_id} · <t:{due}:f> · {rest}"


# /remind list


def test_list_empty(migrated_db):
    assert listing(migrated_db) == "You have no pending reminders."


def test_list_pending_only(migrated_db):
    reminder_id = add(migrated_db)

    assert listing(migrated_db) == (
        "Pending reminders\n"
        + pending_line(reminder_id, NOW + DAY, "water plants · DM")
    )


def test_list_both_sections(migrated_db):
    pending = add(migrated_db)
    failed = add(
        migrated_db,
        message="check the oven",
        due_at=NOW - DAY,
        status="failed",
    )

    assert listing(migrated_db) == (
        "Pending reminders\n"
        + pending_line(pending, NOW + DAY, "water plants · DM")
        + "\n\nNot delivered (last 7 days)\n"
        + failed_line(failed, NOW - DAY, "check the oven · DM")
    )


def test_list_only_not_delivered(migrated_db):
    failed = add(migrated_db, due_at=NOW - DAY, status="failed")

    assert listing(migrated_db) == (
        "You have no pending reminders.\n\n"
        "Not delivered (last 7 days)\n"
        + failed_line(failed, NOW - DAY, "water plants · DM")
    )


def test_list_ordering(migrated_db):
    later = add(migrated_db, message="later", due_at=NOW + 3 * DAY)
    sooner = add(migrated_db, message="sooner", due_at=NOW + DAY)
    older_failed = add(
        migrated_db, message="old", due_at=NOW - 3 * DAY, status="failed"
    )
    newer_failed = add(
        migrated_db, message="new", due_at=NOW - DAY, status="failed"
    )

    lines = listing(migrated_db).split("\n")

    assert lines == [
        "Pending reminders",
        pending_line(sooner, NOW + DAY, "sooner · DM"),
        pending_line(later, NOW + 3 * DAY, "later · DM"),
        "",
        "Not delivered (last 7 days)",
        failed_line(older_failed, NOW - 3 * DAY, "old · DM"),
        failed_line(newer_failed, NOW - DAY, "new · DM"),
    ]


def test_list_entry_routes_and_repeats(migrated_db):
    dm = add(migrated_db, message="one-time dm", due_at=NOW + DAY)
    channel = add(
        migrated_db,
        message="in channel",
        due_at=NOW + 2 * DAY,
        delivery="channel",
    )
    repeating = add(
        migrated_db,
        message="trash",
        due_at=NOW + 3 * DAY,
        repeat_rule=WEEKLY_RULE,
        timezone=LOS_ANGELES_NAME,
    )
    repeating_channel = add(
        migrated_db,
        message="rent",
        due_at=NOW + 4 * DAY,
        delivery="channel",
        repeat_rule=MONTHLY_RULE,
        timezone="Asia/Tokyo",
    )

    assert listing(migrated_db).split("\n")[1:] == [
        pending_line(dm, NOW + DAY, "one-time dm · DM"),
        pending_line(channel, NOW + 2 * DAY, f"in channel · in <#{CHANNEL}>"),
        pending_line(
            repeating,
            NOW + 3 * DAY,
            "trash · DM · Repeats: Weekly (America/Los_Angeles)",
        ),
        pending_line(
            repeating_channel,
            NOW + 4 * DAY,
            f"rent · in <#{CHANNEL}> · Repeats: Monthly (Asia/Tokyo)",
        ),
    ]


def test_list_repeating_failed_entry_shows_repeat(migrated_db):
    failed = add(
        migrated_db,
        due_at=NOW - DAY,
        status="failed",
        repeat_rule=WEEKLY_RULE,
        timezone=LOS_ANGELES_NAME,
    )

    assert listing(migrated_db).split("\n")[-1] == failed_line(
        failed,
        NOW - DAY,
        "water plants · DM · Repeats: Weekly (America/Los_Angeles)",
    )


def test_list_custom_repeat_label(migrated_db):
    add(
        migrated_db,
        repeat_rule="FREQ=HOURLY;BYMINUTE=0",
        timezone=LOS_ANGELES_NAME,
    )

    assert listing(migrated_db).endswith(
        " · Repeats: Custom repeat (America/Los_Angeles)"
    )


def test_list_shortens_long_pending_message(migrated_db):
    reminder_id = add(migrated_db, message="a" * 150)

    shortened = "a" * 99 + "…"
    assert len(shortened) == 100
    assert listing(migrated_db).split("\n")[1] == pending_line(
        reminder_id, NOW + DAY, f"{shortened} · DM"
    )


def test_list_keeps_pending_message_of_100_characters(migrated_db):
    reminder_id = add(migrated_db, message="b" * 100)

    assert listing(migrated_db).split("\n")[1] == pending_line(
        reminder_id, NOW + DAY, f"{'b' * 100} · DM"
    )


def test_list_shows_full_not_delivered_message(migrated_db):
    failed = add(
        migrated_db, message="c" * 150, due_at=NOW - DAY, status="failed"
    )

    assert listing(migrated_db).split("\n")[-1] == failed_line(
        failed, NOW - DAY, f"{'c' * 150} · DM"
    )


def test_list_puts_pending_message_on_one_line(migrated_db):
    reminder_id = add(migrated_db, message="buy\nmilk  and\t\neggs")

    assert listing(migrated_db) == (
        "Pending reminders\n"
        + pending_line(reminder_id, NOW + DAY, "buy milk and eggs · DM")
    )


def test_list_shortens_after_collapsing(migrated_db):
    reminder_id = add(migrated_db, message="word\n\n\n" * 30)

    line = listing(migrated_db).split("\n")[1]

    collapsed = " ".join(["word"] * 30)
    assert line == pending_line(
        reminder_id, NOW + DAY, f"{collapsed[:99]}… · DM"
    )


@pytest.mark.parametrize(
    ("days_ago", "shown"),
    [(8, False), (6, True)],
)
def test_list_not_delivered_cutoff(migrated_db, days_ago, shown):
    add(migrated_db, due_at=NOW - days_ago * DAY, status="failed")

    text = listing(migrated_db)

    if shown:
        assert "Not delivered (last 7 days)" in text
    else:
        assert text == "You have no pending reminders."


def test_list_hides_other_people_and_other_statuses(migrated_db):
    add(migrated_db, user_id=SAM)
    add(migrated_db, user_id=SAM, due_at=NOW - DAY, status="failed")
    add(migrated_db, status="delivered")
    add(migrated_db, status="cancelled")
    add(migrated_db, due_at=NOW - DAY, status="delivered")
    add(migrated_db, due_at=NOW - DAY, status="cancelled")

    assert listing(migrated_db) == "You have no pending reminders."


def test_list_many_long_reminders_split_into_messages(migrated_db):
    for number in range(25):
        add(migrated_db, message="x" * 150, due_at=NOW + (number + 1) * DAY)

    text = listing(migrated_db)
    parts = split_message(text)

    assert len(parts) > 1
    assert all(len(part) <= REPLY_PART_LIMIT for part in parts)
    assert "\n".join(parts) == text


def test_list_writes_nothing(migrated_db):
    add(migrated_db)
    add(migrated_db, due_at=NOW - DAY, status="failed")

    listing(migrated_db)

    assert audit_count(migrated_db) == 0


# /remind cancel


def cancel(connection, reminder_id, user_id=ALEX):
    return remind_cancel(
        connection, user_id=user_id, reminder_id=reminder_id, now=NOW
    )


def test_cancel_one_time(migrated_db):
    reminder_id = add(migrated_db)

    result = cancel(migrated_db, reminder_id)

    assert result.success is True
    assert result.message == f"Cancelled reminder #{reminder_id}: water plants"
    assert status_of(migrated_db, reminder_id) == "cancelled"
    assert audit_count(migrated_db) == 0
    assert not migrated_db.in_transaction


@pytest.mark.parametrize(
    ("repeat_rule", "timezone", "middle"),
    [
        (None, None, ""),
        (
            WEEKLY_RULE,
            LOS_ANGELES_NAME,
            " (Weekly), so it will not fire again",
        ),
    ],
)
def test_cancel_message_ending_with_period(
    migrated_db, repeat_rule, timezone, middle
):
    reminder_id = add(
        migrated_db,
        message="Call the dentist.",
        repeat_rule=repeat_rule,
        timezone=timezone,
    )

    result = cancel(migrated_db, reminder_id)

    assert result.message == (
        f"Cancelled reminder #{reminder_id}{middle}: Call the dentist."
    )
    assert ".." not in result.message


def test_cancel_repeating(migrated_db):
    reminder_id = add(
        migrated_db,
        message="take out the trash",
        repeat_rule=WEEKLY_RULE,
        timezone=LOS_ANGELES_NAME,
    )

    result = cancel(migrated_db, reminder_id)

    assert result.success is True
    assert result.message == (
        f"Cancelled reminder #{reminder_id} (Weekly), so it will not fire "
        "again: take out the trash"
    )
    assert status_of(migrated_db, reminder_id) == "cancelled"
    assert audit_count(migrated_db) == 0


def test_cancel_someone_elses(migrated_db):
    reminder_id = add(migrated_db, user_id=SAM)

    result = cancel(migrated_db, reminder_id)

    assert result.failure_kind is FailureKind.NOT_FOUND
    assert result.message == NOT_FOUND_TEXT
    assert status_of(migrated_db, reminder_id) == "pending"


def test_cancel_missing(migrated_db):
    result = cancel(migrated_db, 999)

    assert result.failure_kind is FailureKind.NOT_FOUND
    assert result.message == NOT_FOUND_TEXT


@pytest.mark.parametrize("status", ["delivered", "failed", "cancelled"])
def test_cancel_not_pending(migrated_db, status):
    reminder_id = add(migrated_db, status=status)

    result = cancel(migrated_db, reminder_id)

    assert result.failure_kind is FailureKind.INVALID
    assert result.message == NOT_PENDING_TEXT
    assert status_of(migrated_db, reminder_id) == status


def test_cancelled_reminder_leaves_the_list(migrated_db):
    kept = add(migrated_db, message="kept")
    gone = add(migrated_db, message="gone")

    cancel(migrated_db, gone)

    assert listing(migrated_db) == (
        "Pending reminders\n" + pending_line(kept, NOW + DAY, "kept · DM")
    )


# Autocomplete choices


def choices(connection, current="", user_id=ALEX):
    return reminder_choices(connection, user_id=user_id, current=current)


def test_choices_own_pending_only_soonest_first(migrated_db):
    later = add(migrated_db, message="later", due_at=NOW + 2 * DAY)
    sooner = add(migrated_db, message="sooner", due_at=NOW + DAY)
    add(migrated_db, user_id=SAM, message="sam's")
    for status in ("delivered", "failed", "cancelled"):
        add(migrated_db, message=status, status=status)

    assert choices(migrated_db) == [
        (f"#{sooner} sooner", sooner),
        (f"#{later} later", later),
    ]


def test_choices_empty(migrated_db):
    add(migrated_db, user_id=SAM)
    assert choices(migrated_db) == []


def test_choices_at_most_25(migrated_db):
    reminder_ids = [
        add(migrated_db, message=f"item {number}", due_at=NOW + number + 1)
        for number in range(30)
    ]

    result = choices(migrated_db)

    assert [value for _, value in result] == reminder_ids[:25]


def test_choice_name_shortens_message_and_keeps_number(migrated_db):
    reminder_id = add(migrated_db, message="m" * 80)

    ((name, value),) = choices(migrated_db)

    assert name == f"#{reminder_id} " + "m" * 49 + "…"
    assert value == reminder_id
    assert len(name) <= 100


def test_choice_name_puts_message_on_one_line(migrated_db):
    reminder_id = add(migrated_db, message="buy\nmilk   now")

    assert choices(migrated_db) == [
        (f"#{reminder_id} buy milk now", reminder_id)
    ]


def test_choice_names_fit_the_limit(migrated_db):
    for _ in range(5):
        add(migrated_db, message="z" * 500)

    assert all(len(name) <= 100 for name, _ in choices(migrated_db))


@pytest.mark.parametrize("typed", ["PLANT", "plants", "  water ", "Water P"])
def test_choices_match_text_in_any_case(migrated_db, typed):
    plants = add(migrated_db, message="Water plants")
    add(migrated_db, message="pay rent")

    assert [value for _, value in choices(migrated_db, typed)] == [plants]


def test_choices_match_by_number(migrated_db):
    reminder_ids = [add(migrated_db, message="same") for _ in range(12)]

    assert [value for _, value in choices(migrated_db, "12")] == [
        reminder_ids[11]
    ]
    assert [value for _, value in choices(migrated_db, "#3")] == [
        reminder_ids[2]
    ]
    # "1" is the start of 1, 10, 11 and 12.
    assert [value for _, value in choices(migrated_db, "1")] == [
        reminder_ids[0],
        reminder_ids[9],
        reminder_ids[10],
        reminder_ids[11],
    ]


def test_choices_number_does_not_reach_other_people(migrated_db):
    sams = add(migrated_db, user_id=SAM)

    assert choices(migrated_db, str(sams)) == []


def test_choices_no_match(migrated_db):
    add(migrated_db)
    assert choices(migrated_db, "zzz") == []


# Permissions and registry


def run_as(connection, command_name, function, user_id=ALEX):
    context = CommandContext(
        user_id=user_id, guild_id=GUILD, channel_id=CHANNEL, now=NOW
    )
    usages = []
    result = run_command(
        connection,
        REAL_REGISTRY,
        owner_id=OWNER,
        command_name=command_name,
        context=context,
        function=function,
        log_usage=usages.append,
    )
    return result, usages


def block(connection, kind):
    set_block(
        connection,
        user_id=ALEX,
        kind=kind,
        guild_id=0,
        owner_id=OWNER,
        actor_id=OWNER,
        now=NOW,
    )


def test_all_block_refuses_list(migrated_db):
    add(migrated_db)
    block(migrated_db, BLOCK_KIND_ALL)

    result, usages = run_as(
        migrated_db,
        REMIND_LIST_NAME,
        lambda: remind_list(migrated_db, user_id=ALEX, now=NOW),
    )

    assert result.failure_kind is FailureKind.DENIED
    assert usages[0].allowed is False


def test_all_block_refuses_cancel(migrated_db):
    reminder_id = add(migrated_db)
    block(migrated_db, BLOCK_KIND_ALL)

    result, usages = run_as(
        migrated_db,
        REMIND_CANCEL_NAME,
        lambda: cancel(migrated_db, reminder_id),
    )

    assert result.failure_kind is FailureKind.DENIED
    assert usages[0].allowed is False
    assert status_of(migrated_db, reminder_id) == "pending"


def test_chat_block_allows_list_and_cancel(migrated_db):
    reminder_id = add(migrated_db)
    block(migrated_db, BLOCK_KIND_CHAT)

    listed, _ = run_as(
        migrated_db,
        REMIND_LIST_NAME,
        lambda: remind_list(migrated_db, user_id=ALEX, now=NOW),
    )
    cancelled, _ = run_as(
        migrated_db,
        REMIND_CANCEL_NAME,
        lambda: cancel(migrated_db, reminder_id),
    )

    assert listed.success is True
    assert cancelled.success is True
    assert status_of(migrated_db, reminder_id) == "cancelled"


@pytest.mark.parametrize(
    ("name", "key", "example"),
    [
        ("remind list", "remind.list", "/remind list"),
        ("remind cancel", "remind.cancel", "/remind cancel reminder:12"),
    ],
)
def test_registry_entries(name, key, example):
    entry = REAL_REGISTRY.get(name)

    assert entry.key.key == key
    assert entry.key.default_level is Level.EVERYONE
    assert entry.key.owner_only is False
    assert entry.key.protected is False
    assert entry.category is CommandCategory.REMINDERS_AND_TIME
    assert entry.visibility is Visibility.EPHEMERAL
    assert entry.example == example


def test_remind_keys_registered():
    registry = REAL_REGISTRY.permission_registry()
    for key in ("remind.create", "remind.list", "remind.cancel"):
        assert registry.get(key).default_level is Level.EVERYONE


# Slash commands


class FakeRouter:
    def __init__(self):
        self.posts = []

    def post(self, stream, level, text):
        self.posts.append((stream, level, text))

    async def run(self):
        await asyncio.Event().wait()

    async def close(self):
        pass


def fake_interaction(user_id=ALEX, *, guild_id=GUILD):
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id),
        guild_id=guild_id,
        channel_id=CHANNEL,
        channel=None,
        app_permissions=discord.Permissions(send_messages=True),
        created_at=datetime.datetime.fromtimestamp(NOW, tz=datetime.UTC),
        response=SimpleNamespace(
            is_done=Mock(return_value=False),
            send_message=AsyncMock(),
        ),
        followup=SimpleNamespace(send=AsyncMock()),
    )


def make_client(connection):
    config = Config(
        environment=Environment.TEST,
        discord_token="unused-token",
        owner_id=OWNER,
        listen_channels=(),
        log_channel_id=None,
        database_path=Path("unused.db"),
    )
    client = MaikaClient(
        config,
        connection=connection,
        command_registry=build_command_registry(),
    )
    client.log_router = FakeRouter()
    client.tree.sync = AsyncMock(return_value=[])
    asyncio.run(client.setup_hook())
    return client


def remind_subcommand(connection, name):
    group = make_client(connection).tree.get_command("remind")
    return group.get_command(name)


def sent_reply(interaction):
    call = interaction.response.send_message.call_args
    return call.args[0], call.kwargs["ephemeral"]


def test_remind_list_has_no_parameters(migrated_db):
    command = remind_subcommand(migrated_db, "list")

    assert command.parameters == []
    assert 1 <= len(command.description) < 100


def test_remind_cancel_parameter(migrated_db):
    command = remind_subcommand(migrated_db, "cancel")

    (reminder,) = command.parameters
    assert reminder.name == "reminder"
    assert reminder.type is discord.AppCommandOptionType.integer
    assert reminder.required is True
    assert reminder.autocomplete is True
    assert 1 <= len(reminder.description) < 100
    assert 1 <= len(command.description) < 100


def test_list_callback_is_ephemeral(migrated_db):
    reminder_id = add(migrated_db)
    interaction = fake_interaction()

    asyncio.run(remind_subcommand(migrated_db, "list").callback(interaction))

    assert sent_reply(interaction) == (
        "Pending reminders\n"
        + pending_line(reminder_id, NOW + DAY, "water plants · DM"),
        True,
    )


def test_list_callback_sends_long_list_as_several_messages(migrated_db):
    for number in range(25):
        add(migrated_db, message="x" * 150, due_at=NOW + (number + 1) * DAY)
    interaction = fake_interaction()

    asyncio.run(remind_subcommand(migrated_db, "list").callback(interaction))

    assert interaction.response.send_message.call_count == 1
    assert interaction.followup.send.call_count >= 1
    for call in interaction.followup.send.call_args_list:
        assert call.kwargs["ephemeral"] is True


def test_cancel_callback(migrated_db):
    reminder_id = add(migrated_db)
    interaction = fake_interaction()

    asyncio.run(
        remind_subcommand(migrated_db, "cancel").callback(
            interaction, reminder=reminder_id
        )
    )

    assert sent_reply(interaction) == (
        f"Cancelled reminder #{reminder_id}: water plants",
        True,
    )
    assert status_of(migrated_db, reminder_id) == "cancelled"


def test_cancel_callback_someone_elses(migrated_db):
    reminder_id = add(migrated_db, user_id=SAM)
    interaction = fake_interaction()

    asyncio.run(
        remind_subcommand(migrated_db, "cancel").callback(
            interaction, reminder=reminder_id
        )
    )

    text, ephemeral = sent_reply(interaction)
    assert ephemeral is True
    assert status_of(migrated_db, reminder_id) == "pending"
    assert "water plants" not in text


def test_cancel_callback_blocked_is_refused(migrated_db):
    reminder_id = add(migrated_db)
    block(migrated_db, BLOCK_KIND_ALL)
    interaction = fake_interaction()

    asyncio.run(
        remind_subcommand(migrated_db, "cancel").callback(
            interaction, reminder=reminder_id
        )
    )

    assert sent_reply(interaction) == (DENIED_TEXT, True)
    assert status_of(migrated_db, reminder_id) == "pending"


def autocomplete_interaction(user_id):
    return SimpleNamespace(user=SimpleNamespace(id=user_id))


def test_autocomplete_returns_caller_choices(migrated_db):
    own = add(migrated_db, message="water plants")
    add(migrated_db, user_id=SAM, message="water the lawn")
    autocomplete = reminder_autocomplete(migrated_db)

    result = asyncio.run(autocomplete(autocomplete_interaction(ALEX), "wat"))

    assert [(c.name, c.value) for c in result] == [
        (f"#{own} water plants", own)
    ]
    assert isinstance(result[0], discord.app_commands.Choice)


@pytest.mark.parametrize("typed", ["", "abc", "#1", "1", "  "])
def test_autocomplete_accepts_any_typed_text(migrated_db, typed):
    add(migrated_db, message="abc")
    autocomplete = reminder_autocomplete(migrated_db)

    result = asyncio.run(autocomplete(autocomplete_interaction(ALEX), typed))

    assert [c.value for c in result] == [1]


def test_autocomplete_writes_nothing_and_skips_permission_check(
    migrated_db,
):
    reminder_id = add(migrated_db)
    # Only the command is refused; its choices are the caller's own.
    block(migrated_db, BLOCK_KIND_ALL)
    changes_before = migrated_db.total_changes
    autocomplete = reminder_autocomplete(migrated_db)

    result = asyncio.run(autocomplete(autocomplete_interaction(ALEX), ""))

    assert [c.value for c in result] == [reminder_id]
    assert migrated_db.total_changes == changes_before
