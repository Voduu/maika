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
from maika.commands import CommandCategory, CommandRegistry, Visibility
from maika.config import Config, Environment
from maika.discord_adapter import REPLY_PART_LIMIT, handle_command
from maika.perm_commands import perm_show
from maika.perm_lookup_commands import perm_history, perm_keys
from maika.permission_store import (
    remove_block,
    set_block,
    set_level,
    set_override,
)
from maika.permissions import Level, PermissionKey, PermissionRegistry
from maika.results import FailureKind
from maika.slash_commands import (
    owner_area_autocomplete,
    permission_area_choices,
    permission_areas,
    permission_pattern_choices,
)

NOW = 1_800_000_000
OWNER = 1
ADMIN = 2
ALEX = 100
SAM = 200
GUILD = 5000
OTHER_GUILD = 6000
CHANNEL = 7000

DENIED_TEXT = "You don't have permission for that."

OVERRIDE_REGISTRY = PermissionRegistry(
    [
        PermissionKey("cat.view", Level.EVERYONE),
        PermissionKey("feed.remove", Level.TRUSTED),
    ]
)


def change_level(connection, level, now, user_id=ALEX):
    set_level(
        connection,
        user_id=user_id,
        level=level,
        owner_id=OWNER,
        actor_id=OWNER,
        now=now,
    )


def change_override(connection, pattern, effect, now, guild_id=0):
    set_override(
        connection,
        OVERRIDE_REGISTRY,
        user_id=ALEX,
        pattern=pattern,
        effect=effect,
        guild_id=guild_id,
        owner_id=OWNER,
        actor_id=ADMIN,
        now=now,
    )


def change_block(connection, kind, now, guild_id=0):
    set_block(
        connection,
        user_id=ALEX,
        kind=kind,
        guild_id=guild_id,
        owner_id=OWNER,
        actor_id=OWNER,
        now=now,
    )


def history(connection, count=20, guild_id=GUILD, target_id=ALEX):
    return perm_history(
        connection, target_id=target_id, count=count, guild_id=guild_id
    )


# /perm history


def test_history_with_no_entries(migrated_db):
    result = history(migrated_db)

    assert result.success is True
    assert result.message == f"No changes recorded for <@{ALEX}>."


def test_history_level_entry(migrated_db):
    change_level(migrated_db, Level.TRUSTED, NOW)

    result = history(migrated_db)

    assert result.message.splitlines() == [
        f"<@{ALEX}>, newest first (1 of 1):",
        f"<t:{NOW}:f> level: Everyone to Trusted by <@{OWNER}>",
    ]


def test_history_level_back_to_everyone(migrated_db):
    change_level(migrated_db, Level.MEMBER, NOW)
    change_level(migrated_db, Level.EVERYONE, NOW + 1)

    lines = history(migrated_db).message.splitlines()

    assert lines[1] == (
        f"<t:{NOW + 1}:f> level: Member to Everyone by <@{OWNER}>"
    )


def test_history_override_with_key_and_note(migrated_db):
    change_override(migrated_db, "feed.remove", "allow", NOW)
    change_override(migrated_db, "*", "deny", NOW + 1)

    lines = history(migrated_db).message.splitlines()

    assert lines == [
        f"<@{ALEX}>, newest first (3 of 3):",
        f"<t:{NOW + 1}:f> override `feed.remove` (everywhere): allow to "
        f"none by <@{ADMIN}> (cleared by /perm deny \\*)",
        f"<t:{NOW + 1}:f> override `*` (everywhere): none to deny by "
        f"<@{ADMIN}>",
        f"<t:{NOW}:f> override `feed.remove` (everywhere): none to allow "
        f"by <@{ADMIN}>",
    ]


def test_history_block_entries(migrated_db):
    change_block(migrated_db, "chat", NOW)
    change_block(migrated_db, "all", NOW + 1)
    remove_block(migrated_db, user_id=ALEX, actor_id=OWNER, now=NOW + 2)

    lines = history(migrated_db).message.splitlines()

    assert lines[1:] == [
        f"<t:{NOW + 2}:f> block (everywhere): all to none by <@{OWNER}>",
        f"<t:{NOW + 1}:f> block (everywhere): chat to all by <@{OWNER}>",
        f"<t:{NOW}:f> block (everywhere): none to chat by <@{OWNER}>",
    ]


