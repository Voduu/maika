import asyncio
import datetime
from datetime import UTC
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from maika import usage_log
from maika.bot import MaikaClient
from maika.command_catalog import build_command_registry
from maika.commands import CommandCategory, Visibility
from maika.config import Config, Environment
from maika.config_commands import (
    SETTINGS,
    config_reset,
    config_set,
)
from maika.discord_adapter import handle_command
from maika.foundation_settings import FOUNDATION_SETTINGS
from maika.log_router import LogLevel, LogRouter, LogStream
from maika.results import FailureKind
from maika.settings import (
    MAX_INT64,
    SettingDefinition,
    SettingLevel,
    SettingsRegistry,
    SettingValueError,
    UnknownSettingError,
    ValueType,
    describe_range,
)
from maika.settings_store import (
    SettingWriteResult,
    get_value,
    reset_setting_value,
    set_setting_value,
)

NOW = 1_800_000_000
LATER = NOW + 60
OWNER = 1
ALEX = 100
GUILD = 5000
CHANNEL = 7000

DAYS_KEY = "backups.days_kept"
DENIED_TEXT = "You don't have permission for that."


def definition(key, value_type, default, **extra):
    return SettingDefinition(
        key=key,
        category="test",
        level=SettingLevel.BASIC,
        server_scope=False,
        value_type=value_type,
        default=default,
        description="A setting used by the tests.",
        **extra,
    )


TYPED_SETTINGS = (
    definition("test.flag", ValueType.BOOL, False),
    definition("test.ratio", ValueType.FLOAT, 0.5, minimum=0, maximum=1),
    definition("test.ids", ValueType.INT_LIST, ()),
    definition("test.words", ValueType.STR_LIST, ()),
    definition("test.blob", ValueType.JSON, {}),
)


@pytest.fixture
def registry():
    return SettingsRegistry([*FOUNDATION_SETTINGS, *TYPED_SETTINGS])


def settings_rows(connection):
    rows = connection.execute(
        "SELECT key, guild_id, value, updated_at, updated_by FROM settings "
        "ORDER BY key, guild_id"
    ).fetchall()
    return [tuple(row) for row in rows]


def audit_rows(connection):
    rows = connection.execute(
        "SELECT logged_at, kind, actor_id, target_id, key, guild_id, "
        "old_value, new_value, note FROM audit_log ORDER BY id"
    ).fetchall()
    return [tuple(row) for row in rows]


def store_set(connection, registry, key, text, guild_id=0, now=NOW):
    return set_setting_value(
        connection,
        registry,
        key=key,
        text=text,
        guild_id=guild_id,
        actor_id=OWNER,
        now=now,
    )


def store_reset(connection, registry, key, guild_id=0, now=NOW):
    return reset_setting_value(
        connection,
        registry,
        key=key,
        guild_id=guild_id,
        actor_id=OWNER,
        now=now,
    )


# Store: set_setting_value


@pytest.mark.parametrize(
    ("key", "text", "value", "stored"),
    [
        ("test.flag", "on", True, "true"),
        ("backups.days_kept", " 45 ", 45, "45"),
        ("test.ratio", "0.25", 0.25, "0.25"),
        ("log.level.feeds", "WARNING", "warning", "warning"),
        ("backups.time", "05:30", datetime.time(5, 30), "05:30"),
        ("test.ids", "[1, 2]", (1, 2), "[1,2]"),
        ("test.words", '["a", "b"]', ("a", "b"), '["a","b"]'),
        ("test.blob", '{"b": 1, "a": 2}', {"a": 2, "b": 1}, '{"a":2,"b":1}'),
        ("log.channel.errors", "1234", 1234, "1234"),
    ],
)
def test_set_each_type(migrated_db, registry, key, text, value, stored):
    result = store_set(migrated_db, registry, key, text)

    assert result.changed is True
    assert result.new_value == value
    assert result.old_value == registry.get(key).default
    assert result.old_stored is False
    assert settings_rows(migrated_db) == [(key, 0, stored, NOW, OWNER)]
    assert get_value(migrated_db, registry, key) == value


