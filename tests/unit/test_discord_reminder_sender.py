import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import aiohttp
import discord
import pytest

from maika.discord_reminder_sender import DiscordReminderSender
from maika.reminder_delivery import ChannelAccess, SendOutcome

USER = 100
CHANNEL = 7000
GUILD = 5000
TEXT = "Reminder: water the secret plants"

SENT = SendOutcome.SENT
REFUSED = SendOutcome.REFUSED
RETRY = SendOutcome.RETRY


def http_error(error_class, status):
    response = SimpleNamespace(status=status, reason="reason")
    return error_class(response, "error")


class FakeUser:
    def __init__(self):
        self.send = AsyncMock()


class FakeGuild:
    def __init__(self, *, member=None, fetch_error=None):
        self.id = GUILD
        self.fetch_member = AsyncMock(
            return_value=member, side_effect=fetch_error
        )
        self.get_member = Mock(return_value=member)


class FakeChannel(discord.abc.Messageable):
    """A server text channel: it can hold messages and has a guild."""

    def __init__(self, *, guild=None, can_view=True):
        self.send = AsyncMock()
        self.guild = guild
        self.can_view = can_view
        self.permissions_for = Mock(
            return_value=discord.Permissions(view_channel=can_view)
        )


class FakeCategory:
    """A category channel: it cannot hold messages."""

    def __init__(self):
        self.guild = FakeGuild()


class FakeClient:
    def __init__(self, *, users=None, channels=None):
        self.users = users or {}
        self.channels = channels or {}
        self.fetch_user = AsyncMock(side_effect=self._fetch_user)
        self.fetch_channel = AsyncMock(side_effect=self._fetch_channel)

    def get_user(self, user_id):
        return None

    def get_channel(self, channel_id):
        return None

    async def _fetch_user(self, user_id):
        if user_id not in self.users:
            raise http_error(discord.NotFound, 404)
        return self.users[user_id]

    async def _fetch_channel(self, channel_id):
        if channel_id not in self.channels:
            raise http_error(discord.NotFound, 404)
        return self.channels[channel_id]


class CachingClient(FakeClient):
    def get_user(self, user_id):
        return self.users.get(user_id)

    def get_channel(self, channel_id):
        return self.channels.get(channel_id)


def run(coroutine):
    return asyncio.run(coroutine)


# send_dm


def test_dm_sent_with_mentions_off():
    user = FakeUser()
    sender = DiscordReminderSender(FakeClient(users={USER: user}))

    outcome = run(sender.send_dm(USER, TEXT))

    assert outcome is SENT
    user.send.assert_awaited_once()
    call = user.send.call_args
    assert call.args == (TEXT,)
    allowed = call.kwargs["allowed_mentions"]
    assert allowed.everyone is False
    assert allowed.users is False
    assert allowed.roles is False
    assert allowed.replied_user is False


def test_dm_uses_cached_user_without_fetch():
    user = FakeUser()
    client = CachingClient(users={USER: user})

    assert run(DiscordReminderSender(client).send_dm(USER, TEXT)) is SENT
    client.fetch_user.assert_not_awaited()


def test_dm_to_missing_user_is_refused():
    sender = DiscordReminderSender(FakeClient())
    assert run(sender.send_dm(USER, TEXT)) is REFUSED


ERROR_OUTCOMES = [
    (http_error(discord.Forbidden, 403), REFUSED),
    (http_error(discord.NotFound, 404), REFUSED),
    (http_error(discord.HTTPException, 500), RETRY),
    (http_error(discord.DiscordServerError, 503), RETRY),
    (http_error(discord.HTTPException, 400), REFUSED),
    (aiohttp.ClientError(), RETRY),
    (aiohttp.ClientConnectionError(), RETRY),
    # asyncio.TimeoutError is the built-in TimeoutError.
    (TimeoutError(), RETRY),
    (OSError(), RETRY),
]


@pytest.mark.parametrize(("error", "outcome"), ERROR_OUTCOMES)
def test_dm_error_mapping(error, outcome):
    user = FakeUser()
    user.send.side_effect = error
    sender = DiscordReminderSender(FakeClient(users={USER: user}))

    assert run(sender.send_dm(USER, TEXT)) is outcome


@pytest.mark.parametrize(("error", "outcome"), ERROR_OUTCOMES)
def test_user_fetch_error_mapping(error, outcome):
    client = FakeClient()
    client.fetch_user.side_effect = error

    assert run(DiscordReminderSender(client).send_dm(USER, TEXT)) is outcome


def test_dm_unexpected_error_propagates():
    user = FakeUser()
    user.send.side_effect = RuntimeError("boom")
    sender = DiscordReminderSender(FakeClient(users={USER: user}))

    with pytest.raises(RuntimeError):
        run(sender.send_dm(USER, TEXT))


def test_refused_http_error_log_has_no_text(caplog):
    user = FakeUser()
    user.send.side_effect = http_error(discord.HTTPException, 400)
    sender = DiscordReminderSender(FakeClient(users={USER: user}))

    run(sender.send_dm(USER, TEXT))

    assert caplog.records
    for record in caplog.records:
        assert TEXT not in record.getMessage()
        assert "secret" not in record.getMessage()


# post_to_channel and post_notice


@pytest.mark.parametrize("method", ["post_to_channel", "post_notice"])
def test_post_mentions_only_that_user(method):
    channel = FakeChannel(guild=FakeGuild())
    sender = DiscordReminderSender(FakeClient(channels={CHANNEL: channel}))

    outcome = run(getattr(sender, method)(CHANNEL, USER, TEXT))

    assert outcome is SENT
    call = channel.send.call_args
    assert call.args == (TEXT,)
    allowed = call.kwargs["allowed_mentions"]
    assert [user.id for user in allowed.users] == [USER]
    assert allowed.everyone is False
    assert allowed.roles is False
    assert allowed.replied_user is False