def test_history_scope_text(migrated_db):
    change_block(migrated_db, "chat", NOW, guild_id=GUILD)
    change_override(migrated_db, "cat.view", "deny", NOW + 1, OTHER_GUILD)

    here = history(migrated_db, guild_id=GUILD).message.splitlines()
    in_dm = history(migrated_db, guild_id=0).message.splitlines()

    assert here[1:] == [
        f"<t:{NOW + 1}:f> override `cat.view` (server {OTHER_GUILD}): none "
        f"to deny by <@{ADMIN}>",
        f"<t:{NOW}:f> block (this server): none to chat by <@{OWNER}>",
    ]
    assert in_dm[2] == (
        f"<t:{NOW}:f> block (server {GUILD}): none to chat by <@{OWNER}>"
    )


def test_history_mixed_kinds_newest_first(migrated_db):
    change_level(migrated_db, Level.MEMBER, NOW)
    change_override(migrated_db, "cat.view", "deny", NOW + 10)
    change_block(migrated_db, "chat", NOW + 20)

    lines = history(migrated_db).message.splitlines()

    assert lines[1:] == [
        f"<t:{NOW + 20}:f> block (everywhere): none to chat by <@{OWNER}>",
        f"<t:{NOW + 10}:f> override `cat.view` (everywhere): none to deny "
        f"by <@{ADMIN}>",
        f"<t:{NOW}:f> level: Everyone to Member by <@{OWNER}>",
    ]


def test_history_count_limit_and_total(migrated_db):
    levels = [Level.MEMBER, Level.TRUSTED, Level.ADMIN, Level.MEMBER]
    for offset, level in enumerate(levels):
        change_level(migrated_db, level, NOW + offset)

    lines = history(migrated_db, count=2).message.splitlines()

    assert lines == [
        f"<@{ALEX}>, newest first (2 of 4):",
        f"<t:{NOW + 3}:f> level: Admin to Member by <@{OWNER}>",
        f"<t:{NOW + 2}:f> level: Trusted to Admin by <@{OWNER}>",
    ]


def test_history_only_the_target(migrated_db):
    change_level(migrated_db, Level.MEMBER, NOW, user_id=SAM)

    result = history(migrated_db)

    assert result.message == f"No changes recorded for <@{ALEX}>."


@pytest.mark.parametrize("count", [0, 101])
def test_history_count_out_of_range(migrated_db, count):
    with pytest.raises(ValueError, match="count must be 1 to 100"):
        history(migrated_db, count=count)


def test_history_writes_nothing(migrated_db):
    change_level(migrated_db, Level.MEMBER, NOW)
    changes_before = migrated_db.total_changes

    history(migrated_db)

    assert migrated_db.total_changes == changes_before


# /perm keys

REAL_REGISTRY = build_command_registry()


def test_keys_lists_every_key_sorted():
    lines = perm_keys(REAL_REGISTRY, area=None).message.splitlines()
    keys = REAL_REGISTRY.permission_registry().keys()

    assert lines[0] == f"{len(keys)} keys."
    assert [line.split("`")[1] for line in lines[1:]] == list(keys)
    assert list(keys) == sorted(keys)


def test_keys_level_text_cases():
    lines = perm_keys(REAL_REGISTRY, area=None).message.splitlines()

    assert (
        "`memory.add`: level 1 (Member). Save a memory about yourself or "
        "another person (/remember)"
    ) in lines
    assert (
        "`memory.view`: protected (everyone). View your own memories "
        "(/memories)"
    ) in lines
    assert (
        "`perm.level`: owner-only. Set a person's permission level"
    ) in lines


def test_keys_trusted_level_text():
    registry = CommandRegistry(
        [],
        extra_keys=[
            PermissionKey("cat.retag", Level.TRUSTED, description="Retag")
        ],
    )

    result = perm_keys(registry, area=None)

    assert result.message == "1 key.\n`cat.retag`: level 2 (Trusted). Retag"


def test_keys_area_filter():
    result = perm_keys(REAL_REGISTRY, area="memory")

    assert result.message.splitlines() == [
        "4 keys in `memory`.",
        "`memory.add`: level 1 (Member). Save a memory about yourself or "
        "another person (/remember)",
        "`memory.forget`: protected (everyone). Delete your own memories "
        "(/forget, /forget all)",
        "`memory.manage`: level 3 (Admin). View and delete other people's "
        "memories",
        "`memory.view`: protected (everyone). View your own memories "
        "(/memories)",
    ]


