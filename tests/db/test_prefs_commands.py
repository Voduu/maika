import asyncio
from datetime import UTC, datetime
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
from maika.permission_store import set_block
from maika.permissions import BLOCK_KIND_ALL, BLOCK_KIND_CHAT, Level
from maika.prefs_commands import (
    BIRTHDAY_SET_NAME,
    COMMON_TIMEZONES,
    SUGGESTED_TIMEZONE_PREFIXES,
    TIMEZONE_SET_NAME,
    TIMEZONE_SHOW_NAME,
    _is_suggested,
    birthday_set,
    clock_text,
    timezone_name_matches,
    timezone_set,
    timezone_show,
)
from maika.results import FailureKind
from maika.slash_commands import timezone_autocomplete, timezone_choices
from maika.user_settings import get_user_setting, set_user_setting

# 2027-01-15 08:00:00 UTC (winter: Los Angeles is UTC-8).
NOW = 1_800_000_000
LATER = NOW + 3600
# 2027-07-15 23:23:00 UTC (summer: Los Angeles is UTC-7).
SUMMER = int(datetime(2027, 7, 15, 23, 23, tzinfo=UTC).timestamp())

OWNER = 1
ALEX = 100
SAM = 200
GUILD = 5000
CHANNEL = 7000

DENIED_TEXT = "You don't have permission for that."

REAL_REGISTRY = build_command_registry()


def stored_row(connection, user_id, key):
    return connection.execute(
        "SELECT value, updated_at FROM user_settings "
        "WHERE user_id = ? AND key = ?",
        (user_id, key),
    ).fetchone()


def audit_count(connection):
    return connection.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]


# /timezone set


def test_timezone_set_valid_name(migrated_db):
    result = timezone_set(
        migrated_db, user_id=ALEX, name="America/Los_Angeles", now=NOW
    )

    assert result.success is True
    assert result.message == "Your timezone is set to America/Los_Angeles."
    row = stored_row(migrated_db, ALEX, "timezone")
    assert (row["value"], row["updated_at"]) == ("America/Los_Angeles", NOW)


def test_timezone_set_stores_canonical_name(migrated_db):
    result = timezone_set(
        migrated_db, user_id=ALEX, name="america/los_angeles", now=NOW
    )

    assert result.message == "Your timezone is set to America/Los_Angeles."
    assert get_user_setting(migrated_db, ALEX, "timezone") == (
        "America/Los_Angeles"
    )


def test_timezone_set_unknown_name(migrated_db):
    result = timezone_set(
        migrated_db, user_id=ALEX, name="Mars/Olympus_Mons", now=NOW
    )

    assert result.success is False
    assert result.failure_kind is FailureKind.INVALID
    assert result.message == (
        "I don't know the timezone Mars/Olympus_Mons. Use an IANA name "
        "such as America/New_York or Europe/London."
    )
    assert get_user_setting(migrated_db, ALEX, "timezone") is None


def test_timezone_set_empty_name(migrated_db):
    result = timezone_set(migrated_db, user_id=ALEX, name="  ", now=NOW)

    assert result.failure_kind is FailureKind.INVALID
    assert "Please give a timezone" in result.message


def test_timezone_set_replaces_value(migrated_db):
    timezone_set(migrated_db, user_id=ALEX, name="Europe/Paris", now=NOW)
    result = timezone_set(
        migrated_db, user_id=ALEX, name="Asia/Tokyo", now=LATER
    )

    assert result.message == "Your timezone is set to Asia/Tokyo."
    row = stored_row(migrated_db, ALEX, "timezone")
    assert (row["value"], row["updated_at"]) == ("Asia/Tokyo", LATER)


# /timezone show


