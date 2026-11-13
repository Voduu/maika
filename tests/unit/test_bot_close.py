import asyncio
import logging
from pathlib import Path
from unittest import mock

import discord

from maika.bot import MaikaClient
from maika.command_catalog import build_command_registry
from maika.config import Config, Environment
from maika.log_router import LogLevel, LogStream, RouterLogHandler

LOG_CHANNEL_ID = 1234
SHUTDOWN_LINE = "Maika (test) is shutting down."


class FakeRouter:
    def __init__(self, events):
        self.events = events
        self.posts = []

    def post(self, stream, level, text):
        self.posts.append((stream, level, text))
        self.events.append(("post", text))

    def warn_if_no_channel(self):
        self.events.append("warn_if_no_channel")

    async def run(self):
        await asyncio.Event().wait()

    async def close(self):
        self.events.append("router.close")


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
    events = []
    client.log_router = FakeRouter(events)
    return client, events


def maika_handlers():
    return [
        handler
        for handler in logging.getLogger("maika").handlers
        if isinstance(handler, RouterLogHandler)
    ]


def test_close_before_ready_posts_nothing():
    client, events = make_client()

    asyncio.run(client.close())

    assert client.log_router.posts == []
    assert events == ["router.close"]


def test_startup_line_reaches_router():
    client, _ = make_client()

    asyncio.run(client.on_ready())

    [(stream, level, text)] = client.log_router.posts
    assert stream is LogStream.BOT_LOG
    assert level is LogLevel.INFO
    assert text.startswith("Maika (test) is online. Version ")


def test_startup_line_posted_once_after_reconnect():
    client, _ = make_client()

    async def ready_twice():
        await client.on_ready()
        await client.on_ready()

    asyncio.run(ready_twice())

    assert len(client.log_router.posts) == 1


def test_close_posts_shutdown_then_drains_then_closes_client():
    client, events = make_client()

    async def record_client_close(self):
        events.append("client.close")

    async def ready_then_close():
        await client.on_ready()
        events.clear()
        await client.close()

    with mock.patch.object(discord.Client, "close", record_client_close):
        asyncio.run(ready_then_close())

    assert events == [
        ("post", SHUTDOWN_LINE),
        "router.close",
        "client.close",
    ]
    assert client.log_router.posts[-1] == (
        LogStream.BOT_LOG,
        LogLevel.INFO,
        SHUTDOWN_LINE,
    )


def test_second_close_posts_shutdown_line_once():
    client, _ = make_client()

    async def ready_then_close_twice():
        await client.on_ready()
        await client.close()
        await client.close()

    asyncio.run(ready_then_close_twice())

    shutdown_posts = [
        post for post in client.log_router.posts if post[2] == SHUTDOWN_LINE
    ]
    assert len(shutdown_posts) == 1


def test_setup_hook_starts_router_and_close_detaches_handler():
    client, _ = make_client()
    client.tree.sync = mock.AsyncMock(return_value=[])

    async def setup_then_close():
        await client.setup_hook()
        task = client._log_router_task
        assert task is not None
        assert not task.done()
        assert len(maika_handlers()) == 1
        task.cancel()
        await client.close()

    asyncio.run(setup_then_close())

    assert maika_handlers() == []


def test_setup_hook_starts_backup_task_and_close_stops_it():
    client, _ = make_client()
    client.tree.sync = mock.AsyncMock(return_value=[])

    async def setup_then_close():
        await client.setup_hook()
        task = client._backup_task
        assert task is not None
        assert not task.done()
        client._log_router_task.cancel()
        await client.close()
        return task

    task = asyncio.run(setup_then_close())

    assert task.cancelled()
    assert client._backup_task is None


def test_setup_hook_starts_offsite_task_and_close_stops_it():
    client, _ = make_client()
    client.tree.sync = mock.AsyncMock(return_value=[])

    async def setup_then_close():
        await client.setup_hook()
        task = client._offsite_task
        assert task is not None
        assert not task.done()
        client._log_router_task.cancel()
        await client.close()
        return task

    task = asyncio.run(setup_then_close())

    assert task.cancelled()
    assert client._offsite_task is None


def test_setup_hook_starts_reminder_task_and_close_stops_it():
    client, _ = make_client()
    client.tree.sync = mock.AsyncMock(return_value=[])

    async def setup_then_close():
        await client.setup_hook()
        task = client._reminder_task
        assert task is not None
        assert not task.done()
        client._log_router_task.cancel()
        await client.close()
        return task

    task = asyncio.run(setup_then_close())

    assert task.cancelled()
    assert client._reminder_task is None


def test_close_works_when_reminder_task_never_started():
    client, events = make_client()

    asyncio.run(client.close())

    assert client._reminder_task is None
    assert events == ["router.close"]


def test_send_log_line_turns_mentions_off():
    client, _ = make_client()
    channel = mock.AsyncMock()
    client.get_partial_messageable = mock.Mock(return_value=channel)

    asyncio.run(client._send_log_line(LOG_CHANNEL_ID, "hello @everyone"))

    client.get_partial_messageable.assert_called_once_with(LOG_CHANNEL_ID)
    channel.send.assert_awaited_once()
    assert channel.send.call_args.args == ("hello @everyone",)
    mentions = channel.send.call_args.kwargs["allowed_mentions"]
    assert mentions.everyone is False
    assert mentions.users is False
    assert mentions.roles is False
    assert mentions.replied_user is False


def test_send_log_line_times_out(monkeypatch):
    monkeypatch.setattr("maika.bot.LOG_POST_TIMEOUT_SECONDS", 0.01)
    client, _ = make_client()

    async def never_returns(*args, **kwargs):
        await asyncio.Event().wait()

    channel = mock.Mock()
    channel.send = never_returns
    client.get_partial_messageable = mock.Mock(return_value=channel)

    async def send():
        try:
            await client._send_log_line(LOG_CHANNEL_ID, "slow")
        except TimeoutError:
            return True
        return False

    assert asyncio.run(send()) is True


def test_channel_check_runs_before_startup_line():
    client, events = make_client()

    asyncio.run(client.on_ready())

    assert events[0] == "warn_if_no_channel"
    assert events[1][0] == "post"
