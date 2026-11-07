import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest
from discord import app_commands

from maika import usage_log
from maika.bot import MaikaClient
from maika.command_catalog import build_command_registry
from maika.commands import CommandCategory, Visibility
from maika.config import Config, Environment
from maika.config_commands import config_get
from maika.discord_adapter import handle_command, handle_tree_error
from maika.permission_store import set_level
from maika.permissions import DecisionSource, Level, check_permission
from maika.slash_commands import (
    CONFIG_GET_DESCRIPTION,
    CONFIG_GROUP_DESCRIPTION,
    CONFIG_KEY_DESCRIPTION,
)

NOW = 1_800_000_000
OWNER = 1
ALEX = 100
GUILD = 5000
CHANNEL = 7000

DENIED_TEXT = "You don't have permission for that."
DEFAULT_REPLY = (
    "`backups.days_kept = 30` (default)\n"
    "Days of daily backups kept.\n"
    "Type: int. Range: 1 to 365. Default: 30."
)


def fake_interaction(user_id, guild_id=GUILD):
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id),
        guild_id=guild_id,
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
    return MaikaClient(
        config,
        connection=connection,
        command_registry=build_command_registry(),
    )


def run_setup_hook(client, synced=("config",)):
    client.tree.sync = AsyncMock(return_value=list(synced))
    asyncio.run(client.setup_hook())


def get_subcommand(client):
    group = client.tree.get_command("config")
    return group.get_command("get")


def reply(interaction):
    send = interaction.response.send_message
    send.assert_awaited_once()
    return send.call_args.args[0], send.call_args.kwargs["ephemeral"]


# The registry entry


def test_registry_entry():
    definition = build_command_registry().get("config get")

    assert definition.key.key == "config.get"
    assert definition.key.owner_only is True
    assert definition.key.protected is False
    assert definition.category is CommandCategory.OWNER
    assert definition.visibility is Visibility.EPHEMERAL
    assert "/config get" in definition.help_text
    assert definition.example.startswith("/config get")


@pytest.mark.parametrize("level", list(Level))
def test_non_owner_refused_at_owner_only_step(migrated_db, level):
    set_level(
        migrated_db,
        user_id=ALEX,
        level=level,
        owner_id=OWNER,
        actor_id=OWNER,
        now=NOW,
    )
    registry = build_command_registry()

    decision = check_permission(
        migrated_db,
        registry.permission_registry(),
        owner_id=OWNER,
        user_id=ALEX,
        key="config.get",
        guild_id=GUILD,
    )

    assert decision.allowed is False
    assert decision.source is DecisionSource.OWNER_ONLY_KEY


def test_non_owner_refused_through_handle_command(migrated_db):
    function = Mock()
    interaction = fake_interaction(ALEX)

    asyncio.run(
        handle_command(
            interaction,
            connection=migrated_db,
            registry=build_command_registry(),
            owner_id=OWNER,
            command_name="config get",
            function=function,
            log_usage=usage_log.log_usage,
        )
    )

    function.assert_not_called()
    assert reply(interaction) == (DENIED_TEXT, True)


# The tree after setup_hook


def tree_command_names(client):
    return {
        command.qualified_name
        for command in client.tree.walk_commands()
        if not isinstance(command, app_commands.Group)
    }


def test_tree_and_registry_match(migrated_db):
    client = make_client(migrated_db)
    run_setup_hook(client)

    registry_names = {
        definition.name for definition in client.command_registry.commands()
    }
    assert tree_command_names(client) == registry_names
    assert "config get" in registry_names


def test_tree_has_config_get(migrated_db):
    client = make_client(migrated_db)
    run_setup_hook(client)

    group = client.tree.get_command("config")
    assert isinstance(group, app_commands.Group)
    assert group.description == CONFIG_GROUP_DESCRIPTION
    assert [command.name for command in group.commands] == ["get"]

    get = get_subcommand(client)
    assert get.description == CONFIG_GET_DESCRIPTION
    (key,) = get.parameters
    assert key.name == "key"
    assert key.required is True
    assert key.type is discord.AppCommandOptionType.string
    assert key.description == CONFIG_KEY_DESCRIPTION
    client.tree.sync.assert_awaited_once_with()


