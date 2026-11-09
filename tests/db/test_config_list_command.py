import asyncio
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest

from maika import usage_log
from maika.bot import MaikaClient
from maika.command_catalog import build_command_registry
from maika.commands import CommandCategory, Visibility
from maika.config import Config, Environment
from maika.config_commands import SETTINGS, config_list, setting_categories
from maika.discord_adapter import handle_command
from maika.results import FailureKind
from maika.settings import (
    SettingDefinition,
    SettingLevel,
    SettingsRegistry,
    ValueType,
)
from maika.settings_store import set_value
from maika.slash_commands import (
    AUTOCOMPLETE_CHOICE_LIMIT,
    CHOICE_NAME_LIMIT,
    category_choices,
    key_choices,
    owner_autocomplete,
)

NOW = 1_800_000_000
OWNER = 1
ALEX = 100
GUILD = 5000
CHANNEL = 7000

DENIED_TEXT = "You don't have permission for that."


def definition(key, category, level, default, value_type=ValueType.INT):
    return SettingDefinition(
        key=key,
        category=category,
        level=level,
        server_scope=False,
        value_type=value_type,
        default=default,
        description=f"Description of {key}.",
    )


BASIC = SettingLevel.BASIC
ADVANCED = SettingLevel.ADVANCED

TEST_REGISTRY = SettingsRegistry(
    [
        definition("presence.idle_minutes", "presence", BASIC, 10),
        definition("presence.jitter", "presence", ADVANCED, 2),
        definition("mood.half_life_hours", "mood", BASIC, 6),
        definition(
            "presence.enabled", "presence", BASIC, True, ValueType.BOOL
        ),
        definition("tuning.secret_a", "tuning", ADVANCED, 1),
        definition("tuning.secret_b", "tuning", ADVANCED, 2),
    ]
)


def store(connection, key, text, registry=TEST_REGISTRY):
    set_value(connection, registry, key, text, actor_id=OWNER, now=NOW)


# config_list


def test_categories_in_first_appearance_order():
    assert setting_categories(TEST_REGISTRY) == ("presence", "mood", "tuning")


def test_overview_counts_basic_settings(migrated_db):
    result = config_list(migrated_db, TEST_REGISTRY, None, False)

    assert result.success is True
    assert result.message == (
        "presence (2)\n"
        "mood (1)\n"
        "tuning (0)\n"
        "Use `/config list category:<name>`."
    )


def test_overview_with_all_counts_every_setting(migrated_db):
    result = config_list(migrated_db, TEST_REGISTRY, None, True)

    assert result.message == (
        "presence (3)\n"
        "mood (1)\n"
        "tuning (2)\n"
        "Use `/config list category:<name>`."
    )


def test_overview_for_blank_category(migrated_db):
    result = config_list(migrated_db, TEST_REGISTRY, "  ", False)
    assert result.message.startswith("presence (2)\n")


def test_overview_with_foundation_settings(migrated_db):
    basic = config_list(migrated_db, SETTINGS, None, False)
    every = config_list(migrated_db, SETTINGS, None, True)

    assert basic.message == (
        "logging (10)\ncommands (0)\nUse `/config list category:<name>`."
    )
    assert every.message == (
        "logging (20)\ncommands (1)\nUse `/config list category:<name>`."
    )


def test_category_listing_shows_default_and_changed(migrated_db):
    store(migrated_db, "presence.enabled", "off")

    result = config_list(migrated_db, TEST_REGISTRY, "presence", False)

    assert result.success is True
    assert result.message == (
        "`presence.idle_minutes = 10`\n`presence.enabled = false` (changed)"
    )


def test_category_listing_with_all_includes_advanced(migrated_db):
    store(migrated_db, "presence.jitter", "5")

    result = config_list(migrated_db, TEST_REGISTRY, "presence", True)

    assert result.message == (
        "`presence.idle_minutes = 10`\n"
        "`presence.jitter = 5` (changed)\n"
        "`presence.enabled = true`"
    )


def test_category_listing_marks_a_stored_default_as_changed(migrated_db):
    store(migrated_db, "presence.idle_minutes", "10")

    result = config_list(migrated_db, TEST_REGISTRY, "presence", False)

    assert result.message.splitlines()[0] == (
        "`presence.idle_minutes = 10` (changed)"
    )


