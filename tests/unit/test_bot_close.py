import asyncio
import logging
from pathlib import Path
from unittest import mock

import discord

from maika.bot import MaikaClient
from maika.command_catalog import build_command_registry
from maika.config import Config, Environment

LOG_CHANNEL_ID = 1234
SHUTDOWN_LINE = "Maika (test) is shutting down."


def make_client():
    config = Config(
        environment=Environment.TEST,
        discord_token="unused-token",
        owner_id=1,
        listen_channels=(),
        log_channel_id=LOG_CHANNEL_ID,
        database_path=Path("unused.db"),
    )
    client = MaikaClient(
        config,
        connection=mock.Mock(),
        command_registry=build_command_registry(),
    )
    channel = mock.AsyncMock(spec=discord.TextChannel)
    client.get_channel = mock.Mock(return_value=channel)
    return client, channel


def test_close_before_ready_posts_nothing(caplog):
    caplog.set_level(logging.DEBUG)
    client, channel = make_client()

    asyncio.run(client.close())

    client.get_channel.assert_not_called()
    channel.send.assert_not_called()
    assert "not found" not in caplog.text
    assert "log channel" not in caplog.text.lower()


def test_close_after_ready_posts_shutdown_line():
    client, channel = make_client()

    async def ready_then_close():
        await client.on_ready()
        channel.send.reset_mock()
        await client.close()

    asyncio.run(ready_then_close())

    client.get_channel.assert_called_with(LOG_CHANNEL_ID)
    channel.send.assert_awaited_once_with(SHUTDOWN_LINE)
