import asyncio
import datetime
import zoneinfo
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest
from discord import app_commands

from maika.bot import MaikaClient
from maika.command_catalog import build_command_registry
from maika.command_runner import CommandContext, run_command
from maika.commands import CommandCategory, Visibility
from maika.config import Config, Environment
from maika.foundation_settings import build_foundation_registry
from maika.permission_store import set_block
from maika.permissions import BLOCK_KIND_ALL, BLOCK_KIND_CHAT, Level
from maika.remind_commands import REMIND_ADD_NAME, remind_add
from maika.reminders import create_reminder
from maika.results import FailureKind
from maika.settings_store import set_value
from maika.time_parser import TOO_FAR_MESSAGE
from maika.user_settings import set_user_setting

LOS_ANGELES_NAME = "America/Los_Angeles"
LOS_ANGELES = zoneinfo.ZoneInfo(LOS_ANGELES_NAME)


def local(year, month, day, hour, minute):
    moment = datetime.datetime(
        year, month, day, hour, minute, tzinfo=LOS_ANGELES
    )
    return int(moment.timestamp())


# Wednesday 2026-11-11 19:42 in Los Angeles.
NOW = local(2026, 11, 11, 19, 42)

OWNER = 1
ALEX = 100
SAM = 200
GUILD = 5000
CHANNEL = 7000

DENIED_TEXT = "You don't have permission for that."

REAL_REGISTRY = build_command_registry()
SETTINGS = build_foundation_registry()


def add(connection, when="2h", message="submit assignment", **changes):
    arguments = {
        "user_id": ALEX,
        "when": when,
        "message": message,
        "delivery": "dm",
        "repeat": None,
        "channel_id": CHANNEL,
        "bot_can_post": True,
        "now": NOW,
    }
    arguments.update(changes)
    return remind_add(connection, **arguments)


def set_timezone(connection, user_id=ALEX, name=LOS_ANGELES_NAME):
    set_user_setting(connection, user_id, "timezone", name, NOW)


def rows(connection):
    return [
        dict(row)
        for row in connection.execute("SELECT * FROM reminders ORDER BY id")
    ]


def only_row(connection):
    (row,) = rows(connection)
    return row


def audit_count(connection):
    return connection.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]


def created_line(due_at, message="submit assignment"):
    return (
        f"Okay, I'll remind you on <t:{due_at}:F> (<t:{due_at}:R>): {message}"
    )


# One-time reminders


def test_duration_without_timezone(migrated_db):
    result = add(migrated_db)

    assert result.success is True
    assert result.message == created_line(NOW + 7200)
    row = only_row(migrated_db)
    assert row["user_id"] == ALEX
    assert row["message"] == "submit assignment"
    assert row["due_at"] == NOW + 7200
    assert row["delivery"] == "dm"
    assert row["channel_id"] == CHANNEL
    assert row["created_at"] == NOW
    assert row["repeat_rule"] is None
    assert row["timezone"] is None
    assert row["status"] == "pending"


def test_dm_reminder_set_in_a_dm_has_no_channel(migrated_db):
    add(migrated_db, channel_id=None)
    assert only_row(migrated_db)["channel_id"] is None


def test_message_is_stripped(migrated_db):
    result = add(migrated_db, message="  submit assignment  ")

    assert result.message == created_line(NOW + 7200)
    assert only_row(migrated_db)["message"] == "submit assignment"


def test_clock_form_with_timezone(migrated_db):
    set_timezone(migrated_db)

    result = add(migrated_db, when="tomorrow 9am")

    due_at = local(2026, 11, 12, 9, 0)
    assert result.message == (
        created_line(due_at) + "\nTimes are read in America/Los_Angeles."
    )
    row = only_row(migrated_db)
    assert row["due_at"] == due_at
    assert row["timezone"] is None
    assert row["repeat_rule"] is None


def test_day_without_time_uses_default_time(migrated_db):
    set_timezone(migrated_db)

    add(migrated_db, when="tomorrow")

    assert only_row(migrated_db)["due_at"] == local(2026, 11, 12, 8, 0)


