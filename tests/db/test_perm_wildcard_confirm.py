import asyncio
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from maika.bot import MaikaClient
from maika.command_catalog import build_command_registry
from maika.config import Config, Environment
from maika.discord_adapter import ConfirmView, split_message
from maika.log_router import LogLevel, LogStream
from maika.perm_commands import (
    CONFIRM_KEY_LIMIT,
    CONFIRM_ROW_LIMIT,
    SCOPE_OPTION_EVERYWHERE,
    SCOPE_OPTION_SERVER,
    perm_deny,
    perm_grant,
    perm_reset,
)
from maika.permissions import Level, PermissionKey, PermissionRegistry

NOW = 1_800_000_000
LATER = NOW + 60
OWNER = 1
ALEX = 100
GUILD = 5000
CHANNEL = 7000

EVERYWHERE = SCOPE_OPTION_EVERYWHERE
SERVER = SCOPE_OPTION_SERVER

DISCORD_MESSAGE_LIMIT = 2000

REGISTRY = PermissionRegistry(
    [
        PermissionKey("feed.subscribe", Level.MEMBER),
        PermissionKey("feed.remove", Level.TRUSTED),
        PermissionKey("cat.view", Level.EVERYONE),
        PermissionKey("chat.talk", Level.EVERYONE),
    ]
)

COMMANDS = {"grant": perm_grant, "deny": perm_deny, "reset": perm_reset}
EFFECTS = {"grant": "allow", "deny": "deny"}


class Announcer:
    def __init__(self):
        self.lines = []

    def __call__(self, line):
        self.lines.append(line)


def run(connection, command, key, *, scope=EVERYWHERE, announce=None, now=NOW):
    return COMMANDS[command](
        connection,
        REGISTRY,
        target_id=ALEX,
        target_is_bot=False,
        key=key,
        scope=scope,
        owner_id=OWNER,
        actor_id=OWNER,
        guild_id=GUILD,
        now=now,
        announce=announce if announce is not None else Announcer(),
    )


def insert_row(connection, key, effect, guild_id=0, user_id=ALEX):
    """Store an override row directly, with no audit entry."""
    with connection:
        connection.execute(
            "INSERT INTO permission_overrides "
            "(user_id, key, effect, guild_id, set_by, set_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, key, effect, guild_id, OWNER, NOW),
        )


def override_rows(connection):
    rows = connection.execute(
        "SELECT user_id, key, effect, guild_id, set_by, set_at "
        "FROM permission_overrides ORDER BY guild_id, key"
    ).fetchall()
    return [tuple(row) for row in rows]


def audit_rows(connection):
    rows = connection.execute(
        "SELECT logged_at, kind, actor_id, target_id, key, guild_id, "
        "old_value, new_value, note FROM audit_log ORDER BY id"
    ).fetchall()
    return [tuple(row) for row in rows]


def seed_feed_rows(connection):
    """Rows beneath both ``feed.*`` and ``*``."""
    insert_row(connection, "feed.remove", "deny")
    insert_row(connection, "feed.subscribe", "allow")


def prompt_question(command, pattern):
    if command == "reset":
        return f"Remove <@{ALEX}>'s `{pattern}` exception (everywhere)?"
    verb = {"grant": "Grant", "deny": "Deny"}[command]
    return f"{verb} <@{ALEX}> `{pattern}` (everywhere)?"


COMMAND_NAMES = ["grant", "deny", "reset"]
WILDCARDS = ["*", "feed.*"]


# No covered rows: no confirmation


@pytest.mark.parametrize("command", ["grant", "deny"])
@pytest.mark.parametrize("pattern", WILDCARDS)
def test_set_without_covered_rows_runs_at_once(migrated_db, command, pattern):
    announce = Announcer()

    result = run(migrated_db, command, pattern, announce=announce)

    assert result.confirmation is None
    assert result.success is True
    assert override_rows(migrated_db) == [
        (ALEX, pattern, EFFECTS[command], 0, OWNER, NOW)
    ]
    assert len(announce.lines) == 1


@pytest.mark.parametrize("pattern", WILDCARDS)
def test_reset_without_covered_rows_runs_at_once(migrated_db, pattern):
    insert_row(migrated_db, pattern, "deny")

    result = run(migrated_db, "reset", pattern)

    assert result.confirmation is None
    assert result.message == (
        f"<@{ALEX}>: `{pattern}` exception removed (everywhere)."
    )
    assert override_rows(migrated_db) == []