@pytest.mark.parametrize(
    ("name", "now", "expected_time"),
    [
        ("America/Los_Angeles", NOW, "12:00 AM"),
        ("America/Los_Angeles", SUMMER, "4:23 PM"),
        ("Europe/London", SUMMER, "12:23 AM"),
        ("Asia/Kolkata", NOW, "1:30 PM"),
        ("UTC", NOW, "8:00 AM"),
    ],
)
def test_timezone_show_local_time(migrated_db, name, now, expected_time):
    set_user_setting(migrated_db, ALEX, "timezone", name, NOW)

    result = timezone_show(migrated_db, user_id=ALEX, now=now)

    assert result.success is True
    assert result.message == (
        f"Your timezone is {name}. It is {expected_time} there now."
    )


def test_timezone_show_nothing_stored(migrated_db):
    result = timezone_show(migrated_db, user_id=ALEX, now=NOW)

    assert result.success is True
    assert result.failure_kind is None
    assert result.message == "You have not set a timezone. Use /timezone set."


def test_timezone_show_only_own_value(migrated_db):
    set_user_setting(migrated_db, SAM, "timezone", "Asia/Tokyo", NOW)

    result = timezone_show(migrated_db, user_id=ALEX, now=NOW)

    assert result.message == "You have not set a timezone. Use /timezone set."


@pytest.mark.parametrize(
    ("hour", "minute", "expected"),
    [
        (0, 5, "12:05 AM"),
        (9, 0, "9:00 AM"),
        (11, 59, "11:59 AM"),
        (12, 0, "12:00 PM"),
        (16, 23, "4:23 PM"),
        (23, 59, "11:59 PM"),
    ],
)
def test_clock_text(hour, minute, expected):
    assert clock_text(datetime(2027, 1, 1, hour, minute)) == expected


# /birthday set


@pytest.mark.parametrize(
    ("month", "day", "stored", "message"),
    [
        (3, 14, "03-14", "Your birthday is set to March 14."),
        (1, 1, "01-01", "Your birthday is set to January 1."),
        (12, 31, "12-31", "Your birthday is set to December 31."),
    ],
)
def test_birthday_set_valid(migrated_db, month, day, stored, message):
    result = birthday_set(
        migrated_db, user_id=ALEX, month=month, day=day, now=NOW
    )

    assert result.success is True
    assert result.message == message
    row = stored_row(migrated_db, ALEX, "birthday")
    assert (row["value"], row["updated_at"]) == (stored, NOW)


def test_birthday_set_february_29(migrated_db):
    result = birthday_set(migrated_db, user_id=ALEX, month=2, day=29, now=NOW)

    assert result.message == "Your birthday is set to February 29."
    assert get_user_setting(migrated_db, ALEX, "birthday") == "02-29"


@pytest.mark.parametrize(
    ("month", "day", "message"),
    [
        (2, 30, "February has only 29 days."),
        (2, 31, "February has only 29 days."),
        (4, 31, "April has only 30 days."),
        (6, 31, "June has only 30 days."),
        (9, 31, "September has only 30 days."),
        (11, 31, "November has only 30 days."),
    ],
)
def test_birthday_set_impossible_date(migrated_db, month, day, message):
    result = birthday_set(
        migrated_db, user_id=ALEX, month=month, day=day, now=NOW
    )

    assert result.success is False
    assert result.failure_kind is FailureKind.INVALID
    assert result.message == message
    assert "-" not in result.message
    assert get_user_setting(migrated_db, ALEX, "birthday") is None


@pytest.mark.parametrize(
    ("month", "day", "message"),
    [
        (0, 1, "The month must be from 1 to 12."),
        (13, 1, "The month must be from 1 to 12."),
        (3, 0, "The day must be from 1 to 31."),
    ],
)
def test_birthday_set_out_of_range(migrated_db, month, day, message):
    result = birthday_set(
        migrated_db, user_id=ALEX, month=month, day=day, now=NOW
    )

    assert result.failure_kind is FailureKind.INVALID
    assert result.message == message
    assert get_user_setting(migrated_db, ALEX, "birthday") is None