def test_day_without_time_follows_default_time_setting(migrated_db):
    set_timezone(migrated_db)
    set_value(
        migrated_db,
        SETTINGS,
        "reminders.default_time",
        "07:15",
        actor_id=OWNER,
        now=NOW,
    )

    add(migrated_db, when="tomorrow")

    assert only_row(migrated_db)["due_at"] == local(2026, 11, 12, 7, 15)


# Repeats


def test_daily_repeat_with_clock_form(migrated_db):
    set_timezone(migrated_db)

    result = add(migrated_db, when="tomorrow 9am", repeat="DAILY")

    due_at = local(2026, 11, 12, 9, 0)
    assert result.message == (
        created_line(due_at) + "\nRepeats: Daily."
        "\nTimes are read in America/Los_Angeles."
    )
    row = only_row(migrated_db)
    assert row["due_at"] == due_at
    assert row["repeat_rule"] == "FREQ=DAILY;BYHOUR=9;BYMINUTE=0;BYSECOND=0"
    assert row["timezone"] == LOS_ANGELES_NAME


def test_repeat_with_duration_needs_timezone(migrated_db):
    result = add(migrated_db, when="2h", repeat="WEEKLY")

    assert result.failure_kind is FailureKind.INVALID
    assert result.message == "Set your timezone first with /timezone set."
    assert rows(migrated_db) == []


def test_repeat_with_duration_and_timezone(migrated_db):
    set_timezone(migrated_db)

    result = add(migrated_db, when="2h", repeat="WEEKLY")

    # 2h after 19:42 Wednesday is 21:42 Wednesday.
    assert result.message == (
        created_line(NOW + 7200) + "\nRepeats: Weekly."
        "\nTimes are read in America/Los_Angeles."
    )
    row = only_row(migrated_db)
    assert row["repeat_rule"] == (
        "FREQ=WEEKLY;BYDAY=WE;BYHOUR=21;BYMINUTE=42;BYSECOND=0"
    )
    assert row["timezone"] == LOS_ANGELES_NAME


@pytest.mark.parametrize(
    ("repeat", "label"),
    [
        ("DAILY", "Daily"),
        ("WEEKDAYS", "Weekdays (Mon to Fri)"),
        ("WEEKLY", "Weekly"),
        ("BIWEEKLY", "Every 2 weeks"),
        ("MONTHLY", "Monthly"),
        ("LAST_DAY", "Last day of the month"),
        ("YEARLY", "Yearly"),
    ],
)
def test_every_repeat_choice_is_accepted(migrated_db, repeat, label):
    set_timezone(migrated_db)

    result = add(migrated_db, when="friday 3pm", repeat=repeat)

    assert result.success is True
    assert f"\nRepeats: {label}.\n" in result.message


# Delivery


def test_channel_delivery(migrated_db):
    result = add(migrated_db, delivery="channel")

    assert result.message == (
        created_line(NOW + 7200) + f"\nI'll post it in <#{CHANNEL}>."
    )
    row = only_row(migrated_db)
    assert row["delivery"] == "channel"
    assert row["channel_id"] == CHANNEL


def test_channel_delivery_outside_a_server_is_refused(migrated_db):
    result = add(migrated_db, delivery="channel", channel_id=None)

    assert result.failure_kind is FailureKind.INVALID
    assert result.message == "Channel delivery needs a server channel."
    assert rows(migrated_db) == []


def test_channel_delivery_without_post_permission_is_refused(migrated_db):
    result = add(migrated_db, delivery="channel", bot_can_post=False)

    assert result.failure_kind is FailureKind.INVALID
    assert result.message == "I can't post in this channel."
    assert rows(migrated_db) == []


def test_dm_delivery_ignores_post_permission(migrated_db):
    result = add(migrated_db, delivery="dm", bot_can_post=False)
    assert result.success is True


def test_unknown_delivery_raises(migrated_db):
    with pytest.raises(ValueError, match="delivery"):
        add(migrated_db, delivery="pigeon")


def test_unknown_repeat_raises(migrated_db):
    set_timezone(migrated_db)
    with pytest.raises(ValueError, match="repeat"):
        add(migrated_db, repeat="HOURLY")


# Message


@pytest.mark.parametrize("message", ["", "   "])
def test_empty_message(migrated_db, message):
    result = add(migrated_db, message=message)

    assert result.failure_kind is FailureKind.MISSING_ARGUMENT
    assert result.message == (
        "Say what to remind you about, for example "
        "/remind add when:2h message:submit assignment."
    )
    assert rows(migrated_db) == []


