import asyncio
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest
from discord import app_commands

from maika import usage_log
from maika.block_commands import block_user, unblock_user
from maika.bot import MaikaClient
from maika.command_catalog import build_command_registry
from maika.commands import CommandCategory, Visibility
from maika.config import Config, Environment
from maika.discord_adapter import handle_command
from maika.log_router import LogLevel, LogStream
from maika.perm_commands import (
    SCOPE_OPTION_EVERYWHERE,
    SCOPE_OPTION_SERVER,
    perm_show,
)
from maika.permissions import (
    DecisionSource,
    Level,
    PermissionKey,
    PermissionRegistry,
    check_permission,
)
from maika.results import FailureKind

NOW = 1_800_000_000
LATER = NOW + 60
OWNER = 1
ALEX = 100
SAM = 200
BOT_ID = 300
GUILD = 5000
OTHER_GUILD = 6000
CHANNEL = 7000

EVERYWHERE = SCOPE_OPTION_EVERYWHERE
SERVER = SCOPE_OPTION_SERVER

DENIED_TEXT = "You don't have permission for that."

REGISTRY = PermissionRegistry(
    [
        PermissionKey("cat.view", Level.EVERYONE),
        PermissionKey("chat.talk", Level.EVERYONE),
        PermissionKey("memory.forget", Level.EVERYONE, protected=True),
    ]
)


class Announcer:
    def __init__(self):
        self.lines = []

    def __call__(self, line):
        self.lines.append(line)


def block(
    connection,
    kind="chat",
    *,
    scope=EVERYWHERE,
    target_id=ALEX,
    target_is_bot=False,
    guild_id=GUILD,
    announce=None,
    now=NOW,
):
    return block_user(
        connection,
        target_id=target_id,
        target_is_bot=target_is_bot,
        kind=kind,
        scope=scope,
        owner_id=OWNER,
        actor_id=OWNER,
        guild_id=guild_id,
        now=now,
        announce=announce if announce is not None else Announcer(),
    )


def unblock(
    connection,
    *,
    scope=EVERYWHERE,
    target_id=ALEX,
    target_is_bot=False,
    guild_id=GUILD,
    announce=None,
    now=NOW,
):
    return unblock_user(
        connection,
        target_id=target_id,
        target_is_bot=target_is_bot,
        scope=scope,
        owner_id=OWNER,
        actor_id=OWNER,
        guild_id=guild_id,
        now=now,
        announce=announce if announce is not None else Announcer(),
    )


def block_rows(connection):
    rows = connection.execute(
        "SELECT user_id, guild_id, kind, set_by, set_at FROM blocks "
        "ORDER BY user_id, guild_id"
    ).fetchall()
    return [tuple(row) for row in rows]


def audit_rows(connection):
    rows = connection.execute(
        "SELECT logged_at, kind, actor_id, target_id, key, guild_id, "
        "old_value, new_value, note FROM audit_log ORDER BY id"
    ).fetchall()
    return [tuple(row) for row in rows]


def decision(connection, key, guild_id=GUILD):
    return check_permission(
        connection,
        REGISTRY,
        owner_id=OWNER,
        user_id=ALEX,
        key=key,
        guild_id=guild_id,
    )


# Block in both scopes


@pytest.mark.parametrize(
    ("kind", "word"), [("chat", "chat"), ("all", "chat and commands")]
)
def test_block_everywhere(migrated_db, kind, word):
    announce = Announcer()

    result = block(migrated_db, kind, announce=announce)

    assert result.success is True
    assert result.message == (
        f"<@{ALEX}> is now blocked from {word} (everywhere)."
    )
    assert block_rows(migrated_db) == [(ALEX, 0, kind, OWNER, NOW)]
    assert audit_rows(migrated_db) == [
        (NOW, "block", OWNER, ALEX, None, 0, None, kind, None)
    ]
    assert announce.lines == [
        f"Block set: <@{ALEX}> {kind} (everywhere) by <@{OWNER}>."
    ]