def test_exact_key_never_asks(migrated_db):
    insert_row(migrated_db, "feed.*", "deny")

    result = run(migrated_db, "grant", "feed.remove")

    assert result.confirmation is None
    assert len(override_rows(migrated_db)) == 2


def test_rows_in_another_scope_do_not_ask(migrated_db):
    insert_row(migrated_db, "feed.remove", "deny", guild_id=GUILD)

    result = run(migrated_db, "grant", "feed.*")

    assert result.confirmation is None


def test_rows_of_another_person_do_not_ask(migrated_db):
    insert_row(migrated_db, "feed.remove", "deny", user_id=200)

    result = run(migrated_db, "deny", "*")

    assert result.confirmation is None


# Covered rows: ask first


@pytest.mark.parametrize("command", COMMAND_NAMES)
@pytest.mark.parametrize("pattern", WILDCARDS)
def test_covered_rows_ask_and_write_nothing(migrated_db, command, pattern):
    seed_feed_rows(migrated_db)
    rows_before = override_rows(migrated_db)
    announce = Announcer()

    result = run(migrated_db, command, pattern, announce=announce)

    assert result.success is True
    assert result.failure_kind is None
    assert result.confirmation is not None
    assert result.message == result.confirmation.prompt
    assert result.message == (
        f"{prompt_question(command, pattern)}\n"
        "This also clears 2 exceptions beneath it:\n"
        "- `feed.remove` deny\n"
        "- `feed.subscribe` allow"
    )
    assert override_rows(migrated_db) == rows_before
    assert audit_rows(migrated_db) == []
    assert announce.lines == []


def test_prompt_for_one_row_says_exception(migrated_db):
    insert_row(migrated_db, "cat.view", "deny")

    result = run(migrated_db, "grant", "*")

    assert result.message == (
        f"Grant <@{ALEX}> `*` (everywhere)?\n"
        "This also clears 1 exception beneath it:\n"
        "- `cat.view` deny"
    )


def test_prompt_names_this_server(migrated_db):
    insert_row(migrated_db, "cat.view", "deny", guild_id=GUILD)

    result = run(migrated_db, "deny", "*", scope=SERVER)

    assert result.message.startswith(f"Deny <@{ALEX}> `*` (this server)?\n")


def test_star_prompt_lists_area_wildcards(migrated_db):
    insert_row(migrated_db, "cat.*", "deny")
    insert_row(migrated_db, "feed.remove", "allow")

    result = run(migrated_db, "reset", "*")

    assert result.message.splitlines()[2:] == [
        "- `cat.*` deny",
        "- `feed.remove` allow",
    ]


# Confirm


@pytest.mark.parametrize("command", ["grant", "deny"])
@pytest.mark.parametrize("pattern", WILDCARDS)
def test_confirm_set_matches_the_old_write(migrated_db, command, pattern):
    seed_feed_rows(migrated_db)
    announce = Announcer()
    effect = EFFECTS[command]
    word = {"allow": "allowed", "deny": "denied"}[effect]

    asked = run(migrated_db, command, pattern, announce=announce, now=LATER)
    result = asked.confirmation.on_confirm()

    assert result.confirmation is None
    assert result.message == (
        f"<@{ALEX}>: `{pattern}` is now {word} (everywhere).\nCleared 2 "
        "exceptions: `feed.remove` deny, `feed.subscribe` allow."
    )
    assert override_rows(migrated_db) == [
        (ALEX, pattern, effect, 0, OWNER, LATER)
    ]
    note = f"cleared by /perm {command} {pattern}"
    assert audit_rows(migrated_db) == [
        (LATER, "override", OWNER, ALEX, pattern, 0, None, effect, None),
        (LATER, "override", OWNER, ALEX, "feed.remove", 0, "deny", None, note),
        (
            LATER,
            "override",
            OWNER,
            ALEX,
            "feed.subscribe",
            0,
            "allow",
            None,
            note,
        ),
    ]
    assert announce.lines == [
        f"Override set: <@{ALEX}> `{pattern}` {effect} (everywhere) by "
        f"<@{OWNER}>. Cleared 2 exceptions: `feed.remove` deny, "
        "`feed.subscribe` allow."
    ]