def test_message_of_500_characters(migrated_db):
    result = add(migrated_db, message="a" * 500)

    assert result.success is True
    assert only_row(migrated_db)["message"] == "a" * 500


def test_message_of_501_characters(migrated_db):
    result = add(migrated_db, message="a" * 501)

    assert result.failure_kind is FailureKind.INVALID
    assert result.message == "That reminder is longer than 500 characters."
    assert rows(migrated_db) == []


# Pending limit


def insert_row(connection, user_id=ALEX, status="pending"):
    reminder_id = create_reminder(
        connection,
        user_id=user_id,
        message="old",
        due_at=NOW + 60,
        delivery="dm",
        channel_id=None,
        repeat_rule=None,
        timezone=None,
        now=NOW,
    )
    connection.execute(
        "UPDATE reminders SET status = ? WHERE id = ?", (status, reminder_id)
    )


def test_pending_limit_reached(migrated_db):
    for _ in range(25):
        insert_row(migrated_db)

    result = add(migrated_db)

    assert result.failure_kind is FailureKind.INVALID
    assert result.message == (
        "You have 25 pending reminders, and the limit is 25. "
        "Cancel some of them with /remind cancel to add another."
    )
    assert len(rows(migrated_db)) == 25
    assert not migrated_db.in_transaction


def test_pending_limit_counts_only_own_pending_rows(migrated_db):
    for _ in range(24):
        insert_row(migrated_db)
    insert_row(migrated_db, status="cancelled")
    insert_row(migrated_db, status="delivered")
    insert_row(migrated_db, status="failed")
    insert_row(migrated_db, user_id=SAM)

    result = add(migrated_db)

    assert result.success is True
    assert add(migrated_db).failure_kind is FailureKind.INVALID


def test_pending_limit_follows_setting(migrated_db):
    set_value(
        migrated_db,
        SETTINGS,
        "reminders.max_pending_per_person",
        "1",
        actor_id=OWNER,
        now=NOW,
    )

    assert add(migrated_db).success is True
    second = add(migrated_db)
    assert second.message.startswith(
        "You have 1 pending reminders, and the limit is 1."
    )


def test_pending_limit_lowered_below_current_count(migrated_db):
    for _ in range(5):
        insert_row(migrated_db)
    set_value(
        migrated_db,
        SETTINGS,
        "reminders.max_pending_per_person",
        "3",
        actor_id=OWNER,
        now=NOW,
    )

    result = add(migrated_db)

    assert result.failure_kind is FailureKind.INVALID
    assert result.message == (
        "You have 5 pending reminders, and the limit is 3. "
        "Cancel some of them with /remind cancel to add another."
    )
    assert len(rows(migrated_db)) == 5


# Time errors


@pytest.mark.parametrize(
    ("when", "timezone", "message"),
    [
        (
            "15pm",
            LOS_ANGELES_NAME,
            "I couldn't read that time. Try 2h, tomorrow 9am, friday 3pm, "
            "or 2026-11-20 14:30.",
        ),
        ("9am", None, "Set your timezone first with /timezone set."),
        ("today 9am", LOS_ANGELES_NAME, "That time has already passed."),
        ("april 31", LOS_ANGELES_NAME, "April has only 30 days."),
        ("2040-01-01", LOS_ANGELES_NAME, TOO_FAR_MESSAGE),
    ],
)
def test_time_errors(migrated_db, when, timezone, message):
    if timezone is not None:
        set_timezone(migrated_db, name=timezone)

    result = add(migrated_db, when=when)

    assert result.failure_kind is FailureKind.INVALID
    assert result.message == message
    assert rows(migrated_db) == []


def test_message_is_checked_before_time(migrated_db):
    result = add(migrated_db, when="15pm", message="")
    assert result.failure_kind is FailureKind.MISSING_ARGUMENT


def test_delivery_is_checked_before_time(migrated_db):
    result = add(migrated_db, when="15pm", delivery="channel", channel_id=None)
    assert result.message == "Channel delivery needs a server channel."


# Confirmation notes