def test_category_name_ignores_case_and_spaces(migrated_db):
    result = config_list(migrated_db, TEST_REGISTRY, " Mood ", False)
    assert result.message == "`mood.half_life_hours = 6`"


def test_foundation_logging_listing(migrated_db):
    store(migrated_db, "backups.days_kept", "14", registry=SETTINGS)

    result = config_list(migrated_db, SETTINGS, "logging", False)

    lines = result.message.splitlines()
    assert len(lines) == 10
    assert lines[0] == "`log.channel.bot_log = none`"
    assert "`backups.time = 04:00`" in lines
    assert "`backups.days_kept = 14` (changed)" in lines
    assert not any(line.startswith("`log.level.") for line in lines)


def test_unknown_category(migrated_db):
    result = config_list(migrated_db, TEST_REGISTRY, "Nope", False)

    assert result.failure_kind is FailureKind.NOT_FOUND
    assert result.message == (
        "I don't know a settings category called `Nope`. "
        "Categories: presence, mood, tuning."
    )


def test_category_with_no_basic_settings(migrated_db):
    result = config_list(migrated_db, TEST_REGISTRY, "tuning", False)

    assert result.success is True
    assert result.message == (
        "No Basic settings in `tuning`. Add `all:true` to include "
        "Advanced settings."
    )


def test_category_with_no_basic_settings_shown_with_all(migrated_db):
    result = config_list(migrated_db, TEST_REGISTRY, "tuning", True)

    assert result.message == "`tuning.secret_a = 1`\n`tuning.secret_b = 2`"


def test_registry_entry():
    entry = build_command_registry().get("config list")

    assert entry.key.key == "config.list"
    assert entry.key.owner_only is True
    assert entry.key.protected is False
    assert entry.category is CommandCategory.OWNER
    assert entry.visibility is Visibility.EPHEMERAL
    assert "/config list" in entry.help_text
    assert entry.example.startswith("/config list")


# key_choices and category_choices


def names_and_values(choices):
    return [(choice.name, choice.value) for choice in choices]


def test_key_choices_match_key_or_description():
    registry = SettingsRegistry(
        [
            definition("mood.limit", "mood", BASIC, 1),
            SettingDefinition(
                key="sleep.window_start",
                category="sleep",
                level=BASIC,
                server_scope=False,
                value_type=ValueType.INT,
                default=1,
                description="When her mood settles for the night.",
            ),
            definition("presence.idle_minutes", "presence", BASIC, 1),
        ]
    )

    choices = key_choices(registry, "MOOD")

    assert [choice.value for choice in choices] == [
        "mood.limit",
        "sleep.window_start",
    ]
    assert choices[1].name == (
        "sleep.window_start: When her mood settles for the night."
    )


def test_key_choices_starting_matches_come_first():
    registry = SettingsRegistry(
        [
            definition("presence.log_level", "presence", BASIC, 1),
            definition("log.channel", "log", BASIC, 1),
            definition("backups.log_days", "backups", BASIC, 1),
            definition("log.level", "log", BASIC, 1),
        ]
    )

    choices = key_choices(registry, "log")

    assert [choice.value for choice in choices] == [
        "log.channel",
        "log.level",
        "presence.log_level",
        "backups.log_days",
    ]


def test_key_choices_empty_text_lists_keys_in_order():
    choices = key_choices(SETTINGS, "")
    assert [choice.value for choice in choices] == list(SETTINGS.keys())


def test_key_choices_no_match():
    assert key_choices(SETTINGS, "nothing-like-this") == []


def test_key_choices_limit():
    registry = SettingsRegistry(
        [definition(f"many.key_{n:02d}", "many", BASIC, n) for n in range(30)]
    )

    choices = key_choices(registry, "many")

    assert len(choices) == AUTOCOMPLETE_CHOICE_LIMIT == 25
    assert choices[-1].value == "many.key_24"


def test_key_choice_name_cut_to_100():
    long_key = "presence.a_rather_long_setting_name"
    registry = SettingsRegistry(
        [
            SettingDefinition(
                key=long_key,
                category="presence",
                level=BASIC,
                server_scope=False,
                value_type=ValueType.INT,
                default=1,
                description="d" * 100,
            )
        ]
    )

    (choice,) = key_choices(registry, "presence")

    assert len(choice.name) == CHOICE_NAME_LIMIT == 100
    assert choice.name.startswith(f"{long_key}: ddd")
    assert choice.name.endswith("…")
    assert choice.value == long_key