@pytest.mark.parametrize(
    ("key", "text", "rule"),
    [
        (DAYS_KEY, "0", "must be a whole number from 1 to 365"),
        (DAYS_KEY, "366", "must be a whole number from 1 to 365"),
        (DAYS_KEY, "abc", "must be a whole number from 1 to 365"),
        (DAYS_KEY, "1.5", "must be a whole number from 1 to 365"),
        (
            "log.level.bot_log",
            "verbose",
            "must be one of: debug, info, warning, error",
        ),
        (
            "test.ratio",
            "2",
            "must be a decimal number such as 0.5 from 0 to 1",
        ),
        ("backups.time", "25:00", "must be a 24-hour time written HH:MM"),
    ],
)
def test_set_rejects_bad_value(migrated_db, registry, key, text, rule):
    with pytest.raises(SettingValueError) as caught:
        store_set(migrated_db, registry, key, text)

    assert caught.value.key == key
    assert caught.value.rule.startswith(rule)
    assert settings_rows(migrated_db) == []
    assert audit_rows(migrated_db) == []


def test_set_unknown_key(migrated_db, registry):
    with pytest.raises(UnknownSettingError):
        store_set(migrated_db, registry, "nope.x", "1")
    assert audit_rows(migrated_db) == []


def test_set_server_guild_on_global_setting(migrated_db, registry):
    with pytest.raises(SettingValueError, match="global setting"):
        store_set(migrated_db, registry, DAYS_KEY, "14", guild_id=GUILD)
    assert settings_rows(migrated_db) == []
    assert audit_rows(migrated_db) == []


def test_set_audit_row_fields(migrated_db, registry):
    store_set(migrated_db, registry, DAYS_KEY, "14")
    store_set(migrated_db, registry, DAYS_KEY, "20", now=LATER)

    assert audit_rows(migrated_db) == [
        (NOW, "setting", OWNER, None, DAYS_KEY, 0, None, "14", None),
        (LATER, "setting", OWNER, None, DAYS_KEY, 0, "14", "20", None),
    ]
    assert settings_rows(migrated_db) == [(DAYS_KEY, 0, "20", LATER, OWNER)]


def test_set_same_value_writes_nothing(migrated_db, registry):
    store_set(migrated_db, registry, DAYS_KEY, "14")

    result = store_set(migrated_db, registry, DAYS_KEY, " 14 ", now=LATER)

    assert result == SettingWriteResult(
        key=DAYS_KEY,
        guild_id=0,
        changed=False,
        old_value=14,
        new_value=14,
        is_default=False,
        old_stored=True,
    )
    assert settings_rows(migrated_db) == [(DAYS_KEY, 0, "14", NOW, OWNER)]
    assert len(audit_rows(migrated_db)) == 1


def test_set_equal_to_default_writes_row(migrated_db, registry):
    result = store_set(migrated_db, registry, DAYS_KEY, "30")

    assert result.changed is True
    assert result.is_default is True
    assert result.old_value == 30
    assert result.new_value == 30
    assert settings_rows(migrated_db) == [(DAYS_KEY, 0, "30", NOW, OWNER)]


# Store: reset_setting_value


def test_reset_with_row(migrated_db, registry):
    store_set(migrated_db, registry, DAYS_KEY, "14")

    result = store_reset(migrated_db, registry, DAYS_KEY, now=LATER)

    assert result == SettingWriteResult(
        key=DAYS_KEY,
        guild_id=0,
        changed=True,
        old_value=14,
        new_value=30,
        is_default=True,
        old_stored=True,
    )
    assert settings_rows(migrated_db) == []
    assert audit_rows(migrated_db)[-1] == (
        LATER,
        "setting",
        OWNER,
        None,
        DAYS_KEY,
        0,
        "14",
        None,
        None,
    )


def test_reset_without_row(migrated_db, registry):
    result = store_reset(migrated_db, registry, DAYS_KEY)

    assert result.changed is False
    assert result.old_value == 30
    assert result.new_value == 30
    assert result.is_default is True
    assert audit_rows(migrated_db) == []