@pytest.mark.parametrize("pattern", WILDCARDS)
def test_confirm_reset_matches_the_old_write(migrated_db, pattern):
    insert_row(migrated_db, pattern, "deny")
    seed_feed_rows(migrated_db)
    announce = Announcer()

    asked = run(migrated_db, "reset", pattern, announce=announce, now=LATER)
    result = asked.confirmation.on_confirm()

    assert result.message == (
        f"<@{ALEX}>: `{pattern}` exception removed (everywhere).\nCleared 2 "
        "exceptions: `feed.remove` deny, `feed.subscribe` allow."
    )
    assert override_rows(migrated_db) == []
    note = f"cleared by /perm reset {pattern}"
    assert [row[4:] for row in audit_rows(migrated_db)] == [
        (pattern, 0, "deny", None, None),
        ("feed.remove", 0, "deny", None, note),
        ("feed.subscribe", 0, "allow", None, note),
    ]
    assert announce.lines == [
        f"Override removed: <@{ALEX}> `{pattern}` (everywhere) by "
        f"<@{OWNER}>. Cleared 2 exceptions: `feed.remove` deny, "
        "`feed.subscribe` allow."
    ]


def test_confirmed_and_unconfirmed_paths_match(migrated_db):
    # The same command with nothing to clear, then with rows to clear
    # and confirmed: the pattern row and its audit entry are the same.
    run(migrated_db, "deny", "feed.*")
    direct_rows = override_rows(migrated_db)
    direct_audit = audit_rows(migrated_db)
    run(migrated_db, "reset", "feed.*")
    seed_feed_rows(migrated_db)

    run(migrated_db, "deny", "feed.*").confirmation.on_confirm()

    assert override_rows(migrated_db) == direct_rows
    assert audit_rows(migrated_db)[2] == direct_audit[0]


def test_row_added_before_the_press_is_cleared(migrated_db):
    insert_row(migrated_db, "feed.remove", "deny")
    asked = run(migrated_db, "grant", "feed.*")
    insert_row(migrated_db, "feed.subscribe", "deny")

    result = asked.confirmation.on_confirm()

    assert result.message.endswith(
        "Cleared 2 exceptions: `feed.remove` deny, `feed.subscribe` deny."
    )
    assert override_rows(migrated_db) == [
        (ALEX, "feed.*", "allow", 0, OWNER, NOW)
    ]


def test_row_removed_before_the_press(migrated_db):
    insert_row(migrated_db, "feed.remove", "deny")
    asked = run(migrated_db, "grant", "feed.*")
    run(migrated_db, "reset", "feed.remove")

    result = asked.confirmation.on_confirm()

    assert (
        result.message == f"<@{ALEX}>: `feed.*` is now allowed (everywhere)."
    )


# Long lists


def seed_many_rows(connection, count, key_length=None):
    for number in range(count):
        key = f"many.key_{number:02d}"
        if key_length is not None:
            key = key + "x" * (key_length - len(key))
        insert_row(connection, key, "deny")


def test_more_than_ten_rows_shows_ten_and_the_rest(migrated_db):
    seed_many_rows(migrated_db, 15)

    lines = run(migrated_db, "grant", "*").message.splitlines()

    assert CONFIRM_ROW_LIMIT == 10
    assert lines[1] == "This also clears 15 exceptions beneath it:"
    assert lines[2:12] == [f"- `many.key_{n:02d}` deny" for n in range(10)]
    assert lines[12:] == ["- and 5 more"]


def test_exactly_ten_rows_has_no_more_line(migrated_db):
    seed_many_rows(migrated_db, 10)

    lines = run(migrated_db, "grant", "*").message.splitlines()

    assert len(lines) == 12
    assert "more" not in lines[-1]


def test_many_long_keys_stay_under_the_message_limit(migrated_db):
    seed_many_rows(migrated_db, 40, key_length=500)

    result = run(migrated_db, "deny", "*")

    assert len(result.message) < DISCORD_MESSAGE_LIMIT
    row_lines = result.message.splitlines()[2:12]
    for line in row_lines:
        key = line.split("`")[1]
        assert len(key) == CONFIRM_KEY_LIMIT
        assert key.endswith("…")
    assert result.message.endswith("- and 30 more")


def test_confirm_clears_every_row_not_only_the_listed_ones(migrated_db):
    seed_many_rows(migrated_db, 15)

    result = run(migrated_db, "grant", "*").confirmation.on_confirm()

    assert "Cleared 15 exceptions" in result.message
    assert override_rows(migrated_db) == [(ALEX, "*", "allow", 0, OWNER, NOW)]