@pytest.mark.parametrize("typed", [" Memory ", "MEMORY"])
def test_keys_area_ignores_case_and_spaces(typed):
    result = perm_keys(REAL_REGISTRY, area=typed)
    assert result.message.startswith("4 keys in `memory`.")


@pytest.mark.parametrize("typed", ["", "   "])
def test_keys_empty_area_lists_everything(typed):
    result = perm_keys(REAL_REGISTRY, area=typed)
    assert result.message == perm_keys(REAL_REGISTRY, area=None).message


def test_keys_single_key_in_area():
    registry = CommandRegistry(
        [], extra_keys=[PermissionKey("cat.view", Level.EVERYONE)]
    )

    result = perm_keys(registry, area="cat")

    # No description: the line ends after the level text.
    assert result.message == "1 key in `cat`.\n`cat.view`: level 0 (Everyone)."


def test_keys_unknown_area():
    result = perm_keys(REAL_REGISTRY, area=" Nope ")

    assert result.failure_kind is FailureKind.NOT_FOUND
    assert result.message == "I don't know a permission area called `nope`."


def test_keys_unknown_area_with_backtick():
    result = perm_keys(REAL_REGISTRY, area="a`b")

    assert result.failure_kind is FailureKind.NOT_FOUND
    assert result.message == "I don't know a permission area called `ab`."


def test_keys_command_key_uses_command_description():
    lines = perm_keys(REAL_REGISTRY, area="perm").message.splitlines()

    assert (
        "`perm.history`: owner-only. List the permission changes made to "
        "one person"
    ) in lines


# The memory keys


def test_memory_keys_registered():
    registry = REAL_REGISTRY.permission_registry()

    assert registry.get("memory.view").protected is True
    assert registry.get("memory.forget").protected is True
    assert registry.get("memory.add").default_level is Level.MEMBER
    assert registry.get("memory.manage").default_level is Level.ADMIN
    for key in ("memory.add", "memory.manage"):
        assert registry.get(key).protected is False
        assert registry.get(key).owner_only is False


def test_memory_keys_in_perm_show(migrated_db):
    lines = perm_show(
        migrated_db,
        REAL_REGISTRY.permission_registry(),
        target_id=ALEX,
        owner_id=OWNER,
        guild_id=GUILD,
    ).message.splitlines()

    assert "`memory.view`: allowed (protected)" in lines
    assert "`memory.forget`: allowed (protected)" in lines
    assert (
        "`memory.add`: denied (level: level 0 (everyone), memory.add needs "
        "level 1)"
    ) in lines
    assert (
        "`memory.manage`: denied (level: level 0 (everyone), memory.manage "
        "needs level 3)"
    ) in lines


def test_pattern_autocomplete_offers_memory_keys():
    choices = permission_pattern_choices(
        REAL_REGISTRY.permission_registry(), "memory"
    )

    assert [c.value for c in choices] == [
        "memory.*",
        "memory.add",
        "memory.manage",
    ]


# Area autocomplete


def test_permission_areas():
    assert permission_areas(REAL_REGISTRY.permission_registry()) == (
        "config",
        "memory",
        "perm",
        "prefs",
    )


def test_area_choices_match_typed_text():
    choices = permission_area_choices(
        REAL_REGISTRY.permission_registry(), " ME "
    )
    assert [(c.name, c.value) for c in choices] == [("memory", "memory")]


def autocomplete_interaction(user_id):
    return SimpleNamespace(user=SimpleNamespace(id=user_id))


def test_area_autocomplete_answers_owner():
    autocomplete = owner_area_autocomplete(
        OWNER, REAL_REGISTRY.permission_registry()
    )

    choices = asyncio.run(autocomplete(autocomplete_interaction(OWNER), ""))

    assert [c.value for c in choices] == ["config", "memory", "perm", "prefs"]


def test_area_autocomplete_empty_for_non_owner():
    autocomplete = owner_area_autocomplete(
        OWNER, REAL_REGISTRY.permission_registry()
    )

    choices = asyncio.run(autocomplete(autocomplete_interaction(ALEX), ""))

    assert choices == []


