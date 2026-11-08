import asyncio
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest
from discord import app_commands

from maika import usage_log
from maika.bot import MaikaClient
from maika.command_catalog import build_command_registry
from maika.commands import CommandCategory, Visibility
from maika.config import Config, Environment
from maika.discord_adapter import handle_command
from maika.log_router import LogLevel, LogStream
from maika.perm_commands import level_name, perm_level, perm_show
from maika.permission_store import set_block, set_level, set_override
from maika.permissions import (
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
CHANNEL = 7000

DENIED_TEXT = "You don't have permission for that."

REGISTRY = PermissionRegistry(
    [
        PermissionKey("feed.subscribe", Level.MEMBER),
        PermissionKey("feed.remove", Level.TRUSTED),
        PermissionKey("cat.view", Level.EVERYONE),
        PermissionKey("chat.talk", Level.EVERYONE),
        PermissionKey("memory.view", Level.EVERYONE, protected=True),
        PermissionKey("config.get", Level.ADMIN, owner_only=True),
    ]
)


class Announcer:
    def __init__(self):
        self.lines = []

    def __call__(self, line):
        self.lines.append(line)


def users_rows(connection):
    rows = connection.execute(
        "SELECT discord_id, level, updated_at FROM users ORDER BY discord_id"
    ).fetchall()
    return [tuple(row) for row in rows]


def audit_rows(connection):
    rows = connection.execute(
        "SELECT logged_at, kind, actor_id, target_id, key, guild_id, "
        "old_value, new_value, note FROM audit_log ORDER BY id"
    ).fetchall()
    return [tuple(row) for row in rows]


def run_level(
    connection,
    level,
    target_id=ALEX,
    target_is_bot=False,
    announce=None,
    now=NOW,
):
    return perm_level(
        connection,
        target_id=target_id,
        target_is_bot=target_is_bot,
        level=level,
        owner_id=OWNER,
        actor_id=OWNER,
        now=now,
        announce=announce if announce is not None else Announcer(),
    )


# perm_level


@pytest.mark.parametrize(
    ("level", "number", "name"),
    [
        (Level.MEMBER, 1, "Member"),
        (Level.TRUSTED, 2, "Trusted"),
        (Level.ADMIN, 3, "Admin"),
    ],
)
def test_level_set_for_each_level(migrated_db, level, number, name):
    announce = Announcer()

    result = run_level(migrated_db, level, announce=announce)

    assert result.success is True
    assert result.message == f"<@{ALEX}> is now level {number} ({name})."
    assert users_rows(migrated_db) == [(ALEX, number, NOW)]
    assert audit_rows(migrated_db) == [
        (NOW, "level", OWNER, ALEX, None, 0, None, str(number), None)
    ]
    assert announce.lines == [
        f"Level changed: <@{ALEX}> Everyone to {name} by <@{OWNER}>."
    ]


def test_level_cleared_to_everyone(migrated_db):
    run_level(migrated_db, Level.TRUSTED)
    announce = Announcer()

    result = run_level(
        migrated_db, Level.EVERYONE, announce=announce, now=LATER
    )

    assert result.success is True
    assert result.message == f"<@{ALEX}> is now level 0 (Everyone)."
    assert users_rows(migrated_db) == []
    assert audit_rows(migrated_db)[-1] == (
        LATER,
        "level",
        OWNER,
        ALEX,
        None,
        0,
        "2",
        None,
        None,
    )
    assert announce.lines == [
        f"Level changed: <@{ALEX}> Trusted to Everyone by <@{OWNER}>."
    ]


def test_level_change_between_levels(migrated_db):
    run_level(migrated_db, Level.MEMBER)
    announce = Announcer()

    run_level(migrated_db, Level.ADMIN, announce=announce, now=LATER)

    assert users_rows(migrated_db) == [(ALEX, 3, LATER)]
    assert audit_rows(migrated_db)[-1][6:8] == ("1", "3")
    assert announce.lines == [
        f"Level changed: <@{ALEX}> Member to Admin by <@{OWNER}>."
    ]


@pytest.mark.parametrize("level", list(Level))
def test_level_unchanged(migrated_db, level):
    run_level(migrated_db, level)
    before = audit_rows(migrated_db)
    announce = Announcer()

    result = run_level(migrated_db, level, announce=announce, now=LATER)

    assert result.success is True
    assert result.message == (
        f"<@{ALEX}> is already level {level.value} ({level_name(level)})."
    )
    assert audit_rows(migrated_db) == before
    assert announce.lines == []


def test_level_bot_target_refused(migrated_db):
    announce = Announcer()

    result = run_level(
        migrated_db,
        Level.MEMBER,
        target_id=BOT_ID,
        target_is_bot=True,
        announce=announce,
    )

    assert result.failure_kind is FailureKind.INVALID
    assert result.message == "Bots can't be given a level."
    assert users_rows(migrated_db) == []
    assert audit_rows(migrated_db) == []
    assert announce.lines == []


def test_level_owner_target_refused(migrated_db):
    announce = Announcer()

    result = run_level(
        migrated_db, Level.ADMIN, target_id=OWNER, announce=announce
    )

    assert result.failure_kind is FailureKind.INVALID
    assert result.message == "The owner is above the levels."
    assert users_rows(migrated_db) == []
    assert audit_rows(migrated_db) == []
    assert announce.lines == []


def test_level_row_and_audit_written_together(migrated_db, monkeypatch):
    def broken_audit(*args, **kwargs):
        raise RuntimeError("audit write failed")

    monkeypatch.setattr(
        "maika.permission_store.write_audit_entry", broken_audit
    )

    with pytest.raises(RuntimeError, match="audit write failed"):
        run_level(migrated_db, Level.TRUSTED)

    assert users_rows(migrated_db) == []
    assert not migrated_db.in_transaction


def test_level_announce_failure_keeps_success(migrated_db, caplog):
    def broken(line):
        raise RuntimeError("router down")

    result = run_level(migrated_db, Level.MEMBER, announce=broken)

    assert result.success is True
    assert users_rows(migrated_db) == [(ALEX, 1, NOW)]
    assert "Announcing a level change failed." in caplog.messages


def test_level_names():
    assert [level_name(level) for level in Level] == [
        "Everyone",
        "Member",
        "Trusted",
        "Admin",
    ]


# perm_show


def show(connection, target_id, guild_id=GUILD):
    return perm_show(
        connection,
        REGISTRY,
        target_id=target_id,
        owner_id=OWNER,
        guild_id=guild_id,
    ).message.splitlines()


def expected_lines(connection, target_id, guild_id=GUILD):
    """The key lines built straight from check_permission."""
    lines = []
    for key in sorted(REGISTRY.keys()):
        decision = check_permission(
            connection,
            REGISTRY,
            owner_id=OWNER,
            user_id=target_id,
            key=key,
            guild_id=guild_id,
        )
        source = decision.source.value
        detail = discord.utils.escape_markdown(decision.detail)
        if not decision.allowed:
            lines.append(f"`{key}`: denied ({source}: {detail})")
        elif decision.detail == source:
            lines.append(f"`{key}`: allowed ({source})")
        else:
            lines.append(f"`{key}`: allowed ({source}: {detail})")
    return lines


def test_registry_keys_are_sorted():
    assert REGISTRY.keys() == (
        "cat.view",
        "chat.talk",
        "config.get",
        "feed.remove",
        "feed.subscribe",
        "memory.view",
    )


def test_show_owner(migrated_db):
    lines = show(migrated_db, OWNER)

    assert lines[0] == f"<@{OWNER}>: the owner, above the levels, this server."
    assert lines[1:] == [
        "`cat.view`: allowed (owner)",
        "`chat.talk`: allowed (owner)",
        "`config.get`: allowed (owner)",
        "`feed.remove`: allowed (owner)",
        "`feed.subscribe`: allowed (owner)",
        "`memory.view`: allowed (owner)",
    ]


def test_show_level_zero(migrated_db):
    lines = show(migrated_db, SAM)

    assert lines[0] == f"<@{SAM}>: level 0 (Everyone), this server."
    assert lines[1:] == [
        "`cat.view`: allowed (level: level 0 (everyone), cat.view needs "
        "level 0)",
        "`chat.talk`: allowed (level: level 0 (everyone), chat.talk needs "
        "level 0)",
        "`config.get`: denied (owner_only_key: config.get is owner-only)",
        "`feed.remove`: denied (level: level 0 (everyone), feed.remove "
        "needs level 2)",
        "`feed.subscribe`: denied (level: level 0 (everyone), "
        "feed.subscribe needs level 1)",
        "`memory.view`: allowed (protected: memory.view is protected)",
    ]
    assert lines[1:] == expected_lines(migrated_db, SAM)


def test_show_level_two(migrated_db):
    set_level(
        migrated_db,
        user_id=ALEX,
        level=Level.TRUSTED,
        owner_id=OWNER,
        actor_id=OWNER,
        now=NOW,
    )

    lines = show(migrated_db, ALEX)

    assert lines[0] == f"<@{ALEX}>: level 2 (Trusted), this server."
    assert (
        "`feed.remove`: allowed (level: level 2 (trusted), feed.remove "
        "needs level 2)"
    ) in lines
    assert (
        "`feed.subscribe`: allowed (level: level 2 (trusted), "
        "feed.subscribe needs level 1)"
    ) in lines
    assert lines[1:] == expected_lines(migrated_db, ALEX)


def test_show_override_and_block(migrated_db):
    set_level(
        migrated_db,
        user_id=ALEX,
        level=Level.TRUSTED,
        owner_id=OWNER,
        actor_id=OWNER,
        now=NOW,
    )
    set_override(
        migrated_db,
        REGISTRY,
        user_id=ALEX,
        pattern="feed.*",
        effect="deny",
        owner_id=OWNER,
        actor_id=OWNER,
        now=NOW,
    )
    set_override(
        migrated_db,
        REGISTRY,
        user_id=ALEX,
        pattern="cat.view",
        effect="allow",
        guild_id=GUILD,
        owner_id=OWNER,
        actor_id=OWNER,
        now=NOW,
    )
    set_block(
        migrated_db,
        user_id=ALEX,
        kind="chat",
        guild_id=GUILD,
        owner_id=OWNER,
        actor_id=OWNER,
        now=NOW,
    )

    lines = show(migrated_db, ALEX)

    assert lines[1:] == [
        f"`cat.view`: allowed (override: allow override on cat.view, "
        f"server {GUILD})",
        f"`chat.talk`: denied (block: chat block, server {GUILD})",
        "`config.get`: denied (owner_only_key: config.get is owner-only)",
        "`feed.remove`: denied (override: deny override on feed.\\*, "
        "server 0)",
        "`feed.subscribe`: denied (override: deny override on feed.\\*, "
        "server 0)",
        "`memory.view`: allowed (protected: memory.view is protected)",
    ]
    assert lines[1:] == expected_lines(migrated_db, ALEX)


def test_show_allowed_detail_is_escaped(migrated_db):
    set_override(
        migrated_db,
        REGISTRY,
        user_id=ALEX,
        pattern="feed.*",
        effect="allow",
        owner_id=OWNER,
        actor_id=OWNER,
        now=NOW,
    )

    lines = show(migrated_db, ALEX)

    assert (
        "`feed.remove`: allowed (override: allow override on feed.\\*, "
        "server 0)"
    ) in lines
    assert lines[1:] == expected_lines(migrated_db, ALEX)


def test_show_global_scope(migrated_db):
    set_block(
        migrated_db,
        user_id=ALEX,
        kind="chat",
        guild_id=GUILD,
        owner_id=OWNER,
        actor_id=OWNER,
        now=NOW,
    )

    server = show(migrated_db, ALEX, guild_id=GUILD)
    everywhere = show(migrated_db, ALEX, guild_id=0)

    assert server[0] == f"<@{ALEX}>: level 0 (Everyone), this server."
    assert everywhere[0] == f"<@{ALEX}>: level 0 (Everyone), everywhere."
    # The server block applies only with the server's ID.
    chat_allowed = (
        "`chat.talk`: allowed (level: level 0 (everyone), chat.talk needs "
        "level 0)"
    )
    assert chat_allowed in everywhere
    assert chat_allowed not in server
    assert everywhere[1:] == expected_lines(migrated_db, ALEX, guild_id=0)


# Registry entries


@pytest.mark.parametrize(
    ("name", "key"),
    [("perm level", "perm.level"), ("perm show", "perm.show")],
)
def test_registry_entries(name, key):
    entry = build_command_registry().get(name)

    assert entry.key.key == key
    assert entry.key.owner_only is True
    assert entry.key.protected is False
    assert entry.category is CommandCategory.OWNER
    assert entry.visibility is Visibility.EPHEMERAL
    assert f"/{name}" in entry.help_text
    assert entry.example.startswith(f"/{name}")


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


def subcommand(client, name):
    return client.tree.get_command("perm").get_command(name)


def reply(interaction):
    send = interaction.response.send_message
    send.assert_awaited_once()
    return send.call_args.args[0], send.call_args.kwargs["ephemeral"]


def target(user_id, bot=False):
    return SimpleNamespace(id=user_id, bot=bot)


def level_choice(level):
    return app_commands.Choice(name=level_name(level), value=level.value)


def test_perm_group_in_tree(migrated_db):
    client = make_client(migrated_db)

    group = client.tree.get_command("perm")

    assert isinstance(group, app_commands.Group)
    assert 1 <= len(group.description) < 100
    assert [command.name for command in group.commands] == ["level", "show"]
    level_command = subcommand(client, "level")
    user, level = level_command.parameters
    assert user.name == "user"
    assert user.type is discord.AppCommandOptionType.user
    assert user.required is True
    assert level.name == "level"
    assert level.type is discord.AppCommandOptionType.integer
    assert [(c.name, c.value) for c in level.choices] == [
        ("Everyone", 0),
        ("Member", 1),
        ("Trusted", 2),
        ("Admin", 3),
    ]
    (show_user,) = subcommand(client, "show").parameters
    assert show_user.type is discord.AppCommandOptionType.user
    for command in group.commands:
        assert 1 <= len(command.description) < 100
        for parameter in command.parameters:
            assert 1 <= len(parameter.description) < 100


def test_level_callback_writes_and_announces(migrated_db):
    client = make_client(migrated_db)
    interaction = fake_interaction(OWNER, now=LATER)

    asyncio.run(
        subcommand(client, "level").callback(
            interaction, user=target(ALEX), level=level_choice(Level.TRUSTED)
        )
    )

    assert reply(interaction) == (f"<@{ALEX}> is now level 2 (Trusted).", True)
    assert users_rows(migrated_db) == [(ALEX, 2, LATER)]
    assert audit_rows(migrated_db)[0][:4] == (LATER, "level", OWNER, ALEX)
    assert client.log_router.posts == [
        (
            LogStream.PERMISSIONS,
            LogLevel.INFO,
            f"Level changed: <@{ALEX}> Everyone to Trusted by <@{OWNER}>.",
        )
    ]


def test_level_callback_bot_target(migrated_db):
    client = make_client(migrated_db)
    interaction = fake_interaction(OWNER)

    asyncio.run(
        subcommand(client, "level").callback(
            interaction,
            user=target(BOT_ID, bot=True),
            level=level_choice(Level.MEMBER),
        )
    )

    assert reply(interaction) == ("Bots can't be given a level.", True)
    assert users_rows(migrated_db) == []


def test_show_callback_uses_interaction_guild(migrated_db):
    client = make_client(migrated_db)
    interaction = fake_interaction(OWNER, guild_id=None)

    asyncio.run(
        subcommand(client, "show").callback(interaction, user=target(SAM))
    )

    message, ephemeral = reply(interaction)
    assert ephemeral is True
    assert message.splitlines()[0] == (
        f"<@{SAM}>: level 0 (Everyone), everywhere."
    )
    keys = client.command_registry.permission_registry().keys()
    assert len(message.splitlines()) == 1 + len(keys)


@pytest.mark.parametrize("command_name", ["perm level", "perm show"])
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


def test_non_owner_level_callback_writes_nothing(migrated_db):
    client = make_client(migrated_db)
    interaction = fake_interaction(ALEX)

    asyncio.run(
        subcommand(client, "level").callback(
            interaction, user=target(SAM), level=level_choice(Level.ADMIN)
        )
    )

    assert reply(interaction) == (DENIED_TEXT, True)
    assert users_rows(migrated_db) == []
    assert audit_rows(migrated_db) == []
    assert all(
        stream is not LogStream.PERMISSIONS
        for stream, _, _ in client.log_router.posts
    )