def test_all_notes_each_on_own_line(migrated_db):
    set_timezone(migrated_db)

    result = add(
        migrated_db, when="friday 3pm", repeat="WEEKLY", delivery="channel"
    )

    due_at = local(2026, 11, 13, 15, 0)
    assert result.message.split("\n") == [
        created_line(due_at),
        "Repeats: Weekly.",
        f"I'll post it in <#{CHANNEL}>.",
        "Times are read in America/Los_Angeles.",
    ]
    assert f"<t:{due_at}:F>" in result.message
    assert f"<t:{due_at}:R>" in result.message


def test_duration_with_timezone_set_has_no_timezone_note(migrated_db):
    set_timezone(migrated_db)

    result = add(migrated_db, when="in 2 hours")

    assert result.message == created_line(NOW + 7200)


# Audit, permissions and registry


def test_no_audit_rows(migrated_db):
    set_timezone(migrated_db)

    add(migrated_db)
    add(migrated_db, when="friday 3pm", repeat="DAILY", delivery="channel")

    assert len(rows(migrated_db)) == 2
    assert audit_count(migrated_db) == 0


def test_registry_entry():
    entry = REAL_REGISTRY.get(REMIND_ADD_NAME)

    assert entry.key.key == "remind.create"
    assert entry.key.default_level is Level.EVERYONE
    assert entry.key.owner_only is False
    assert entry.key.protected is False
    assert entry.category is CommandCategory.REMINDERS_AND_TIME
    assert entry.visibility is Visibility.EPHEMERAL
    assert entry.example == "/remind add when:2h message:submit assignment"


def test_remind_create_key_registered():
    registry = REAL_REGISTRY.permission_registry()
    assert registry.get("remind.create").default_level is Level.EVERYONE


def run_as(connection, user_id):
    context = CommandContext(
        user_id=user_id, guild_id=GUILD, channel_id=CHANNEL, now=NOW
    )
    usages = []
    result = run_command(
        connection,
        REAL_REGISTRY,
        owner_id=OWNER,
        command_name=REMIND_ADD_NAME,
        context=context,
        function=lambda: add(connection, user_id=user_id),
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


def test_level_0_can_add(migrated_db):
    result, usages = run_as(migrated_db, ALEX)

    assert result.success is True
    assert usages[0].command_name == REMIND_ADD_NAME
    assert usages[0].allowed is True


def test_all_block_refuses_add(migrated_db):
    block(migrated_db, BLOCK_KIND_ALL)

    result, usages = run_as(migrated_db, ALEX)

    assert result.failure_kind is FailureKind.DENIED
    assert usages[0].allowed is False
    assert rows(migrated_db) == []


def test_chat_block_allows_add(migrated_db):
    block(migrated_db, BLOCK_KIND_CHAT)

    result, _ = run_as(migrated_db, ALEX)

    assert result.success is True
    assert len(rows(migrated_db)) == 1


# Slash command


class FakeRouter:
    def __init__(self):
        self.posts = []

    def post(self, stream, level, text):
        self.posts.append((stream, level, text))

    async def run(self):
        await asyncio.Event().wait()

    async def close(self):
        pass


def fake_interaction(
    user_id=ALEX, *, guild_id=GUILD, can_send=True, channel=None
):
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id),
        guild_id=guild_id,
        channel_id=CHANNEL,
        channel=channel,
        app_permissions=discord.Permissions(send_messages=can_send),
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


def add_command(connection):
    group = make_client(connection).tree.get_command("remind")
    return group.get_command("add")


def sent_reply(interaction):
    call = interaction.response.send_message.call_args
    return call.args[0], call.kwargs["ephemeral"]


def test_tree_has_remind_group(migrated_db):
    group = make_client(migrated_db).tree.get_command("remind")

    assert isinstance(group, app_commands.Group)
    assert [c.name for c in group.commands] == ["add", "list", "cancel"]
    assert 1 <= len(group.description) < 100
    for command in group.commands:
        assert 1 <= len(command.description) < 100
        for parameter in command.parameters:
            assert 1 <= len(parameter.description) < 100


