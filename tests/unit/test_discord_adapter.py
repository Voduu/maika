import asyncio
import logging
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest

from maika.command_runner import CommandContext
from maika.commands import CommandCategory, CommandDefinition, Visibility
from maika.discord_adapter import (
    context_from_interaction,
    handle_tree_error,
    is_ephemeral,
    send_result,
)
from maika.permissions import Level, PermissionKey
from maika.results import CommandResult, FailureKind

ALEX = 100
GUILD = 5000
CHANNEL = 7000
NOW = 1_800_000_000
CREATED_AT = datetime.fromtimestamp(NOW + 0.75, tz=UTC)

CAT_VIEW = PermissionKey("cat.view", Level.EVERYONE)

ADAPTER_LOGGER = "maika.discord_adapter"


def define(visibility):
    return CommandDefinition(
        name="cat",
        description="Show a cat photo.",
        category=CommandCategory.CATS,
        key=CAT_VIEW,
        help_text="Posts a random photo of the named cat.",
        example="/cat name:buttons",
        visibility=visibility,
    )


PUBLIC_COMMAND = define(Visibility.PUBLIC)
EPHEMERAL_COMMAND = define(Visibility.EPHEMERAL)

OK = CommandResult.ok("Here is Buttons.")
NOT_FOUND = CommandResult.fail(FailureKind.NOT_FOUND, "No such cat.")
DENIED = CommandResult.fail(FailureKind.DENIED, "No.")


def fake_interaction(guild_id=GUILD, done=False):
    response = SimpleNamespace(
        is_done=Mock(return_value=done),
        send_message=AsyncMock(),
    )
    return SimpleNamespace(
        user=SimpleNamespace(id=ALEX),
        guild_id=guild_id,
        channel_id=CHANNEL,
        created_at=CREATED_AT,
        response=response,
        followup=SimpleNamespace(send=AsyncMock()),
    )


def http_error():
    response = SimpleNamespace(status=500, reason="Server Error")
    return discord.HTTPException(response, "send failed")


def assert_no_mentions(call):
    mentions = call.kwargs["allowed_mentions"]
    assert isinstance(mentions, discord.AllowedMentions)
    assert mentions.everyone is False
    assert mentions.users is False
    assert mentions.roles is False
    assert mentions.replied_user is False


# context_from_interaction


def test_context_in_a_server():
    assert context_from_interaction(fake_interaction()) == CommandContext(
        user_id=ALEX, guild_id=GUILD, channel_id=CHANNEL, now=NOW
    )


def test_context_in_a_direct_message():
    context = context_from_interaction(fake_interaction(guild_id=None))
    assert context.guild_id == 0


def test_context_without_channel_raises():
    interaction = fake_interaction()
    interaction.channel_id = None

    with pytest.raises(ValueError, match="has no channel ID"):
        context_from_interaction(interaction)


def test_context_now_from_created_at():
    # The fraction of a second is dropped.
    context = context_from_interaction(fake_interaction())
    assert context.now == NOW
    assert isinstance(context.now, int)


# is_ephemeral


@pytest.mark.parametrize("definition", [PUBLIC_COMMAND, EPHEMERAL_COMMAND])
def test_denied_is_always_ephemeral(definition):
    assert is_ephemeral(definition, DENIED) is True


def test_public_success():
    assert is_ephemeral(PUBLIC_COMMAND, OK) is False


def test_ephemeral_success():
    assert is_ephemeral(EPHEMERAL_COMMAND, OK) is True


def test_public_failure_stays_public():
    assert is_ephemeral(PUBLIC_COMMAND, NOT_FOUND) is False


def test_ephemeral_failure_stays_ephemeral():
    assert is_ephemeral(EPHEMERAL_COMMAND, NOT_FOUND) is True


# send_result


def test_first_reply_uses_response():
    interaction = fake_interaction()

    asyncio.run(send_result(interaction, PUBLIC_COMMAND, OK))

    send = interaction.response.send_message
    send.assert_awaited_once()
    assert send.call_args.args == ("Here is Buttons.",)
    assert send.call_args.kwargs["ephemeral"] is False
    assert_no_mentions(send.call_args)
    interaction.followup.send.assert_not_awaited()


def test_later_reply_uses_followup():
    interaction = fake_interaction(done=True)

    asyncio.run(send_result(interaction, PUBLIC_COMMAND, DENIED))

    send = interaction.followup.send
    send.assert_awaited_once()
    assert send.call_args.args == ("No.",)
    assert send.call_args.kwargs["ephemeral"] is True
    assert_no_mentions(send.call_args)
    interaction.response.send_message.assert_not_awaited()


@pytest.mark.parametrize("done", [False, True])
def test_http_error_is_logged_not_raised(caplog, done):
    interaction = fake_interaction(done=done)
    interaction.response.send_message.side_effect = http_error()
    interaction.followup.send.side_effect = http_error()

    with caplog.at_level(logging.ERROR, logger=ADAPTER_LOGGER):
        asyncio.run(send_result(interaction, PUBLIC_COMMAND, OK))

    (record,) = caplog.records
    assert record.getMessage() == "Sending the reply to cat failed."
    assert record.exc_info is not None


# handle_tree_error


def test_tree_error_logged_and_answered(caplog):
    interaction = fake_interaction()
    error = RuntimeError("boom")

    with caplog.at_level(logging.ERROR, logger=ADAPTER_LOGGER):
        asyncio.run(handle_tree_error(interaction, error))

    assert caplog.records[0].exc_info[1] is error
    send = interaction.response.send_message
    send.assert_awaited_once()
    assert send.call_args.args == ("Something went wrong.",)
    assert send.call_args.kwargs["ephemeral"] is True
    assert_no_mentions(send.call_args)


def test_tree_error_after_reply_sends_nothing(caplog):
    interaction = fake_interaction(done=True)

    with caplog.at_level(logging.ERROR, logger=ADAPTER_LOGGER):
        asyncio.run(handle_tree_error(interaction, RuntimeError("boom")))

    assert len(caplog.records) == 1
    interaction.response.send_message.assert_not_awaited()
    interaction.followup.send.assert_not_awaited()


def test_tree_error_reply_failure_is_logged(caplog):
    interaction = fake_interaction()
    interaction.response.send_message.side_effect = http_error()

    with caplog.at_level(logging.ERROR, logger=ADAPTER_LOGGER):
        asyncio.run(handle_tree_error(interaction, RuntimeError("boom")))

    assert [r.getMessage() for r in caplog.records] == [
        "A slash-command interaction raised an error.",
        "Sending the error reply failed.",
    ]