def test_birthday_set_replaces_value(migrated_db):
    birthday_set(migrated_db, user_id=ALEX, month=3, day=14, now=NOW)
    result = birthday_set(migrated_db, user_id=ALEX, month=7, day=4, now=LATER)

    assert result.message == "Your birthday is set to July 4."
    row = stored_row(migrated_db, ALEX, "birthday")
    assert (row["value"], row["updated_at"]) == ("07-04", LATER)


def test_commands_write_no_audit_rows(migrated_db):
    timezone_set(migrated_db, user_id=ALEX, name="Europe/Paris", now=NOW)
    timezone_show(migrated_db, user_id=ALEX, now=NOW)
    birthday_set(migrated_db, user_id=ALEX, month=3, day=14, now=NOW)

    assert audit_count(migrated_db) == 0


# Permission keys and the registry


@pytest.mark.parametrize(
    ("name", "key"),
    [
        (TIMEZONE_SET_NAME, "prefs.set"),
        (TIMEZONE_SHOW_NAME, "prefs.view"),
        (BIRTHDAY_SET_NAME, "prefs.set"),
    ],
)
def test_registry_entries(name, key):
    entry = REAL_REGISTRY.get(name)

    assert entry.key.key == key
    assert entry.key.default_level is Level.EVERYONE
    assert entry.key.owner_only is False
    assert entry.key.protected is False
    assert entry.category is CommandCategory.REMINDERS_AND_TIME
    assert entry.visibility is Visibility.EPHEMERAL
    assert entry.example.startswith(f"/{name}")


def test_prefs_keys_registered():
    registry = REAL_REGISTRY.permission_registry()

    for key in ("prefs.view", "prefs.set"):
        assert registry.get(key).default_level is Level.EVERYONE


def run_as(connection, user_id, command_name, function):
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


def command_functions(connection):
    return [
        (
            TIMEZONE_SET_NAME,
            lambda: timezone_set(
                connection, user_id=ALEX, name="Europe/Paris", now=NOW
            ),
        ),
        (
            TIMEZONE_SHOW_NAME,
            lambda: timezone_show(connection, user_id=ALEX, now=NOW),
        ),
        (
            BIRTHDAY_SET_NAME,
            lambda: birthday_set(
                connection, user_id=ALEX, month=3, day=14, now=NOW
            ),
        ),
    ]


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


def test_level_0_can_run_all_three(migrated_db):
    for command_name, function in command_functions(migrated_db):
        result, usages = run_as(migrated_db, ALEX, command_name, function)

        assert result.success is True, command_name
        assert len(usages) == 1
        assert usages[0].command_name == command_name
        assert usages[0].allowed is True


def test_all_block_refuses_all_three(migrated_db):
    block(migrated_db, BLOCK_KIND_ALL)

    for command_name, function in command_functions(migrated_db):
        result, usages = run_as(migrated_db, ALEX, command_name, function)

        assert result.failure_kind is FailureKind.DENIED, command_name
        assert usages[0].allowed is False
    assert get_user_setting(migrated_db, ALEX, "timezone") is None
    assert get_user_setting(migrated_db, ALEX, "birthday") is None


def test_chat_block_leaves_commands_working(migrated_db):
    block(migrated_db, BLOCK_KIND_CHAT)

    for command_name, function in command_functions(migrated_db):
        result, _ = run_as(migrated_db, ALEX, command_name, function)

        assert result.success is True, command_name


# Timezone autocomplete


def test_autocomplete_empty_returns_common_list():
    assert timezone_name_matches("") == list(COMMON_TIMEZONES)
    assert timezone_name_matches("   ") == list(COMMON_TIMEZONES)


def test_autocomplete_partial_wrong_case():
    names = timezone_name_matches("LOS_ang")

    assert "America/Los_Angeles" in names


def test_autocomplete_prefix_matches_first():
    names = timezone_name_matches("europe/l")

    prefix = [name for name in names if name.lower().startswith("europe/l")]
    assert names[: len(prefix)] == prefix
    assert prefix == sorted(prefix)
    assert "Europe/London" in prefix