def test_block_this_server(migrated_db):
    announce = Announcer()

    result = block(migrated_db, "all", scope=SERVER, announce=announce)

    assert result.message == (
        f"<@{ALEX}> is now blocked from chat and commands (this server)."
    )
    assert block_rows(migrated_db) == [(ALEX, GUILD, "all", OWNER, NOW)]
    assert audit_rows(migrated_db)[0][5] == GUILD
    assert announce.lines == [
        f"Block set: <@{ALEX}> all (this server) by <@{OWNER}>."
    ]
    assert decision(migrated_db, "cat.view").allowed is False
    assert decision(migrated_db, "cat.view", guild_id=0).allowed is True


def test_block_everywhere_in_dm_works(migrated_db):
    result = block(migrated_db, guild_id=0)

    assert result.success is True
    assert block_rows(migrated_db) == [(ALEX, 0, "chat", OWNER, NOW)]


# Unblock in both scopes


def test_unblock_everywhere(migrated_db):
    block(migrated_db, "all")
    announce = Announcer()

    result = unblock(migrated_db, announce=announce, now=LATER)

    assert result.success is True
    assert result.message == f"<@{ALEX}> is no longer blocked (everywhere)."
    assert block_rows(migrated_db) == []
    assert audit_rows(migrated_db)[-1] == (
        LATER,
        "block",
        OWNER,
        ALEX,
        None,
        0,
        "all",
        None,
        None,
    )
    assert announce.lines == [
        f"Block removed: <@{ALEX}> (everywhere) by <@{OWNER}>."
    ]


def test_unblock_this_server(migrated_db):
    block(migrated_db, "chat", scope=SERVER)
    announce = Announcer()

    result = unblock(migrated_db, scope=SERVER, announce=announce)

    assert result.message == f"<@{ALEX}> is no longer blocked (this server)."
    assert block_rows(migrated_db) == []
    assert announce.lines == [
        f"Block removed: <@{ALEX}> (this server) by <@{OWNER}>."
    ]


# A kind change


def test_kind_change_overwrites_the_row(migrated_db):
    block(migrated_db, "chat")
    announce = Announcer()

    result = block(migrated_db, "all", announce=announce, now=LATER)

    assert result.message == (
        f"<@{ALEX}> is now blocked from chat and commands (everywhere)."
    )
    assert block_rows(migrated_db) == [(ALEX, 0, "all", OWNER, LATER)]
    assert audit_rows(migrated_db)[-1][6:8] == ("chat", "all")
    assert announce.lines == [
        f"Block set: <@{ALEX}> chat to all (everywhere) by <@{OWNER}>."
    ]


def test_kind_change_all_to_chat(migrated_db):
    block(migrated_db, "all", scope=SERVER)
    announce = Announcer()

    block(migrated_db, "chat", scope=SERVER, announce=announce)

    assert announce.lines == [
        f"Block set: <@{ALEX}> all to chat (this server) by <@{OWNER}>."
    ]


# Unchanged cases


@pytest.mark.parametrize(
    ("kind", "word"), [("chat", "chat"), ("all", "chat and commands")]
)
def test_block_unchanged(migrated_db, kind, word):
    block(migrated_db, kind)
    before = audit_rows(migrated_db)
    announce = Announcer()

    result = block(migrated_db, kind, announce=announce, now=LATER)

    assert result.success is True
    assert result.message == (
        f"<@{ALEX}> is already blocked from {word} (everywhere)."
    )
    assert block_rows(migrated_db) == [(ALEX, 0, kind, OWNER, NOW)]
    assert audit_rows(migrated_db) == before
    assert announce.lines == []


def test_unblock_with_no_row(migrated_db):
    announce = Announcer()

    result = unblock(migrated_db, scope=SERVER, announce=announce)

    assert result.success is True
    assert result.message == f"<@{ALEX}> has no block (this server)."
    assert audit_rows(migrated_db) == []
    assert announce.lines == []


# Refusals