def test_reset_unknown_key(migrated_db, registry):
    with pytest.raises(UnknownSettingError):
        store_reset(migrated_db, registry, "nope.x")


def test_reset_server_guild_on_global_setting(migrated_db, registry):
    store_set(migrated_db, registry, DAYS_KEY, "14")
    with pytest.raises(SettingValueError, match="global setting"):
        store_reset(migrated_db, registry, DAYS_KEY, guild_id=GUILD)
    assert len(settings_rows(migrated_db)) == 1


# Commands: config_set and config_reset


class Announcer:
    def __init__(self):
        self.lines = []

    def __call__(self, line):
        self.lines.append(line)


def run_set(connection, key, value, announce, now=NOW):
    return config_set(
        connection,
        SETTINGS,
        key,
        value,
        actor_id=OWNER,
        now=now,
        announce=announce,
    )


def run_reset(connection, key, announce, now=NOW):
    return config_reset(
        connection,
        SETTINGS,
        key,
        actor_id=OWNER,
        now=now,
        announce=announce,
    )


def test_set_reply_and_announce(migrated_db):
    announce = Announcer()

    result = run_set(migrated_db, DAYS_KEY, "14", announce)

    assert result.success is True
    assert result.message == "`backups.days_kept` is now `14` (was `30`)."
    assert announce.lines == [
        "Setting changed: `backups.days_kept` `30` to `14`."
    ]
    assert isinstance(result.data, SettingWriteResult)


def test_set_reply_uses_display_rules(migrated_db):
    announce = Announcer()

    result = run_set(migrated_db, "log.channel.errors", "1234", announce)

    assert result.message == "`log.channel.errors` is now `1234` (was `none`)."
    assert announce.lines == [
        "Setting changed: `log.channel.errors` `none` to `1234`."
    ]


def test_set_to_default_adds_note(migrated_db):
    announce = Announcer()
    run_set(migrated_db, DAYS_KEY, "14", announce)

    result = run_set(migrated_db, DAYS_KEY, "30", announce, now=LATER)

    assert result.message == (
        "`backups.days_kept` is now `30` (was `14`).\nThis is the default "
        "value. `/config reset backups.days_kept` removes the override, "
        "so later changes to the default apply."
    )
    assert announce.lines[-1] == (
        "Setting changed: `backups.days_kept` `14` to `30`."
    )


def test_set_same_value_replies_already(migrated_db):
    announce = Announcer()
    run_set(migrated_db, DAYS_KEY, "14", announce)
    announce.lines.clear()

    result = run_set(migrated_db, DAYS_KEY, "14", announce, now=LATER)

    assert result.success is True
    assert result.message == "`backups.days_kept` is already `14`."
    assert announce.lines == []
    assert len(audit_rows(migrated_db)) == 1


def test_set_unknown_key_reply(migrated_db):
    announce = Announcer()

    result = run_set(migrated_db, "nope.x", "1", announce)

    assert result.failure_kind is FailureKind.NOT_FOUND
    assert result.message == "I don't know a setting called `nope.x`."
    assert announce.lines == []


def test_set_rejected_value_reply(migrated_db):
    announce = Announcer()

    result = run_set(migrated_db, DAYS_KEY, "400", announce)

    assert result.failure_kind is FailureKind.INVALID
    assert result.message == (
        "`backups.days_kept` must be a whole number from 1 to 365."
    )
    assert announce.lines == []
    assert settings_rows(migrated_db) == []


def test_set_choices_rejected_reply(migrated_db):
    result = run_set(migrated_db, "log.level.errors", "loud", Announcer())

    assert result.failure_kind is FailureKind.INVALID
    assert result.message == (
        "`log.level.errors` must be one of: debug, info, warning, error."
    )


LOG_CHANNEL_KEYS = [
    f"log.channel.{stream}"
    for stream in (
        "bot_log",
        "errors",
        "permissions",
        "feeds",
        "decisions",
        "persona_log",
        "events",
        "memory_review",
    )
]


