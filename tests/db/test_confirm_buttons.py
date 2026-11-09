import asyncio
import logging
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest

from maika.command_runner import DENIED_MESSAGE
from maika.commands import (
    CommandCategory,
    CommandDefinition,
    CommandRegistry,
    Visibility,
)
from maika.discord_adapter import (
    CANCELLED_MESSAGE,
    NOT_YOUR_BUTTONS_MESSAGE,
    SETTINGS,
    TIMED_OUT_MESSAGE,
    ConfirmView,
    handle_command,
    send_result,
)
from maika.foundation_settings import CONFIRM_TIMEOUT_KEY
from maika.permission_store import set_level
from maika.permissions import DecisionSource, Level, PermissionKey
from maika.results import CommandResult
from maika.settings_store import set_value

NOW = 1_800_000_000
PRESSED_AT = NOW + 30
OWNER = 1
ALEX = 100
SAM = 200
GUILD = 5000
CHANNEL = 7000

PROMPT = "Retag photo 214 as Buttons?"
DONE = "Retagged photo 214."

ADAPTER_LOGGER = "maika.discord_adapter"

RETAG = CommandDefinition(
    name="retag",
    description="Change a photo's cats.",
    category=CommandCategory.MANAGE,
    key=PermissionKey("cat.retag", Level.TRUSTED),
    help_text="Adds or removes a cat on a photo.",
    example="/retag photo:214 cat:buttons",
    visibility=Visibility.PUBLIC,
)
REGISTRY = CommandRegistry([RETAG])


def fake_interaction(user_id, done=False, now=NOW):
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id),
        guild_id=GUILD,
        channel_id=CHANNEL,
        created_at=datetime.fromtimestamp(now, tz=UTC),
        response=SimpleNamespace(
            is_done=Mock(return_value=done),
            send_message=AsyncMock(),
            edit_message=AsyncMock(),
        ),
        followup=SimpleNamespace(send=AsyncMock()),
        edit_original_response=AsyncMock(),
    )


def fake_press(user_id):
    """A button interaction pressed by ``user_id``."""
    return fake_interaction(user_id, now=PRESSED_AT)


def http_error():
    response = SimpleNamespace(status=500, reason="Server Error")
    return discord.HTTPException(response, "edit failed")


async def ask(connection, interaction, action, usages):
    """Run retag with a result that asks first; return the view."""
    await handle_command(
        interaction,
        connection=connection,
        registry=REGISTRY,
        owner_id=OWNER,
        command_name="retag",
        function=lambda: CommandResult.ask(PROMPT, action),
        log_usage=usages.append,
    )
    send = interaction.response.send_message
    if send.await_count:
        return send.call_args.kwargs.get("view")
    return interaction.followup.send.call_args.kwargs.get("view")


def assert_no_mentions(call):
    mentions = call.kwargs["allowed_mentions"]
    assert isinstance(mentions, discord.AllowedMentions)
    assert mentions.everyone is False
    assert mentions.users is False
    assert mentions.roles is False


def edited_to(press):
    edit = press.response.edit_message
    edit.assert_awaited_once()
    assert edit.call_args.kwargs["view"] is None
    assert_no_mentions(edit.call_args)
    return edit.call_args.kwargs["content"]


# The prompt


def test_prompt_is_ephemeral_with_two_buttons(migrated_db):
    action = Mock(return_value=CommandResult.ok(DONE))
    interaction = fake_interaction(OWNER)
    usages = []

    async def scenario():
        return await ask(migrated_db, interaction, action, usages)

    view = asyncio.run(scenario())

    send = interaction.response.send_message
    send.assert_awaited_once()
    assert send.call_args.args == (PROMPT,)
    # retag is a public command, but the prompt is still ephemeral.
    assert send.call_args.kwargs["ephemeral"] is True
    assert_no_mentions(send.call_args)
    assert isinstance(view, ConfirmView)
    assert [(b.label, b.style) for b in view.children] == [
        ("Confirm", discord.ButtonStyle.success),
        ("Cancel", discord.ButtonStyle.secondary),
    ]
    assert view.timeout == 60
    action.assert_not_called()
    assert len(usages) == 1
    assert usages[0].success is True


def test_timeout_comes_from_the_setting(migrated_db):
    set_value(
        migrated_db,
        SETTINGS,
        CONFIRM_TIMEOUT_KEY,
        "120",
        actor_id=OWNER,
        now=NOW,
    )
    interaction = fake_interaction(OWNER)

    async def scenario():
        return await ask(migrated_db, interaction, Mock(), [])

    view = asyncio.run(scenario())

    assert view.timeout == 120