# Through the slash command and the buttons


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
        created_at=datetime.fromtimestamp(now, tz=UTC),
        response=SimpleNamespace(
            is_done=Mock(return_value=False),
            send_message=AsyncMock(),
            edit_message=AsyncMock(),
        ),
        followup=SimpleNamespace(send=AsyncMock()),
        edit_original_response=AsyncMock(),
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


def permission_posts(client):
    return [
        text
        for stream, level, text in client.log_router.posts
        if stream is LogStream.PERMISSIONS and level is LogLevel.INFO
    ]


def test_slash_grant_asks_then_confirm_writes(migrated_db):
    # The real registry has memory.add and memory.manage.
    insert_row(migrated_db, "memory.add", "deny")
    client = make_client(migrated_db)
    interaction = fake_interaction(OWNER)
    press = fake_interaction(OWNER, now=LATER)
    command = client.tree.get_command("perm").get_command("grant")

    async def scenario():
        await command.callback(
            interaction, user=SimpleNamespace(id=ALEX, bot=False), key="*"
        )
        view = interaction.response.send_message.call_args.kwargs["view"]
        assert override_rows(migrated_db) == [
            (ALEX, "memory.add", "deny", 0, OWNER, NOW)
        ]
        assert permission_posts(client) == []
        await view.confirm(press)
        return view

    view = asyncio.run(scenario())

    send = interaction.response.send_message
    assert isinstance(view, ConfirmView)
    assert send.call_args.args == (
        f"Grant <@{ALEX}> `*` (everywhere)?\n"
        "This also clears 1 exception beneath it:\n"
        "- `memory.add` deny",
    )
    assert send.call_args.kwargs["ephemeral"] is True
    reply = (
        f"<@{ALEX}>: `*` is now allowed (everywhere).\nCleared 1 "
        "exception: `memory.add` deny."
    )
    assert press.response.edit_message.call_args.kwargs["content"] == reply
    assert override_rows(migrated_db) == [(ALEX, "*", "allow", 0, OWNER, NOW)]
    assert permission_posts(client) == [
        f"Override set: <@{ALEX}> `*` allow (everywhere) by <@{OWNER}>. "
        "Cleared 1 exception: `memory.add` deny."
    ]


def test_slash_reset_cancel_changes_nothing(migrated_db):
    insert_row(migrated_db, "*", "deny")
    insert_row(migrated_db, "memory.add", "allow")
    client = make_client(migrated_db)
    interaction = fake_interaction(OWNER)
    press = fake_interaction(OWNER, now=LATER)
    command = client.tree.get_command("perm").get_command("reset")
    rows_before = override_rows(migrated_db)

    async def scenario():
        await command.callback(
            interaction, user=SimpleNamespace(id=ALEX, bot=False), key="*"
        )
        view = interaction.response.send_message.call_args.kwargs["view"]
        await view.cancel(press)

    asyncio.run(scenario())

    assert press.response.edit_message.call_args.kwargs["content"] == (
        "Cancelled. Nothing changed."
    )
    assert override_rows(migrated_db) == rows_before
    assert audit_rows(migrated_db) == []
    assert permission_posts(client) == []


# Notes for rows beneath a wildcard in other scopes

OTHER_GUILD = 6000


def test_slash_grant_notes_server_row_beneath_it(migrated_db):
    # The example from the design: a server deny, then a wildcard
    # grant everywhere. The server row still decides memory.add there.
    insert_row(migrated_db, "memory.add", "deny", guild_id=GUILD)
    client = make_client(migrated_db)
    interaction = fake_interaction(OWNER)
    command = client.tree.get_command("perm").get_command("grant")

    asyncio.run(
        command.callback(
            interaction,
            user=SimpleNamespace(id=ALEX, bot=False),
            key="memory.*",
        )
    )

    send = interaction.response.send_message
    assert "view" not in send.call_args.kwargs
    assert send.call_args.args == (
        f"<@{ALEX}>: `memory.*` is now allowed (everywhere).\nNote: 1 "
        "exception beneath it is still in effect in another scope: "
        "`memory.add` deny (this server).",
    )
    assert (ALEX, "memory.add", "deny", GUILD, OWNER, NOW) in override_rows(
        migrated_db
    )
    # The announce line has no note, as for the same-key note.
    assert permission_posts(client) == [
        f"Override set: <@{ALEX}> `memory.*` allow (everywhere) by <@{OWNER}>."
    ]


