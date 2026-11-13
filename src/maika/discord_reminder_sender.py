"""The ReminderSender that delivers reminders through Discord.

See ``docs/design.md`` section 2.2 (Delivery) and
``maika.reminder_delivery``, which decides what to send and records
the outcome. This module only talks to Discord and turns each result
into a SendOutcome:

- SENT: Discord accepted the message.
- REFUSED, a permanent refusal: Forbidden (DMs closed, the bot
  blocked, a lost permission), NotFound (the user or channel is
  gone), any other 4xx error, or a channel that cannot hold messages.
- RETRY, a temporary problem: a Discord server error (status 500 or
  higher), a network error (aiohttp.ClientError, OSError) or a
  timeout.

Any other exception is raised; ``deliver_due_reminders`` logs it and
leaves the reminder unchanged. Nothing here logs or returns the
message text.

Whether the creator can still see a channel is read with a request
to Discord (``guild.fetch_member``), because the bot has no Members
intent and its member cache is empty.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable

import aiohttp
import discord

from maika.reminder_delivery import ChannelAccess, SendOutcome

logger = logging.getLogger(__name__)

# A status at or above this is Discord's own problem, so it may pass.
SERVER_ERROR_STATUS = 500

# Network problems and timeouts, which may pass. asyncio.TimeoutError
# is the built-in TimeoutError, itself an OSError; both are named so
# the list reads plainly.
TEMPORARY_ERRORS = (aiohttp.ClientError, asyncio.TimeoutError, OSError)

# What the caller sees for a channel the bot cannot reach.
UNREACHABLE_CHANNEL = ChannelAccess(server_id=None, creator_can_view=False)

type ChannelLike = (
    discord.abc.GuildChannel | discord.abc.PrivateChannel | discord.Thread
)


def _is_temporary(error: discord.HTTPException) -> bool:
    """Return True for a Discord server error (500 or higher)."""
    return error.status >= SERVER_ERROR_STATUS


def _http_outcome(error: discord.HTTPException) -> SendOutcome:
    """Return the outcome for an error response from Discord.

    Forbidden and NotFound are REFUSED; a server error (500 or
    higher) is RETRY; any other status is REFUSED.
    """
    if isinstance(error, (discord.Forbidden, discord.NotFound)):
        return SendOutcome.REFUSED
    if _is_temporary(error):
        return SendOutcome.RETRY
    # Only the status and Discord's error code are logged, never the
    # text that was being sent.
    logger.warning(
        "Discord refused a reminder message (HTTP %d, code %d).",
        error.status,
        error.code,
    )
    return SendOutcome.REFUSED


async def _attempt(
    action: Callable[[], Awaitable[SendOutcome]],
) -> SendOutcome:
    """Run one send and turn the known errors into a SendOutcome.

    ``action`` returns its own outcome (SENT, or REFUSED for a channel
    that cannot hold messages). Any other exception is raised.
    """
    try:
        return await action()
    except discord.HTTPException as error:
        # Forbidden, NotFound, server errors and other statuses.
        return _http_outcome(error)
    except TEMPORARY_ERRORS:
        # Network problems and timeouts.
        return SendOutcome.RETRY


def _only_mention(user_id: int) -> discord.AllowedMentions:
    """Return allowed mentions that ping ``user_id`` and nobody else."""
    return discord.AllowedMentions(
        everyone=False,
        users=[discord.Object(id=user_id)],
        roles=False,
        replied_user=False,
    )


class DiscordReminderSender:
    """Send reminders, notices and access checks through a client.

    Users and channels are read from the client's cache first and
    fetched from Discord when they are not cached.
    """

    def __init__(self, client: discord.Client) -> None:
        """Store the client used for every request."""
        self.client = client

    async def _user(self, user_id: int) -> discord.User:
        """Return the user, from the cache or from Discord."""
        user = self.client.get_user(user_id)
        if user is not None:
            return user
        return await self.client.fetch_user(user_id)

    async def _channel(self, channel_id: int) -> ChannelLike:
        """Return the channel, from the cache or from Discord."""
        channel = self.client.get_channel(channel_id)
        if channel is not None:
            return channel
        return await self.client.fetch_channel(channel_id)

    async def send_dm(self, user_id: int, text: str) -> SendOutcome:
        """Send a DM with every mention turned off."""

        async def send() -> SendOutcome:
            """Find the user and send the DM."""
            user = await self._user(user_id)
            await user.send(
                text, allowed_mentions=discord.AllowedMentions.none()
            )
            return SendOutcome.SENT

        return await _attempt(send)

    async def _post(
        self, channel_id: int, user_id: int, text: str
    ) -> SendOutcome:
        """Post in a channel, allowing a mention of ``user_id`` only.

        A channel that cannot hold messages (a category or a forum,
        for example) is REFUSED.
        """

        async def post() -> SendOutcome:
            """Find the channel and post the text."""
            channel = await self._channel(channel_id)
            if not isinstance(channel, discord.abc.Messageable):
                return SendOutcome.REFUSED
            await channel.send(text, allowed_mentions=_only_mention(user_id))
            return SendOutcome.SENT

        return await _attempt(post)

    async def post_to_channel(
        self, channel_id: int, user_id: int, text: str
    ) -> SendOutcome:
        """Post a channel reminder that mentions its creator only."""
        return await self._post(channel_id, user_id, text)

    async def post_notice(
        self, channel_id: int, user_id: int, text: str
    ) -> SendOutcome:
        """Post a failure notice that mentions the person only."""
        return await self._post(channel_id, user_id, text)

    async def channel_access(
        self, channel_id: int, user_id: int
    ) -> ChannelAccess | None:
        """Return the channel's server and whether the person sees it.

        A channel that is gone, hidden from the bot, or not in a
        server (a DM channel) is unreachable. A person who is not a
        member is ``creator_can_view`` False. A temporary problem
        returns None, so the reminder is tried at the next check.
        """
        try:
            channel = await self._channel(channel_id)
        except discord.HTTPException as error:
            if _is_temporary(error):
                return None
            # NotFound, Forbidden and other refusals.
            return UNREACHABLE_CHANNEL
        except TEMPORARY_ERRORS:
            return None

        guild = getattr(channel, "guild", None)
        if guild is None:
            # A DM or group channel is not a server channel.
            return UNREACHABLE_CHANNEL

        try:
            # A REST request: without the Members intent the member
            # cache is empty, so guild.get_member would miss.
            member = await guild.fetch_member(user_id)
        except discord.HTTPException as error:
            if _is_temporary(error):
                return None
            # NotFound (not a member), Forbidden and other refusals.
            return ChannelAccess(server_id=guild.id, creator_can_view=False)
        except TEMPORARY_ERRORS:
            return None

        # For a thread, discord.py reads the parent's permissions.
        can_view = channel.permissions_for(member).view_channel
        return ChannelAccess(server_id=guild.id, creator_can_view=can_view)