@pytest.mark.parametrize("command", ["block", "unblock"])
def test_bot_target(migrated_db, command):
    function = block if command == "block" else unblock
    announce = Announcer()

    result = function(
        migrated_db, target_id=BOT_ID, target_is_bot=True, announce=announce
    )

    assert result.failure_kind is FailureKind.INVALID
    assert result.message == "Bots can't be blocked."
    assert block_rows(migrated_db) == []
    assert audit_rows(migrated_db) == []
    assert announce.lines == []


@pytest.mark.parametrize("command", ["block", "unblock"])
def test_owner_target(migrated_db, command):
    function = block if command == "block" else unblock

    result = function(migrated_db, target_id=OWNER)

    assert result.failure_kind is FailureKind.INVALID
    assert result.message == "The owner can't be blocked."
    assert block_rows(migrated_db) == []


@pytest.mark.parametrize("command", ["block", "unblock"])
def test_server_scope_in_dm_is_invalid(migrated_db, command):
    function = block if command == "block" else unblock
    announce = Announcer()

    result = function(migrated_db, scope=SERVER, guild_id=0, announce=announce)

    assert result.failure_kind is FailureKind.INVALID
    assert result.message == "Use this in a server, or choose Everywhere."
    assert block_rows(migrated_db) == []
    assert announce.lines == []


def test_unknown_scope_is_a_programming_error(migrated_db):
    with pytest.raises(ValueError, match="scope must be"):
        block(migrated_db, scope="nowhere")


def test_unknown_kind_is_a_programming_error(migrated_db):
    with pytest.raises(ValueError, match="Unknown block kind"):
        block(migrated_db, "most")


# Notes for blocks in other scopes


def test_block_notes_global_row(migrated_db):
    block(migrated_db, "all")

    result = block(migrated_db, "chat", scope=SERVER)

    assert result.message == (
        f"<@{ALEX}> is now blocked from chat (this server). "
        f"Note: <@{ALEX}> is also blocked from chat and commands "
        "(everywhere)."
    )


def test_block_notes_global_first_then_servers_by_id(migrated_db):
    block(migrated_db, "chat", scope=SERVER, guild_id=OTHER_GUILD)
    block(migrated_db, "all", scope=SERVER)

    result = block(migrated_db, "chat", guild_id=OTHER_GUILD + 1)

    assert result.message == (
        f"<@{ALEX}> is now blocked from chat (everywhere). "
        f"Note: <@{ALEX}> is also blocked from chat and commands "
        f"(server {GUILD}). "
        f"Note: <@{ALEX}> is also blocked from chat "
        f"(server {OTHER_GUILD})."
    )


def test_block_unchanged_has_notes(migrated_db):
    block(migrated_db, "chat")
    block(migrated_db, "all", scope=SERVER, guild_id=OTHER_GUILD)

    result = block(migrated_db, "chat")

    assert result.message == (
        f"<@{ALEX}> is already blocked from chat (everywhere). "
        f"Note: <@{ALEX}> is also blocked from chat and commands "
        f"(server {OTHER_GUILD})."
    )


def test_block_notes_this_server_by_name(migrated_db):
    block(migrated_db, "chat", scope=SERVER)

    result = block(migrated_db, "all")

    assert result.message.endswith(
        f"Note: <@{ALEX}> is also blocked from chat (this server)."
    )


def test_unblock_lists_remaining_blocks(migrated_db):
    block(migrated_db, "all")
    block(migrated_db, "chat", scope=SERVER)
    block(migrated_db, "all", scope=SERVER, guild_id=OTHER_GUILD)
    announce = Announcer()

    result = unblock(migrated_db, scope=SERVER, announce=announce)

    assert result.message == (
        f"<@{ALEX}> is no longer blocked (this server). "
        "Still blocked: chat and commands (everywhere). "
        f"Still blocked: chat and commands (server {OTHER_GUILD})."
    )
    # The announce line has no notes.
    assert announce.lines == [
        f"Block removed: <@{ALEX}> (this server) by <@{OWNER}>."
    ]