@pytest.mark.parametrize("command", ["grant", "deny"])
def test_global_wildcard_notes_server_row(migrated_db, command):
    insert_row(migrated_db, "feed.remove", "deny", guild_id=GUILD)

    result = run(migrated_db, command, "feed.*")

    assert result.confirmation is None
    assert result.message.endswith(
        "\nNote: 1 exception beneath it is still in effect in another "
        "scope: `feed.remove` deny (this server)."
    )
    assert (ALEX, "feed.remove", "deny", GUILD, OWNER, NOW) in override_rows(
        migrated_db
    )


def test_server_wildcard_notes_global_row(migrated_db):
    insert_row(migrated_db, "cat.view", "allow")

    result = run(migrated_db, "deny", "*", scope=SERVER)

    assert result.confirmation is None
    assert result.message == (
        f"<@{ALEX}>: `*` is now denied (this server).\nNote: 1 exception "
        "beneath it is still in effect in another scope: `cat.view` allow "
        "(everywhere)."
    )
    assert (ALEX, "cat.view", "allow", 0, OWNER, NOW) in override_rows(
        migrated_db
    )


def test_server_reset_wildcard_notes_global_rows(migrated_db):
    insert_row(migrated_db, "*", "deny", guild_id=GUILD)
    insert_row(migrated_db, "feed.*", "allow")
    insert_row(migrated_db, "feed.remove", "deny")

    result = run(migrated_db, "reset", "*", scope=SERVER)

    assert result.message == (
        f"<@{ALEX}>: `*` exception removed (this server).\nNote: 2 "
        "exceptions beneath it are still in effect in another scope: "
        "`feed.*` allow (everywhere), `feed.remove` deny (everywhere)."
    )


def test_rows_in_several_other_scopes(migrated_db):
    insert_row(migrated_db, "feed.subscribe", "allow")
    insert_row(migrated_db, "feed.remove", "deny", guild_id=OTHER_GUILD)

    result = run(migrated_db, "deny", "feed.*", scope=SERVER)

    assert result.message.endswith(
        "\nNote: 2 exceptions beneath it are still in effect in other "
        "scopes: `feed.subscribe` allow (everywhere), `feed.remove` deny "
        f"(server {OTHER_GUILD})."
    )


def test_confirm_has_cleared_line_and_other_scope_note(migrated_db):
    insert_row(migrated_db, "feed.remove", "deny")
    insert_row(migrated_db, "feed.subscribe", "deny", guild_id=GUILD)

    asked = run(migrated_db, "grant", "feed.*")

    # The prompt lists only the rows in the command's own scope.
    assert asked.message == (
        f"Grant <@{ALEX}> `feed.*` (everywhere)?\n"
        "This also clears 1 exception beneath it:\n"
        "- `feed.remove` deny"
    )

    result = asked.confirmation.on_confirm()

    assert result.message == (
        f"<@{ALEX}>: `feed.*` is now allowed (everywhere).\nCleared 1 "
        "exception: `feed.remove` deny.\nNote: 1 exception beneath it is "
        "still in effect in another scope: `feed.subscribe` deny "
        "(this server)."
    )
    assert override_rows(migrated_db) == [
        (ALEX, "feed.*", "allow", 0, OWNER, NOW),
        (ALEX, "feed.subscribe", "deny", GUILD, OWNER, NOW),
    ]


def test_same_key_note_comes_before_covered_note(migrated_db):
    insert_row(migrated_db, "feed.*", "deny", guild_id=GUILD)
    insert_row(migrated_db, "feed.remove", "allow", guild_id=GUILD)

    result = run(migrated_db, "grant", "feed.*")

    assert result.message == (
        f"<@{ALEX}>: `feed.*` is now allowed (everywhere).\n"
        "Note: `feed.*` is still denied (this server).\nNote: 1 exception "
        "beneath it is still in effect in another scope: `feed.remove` "
        "allow (this server)."
    )


def test_unchanged_reply_has_covered_note(migrated_db):
    insert_row(migrated_db, "feed.*", "allow")
    insert_row(migrated_db, "feed.remove", "deny", guild_id=GUILD)

    result = run(migrated_db, "grant", "feed.*")

    assert result.message == (
        f"<@{ALEX}>: `feed.*` is already allowed (everywhere).\nNote: 1 "
        "exception beneath it is still in effect in another scope: "
        "`feed.remove` deny (this server)."
    )