@pytest.mark.parametrize("method", ["post_to_channel", "post_notice"])
@pytest.mark.parametrize(("error", "outcome"), ERROR_OUTCOMES)
def test_post_error_mapping(method, error, outcome):
    channel = FakeChannel(guild=FakeGuild())
    channel.send.side_effect = error
    sender = DiscordReminderSender(FakeClient(channels={CHANNEL: channel}))

    assert run(getattr(sender, method)(CHANNEL, USER, TEXT)) is outcome


def test_post_unexpected_error_propagates():
    channel = FakeChannel(guild=FakeGuild())
    channel.send.side_effect = RuntimeError("boom")
    sender = DiscordReminderSender(FakeClient(channels={CHANNEL: channel}))

    with pytest.raises(RuntimeError):
        run(sender.post_to_channel(CHANNEL, USER, TEXT))


def test_post_falls_back_to_fetch_channel():
    channel = FakeChannel(guild=FakeGuild())
    client = FakeClient(channels={CHANNEL: channel})

    outcome = run(
        DiscordReminderSender(client).post_to_channel(CHANNEL, USER, TEXT)
    )

    assert outcome is SENT
    client.fetch_channel.assert_awaited_once_with(CHANNEL)


def test_post_uses_cached_channel_without_fetch():
    channel = FakeChannel(guild=FakeGuild())
    client = CachingClient(channels={CHANNEL: channel})

    outcome = run(
        DiscordReminderSender(client).post_to_channel(CHANNEL, USER, TEXT)
    )

    assert outcome is SENT
    client.fetch_channel.assert_not_awaited()


def test_post_to_missing_channel_is_refused():
    sender = DiscordReminderSender(FakeClient())
    assert run(sender.post_to_channel(CHANNEL, USER, TEXT)) is REFUSED


def test_post_to_channel_that_cannot_send_is_refused():
    sender = DiscordReminderSender(
        FakeClient(channels={CHANNEL: FakeCategory()})
    )
    assert run(sender.post_to_channel(CHANNEL, USER, TEXT)) is REFUSED


# channel_access


def access_for(channel, client_class=FakeClient):
    sender = DiscordReminderSender(client_class(channels={CHANNEL: channel}))
    return run(sender.channel_access(CHANNEL, USER))


@pytest.mark.parametrize("can_view", [True, False])
def test_member_access(can_view):
    member = object()
    guild = FakeGuild(member=member)
    channel = FakeChannel(guild=guild, can_view=can_view)

    access = access_for(channel)

    assert access == ChannelAccess(server_id=GUILD, creator_can_view=can_view)
    guild.fetch_member.assert_awaited_once_with(USER)
    channel.permissions_for.assert_called_once_with(member)
    guild.get_member.assert_not_called()


def test_not_a_member_cannot_view():
    guild = FakeGuild(fetch_error=http_error(discord.NotFound, 404))
    channel = FakeChannel(guild=guild)

    access = access_for(channel)

    assert access == ChannelAccess(server_id=GUILD, creator_can_view=False)
    channel.permissions_for.assert_not_called()


@pytest.mark.parametrize(
    "error",
    [
        http_error(discord.HTTPException, 500),
        aiohttp.ClientError(),
        TimeoutError(),
        OSError(),
    ],
)
def test_member_fetch_temporary_error_is_unknown(error):
    channel = FakeChannel(guild=FakeGuild(fetch_error=error))
    assert access_for(channel) is None


def test_member_fetch_forbidden_cannot_view():
    guild = FakeGuild(fetch_error=http_error(discord.Forbidden, 403))
    access = access_for(FakeChannel(guild=guild))
    assert access == ChannelAccess(server_id=GUILD, creator_can_view=False)


def test_missing_channel_is_unreachable():
    sender = DiscordReminderSender(FakeClient())

    access = run(sender.channel_access(CHANNEL, USER))

    assert access == ChannelAccess(server_id=None, creator_can_view=False)


def test_forbidden_channel_is_unreachable():
    client = FakeClient()
    client.fetch_channel.side_effect = http_error(discord.Forbidden, 403)

    access = run(DiscordReminderSender(client).channel_access(CHANNEL, USER))

    assert access == ChannelAccess(server_id=None, creator_can_view=False)


@pytest.mark.parametrize(
    "error",
    [http_error(discord.HTTPException, 502), aiohttp.ClientError()],
)
def test_channel_fetch_temporary_error_is_unknown(error):
    client = FakeClient()
    client.fetch_channel.side_effect = error

    assert (
        run(DiscordReminderSender(client).channel_access(CHANNEL, USER))
        is None
    )


def test_dm_channel_is_unreachable():
    dm_channel = FakeChannel(guild=None)

    assert access_for(dm_channel) == ChannelAccess(
        server_id=None, creator_can_view=False
    )


def test_access_uses_cached_channel():
    guild = FakeGuild(member=object())
    client = CachingClient(channels={CHANNEL: FakeChannel(guild=guild)})

    access = run(DiscordReminderSender(client).channel_access(CHANNEL, USER))

    assert access == ChannelAccess(server_id=GUILD, creator_can_view=True)
    client.fetch_channel.assert_not_awaited()
    guild.fetch_member.assert_awaited_once_with(USER)
    guild.get_member.assert_not_called()


def test_access_unexpected_error_propagates():
    guild = FakeGuild(fetch_error=RuntimeError("boom"))

    with pytest.raises(RuntimeError):
        access_for(FakeChannel(guild=guild))