def test_unblock_everywhere_lists_server_rows(migrated_db):
    block(migrated_db, "chat")
    block(migrated_db, "chat", scope=SERVER)

    result = unblock(migrated_db)

    assert result.message == (
        f"<@{ALEX}> is no longer blocked (everywhere). "
        "Still blocked: chat (this server)."
    )


def test_unblock_in_dm_names_servers_by_id(migrated_db):
    block(migrated_db, "chat")
    block(migrated_db, "all", scope=SERVER)

    result = unblock(migrated_db, guild_id=0)

    assert result.message == (
        f"<@{ALEX}> is no longer blocked (everywhere). "
        f"Still blocked: chat and commands (server {GUILD})."
    )


def test_no_block_reply_lists_remaining(migrated_db):
    block(migrated_db, "chat", scope=SERVER, guild_id=OTHER_GUILD)

    result = unblock(migrated_db)

    assert result.message == (
        f"<@{ALEX}> has no block (everywhere). "
        f"Still blocked: chat (server {OTHER_GUILD})."
    )


def test_other_people_are_not_noted(migrated_db):
    block(migrated_db, "all", scope=SERVER, target_id=SAM)

    result = block(migrated_db, "chat")

    assert result.message == (
        f"<@{ALEX}> is now blocked from chat (everywhere)."
    )


# Announce failure


def test_announce_failure_keeps_success(migrated_db, caplog):
    def broken(line):
        raise RuntimeError("router down")

    blocked = block(migrated_db, "all", announce=broken)
    unblocked = unblock(migrated_db, announce=broken)

    assert blocked.success is True
    assert unblocked.success is True
    assert block_rows(migrated_db) == []
    assert len(audit_rows(migrated_db)) == 2
    assert caplog.messages.count("Announcing a block change failed.") == 2


# One transaction


@pytest.mark.parametrize("command", ["block", "unblock"])
def test_rows_and_audit_written_together(migrated_db, monkeypatch, command):
    block(migrated_db, "chat")
    rows_before = block_rows(migrated_db)
    audit_before = audit_rows(migrated_db)

    def broken_audit(*args, **kwargs):
        raise RuntimeError("audit write failed")

    monkeypatch.setattr(
        "maika.permission_store.write_audit_entry", broken_audit
    )

    with pytest.raises(RuntimeError, match="audit write failed"):
        if command == "block":
            block(migrated_db, "all", now=LATER)
        else:
            unblock(migrated_db, now=LATER)

    assert block_rows(migrated_db) == rows_before
    assert audit_rows(migrated_db) == audit_before
    assert not migrated_db.in_transaction


# check_permission and /perm show


def test_chat_block_refuses_chat_talk_only(migrated_db):
    block(migrated_db, "chat")

    talk = decision(migrated_db, "chat.talk")
    assert talk.allowed is False
    assert talk.source is DecisionSource.BLOCK
    assert decision(migrated_db, "cat.view").allowed is True


def test_stricter_kind_wins_global_all_server_chat(migrated_db):
    block(migrated_db, "all")
    block(migrated_db, "chat", scope=SERVER)

    result = decision(migrated_db, "cat.view")

    assert result.allowed is False
    assert result.detail == "all block, everywhere"


def test_stricter_kind_wins_global_chat_server_all(migrated_db):
    block(migrated_db, "chat")
    block(migrated_db, "all", scope=SERVER)

    result = decision(migrated_db, "cat.view")

    assert result.allowed is False
    assert result.detail == f"all block, server {GUILD}"
    # Outside that server only the global chat block applies.
    assert decision(migrated_db, "cat.view", guild_id=0).allowed is True


def test_protected_key_passes_an_all_block(migrated_db):
    block(migrated_db, "all")

    result = decision(migrated_db, "memory.forget")

    assert result.allowed is True
    assert result.source is DecisionSource.PROTECTED


def test_unblock_restores_access(migrated_db):
    block(migrated_db, "all")
    unblock(migrated_db)

    result = decision(migrated_db, "cat.view")
    assert result.allowed is True
    assert result.source is DecisionSource.LEVEL