def test_prompt_after_reply_uses_followup(migrated_db):
    interaction = fake_interaction(OWNER, done=True)

    async def scenario():
        return await ask(migrated_db, interaction, Mock(), [])

    view = asyncio.run(scenario())

    interaction.response.send_message.assert_not_awaited()
    send = interaction.followup.send
    send.assert_awaited_once()
    assert send.call_args.args == (PROMPT,)
    assert send.call_args.kwargs["ephemeral"] is True
    assert send.call_args.kwargs["wait"] is True
    assert view.message is send.return_value


def test_failed_prompt_send_is_logged(migrated_db, caplog):
    interaction = fake_interaction(OWNER)
    interaction.response.send_message.side_effect = http_error()

    async def scenario():
        return await ask(migrated_db, interaction, Mock(), [])

    with caplog.at_level(logging.ERROR, logger=ADAPTER_LOGGER):
        view = asyncio.run(scenario())

    assert caplog.messages == ["Sending the confirmation of retag failed."]
    assert view.is_finished()


# Confirm


def test_confirm_runs_the_action_once(migrated_db):
    action = Mock(return_value=CommandResult.ok(DONE))
    interaction = fake_interaction(OWNER)
    press = fake_press(OWNER)
    usages = []

    async def scenario():
        view = await ask(migrated_db, interaction, action, usages)
        await view.confirm(press)
        return view

    view = asyncio.run(scenario())

    action.assert_called_once_with()
    assert edited_to(press) == DONE
    assert view.is_finished()
    assert len(usages) == 2
    confirmed = usages[1]
    assert confirmed.command_name == "retag"
    assert confirmed.key == "cat.retag"
    assert confirmed.user_id == OWNER
    assert confirmed.now == PRESSED_AT
    assert confirmed.allowed is True
    assert confirmed.success is True


def test_confirm_checks_permission_again(migrated_db):
    set_level(
        migrated_db,
        user_id=ALEX,
        level=Level.TRUSTED,
        owner_id=OWNER,
        actor_id=OWNER,
        now=NOW,
    )
    action = Mock(return_value=CommandResult.ok(DONE))
    interaction = fake_interaction(ALEX)
    press = fake_press(ALEX)
    usages = []

    async def scenario():
        view = await ask(migrated_db, interaction, action, usages)
        # Alex loses the level while the prompt is open.
        set_level(
            migrated_db,
            user_id=ALEX,
            level=Level.EVERYONE,
            owner_id=OWNER,
            actor_id=OWNER,
            now=NOW,
        )
        await view.confirm(press)

    asyncio.run(scenario())

    action.assert_not_called()
    assert edited_to(press) == DENIED_MESSAGE
    assert [usage.allowed for usage in usages] == [True, False]
    assert usages[1].decision_source is DecisionSource.LEVEL


def test_failed_confirm_edit_is_logged(migrated_db, caplog):
    action = Mock(return_value=CommandResult.ok(DONE))
    press = fake_press(OWNER)
    press.response.edit_message.side_effect = http_error()

    async def scenario():
        view = await ask(migrated_db, fake_interaction(OWNER), action, [])
        await view.confirm(press)

    with caplog.at_level(logging.ERROR, logger=ADAPTER_LOGGER):
        asyncio.run(scenario())

    action.assert_called_once_with()
    assert caplog.messages == ["Editing the confirmation of retag failed."]


# Cancel


def test_cancel_changes_nothing(migrated_db):
    action = Mock(return_value=CommandResult.ok(DONE))
    press = fake_press(OWNER)
    usages = []

    async def scenario():
        view = await ask(migrated_db, fake_interaction(OWNER), action, usages)
        await view.cancel(press)
        return view

    view = asyncio.run(scenario())

    action.assert_not_called()
    assert edited_to(press) == CANCELLED_MESSAGE
    assert CANCELLED_MESSAGE == "Cancelled. Nothing changed."
    assert view.is_finished()
    assert len(usages) == 1


# Who may press, and how often


@pytest.mark.parametrize("button", ["confirm", "cancel"])
def test_another_user_is_refused(migrated_db, button):
    action = Mock(return_value=CommandResult.ok(DONE))
    other = fake_press(SAM)
    usages = []

    async def scenario():
        view = await ask(migrated_db, fake_interaction(OWNER), action, usages)
        await getattr(view, button)(other)
        return view

    view = asyncio.run(scenario())

    send = other.response.send_message
    send.assert_awaited_once()
    assert send.call_args.args == (NOT_YOUR_BUTTONS_MESSAGE,)
    assert NOT_YOUR_BUTTONS_MESSAGE == "These buttons belong to someone else."
    assert send.call_args.kwargs["ephemeral"] is True
    assert_no_mentions(send.call_args)
    other.response.edit_message.assert_not_awaited()
    action.assert_not_called()
    assert not view.is_finished()
    assert len(usages) == 1