def test_remind_add_parameters(migrated_db):
    when, message, delivery, repeat = add_command(migrated_db).parameters

    assert when.name == "when"
    assert when.type is discord.AppCommandOptionType.string
    assert when.required is True
    assert message.name == "message"
    assert message.type is discord.AppCommandOptionType.string
    assert message.required is True
    # discord.py sends a string option's max_value as its max_length.
    assert message.max_value == 500
    assert delivery.name == "delivery"
    assert delivery.required is False
    assert [(c.name, c.value) for c in delivery.choices] == [
        ("Direct message", "dm"),
        ("This channel", "channel"),
    ]
    assert repeat.name == "repeat"
    assert repeat.required is False
    assert [(c.name, c.value) for c in repeat.choices] == [
        ("Daily", "DAILY"),
        ("Weekdays (Mon to Fri)", "WEEKDAYS"),
        ("Weekly", "WEEKLY"),
        ("Every 2 weeks", "BIWEEKLY"),
        ("Monthly", "MONTHLY"),
        ("Last day of the month", "LAST_DAY"),
        ("Yearly", "YEARLY"),
    ]


def test_callback_defaults_to_dm_and_is_ephemeral(migrated_db):
    interaction = fake_interaction()

    asyncio.run(
        add_command(migrated_db).callback(
            interaction, when="2h", message="submit assignment"
        )
    )

    assert sent_reply(interaction) == (created_line(NOW + 7200), True)
    row = only_row(migrated_db)
    assert row["delivery"] == "dm"
    assert row["channel_id"] == CHANNEL
    assert row["created_at"] == NOW


def test_callback_reply_has_mentions_off(migrated_db):
    interaction = fake_interaction()

    asyncio.run(
        add_command(migrated_db).callback(
            interaction, when="2h", message="ping @everyone"
        )
    )

    call = interaction.response.send_message.call_args
    allowed = call.kwargs["allowed_mentions"]
    assert allowed.everyone is False
    assert allowed.users is False
    assert allowed.roles is False


def test_callback_with_choices(migrated_db):
    set_timezone(migrated_db)
    interaction = fake_interaction()

    asyncio.run(
        add_command(migrated_db).callback(
            interaction,
            when="friday 3pm",
            message="water plants",
            delivery=app_commands.Choice(name="This channel", value="channel"),
            repeat=app_commands.Choice(name="Weekly", value="WEEKLY"),
        )
    )

    text, ephemeral = sent_reply(interaction)
    assert ephemeral is True
    assert text.split("\n")[1:] == [
        "Repeats: Weekly.",
        f"I'll post it in <#{CHANNEL}>.",
        "Times are read in America/Los_Angeles.",
    ]
    row = only_row(migrated_db)
    assert row["delivery"] == "channel"
    assert row["repeat_rule"].startswith("FREQ=WEEKLY;BYDAY=FR;")


def test_callback_in_a_dm_has_no_channel(migrated_db):
    interaction = fake_interaction(guild_id=None)

    asyncio.run(
        add_command(migrated_db).callback(
            interaction, when="2h", message="submit assignment"
        )
    )

    assert only_row(migrated_db)["channel_id"] is None


def test_callback_channel_delivery_in_a_dm_is_refused(migrated_db):
    interaction = fake_interaction(guild_id=None)

    asyncio.run(
        add_command(migrated_db).callback(
            interaction,
            when="2h",
            message="submit assignment",
            delivery=app_commands.Choice(name="This channel", value="channel"),
        )
    )

    assert sent_reply(interaction) == (
        "Channel delivery needs a server channel.",
        True,
    )
    assert rows(migrated_db) == []


def test_callback_without_post_permission_is_refused(migrated_db):
    interaction = fake_interaction(can_send=False)

    asyncio.run(
        add_command(migrated_db).callback(
            interaction,
            when="2h",
            message="submit assignment",
            delivery=app_commands.Choice(name="This channel", value="channel"),
        )
    )

    assert sent_reply(interaction) == ("I can't post in this channel.", True)
    assert rows(migrated_db) == []


def test_callback_blocked_is_refused(migrated_db):
    block(migrated_db, BLOCK_KIND_ALL)
    interaction = fake_interaction()

    asyncio.run(
        add_command(migrated_db).callback(
            interaction, when="2h", message="submit assignment"
        )
    )

    assert sent_reply(interaction) == (DENIED_TEXT, True)
    assert rows(migrated_db) == []