def test_autocomplete_rest_sorted_after_prefix():
    names = timezone_name_matches("york")

    # Nothing starts with "york", so every match contains it, sorted.
    assert "America/New_York" in names
    assert names == sorted(names)
    assert all("york" in name.lower() for name in names)


def test_autocomplete_prefix_group_then_contains_group():
    # "an" starts Antarctica/ names and appears inside others, such
    # as America/Anchorage.
    names = timezone_name_matches("an")

    starts = [name for name in names if name.lower().startswith("an")]
    rest = names[len(starts) :]
    assert starts
    assert rest
    assert starts == sorted(starts)
    assert rest == sorted(rest)
    assert not any(name.lower().startswith("an") for name in rest)


@pytest.mark.parametrize(
    "typed", ["US/Pacific", "us/pacific", "Etc/GMT+8", "posix/", "right/"]
)
def test_autocomplete_leaves_out_aliases(typed):
    names = timezone_name_matches(typed)

    assert names == []


@pytest.mark.parametrize(
    ("name", "suggested"),
    [
        ("America/Los_Angeles", True),
        ("Europe/London", True),
        ("UTC", True),
        ("US/Pacific", False),
        ("Etc/GMT+8", False),
        ("posix/America/Los_Angeles", False),
        ("right/Europe/London", False),
        ("Etc/UTC", False),
    ],
)
def test_suggestion_rule(name, suggested):
    # The posix/ and right/ copies are not on every machine's list,
    # so the rule itself is checked here.
    assert _is_suggested(name) is suggested


@pytest.mark.parametrize("typed", ["", "a", "e", "pacific", "etc", "gmt"])
def test_autocomplete_never_suggests_other_regions(typed):
    for name in timezone_name_matches(typed):
        assert name == "UTC" or name.startswith(SUGGESTED_TIMEZONE_PREFIXES)
        assert not name.startswith(("US/", "Etc/", "posix/", "right/"))


@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        ("America/Los_Angeles", "America/Los_Angeles"),
        ("europe/london", "Europe/London"),
        ("UTC", "UTC"),
        ("utc", "UTC"),
    ],
)
def test_autocomplete_suggests_region_names_and_utc(typed, expected):
    assert expected in timezone_name_matches(typed)


def test_timezone_set_accepts_alias_not_suggested(migrated_db):
    # Etc/GMT+8 is in tzdata's core "etcetera" file, so every build
    # has it, unlike legacy links such as US/Pacific. It sits outside
    # the suggested regions, so autocomplete never offers it.
    result = timezone_set(migrated_db, user_id=ALEX, name="etc/gmt+8", now=NOW)

    assert result.success is True
    assert result.message == "Your timezone is set to Etc/GMT+8."
    assert get_user_setting(migrated_db, ALEX, "timezone") == "Etc/GMT+8"


def test_autocomplete_at_most_25():
    names = timezone_name_matches("america")

    assert len(names) == 25
    assert all(len(name) < 100 for name in names)


def test_autocomplete_no_match():
    assert timezone_name_matches("zzzz-no-such-zone") == []


def test_timezone_choices_name_and_value():
    choices = timezone_choices("los_angeles")

    assert [(c.name, c.value) for c in choices] == [
        ("America/Los_Angeles", "America/Los_Angeles")
    ]


def test_timezone_autocomplete_answers_anyone():
    interaction = SimpleNamespace(user=SimpleNamespace(id=ALEX))

    choices = asyncio.run(timezone_autocomplete(interaction, ""))

    assert [c.value for c in choices] == list(COMMON_TIMEZONES)


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


def fake_interaction(user_id, now=NOW):
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id),
        guild_id=GUILD,
        channel_id=CHANNEL,
        created_at=datetime.fromtimestamp(now, tz=UTC),
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


def sent_reply(interaction):
    call = interaction.response.send_message.call_args
    return call.args[0], call.kwargs["ephemeral"]