def test_more_than_ten_other_scope_rows(migrated_db):
    for number in range(13):
        insert_row(migrated_db, f"many.key_{number:02d}", "deny", GUILD)

    result = run(migrated_db, "grant", "*")

    listed = ", ".join(
        f"`many.key_{number:02d}` deny (this server)" for number in range(10)
    )
    assert result.message == (
        f"<@{ALEX}>: `*` is now allowed (everywhere).\nNote: 13 exceptions "
        "beneath it are still in effect in another scope: "
        f"{listed}, and 3 more."
    )


def test_no_note_without_covered_rows_elsewhere(migrated_db):
    # Rows elsewhere that the wildcard does not cover: another area,
    # the same pattern (the same-key note covers it), another person.
    insert_row(migrated_db, "cat.view", "deny", guild_id=GUILD)
    insert_row(migrated_db, "feed.*", "deny", guild_id=OTHER_GUILD)
    insert_row(migrated_db, "feed.remove", "deny", GUILD, user_id=200)

    result = run(migrated_db, "grant", "feed.*")

    assert "beneath it" not in result.message


def test_exact_key_has_no_covered_note(migrated_db):
    insert_row(migrated_db, "feed.remove", "deny", guild_id=GUILD)

    result = run(migrated_db, "grant", "feed.subscribe")

    assert result.message == (
        f"<@{ALEX}>: `feed.subscribe` is now allowed (everywhere)."
    )


# Each extra part on its own line


def test_cleared_part_and_two_notes_each_get_a_line(migrated_db):
    insert_row(migrated_db, "feed.remove", "deny")
    insert_row(migrated_db, "feed.*", "deny", guild_id=GUILD)
    insert_row(migrated_db, "feed.subscribe", "allow", guild_id=GUILD)
    announce = Announcer()

    result = run(
        migrated_db, "grant", "feed.*", announce=announce
    ).confirmation.on_confirm()

    assert result.message.count("\n") == 3
    assert result.message.splitlines() == [
        f"<@{ALEX}>: `feed.*` is now allowed (everywhere).",
        "Cleared 1 exception: `feed.remove` deny.",
        "Note: `feed.*` is still denied (this server).",
        "Note: 1 exception beneath it is still in effect in another "
        "scope: `feed.subscribe` allow (this server).",
    ]
    (line,) = announce.lines
    assert "\n" not in line
    assert line == (
        f"Override set: <@{ALEX}> `feed.*` allow (everywhere) by "
        f"<@{OWNER}>. Cleared 1 exception: `feed.remove` deny."
    )


@pytest.mark.parametrize("command", COMMAND_NAMES)
def test_one_sentence_reply_has_no_line_break(migrated_db, command):
    if command == "reset":
        insert_row(migrated_db, "feed.*", "deny")
    announce = Announcer()

    result = run(migrated_db, command, "feed.*", announce=announce)

    assert "\n" not in result.message
    assert all("\n" not in line for line in announce.lines)


def test_long_reply_splits_between_parts(migrated_db):
    seed_many_rows(migrated_db, 15, key_length=500)
    for number in range(13):
        key = f"many.other_{number:02d}".ljust(500, "y")
        insert_row(migrated_db, key, "allow", guild_id=GUILD)

    result = run(migrated_db, "deny", "*").confirmation.on_confirm()

    # The reply fits one message, and every part is whole on its line.
    assert split_message(result.message) == [result.message]
    first, cleared, note = result.message.splitlines()
    assert first == f"<@{ALEX}>: `*` is now denied (everywhere)."
    assert cleared.startswith("Cleared 15 exceptions: ")
    assert note.startswith("Note: 13 exceptions beneath it ")


# The cleared list is capped


def test_three_cleared_rows_read_as_before(migrated_db):
    insert_row(migrated_db, "cat.view", "deny")
    seed_feed_rows(migrated_db)
    announce = Announcer()

    result = run(
        migrated_db, "deny", "*", announce=announce
    ).confirmation.on_confirm()

    cleared = (
        "Cleared 3 exceptions: `cat.view` deny, `feed.remove` deny, "
        "`feed.subscribe` allow."
    )
    assert result.message == (
        f"<@{ALEX}>: `*` is now denied (everywhere).\n{cleared}"
    )
    assert announce.lines == [
        f"Override set: <@{ALEX}> `*` deny (everywhere) by <@{OWNER}>. "
        f"{cleared}"
    ]