def test_descriptions_fit_discord_limit():
    for text in (
        CONFIG_GROUP_DESCRIPTION,
        CONFIG_GET_DESCRIPTION,
        CONFIG_KEY_DESCRIPTION,
    ):
        assert 1 <= len(text) < 100


def test_tree_error_handler_installed(migrated_db):
    client = make_client(migrated_db)
    run_setup_hook(client)
    assert client.tree.on_error is handle_tree_error


def test_sync_count_logged(migrated_db, caplog):
    client = make_client(migrated_db)

    with caplog.at_level(logging.INFO, logger="maika.bot"):
        run_setup_hook(client, synced=("config", "other"))

    assert "Synced 2 slash commands." in caplog.messages


def test_sync_count_singular(migrated_db, caplog):
    client = make_client(migrated_db)

    with caplog.at_level(logging.INFO, logger="maika.bot"):
        run_setup_hook(client, synced=("config",))

    assert "Synced 1 slash command." in caplog.messages


def test_sync_count_zero(migrated_db, caplog):
    client = make_client(migrated_db)

    with caplog.at_level(logging.INFO, logger="maika.bot"):
        run_setup_hook(client, synced=())

    assert "Synced 0 slash commands." in caplog.messages


def test_sync_failure_logged_and_kept_running(migrated_db, caplog):
    client = make_client(migrated_db)
    response = SimpleNamespace(status=500, reason="Server Error")
    client.tree.sync = AsyncMock(
        side_effect=discord.HTTPException(response, "sync failed")
    )

    with caplog.at_level(logging.ERROR, logger="maika.bot"):
        asyncio.run(client.setup_hook())

    (record,) = caplog.records
    assert record.getMessage() == "Syncing the slash commands failed."
    assert record.exc_info is not None
    assert client.tree.get_command("config") is not None


# The callback


def test_callback_owner_gets_value(migrated_db, caplog):
    client = make_client(migrated_db)
    run_setup_hook(client)
    interaction = fake_interaction(OWNER)

    with caplog.at_level(logging.INFO, logger="maika"):
        asyncio.run(
            get_subcommand(client).callback(
                interaction, key="backups.days_kept"
            )
        )

    assert reply(interaction) == (DEFAULT_REPLY, True)
    usage = [
        message
        for message in caplog.messages
        if message.startswith("Command config get")
    ]
    assert len(usage) == 1
    assert "decided by owner" in usage[0]


def test_callback_unknown_key(migrated_db):
    client = make_client(migrated_db)
    run_setup_hook(client)
    interaction = fake_interaction(OWNER)

    asyncio.run(get_subcommand(client).callback(interaction, key="nope.x"))

    assert reply(interaction) == (
        "I don't know a setting called `nope.x`.",
        True,
    )


def test_callback_non_owner_refused(migrated_db):
    client = make_client(migrated_db)
    run_setup_hook(client)
    interaction = fake_interaction(ALEX)

    asyncio.run(
        get_subcommand(client).callback(interaction, key="backups.days_kept")
    )

    assert reply(interaction) == (DENIED_TEXT, True)


def test_callback_in_direct_message(migrated_db):
    client = make_client(migrated_db)
    run_setup_hook(client)
    interaction = fake_interaction(OWNER, guild_id=None)

    asyncio.run(
        get_subcommand(client).callback(interaction, key="backups.days_kept")
    )

    assert reply(interaction) == (DEFAULT_REPLY, True)


def test_callback_matches_config_get(migrated_db):
    client = make_client(migrated_db)
    run_setup_hook(client)
    interaction = fake_interaction(OWNER)

    asyncio.run(
        get_subcommand(client).callback(interaction, key="log.level.errors")
    )

    expected = config_get(migrated_db, "log.level.errors", GUILD).message
    assert reply(interaction)[0] == expected