@pytest.mark.parametrize("value", ["0", "-5"])
@pytest.mark.parametrize("key", LOG_CHANNEL_KEYS)
def test_set_refuses_channel_below_one(migrated_db, key, value):
    announce = Announcer()

    result = run_set(migrated_db, key, value, announce)

    assert describe_range(SETTINGS.get(key)) == "at least 1"
    assert result.failure_kind is FailureKind.INVALID
    assert result.message == (
        f"`{key}` must be a whole number from 1 to {MAX_INT64}."
    )
    assert announce.lines == []
    assert settings_rows(migrated_db) == []
    assert audit_rows(migrated_db) == []


def test_set_accepts_channel_of_one(migrated_db):
    result = run_set(migrated_db, "log.channel.errors", "1", Announcer())

    assert result.success is True
    assert get_value(migrated_db, SETTINGS, "log.channel.errors") == 1


def test_set_failing_announce_keeps_success(migrated_db, caplog):
    def broken(line):
        raise RuntimeError("router down")

    result = run_set(migrated_db, DAYS_KEY, "14", broken)

    assert result.success is True
    assert get_value(migrated_db, SETTINGS, DAYS_KEY) == 14
    assert "Announcing a settings change failed." in caplog.messages


def test_reset_reply_and_announce(migrated_db):
    announce = Announcer()
    run_set(migrated_db, DAYS_KEY, "14", announce)
    announce.lines.clear()

    result = run_reset(migrated_db, DAYS_KEY, announce, now=LATER)

    assert result.success is True
    assert result.message == (
        "`backups.days_kept` is back to its default, `30` (was `14`)."
    )
    assert announce.lines == [
        "Setting reset: `backups.days_kept` `14` to its default `30`."
    ]


def test_reset_without_override_replies_already(migrated_db):
    announce = Announcer()

    result = run_reset(migrated_db, DAYS_KEY, announce)

    assert result.success is True
    assert result.message == "`backups.days_kept` already uses its default."
    assert announce.lines == []
    assert audit_rows(migrated_db) == []


def test_reset_unknown_key_reply(migrated_db):
    result = run_reset(migrated_db, "nope.x", Announcer())

    assert result.failure_kind is FailureKind.NOT_FOUND
    assert result.message == "I don't know a setting called `nope.x`."


# Registry entries


@pytest.mark.parametrize(
    ("name", "key"),
    [("config set", "config.set"), ("config reset", "config.reset")],
)
def test_registry_entries(name, key):
    definition = build_command_registry().get(name)

    assert definition.key.key == key
    assert definition.key.owner_only is True
    assert definition.key.protected is False
    assert definition.category is CommandCategory.OWNER
    assert definition.visibility is Visibility.EPHEMERAL
    assert f"/{name}" in definition.help_text
    assert definition.example.startswith(f"/{name}")


# Slash commands


class FakeRouter:
    def __init__(self):
        self.posts = []

    def post(self, stream, level, text):
        self.posts.append((stream, level, text))

    async def run(self):
        await asyncio.Event().wait()

    async def close(self):
        pass


def fake_interaction(user_id, now=NOW):
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id),
        guild_id=GUILD,
        channel_id=CHANNEL,
        created_at=datetime.datetime.fromtimestamp(now, tz=UTC),
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
    client = MaikaClient(
        config,
        connection=connection,
        command_registry=build_command_registry(),
    )
    client.log_router = FakeRouter()
    client.tree.sync = AsyncMock(return_value=[])
    asyncio.run(client.setup_hook())
    return client


def subcommand(client, name):
    return client.tree.get_command("config").get_command(name)


def reply(interaction):
    send = interaction.response.send_message
    send.assert_awaited_once()
    return send.call_args.args[0], send.call_args.kwargs["ephemeral"]


def test_set_parameters_and_descriptions(migrated_db):
    client = make_client(migrated_db)

    set_command = subcommand(client, "set")
    reset_command = subcommand(client, "reset")

    assert [p.name for p in set_command.parameters] == ["key", "value"]
    assert all(p.required for p in set_command.parameters)
    assert [p.name for p in reset_command.parameters] == ["key"]
    for command in (set_command, reset_command):
        assert 1 <= len(command.description) < 100
        for parameter in command.parameters:
            assert 1 <= len(parameter.description) < 100