def test_tree_has_timezone_and_birthday_groups(migrated_db):
    tree = make_client(migrated_db).tree

    timezone_group = tree.get_command("timezone")
    birthday_group = tree.get_command("birthday")
    assert isinstance(timezone_group, app_commands.Group)
    assert isinstance(birthday_group, app_commands.Group)
    assert [c.name for c in timezone_group.commands] == ["set", "show"]
    assert [c.name for c in birthday_group.commands] == ["set"]
    for group in (timezone_group, birthday_group):
        assert 1 <= len(group.description) < 100
        for command in group.commands:
            assert 1 <= len(command.description) < 100
            for parameter in command.parameters:
                assert 1 <= len(parameter.description) < 100


def test_timezone_set_parameters(migrated_db):
    command = make_client(migrated_db).tree.get_command("timezone")
    (name,) = command.get_command("set").parameters

    assert name.name == "name"
    assert name.type is discord.AppCommandOptionType.string
    assert name.required is True
    assert name.autocomplete is True


def test_timezone_show_has_no_parameters(migrated_db):
    command = make_client(migrated_db).tree.get_command("timezone")

    assert command.get_command("show").parameters == []


def test_birthday_set_parameters(migrated_db):
    command = make_client(migrated_db).tree.get_command("birthday")
    month, day = command.get_command("set").parameters

    assert month.name == "month"
    assert month.type is discord.AppCommandOptionType.integer
    assert month.required is True
    assert [(c.name, c.value) for c in month.choices] == [
        ("January", 1),
        ("February", 2),
        ("March", 3),
        ("April", 4),
        ("May", 5),
        ("June", 6),
        ("July", 7),
        ("August", 8),
        ("September", 9),
        ("October", 10),
        ("November", 11),
        ("December", 12),
    ]
    assert day.name == "day"
    assert day.type is discord.AppCommandOptionType.integer
    assert day.required is True
    assert day.min_value == 1
    assert day.max_value == 31


def test_timezone_set_callback_is_ephemeral(migrated_db):
    group = make_client(migrated_db).tree.get_command("timezone")
    interaction = fake_interaction(ALEX)

    asyncio.run(
        group.get_command("set").callback(interaction, name="america/new_york")
    )

    assert sent_reply(interaction) == (
        "Your timezone is set to America/New_York.",
        True,
    )
    row = stored_row(migrated_db, ALEX, "timezone")
    assert (row["value"], row["updated_at"]) == ("America/New_York", NOW)


def test_timezone_set_callback_invalid_is_ephemeral(migrated_db):
    group = make_client(migrated_db).tree.get_command("timezone")
    interaction = fake_interaction(ALEX)

    asyncio.run(group.get_command("set").callback(interaction, name="Nope"))

    text, ephemeral = sent_reply(interaction)
    assert text.startswith("I don't know the timezone Nope.")
    assert ephemeral is True


def test_timezone_show_callback_is_ephemeral(migrated_db):
    set_user_setting(migrated_db, ALEX, "timezone", "America/Los_Angeles", NOW)
    group = make_client(migrated_db).tree.get_command("timezone")
    interaction = fake_interaction(ALEX, now=SUMMER)

    asyncio.run(group.get_command("show").callback(interaction))

    assert sent_reply(interaction) == (
        "Your timezone is America/Los_Angeles. It is 4:23 PM there now.",
        True,
    )


def test_birthday_set_callback_is_ephemeral(migrated_db):
    group = make_client(migrated_db).tree.get_command("birthday")
    interaction = fake_interaction(ALEX)
    march = app_commands.Choice(name="March", value=3)

    asyncio.run(
        group.get_command("set").callback(interaction, month=march, day=14)
    )

    assert sent_reply(interaction) == (
        "Your birthday is set to March 14.",
        True,
    )
    assert get_user_setting(migrated_db, ALEX, "birthday") == "03-14"


def test_blocked_callback_refused(migrated_db):
    block(migrated_db, BLOCK_KIND_ALL)
    group = make_client(migrated_db).tree.get_command("timezone")
    interaction = fake_interaction(ALEX)

    asyncio.run(group.get_command("show").callback(interaction))

    assert sent_reply(interaction) == (DENIED_TEXT, True)