def test_owner_can_still_confirm_after_a_refused_press(migrated_db):
    action = Mock(return_value=CommandResult.ok(DONE))
    press = fake_press(OWNER)

    async def scenario():
        view = await ask(migrated_db, fake_interaction(OWNER), action, [])
        await view.confirm(fake_press(SAM))
        await view.confirm(press)

    asyncio.run(scenario())

    action.assert_called_once_with()
    assert edited_to(press) == DONE


@pytest.mark.parametrize("second", ["confirm", "cancel"])
def test_second_press_is_ignored(migrated_db, second):
    action = Mock(return_value=CommandResult.ok(DONE))
    first = fake_press(OWNER)
    again = fake_press(OWNER)
    usages = []

    async def scenario():
        view = await ask(migrated_db, fake_interaction(OWNER), action, usages)
        await view.confirm(first)
        await getattr(view, second)(again)

    asyncio.run(scenario())

    action.assert_called_once_with()
    assert edited_to(first) == DONE
    again.response.edit_message.assert_not_awaited()
    again.response.send_message.assert_not_awaited()
    assert len(usages) == 2


# Timeout


def test_timeout_edits_the_prompt(migrated_db):
    action = Mock(return_value=CommandResult.ok(DONE))
    interaction = fake_interaction(OWNER)

    async def scenario():
        view = await ask(migrated_db, interaction, action, [])
        await view.on_timeout()

    asyncio.run(scenario())

    edit = interaction.edit_original_response
    edit.assert_awaited_once()
    assert edit.call_args.kwargs["content"] == TIMED_OUT_MESSAGE
    assert TIMED_OUT_MESSAGE == "Timed out. Nothing changed."
    assert edit.call_args.kwargs["view"] is None
    assert_no_mentions(edit.call_args)
    action.assert_not_called()


def test_timeout_edits_a_followup_prompt(migrated_db):
    interaction = fake_interaction(OWNER, done=True)
    message = SimpleNamespace(edit=AsyncMock())
    interaction.followup.send.return_value = message

    async def scenario():
        view = await ask(migrated_db, interaction, Mock(), [])
        await view.on_timeout()

    asyncio.run(scenario())

    message.edit.assert_awaited_once()
    assert message.edit.call_args.kwargs["content"] == TIMED_OUT_MESSAGE
    assert message.edit.call_args.kwargs["view"] is None
    interaction.edit_original_response.assert_not_awaited()


def test_failed_timeout_edit_is_logged(migrated_db, caplog):
    interaction = fake_interaction(OWNER)
    interaction.edit_original_response.side_effect = http_error()

    async def scenario():
        view = await ask(migrated_db, interaction, Mock(), [])
        await view.on_timeout()

    with caplog.at_level(logging.ERROR, logger=ADAPTER_LOGGER):
        asyncio.run(scenario())

    (record,) = caplog.records
    assert record.getMessage() == (
        "Editing the timed-out confirmation of retag failed."
    )
    assert record.exc_info is not None


def test_timeout_after_a_press_edits_nothing(migrated_db):
    interaction = fake_interaction(OWNER)

    async def scenario():
        view = await ask(migrated_db, interaction, Mock(), [])
        await view.cancel(fake_press(OWNER))
        await view.on_timeout()

    asyncio.run(scenario())

    interaction.edit_original_response.assert_not_awaited()


# Results without a confirmation


def test_result_without_confirmation_is_sent_as_before(migrated_db):
    interaction = fake_interaction(OWNER)

    async def scenario():
        await handle_command(
            interaction,
            connection=migrated_db,
            registry=REGISTRY,
            owner_id=OWNER,
            command_name="retag",
            function=lambda: CommandResult.ok(DONE),
            log_usage=lambda usage: None,
        )

    asyncio.run(scenario())

    send = interaction.response.send_message
    send.assert_awaited_once()
    assert send.call_args.args == (DONE,)
    assert send.call_args.kwargs["ephemeral"] is False
    assert "view" not in send.call_args.kwargs


def test_confirmation_without_lane_raises():
    result = CommandResult.ask(PROMPT, Mock())

    with pytest.raises(ValueError, match="needs a lane"):
        asyncio.run(send_result(fake_interaction(OWNER), RETAG, result))
