import asyncio
import logging
import os
import signal
import time
from pathlib import Path
from unittest import mock

import pytest
from discord import app_commands

from maika import __main__ as entry
from maika import log_router
from maika.bot import MaikaClient
from maika.command_catalog import build_command_registry
from maika.config import Config, Environment
from maika.shutdown import ShutdownHandler, run_client

TOKEN = "unused-token"
LOG_CHANNEL_ID = 1234
SHORT_DRAIN_SECONDS = 0.2
# Room for the rest of the shutdown on a slow test machine.
SHUTDOWN_MARGIN_SECONDS = 1.0


def send_signal(signum):
    os.kill(os.getpid(), signum)


def shutdown_lines(caplog):
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == "maika.shutdown"
    ]


class FakeClient:
    """Stands in for discord.Client: start waits until close runs."""

    def __init__(self, during_start):
        self.during_start = during_start
        self.close_calls = 0
        self.closed = asyncio.Event()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        pass

    async def start(self, token):
        await self.during_start()
        await self.closed.wait()

    async def close(self):
        self.close_calls += 1
        # Long enough for a second signal to arrive mid-shutdown.
        await asyncio.sleep(0.05)
        self.closed.set()


@pytest.mark.parametrize(
    ("signum", "name"),
    [(signal.SIGTERM, "SIGTERM"), (signal.SIGINT, "SIGINT")],
)
def test_signal_closes_client_once(signum, name, caplog):
    caplog.set_level(logging.INFO, logger="maika.shutdown")

    async def during_start():
        send_signal(signum)
        await asyncio.sleep(0.01)
        # A second signal while the shutdown runs.
        send_signal(signal.SIGTERM)
        send_signal(signal.SIGINT)

    client = FakeClient(during_start)

    asyncio.run(run_client(client, TOKEN))

    assert client.close_calls == 1
    assert shutdown_lines(caplog) == [f"Shutting down ({name})."]


def test_handlers_removed_after_run():
    async def during_start():
        send_signal(signal.SIGTERM)

    asyncio.run(run_client(FakeClient(during_start), TOKEN))

    assert signal.getsignal(signal.SIGTERM) == signal.SIG_DFL
    assert signal.getsignal(signal.SIGINT) is signal.default_int_handler


def test_start_error_passes_through_and_removes_handlers():
    class FailingClient(FakeClient):
        async def start(self, token):
            raise RuntimeError("login failed")

    with pytest.raises(RuntimeError):
        asyncio.run(run_client(FailingClient(None), TOKEN))

    assert signal.getsignal(signal.SIGTERM) == signal.SIG_DFL


def test_handler_ignores_second_request():
    calls = []

    async def close():
        calls.append("close")

    async def scenario():
        handler = ShutdownHandler(close)
        assert not handler.started
        handler.request(signal.SIGTERM)
        handler.request(signal.SIGINT)
        assert handler.started
        await handler.wait()

    asyncio.run(scenario())

    assert calls == ["close"]


# The real client


def make_config(database_path, log_channel_id=None):
    return Config(
        environment=Environment.TEST,
        discord_token=TOKEN,
        owner_id=1,
        listen_channels=(),
        log_channel_id=log_channel_id,
        database_path=database_path,
    )


@pytest.fixture
def no_sync(monkeypatch):
    monkeypatch.setattr(
        app_commands.CommandTree, "sync", mock.AsyncMock(return_value=[])
    )


async def wait_until_closed(client):
    while not client.is_closed():
        await asyncio.sleep(0.01)


def test_sigterm_through_main_exits_0_and_stops_tasks(
    tmp_path, monkeypatch, real_migrations_dir, no_sync, caplog
):
    caplog.set_level(logging.INFO, logger="maika.shutdown")
    database_path = tmp_path / "var" / "maika.db"
    monkeypatch.setattr(entry, "MIGRATIONS_DIR", real_migrations_dir)
    monkeypatch.setattr(
        entry, "load_config_from_env", lambda: make_config(database_path)
    )
    opened = []
    real_open = entry.open_database

    def recording_open(path):
        connection = real_open(path)
        opened.append(connection)
        return connection

    monkeypatch.setattr(entry, "open_database", recording_open)

    clients = []
    close_calls = []
    tasks = {}
    real_close = MaikaClient.close

    async def counting_close(self):
        close_calls.append(1)
        await real_close(self)

    async def fake_start(self, token):
        clients.append(self)
        await self.setup_hook()
        await self.on_ready()
        tasks["backup"] = self._backup_task
        tasks["offsite"] = self._offsite_task
        send_signal(signal.SIGTERM)
        send_signal(signal.SIGINT)
        await wait_until_closed(self)

    monkeypatch.setattr(MaikaClient, "close", counting_close)
    monkeypatch.setattr(MaikaClient, "start", fake_start)

    assert entry.main() == 0

    [client] = clients
    assert len(close_calls) == 1
    assert tasks["backup"].cancelled()
    assert tasks["offsite"].cancelled()
    assert client._backup_task is None
    assert client._offsite_task is None
    assert shutdown_lines(caplog) == ["Shutting down (SIGTERM)."]
    with pytest.raises(Exception, match="closed database"):
        opened[0].execute("SELECT 1")


def test_stuck_log_post_does_not_hold_shutdown(
    migrated_db, monkeypatch, no_sync
):
    monkeypatch.setattr(log_router, "LOG_DRAIN_SECONDS", SHORT_DRAIN_SECONDS)
    client = MaikaClient(
        make_config(Path("unused.db"), log_channel_id=LOG_CHANNEL_ID),
        connection=migrated_db,
        command_registry=build_command_registry(),
    )
    sends = []

    async def never_returns(channel_id, text):
        sends.append(text)
        await asyncio.Event().wait()

    # The router took the client's sender when it was built.
    client.log_router._send = never_returns
    timing = {}

    async def fake_start(token):
        await client.setup_hook()
        await client.on_ready()
        # Let the router start the stuck post.
        await asyncio.sleep(0.05)
        timing["signal"] = time.monotonic()
        send_signal(signal.SIGTERM)
        await wait_until_closed(client)

    client.start = fake_start

    asyncio.run(run_client(client, TOKEN))
    elapsed = time.monotonic() - timing["signal"]

    # Only the startup line was ever sent; it never finished.
    assert len(sends) == 1
    assert sends[0].startswith("Maika (test) is online.")
    # The drain waited its limit for the stuck post, then gave up.
    assert elapsed >= SHORT_DRAIN_SECONDS
    assert elapsed < SHORT_DRAIN_SECONDS + SHUTDOWN_MARGIN_SECONDS
    assert client.is_closed()


def test_shutdown_drain_limit_is_5_seconds():
    assert log_router.LOG_DRAIN_SECONDS == 5