def test_set_callback_writes_and_announces(migrated_db):
    client = make_client(migrated_db)
    interaction = fake_interaction(OWNER, now=LATER)

    asyncio.run(
        subcommand(client, "set").callback(
            interaction, key=DAYS_KEY, value="14"
        )
    )

    assert reply(interaction) == (
        "`backups.days_kept` is now `14` (was `30`).",
        True,
    )
    assert settings_rows(migrated_db) == [(DAYS_KEY, 0, "14", LATER, OWNER)]
    assert audit_rows(migrated_db)[0][:3] == (LATER, "setting", OWNER)
    assert client.log_router.posts == [
        (
            LogStream.BOT_LOG,
            LogLevel.INFO,
            "Setting changed: `backups.days_kept` `30` to `14`.",
        )
    ]


def test_reset_callback_writes_and_announces(migrated_db):
    client = make_client(migrated_db)
    run_set(migrated_db, DAYS_KEY, "14", Announcer())
    interaction = fake_interaction(OWNER, now=LATER)

    asyncio.run(
        subcommand(client, "reset").callback(interaction, key=DAYS_KEY)
    )

    assert reply(interaction) == (
        "`backups.days_kept` is back to its default, `30` (was `14`).",
        True,
    )
    assert settings_rows(migrated_db) == []
    assert audit_rows(migrated_db)[-1][:3] == (LATER, "setting", OWNER)
    assert client.log_router.posts[-1] == (
        LogStream.BOT_LOG,
        LogLevel.INFO,
        "Setting reset: `backups.days_kept` `14` to its default `30`.",
    )


@pytest.mark.parametrize("command_name", ["config set", "config reset"])
def test_non_owner_refused_through_handle_command(migrated_db, command_name):
    function = Mock()
    interaction = fake_interaction(ALEX)

    asyncio.run(
        handle_command(
            interaction,
            connection=migrated_db,
            registry=build_command_registry(),
            owner_id=OWNER,
            command_name=command_name,
            function=function,
            log_usage=usage_log.log_usage,
        )
    )

    function.assert_not_called()
    assert reply(interaction) == (DENIED_TEXT, True)


@pytest.mark.parametrize("name", ["set", "reset"])
def test_non_owner_callback_writes_nothing(migrated_db, name):
    client = make_client(migrated_db)
    interaction = fake_interaction(ALEX)
    arguments = {"key": DAYS_KEY}
    if name == "set":
        arguments["value"] = "14"

    asyncio.run(subcommand(client, name).callback(interaction, **arguments))

    assert reply(interaction) == (DENIED_TEXT, True)
    assert settings_rows(migrated_db) == []
    assert audit_rows(migrated_db) == []
    assert all(
        not text.startswith("Setting")
        for _, _, text in client.log_router.posts
    )


# The level setting changes what the router posts


def test_bot_log_level_set_through_command_changes_router(migrated_db):
    sent = []

    async def send(channel_id, text):
        sent.append(text)

    router = LogRouter(migrated_db, 4242, send)

    def announce(line):
        router.post(LogStream.BOT_LOG, LogLevel.INFO, line)

    async def scenario():
        task = asyncio.create_task(router.run())
        router.post(LogStream.BOT_LOG, LogLevel.DEBUG, "debug before")
        run_set(migrated_db, "log.level.bot_log", "debug", announce)
        router.post(LogStream.BOT_LOG, LogLevel.DEBUG, "debug after")
        run_set(migrated_db, "log.level.bot_log", "warning", announce)
        router.post(LogStream.BOT_LOG, LogLevel.INFO, "info hidden")
        router.post(LogStream.BOT_LOG, LogLevel.WARNING, "warning shown")
        await router.close()
        await task

    asyncio.run(scenario())

    assert sent == [
        "Setting changed: `log.level.bot_log` `info` to `debug`.",
        "debug after",
        "warning shown",
    ]