def test_foundation_key_choice_names_fit():
    for choice in key_choices(SETTINGS, ""):
        assert len(choice.name) <= CHOICE_NAME_LIMIT


def test_category_choices_contain_text():
    assert names_and_values(category_choices(TEST_REGISTRY, "O")) == [
        ("mood", "mood")
    ]
    assert [c.value for c in category_choices(TEST_REGISTRY, "")] == [
        "presence",
        "mood",
        "tuning",
    ]
    assert category_choices(TEST_REGISTRY, "zzz") == []


def test_category_choices_limit():
    registry = SettingsRegistry(
        [
            definition(f"cat{n:02d}.key", f"cat{n:02d}", BASIC, n)
            for n in range(30)
        ]
    )
    assert len(category_choices(registry, "cat")) == 25


def autocomplete_interaction(user_id):
    return SimpleNamespace(user=SimpleNamespace(id=user_id))


@pytest.mark.parametrize("choices_for", [key_choices, category_choices])
def test_autocomplete_answers_owner(choices_for):
    autocomplete = owner_autocomplete(OWNER, choices_for)

    choices = asyncio.run(autocomplete(autocomplete_interaction(OWNER), ""))

    assert choices == choices_for(SETTINGS, "")
    assert choices != []


@pytest.mark.parametrize("choices_for", [key_choices, category_choices])
def test_autocomplete_empty_for_non_owner(choices_for):
    autocomplete = owner_autocomplete(OWNER, choices_for)

    choices = asyncio.run(autocomplete(autocomplete_interaction(ALEX), ""))

    assert choices == []


# The slash command


def fake_interaction(user_id):
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id),
        guild_id=GUILD,
        channel_id=CHANNEL,
        created_at=datetime.fromtimestamp(NOW, tz=UTC),
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
    client.tree.sync = AsyncMock(return_value=[])
    asyncio.run(client.setup_hook())
    return client


def subcommand(client, name):
    return client.tree.get_command("config").get_command(name)


def reply(interaction):
    send = interaction.response.send_message
    send.assert_awaited_once()
    return send.call_args.args[0], send.call_args.kwargs["ephemeral"]


def test_list_parameters(migrated_db):
    client = make_client(migrated_db)

    command = subcommand(client, "list")

    category, show_all = command.parameters
    assert category.name == "category"
    assert category.required is False
    assert category.type is discord.AppCommandOptionType.string
    assert category.autocomplete is True
    assert show_all.display_name == "all"
    assert show_all.required is False
    assert show_all.type is discord.AppCommandOptionType.boolean
    assert show_all.default is False
    assert 1 <= len(command.description) < 100
    for parameter in command.parameters:
        assert 1 <= len(parameter.description) < 100


@pytest.mark.parametrize("name", ["get", "set", "reset"])
def test_key_parameters_autocomplete(migrated_db, name):
    client = make_client(migrated_db)

    command = subcommand(client, name)

    key = next(p for p in command.parameters if p.name == "key")
    assert key.autocomplete is True


def test_list_callback_owner_overview(migrated_db):
    client = make_client(migrated_db)
    interaction = fake_interaction(OWNER)

    asyncio.run(subcommand(client, "list").callback(interaction))

    assert reply(interaction) == (
        "logging (10)\ncommands (0)\nUse `/config list category:<name>`.",
        True,
    )


def test_list_callback_owner_category_all(migrated_db):
    client = make_client(migrated_db)
    interaction = fake_interaction(OWNER)

    asyncio.run(
        subcommand(client, "list").callback(
            interaction, category="logging", show_all=True
        )
    )

    message, ephemeral = reply(interaction)
    assert ephemeral is True
    assert len(message.splitlines()) == 20


def test_list_callback_non_owner_refused(migrated_db):
    client = make_client(migrated_db)
    interaction = fake_interaction(ALEX)

    asyncio.run(subcommand(client, "list").callback(interaction))

    assert reply(interaction) == (DENIED_TEXT, True)


def test_non_owner_refused_through_handle_command(migrated_db):
    function = Mock()
    interaction = fake_interaction(ALEX)

    asyncio.run(
        handle_command(
            interaction,
            connection=migrated_db,
            registry=build_command_registry(),
            owner_id=OWNER,
            command_name="config list",
            function=function,
            log_usage=usage_log.log_usage,
        )
    )

    function.assert_not_called()
    assert reply(interaction) == (DENIED_TEXT, True)
