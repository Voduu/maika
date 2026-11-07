import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from maika.bot import MaikaClient
from maika.command_catalog import build_command_registry
from maika.config import Config, Environment
from maika.foundation_settings import build_foundation_registry
from maika.log_router import NO_CHANNEL_WARNING
from maika.settings_store import set_value

NOW = 1_800_000_000
OWNER = 1
LOG_CHANNEL_ID = 4242
TOKEN = "fake-token-for-tests"


class FakeChannel:
    def __init__(self, sent):
        self.sent = sent

    async def send(self, text, *, allowed_mentions):
        self.sent.append(text)


def make_client(connection, log_channel_id=LOG_CHANNEL_ID):
    config = Config(
        environment=Environment.TEST,
        discord_token=TOKEN,
        owner_id=OWNER,
        listen_channels=(),
        log_channel_id=log_channel_id,
        database_path=Path("unused.db"),
    )
    client = MaikaClient(
        config,
        connection=connection,
        command_registry=build_command_registry(),
    )
    client.tree.sync = AsyncMock(return_value=[])
    client.get_guild = Mock(
        side_effect=lambda guild_id: (
            SimpleNamespace(name="Maika Test") if guild_id == 5000 else None
        )
    )
    sent = []
    client.get_partial_messageable = Mock(return_value=FakeChannel(sent))
    return client, sent


def fake_interaction(user_id):
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id),
        guild_id=5000,
        channel_id=7000,
        created_at=datetime.fromtimestamp(NOW, tz=UTC),
        response=SimpleNamespace(
            is_done=Mock(return_value=False),
            send_message=AsyncMock(),
        ),
        followup=SimpleNamespace(send=AsyncMock()),
    )


def run_bot(client, during=None):
    """Run setup_hook and on_ready, then ``during``, then close."""

    async def scenario():
        await client.setup_hook()
        await client.on_ready()
        if during is not None:
            await during()
        await client.close()

    asyncio.run(scenario())


def test_startup_and_shutdown_lines_reach_bot_log(migrated_db):
    client, sent = make_client(migrated_db)

    run_bot(client)

    assert len(sent) == 2
    assert sent[0].startswith("Maika (test) is online. Version ")
    assert sent[1] == "Maika (test) is shutting down."
    client.get_partial_messageable.assert_called_with(LOG_CHANNEL_ID)


def test_warning_reaches_errors_with_token_hidden(migrated_db):
    client, sent = make_client(migrated_db)

    async def warn():
        logging.getLogger("maika.feeds").warning("Bad token %s", TOKEN)

    run_bot(client, during=warn)

    assert "maika.feeds: Bad token [redacted]" in sent
    assert not any(TOKEN in line for line in sent)


def test_command_usage_reaches_bot_log(migrated_db):
    client, sent = make_client(migrated_db)

    async def run_config_get():
        group = client.tree.get_command("config")
        await group.get_command("get").callback(
            fake_interaction(OWNER), key="backups.days_kept"
        )

    run_bot(client, during=run_config_get)

    usage = [line for line in sent if line.startswith("`/config get`")]
    assert usage == [
        f"`/config get` by <@{OWNER}> in <#7000> in Maika Test: "
        "allowed (owner)"
    ]


# The no-channel warning


def no_channel_warnings(caplog):
    return [
        record
        for record in caplog.records
        if record.getMessage() == NO_CHANNEL_WARNING
    ]


def test_warns_when_no_log_channel_is_set(migrated_db, caplog):
    client, sent = make_client(migrated_db, log_channel_id=None)

    with caplog.at_level(logging.WARNING, logger="maika"):
        run_bot(client)

    (record,) = no_channel_warnings(caplog)
    assert record.name == "maika.log_router"
    assert record.levelno == logging.WARNING
    assert sent == []


def test_no_warning_with_env_channel(migrated_db, caplog):
    client, _ = make_client(migrated_db)

    with caplog.at_level(logging.WARNING, logger="maika"):
        run_bot(client)

    assert no_channel_warnings(caplog) == []


def test_no_warning_with_bot_log_setting(migrated_db, caplog):
    set_value(
        migrated_db,
        build_foundation_registry(),
        "log.channel.bot_log",
        "9999",
        actor_id=OWNER,
        now=NOW,
    )
    client, sent = make_client(migrated_db, log_channel_id=None)

    with caplog.at_level(logging.WARNING, logger="maika"):
        run_bot(client)

    assert no_channel_warnings(caplog) == []
    client.get_partial_messageable.assert_called_with(9999)


def test_command_usage_unknown_server(migrated_db):
    client, sent = make_client(migrated_db)
    client.get_guild = Mock(return_value=None)

    async def run_config_get():
        group = client.tree.get_command("config")
        await group.get_command("get").callback(
            fake_interaction(OWNER), key="backups.days_kept"
        )

    run_bot(client, during=run_config_get)

    assert (
        f"`/config get` by <@{OWNER}> in <#7000> in server 5000: "
        "allowed (owner)"
    ) in sent
    client.get_guild.assert_called_with(5000)