def test_show_reflects_a_block(migrated_db):
    def show():
        return perm_show(
            migrated_db,
            REGISTRY,
            target_id=ALEX,
            owner_id=OWNER,
            guild_id=GUILD,
        ).message.splitlines()

    block(migrated_db, "chat")
    block(migrated_db, "all", scope=SERVER)

    lines = show()
    assert f"`cat.view`: denied (block: all block, server {GUILD})" in lines
    assert f"`chat.talk`: denied (block: all block, server {GUILD})" in lines
    assert (
        "`memory.forget`: allowed (protected: memory.forget is protected)"
        in lines
    )


# Registry entries


@pytest.mark.parametrize(
    ("name", "key"), [("block", "perm.block"), ("unblock", "perm.unblock")]
)
def test_registry_entries(name, key):
    entry = build_command_registry().get(name)

    assert entry.key.key == key
    assert entry.key.owner_only is True
    assert entry.key.protected is False
    assert entry.category is CommandCategory.OWNER
    assert entry.visibility is Visibility.EPHEMERAL
    assert f"/{name}" in entry.help_text
    assert "/forget" in entry.help_text
    assert entry.example.startswith(f"/{name}")


@pytest.mark.parametrize("key", ["perm.block", "perm.unblock"])
def test_non_owner_refused_at_owner_only_step(migrated_db, key):
    result = check_permission(
        migrated_db,
        build_command_registry().permission_registry(),
        owner_id=OWNER,
        user_id=ALEX,
        key=key,
        guild_id=GUILD,
    )

    assert result.allowed is False
    assert result.source is DecisionSource.OWNER_ONLY_KEY


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


def fake_interaction(user_id, guild_id=GUILD, now=NOW):
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id),
        guild_id=guild_id,
        channel_id=CHANNEL,
        created_at=datetime.fromtimestamp(now, tz=UTC),
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


def reply(interaction):
    send = interaction.response.send_message
    send.assert_awaited_once()
    return send.call_args.args[0], send.call_args.kwargs["ephemeral"]


def target(user_id, bot=False):
    return SimpleNamespace(id=user_id, bot=bot)


def scope_choice(value):
    names = {EVERYWHERE: "Everywhere", SERVER: "This server"}
    return app_commands.Choice(name=names[value], value=value)


def kind_choice(value):
    names = {"chat": "Chat only", "all": "Chat and commands"}
    return app_commands.Choice(name=names[value], value=value)


SCOPE_CHOICE_PAIRS = [("Everywhere", "everywhere"), ("This server", "server")]


def test_tree_has_block_commands(migrated_db):
    client = make_client(migrated_db)

    block_command = client.tree.get_command("block")
    unblock_command = client.tree.get_command("unblock")

    assert isinstance(block_command, app_commands.Command)
    assert isinstance(unblock_command, app_commands.Command)
    assert block_command.qualified_name == "block"
    assert unblock_command.qualified_name == "unblock"


def test_block_parameters(migrated_db):
    command = make_client(migrated_db).tree.get_command("block")

    user, kind, scope = command.parameters
    assert user.type is discord.AppCommandOptionType.user
    assert user.required is True
    assert kind.type is discord.AppCommandOptionType.string
    assert kind.required is True
    assert [(c.name, c.value) for c in kind.choices] == [
        ("Chat only", "chat"),
        ("Chat and commands", "all"),
    ]
    assert scope.required is False
    assert [(c.name, c.value) for c in scope.choices] == SCOPE_CHOICE_PAIRS
    assert 1 <= len(command.description) < 100
    for parameter in command.parameters:
        assert 1 <= len(parameter.description) < 100


def test_unblock_parameters(migrated_db):
    command = make_client(migrated_db).tree.get_command("unblock")

    user, scope = command.parameters
    assert user.type is discord.AppCommandOptionType.user
    assert user.required is True
    assert scope.type is discord.AppCommandOptionType.string
    assert scope.required is False
    assert [(c.name, c.value) for c in scope.choices] == SCOPE_CHOICE_PAIRS
    assert 1 <= len(command.description) < 100
    for parameter in command.parameters:
        assert 1 <= len(parameter.description) < 100