# Registry entries


@pytest.mark.parametrize(
    ("name", "key"),
    [("perm history", "perm.history"), ("perm keys", "perm.keys")],
)
def test_registry_entries(name, key):
    entry = REAL_REGISTRY.get(name)

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


def target(user_id):
    return SimpleNamespace(id=user_id, bot=False)


def sent_parts(interaction):
    first = interaction.response.send_message.call_args
    rest = interaction.followup.send.call_args_list
    return [first.args[0]] + [call.args[0] for call in rest], [
        first.kwargs["ephemeral"]
    ] + [call.kwargs["ephemeral"] for call in rest]


def test_history_parameters(migrated_db):
    command = subcommand(make_client(migrated_db), "history")

    user, count = command.parameters
    assert user.type is discord.AppCommandOptionType.user
    assert user.required is True
    assert count.type is discord.AppCommandOptionType.integer
    assert count.required is False
    assert count.min_value == 1
    assert count.max_value == 100
    assert count.default == 20
    assert 1 <= len(command.description) < 100
    for parameter in command.parameters:
        assert 1 <= len(parameter.description) < 100


def test_keys_parameters(migrated_db):
    command = subcommand(make_client(migrated_db), "keys")

    (area,) = command.parameters
    assert area.type is discord.AppCommandOptionType.string
    assert area.required is False
    assert area.autocomplete is True
    assert 1 <= len(command.description) < 100
    assert 1 <= len(area.description) < 100


def test_history_callback_default_count(migrated_db):
    for offset in range(25):
        level = Level.MEMBER if offset % 2 == 0 else Level.EVERYONE
        change_level(migrated_db, level, NOW + offset)
    client = make_client(migrated_db)
    interaction = fake_interaction(OWNER)

    asyncio.run(
        subcommand(client, "history").callback(interaction, user=target(ALEX))
    )

    parts, ephemeral = sent_parts(interaction)
    assert parts[0].startswith(f"<@{ALEX}>, newest first (20 of 25):")
    assert all(ephemeral)


def test_long_history_splits_into_several_messages(migrated_db):
    for offset in range(100):
        level = Level.MEMBER if offset % 2 == 0 else Level.EVERYONE
        change_level(migrated_db, level, NOW + offset)
    client = make_client(migrated_db)
    interaction = fake_interaction(OWNER)

    asyncio.run(
        subcommand(client, "history").callback(
            interaction, user=target(ALEX), count=100
        )
    )

    parts, ephemeral = sent_parts(interaction)
    assert len(parts) >= 2
    assert all(len(part) <= REPLY_PART_LIMIT for part in parts)
    assert all(ephemeral)
    expected = history(migrated_db, count=100).message
    assert "\n".join(parts) == expected
    assert len(expected.splitlines()) == 101


def test_keys_callback(migrated_db):
    client = make_client(migrated_db)
    interaction = fake_interaction(OWNER)

    asyncio.run(
        subcommand(client, "keys").callback(interaction, area="memory")
    )

    parts, ephemeral = sent_parts(interaction)
    assert parts == [perm_keys(REAL_REGISTRY, area="memory").message]
    assert ephemeral == [True]


@pytest.mark.parametrize("command_name", ["perm history", "perm keys"])
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
    interaction.response.send_message.assert_awaited_once()
    call = interaction.response.send_message.call_args
    assert (call.args[0], call.kwargs["ephemeral"]) == (DENIED_TEXT, True)


@pytest.mark.parametrize(
    ("name", "arguments"),
    [("history", {"user": target(SAM)}), ("keys", {})],
)
def test_non_owner_callback_refused(migrated_db, name, arguments):
    client = make_client(migrated_db)
    interaction = fake_interaction(ALEX)

    asyncio.run(subcommand(client, name).callback(interaction, **arguments))

    parts, ephemeral = sent_parts(interaction)
    assert parts == [DENIED_TEXT]
    assert ephemeral == [True]


def test_tree_has_history_and_keys(migrated_db):
    group = make_client(migrated_db).tree.get_command("perm")

    names = [command.name for command in group.commands]
    assert names[-2:] == ["history", "keys"]
    assert isinstance(group.get_command("history"), app_commands.Command)