def test_fifteen_cleared_rows_show_ten_and_the_rest(migrated_db):
    seed_many_rows(migrated_db, 15)
    announce = Announcer()

    result = run(
        migrated_db, "grant", "*", announce=announce
    ).confirmation.on_confirm()

    listed = ", ".join(f"`many.key_{n:02d}` deny" for n in range(10))
    cleared = f"Cleared 15 exceptions: {listed}, and 5 more."
    assert result.message == (
        f"<@{ALEX}>: `*` is now allowed (everywhere).\n{cleared}"
    )
    assert announce.lines == [
        f"Override set: <@{ALEX}> `*` allow (everywhere) by <@{OWNER}>. "
        f"{cleared}"
    ]
    notes = [row[8] for row in audit_rows(migrated_db)]
    assert notes.count("cleared by /perm grant *") == 15
    assert override_rows(migrated_db) == [(ALEX, "*", "allow", 0, OWNER, NOW)]


def test_cleared_keys_are_cut(migrated_db):
    seed_many_rows(migrated_db, 2, key_length=500)

    result = run(migrated_db, "grant", "*").confirmation.on_confirm()

    keys = result.message.split("Cleared 2 exceptions: ")[1].split("`")[1::2]
    assert [len(key) for key in keys] == [CONFIRM_KEY_LIMIT] * 2
    assert all(key.endswith("…") for key in keys)


def test_long_cleared_and_other_scope_lists_fit(migrated_db):
    seed_many_rows(migrated_db, 40, key_length=500)
    for number in range(13):
        key = f"many.other_{number:02d}".ljust(500, "y")
        insert_row(migrated_db, key, "allow", guild_id=GUILD)
    announce = Announcer()

    result = run(
        migrated_db, "deny", "*", announce=announce
    ).confirmation.on_confirm()

    assert "Cleared 40 exceptions" in result.message
    assert result.message.count(", and 30 more.") == 1
    assert "Note: 13 exceptions beneath it" in result.message
    assert result.message.endswith(", and 3 more.")
    assert len(result.message) < DISCORD_MESSAGE_LIMIT
    (line,) = announce.lines
    assert len(line) < DISCORD_MESSAGE_LIMIT


# The longest reply: 19-digit IDs, a 20-character pattern, the removed
# message, both lists at their cap and six same-key notes.
BIG_USER = 9_223_372_036_854_775_807
BIG_GUILD = 9_223_372_036_854_775_000
LONG_AREA = "a" * 18
LONG_PATTERN = f"{LONG_AREA}.*"
SAME_KEY_SCOPES = 6


def test_worst_case_reply_fits(migrated_db):
    registry = PermissionRegistry(
        [PermissionKey(f"{LONG_AREA}.view", Level.EVERYONE)]
    )
    other_guilds = [BIG_GUILD - number for number in range(1, 6)]
    insert_row(migrated_db, LONG_PATTERN, "deny", BIG_GUILD, BIG_USER)
    for number in range(40):
        key = f"{LONG_AREA}.k_{number:02d}".ljust(500, "x")
        insert_row(migrated_db, key, "deny", BIG_GUILD, BIG_USER)
    for guild_id in [0, *other_guilds]:
        insert_row(migrated_db, LONG_PATTERN, "allow", guild_id, BIG_USER)
    for number in range(13):
        key = f"{LONG_AREA}.o_{number:02d}".ljust(500, "y")
        guild_id = other_guilds[number % len(other_guilds)]
        insert_row(migrated_db, key, "allow", guild_id, BIG_USER)
    announce = Announcer()

    asked = perm_reset(
        migrated_db,
        registry,
        target_id=BIG_USER,
        target_is_bot=False,
        key=LONG_PATTERN,
        scope=SERVER,
        owner_id=OWNER,
        actor_id=BIG_USER - 1,
        guild_id=BIG_GUILD,
        now=NOW,
        announce=announce,
    )
    result = asked.confirmation.on_confirm()

    assert len(LONG_PATTERN) == 20
    assert result.message.count("Note: `") == SAME_KEY_SCOPES
    assert "Cleared 40 exceptions" in result.message
    assert "Note: 13 exceptions beneath it" in result.message
    assert len(asked.message) < DISCORD_MESSAGE_LIMIT
    assert len(result.message) < DISCORD_MESSAGE_LIMIT
    (line,) = announce.lines
    assert len(line) < DISCORD_MESSAGE_LIMIT