def test_block_callback_default_scope_is_everywhere(migrated_db):
    client = make_client(migrated_db)
    interaction = fake_interaction(OWNER, now=LATER)

    asyncio.run(
        client.tree.get_command("block").callback(
            interaction, user=target(ALEX), kind=kind_choice("all")
        )
    )

    assert reply(interaction) == (
        f"<@{ALEX}> is now blocked from chat and commands (everywhere).",
        True,
    )
    assert block_rows(migrated_db) == [(ALEX, 0, "all", OWNER, LATER)]
    assert client.log_router.posts == [
        (
            LogStream.PERMISSIONS,
            LogLevel.INFO,
            f"Block set: <@{ALEX}> all (everywhere) by <@{OWNER}>.",
        )
    ]


def test_block_callback_this_server(migrated_db):
    client = make_client(migrated_db)
    interaction = fake_interaction(OWNER)

    asyncio.run(
        client.tree.get_command("block").callback(
            interaction,
            user=target(ALEX),
            kind=kind_choice("chat"),
            scope=scope_choice(SERVER),
        )
    )

    assert reply(interaction) == (
        f"<@{ALEX}> is now blocked from chat (this server).",
        True,
    )
    assert block_rows(migrated_db) == [(ALEX, GUILD, "chat", OWNER, NOW)]


def test_block_callback_bot_target(migrated_db):
    client = make_client(migrated_db)
    interaction = fake_interaction(OWNER)

    asyncio.run(
        client.tree.get_command("block").callback(
            interaction,
            user=target(BOT_ID, bot=True),
            kind=kind_choice("chat"),
        )
    )

    assert reply(interaction) == ("Bots can't be blocked.", True)
    assert block_rows(migrated_db) == []


def test_unblock_callback(migrated_db):
    block(migrated_db, "chat", scope=SERVER)
    client = make_client(migrated_db)
    interaction = fake_interaction(OWNER)

    asyncio.run(
        client.tree.get_command("unblock").callback(
            interaction, user=target(ALEX), scope=scope_choice(SERVER)
        )
    )

    assert reply(interaction) == (
        f"<@{ALEX}> is no longer blocked (this server).",
        True,
    )
    assert block_rows(migrated_db) == []
    assert client.log_router.posts[-1] == (
        LogStream.PERMISSIONS,
        LogLevel.INFO,
        f"Block removed: <@{ALEX}> (this server) by <@{OWNER}>.",
    )


def test_unblock_callback_in_dm_with_server_scope(migrated_db):
    client = make_client(migrated_db)
    interaction = fake_interaction(OWNER, guild_id=None)

    asyncio.run(
        client.tree.get_command("unblock").callback(
            interaction, user=target(ALEX), scope=scope_choice(SERVER)
        )
    )

    assert reply(interaction) == (
        "Use this in a server, or choose Everywhere.",
        True,
    )


@pytest.mark.parametrize("command_name", ["block", "unblock"])
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


def test_non_owner_block_callback_writes_nothing(migrated_db):
    client = make_client(migrated_db)
    interaction = fake_interaction(ALEX)

    asyncio.run(
        client.tree.get_command("block").callback(
            interaction, user=target(SAM), kind=kind_choice("all")
        )
    )

    assert reply(interaction) == (DENIED_TEXT, True)
    assert block_rows(migrated_db) == []
    assert audit_rows(migrated_db) == []
    assert all(
        stream is not LogStream.PERMISSIONS
        for stream, _, _ in client.log_router.posts
    )


def test_non_owner_unblock_callback_leaves_block(migrated_db):
    block(migrated_db, "all", target_id=SAM)
    client = make_client(migrated_db)
    interaction = fake_interaction(ALEX)

    asyncio.run(
        client.tree.get_command("unblock").callback(
            interaction, user=target(SAM)
        )
    )

    assert reply(interaction) == (DENIED_TEXT, True)
    assert block_rows(migrated_db) == [(SAM, 0, "all", OWNER, NOW)]
