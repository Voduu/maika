import asyncio
import logging
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from maika import usage_log
from maika.commands import (
    CommandCategory,
    CommandDefinition,
    CommandRegistry,
    Visibility,
)
from maika.discord_adapter import handle_command
from maika.permissions import Level, PermissionKey
from maika.results import CommandResult

NOW = 1_800_000_000
OWNER = 1
ALEX = 100
GUILD = 5000
CHANNEL = 7000

REGISTRY = CommandRegistry(
    [
        CommandDefinition(
            name="retag",
            description="Change a photo's cats.",
            category=CommandCategory.MANAGE,
            key=PermissionKey("cat.retag", Level.TRUSTED),
            help_text="Adds or removes a cat on a photo.",
            example="/retag photo:214 cat:buttons",
            visibility=Visibility.PUBLIC,
        )
    ]
)


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


def handle(connection, interaction, function):
    asyncio.run(
        handle_command(
            interaction,
            connection=connection,
            registry=REGISTRY,
            owner_id=OWNER,
            command_name="retag",
            function=function,
            log_usage=usage_log.log_usage,
        )
    )


def usage_lines(caplog):
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == "maika"
    ]


def test_owner_runs_command_publicly(migrated_db, caplog):
    function = Mock(return_value=CommandResult.ok("Retagged photo 214."))
    interaction = fake_interaction(OWNER)

    with caplog.at_level(logging.INFO, logger="maika"):
        handle(migrated_db, interaction, function)

    function.assert_called_once_with()
    send = interaction.response.send_message
    send.assert_awaited_once()
    assert send.call_args.args == ("Retagged photo 214.",)
    assert send.call_args.kwargs["ephemeral"] is False
    assert usage_lines(caplog) == [
        "Command retag (cat.retag): user 1, guild 5000, channel 7000, "
        "allowed True, decided by owner (owner), success True, "
        "failure none"
    ]


def test_non_owner_is_refused_privately(migrated_db, caplog):
    function = Mock(return_value=CommandResult.ok("Retagged photo 214."))
    interaction = fake_interaction(ALEX)

    with caplog.at_level(logging.INFO, logger="maika"):
        handle(migrated_db, interaction, function)

    function.assert_not_called()
    send = interaction.response.send_message
    send.assert_awaited_once()
    assert send.call_args.args == ("You don't have permission for that.",)
    assert send.call_args.kwargs["ephemeral"] is True
    assert usage_lines(caplog) == [
        "Command retag (cat.retag): user 100, guild 5000, channel 7000, "
        "allowed False, decided by level (level 0 (everyone), "
        "cat.retag needs level 2), success False, failure denied"
    ]
