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
from maika.perm_commands import (
    SCOPE_OPTION_EVERYWHERE,
    SCOPE_OPTION_SERVER,
    perm_deny,
    perm_grant,
    perm_reset,
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
from maika.slash_commands import (
    AUTOCOMPLETE_CHOICE_LIMIT,
    owner_permission_autocomplete,
    permission_pattern_choices,
    permission_patterns,
)

NOW = 1_800_000_000
LATER = NOW + 60
OWNER = 1
ALEX = 100
SAM = 200
BOT_ID = 300
GUILD = 5000
CHANNEL = 7000

EVERYWHERE = SCOPE_OPTION_EVERYWHERE
SERVER = SCOPE_OPTION_SERVER

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

COMMANDS = {"grant": perm_grant, "deny": perm_deny, "reset": perm_reset}


class Announcer:
    def __init__(self):
        self.lines = []

    def __call__(self, line):
        self.lines.append(line)


def run(
    connection,
    command,
    key,
    *,
    scope=EVERYWHERE,
    target_id=ALEX,
    target_is_bot=False,
    guild_id=GUILD,
    announce=None,
    now=NOW,
):
    return COMMANDS[command](
        connection,
        REGISTRY,
        target_id=target_id,
        target_is_bot=target_is_bot,
        key=key,
        scope=scope,
        owner_id=OWNER,
        actor_id=OWNER,
        guild_id=guild_id,
        now=now,
        announce=announce if announce is not None else Announcer(),
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


def decision(connection, key, user_id=ALEX, guild_id=GUILD):
    return check_permission(
        connection,
        REGISTRY,
        owner_id=OWNER,
        user_id=user_id,
        key=key,
        guild_id=guild_id,
    )


# Grant and deny with each pattern shape


@pytest.mark.parametrize(
    ("command", "effect", "word"),
    [("grant", "allow", "allowed"), ("deny", "deny", "denied")],
)
@pytest.mark.parametrize("key", ["feed.subscribe", "feed.*", "*"])
def test_set_each_pattern(migrated_db, command, effect, word, key):
    announce = Announcer()

    result = run(migrated_db, command, key, announce=announce)

    assert result.success is True
    assert result.message == f"<@{ALEX}>: `{key}` is now {word} (everywhere)."
    assert override_rows(migrated_db) == [(ALEX, key, effect, 0, OWNER, NOW)]
    assert audit_rows(migrated_db) == [
        (NOW, "override", OWNER, ALEX, key, 0, None, effect, None)
    ]
    assert announce.lines == [
        f"Override set: <@{ALEX}> `{key}` {effect} (everywhere) by <@{OWNER}>."
    ]


@pytest.mark.parametrize("key", ["feed.subscribe", "feed.*", "*"])
def test_reset_each_pattern(migrated_db, key):
    run(migrated_db, "grant", key)
    announce = Announcer()

    result = run(migrated_db, "reset", key, announce=announce, now=LATER)

    assert result.success is True
    assert result.message == (
        f"<@{ALEX}>: `{key}` exception removed (everywhere)."
    )
    assert override_rows(migrated_db) == []
    assert audit_rows(migrated_db)[-1] == (
        LATER,
        "override",
        OWNER,
        ALEX,
        key,
        0,
        "allow",
        None,
        None,
    )
    assert announce.lines == [
        f"Override removed: <@{ALEX}> `{key}` (everywhere) by <@{OWNER}>."
    ]


def test_grant_changes_the_decision(migrated_db):
    assert decision(migrated_db, "feed.remove").allowed is False

    run(migrated_db, "grant", "feed.remove")

    result = decision(migrated_db, "feed.remove")
    assert result.allowed is True
    assert result.source is DecisionSource.OVERRIDE


def test_deny_then_grant_replaces_the_effect(migrated_db):
    run(migrated_db, "deny", "cat.view")

    result = run(migrated_db, "grant", "cat.view", now=LATER)

    assert result.message == (
        f"<@{ALEX}>: `cat.view` is now allowed (everywhere)."
    )
    assert override_rows(migrated_db) == [
        (ALEX, "cat.view", "allow", 0, OWNER, LATER)
    ]
    assert audit_rows(migrated_db)[-1][6:8] == ("deny", "allow")


# Scopes


def test_server_scope_writes_server_row(migrated_db):
    announce = Announcer()

    result = run(
        migrated_db, "deny", "cat.view", scope=SERVER, announce=announce
    )

    assert result.message == (
        f"<@{ALEX}>: `cat.view` is now denied (this server)."
    )
    assert override_rows(migrated_db) == [
        (ALEX, "cat.view", "deny", GUILD, OWNER, NOW)
    ]
    assert audit_rows(migrated_db)[0][5] == GUILD
    assert announce.lines == [
        f"Override set: <@{ALEX}> `cat.view` deny (this server) by <@{OWNER}>."
    ]
    assert decision(migrated_db, "cat.view").allowed is False
    assert decision(migrated_db, "cat.view", guild_id=0).allowed is True


def test_server_reset_leaves_global_row(migrated_db):
    run(migrated_db, "grant", "cat.view")
    run(migrated_db, "deny", "cat.view", scope=SERVER)

    result = run(migrated_db, "reset", "cat.view", scope=SERVER)

    assert result.message == (
        f"<@{ALEX}>: `cat.view` exception removed (this server). "
        "Note: `cat.view` is still allowed (everywhere)."
    )
    assert override_rows(migrated_db) == [
        (ALEX, "cat.view", "allow", 0, OWNER, NOW)
    ]


@pytest.mark.parametrize("command", ["grant", "deny", "reset"])
def test_server_scope_in_dm_is_invalid(migrated_db, command):
    announce = Announcer()

    result = run(
        migrated_db,
        command,
        "cat.view",
        scope=SERVER,
        guild_id=0,
        announce=announce,
    )

    assert result.failure_kind is FailureKind.INVALID
    assert result.message == "Use this in a server, or choose Everywhere."
    assert override_rows(migrated_db) == []
    assert announce.lines == []


def test_everywhere_scope_in_dm_works(migrated_db):
    result = run(migrated_db, "grant", "cat.view", guild_id=0)

    assert result.success is True
    assert override_rows(migrated_db)[0][3] == 0


def test_unknown_scope_is_a_programming_error(migrated_db):
    with pytest.raises(ValueError, match="scope must be"):
        run(migrated_db, "grant", "cat.view", scope="nowhere")


# Cleared rows


def test_wildcard_clears_one_row(migrated_db):
    run(migrated_db, "deny", "feed.remove")
    announce = Announcer()

    result = run(migrated_db, "grant", "feed.*", announce=announce, now=LATER)

    assert result.message == (
        f"<@{ALEX}>: `feed.*` is now allowed (everywhere). Cleared 1 "
        "exception: `feed.remove` deny."
    )
    assert announce.lines == [
        f"Override set: <@{ALEX}> `feed.*` allow (everywhere) by "
        f"<@{OWNER}>. Cleared 1 exception: `feed.remove` deny."
    ]
    assert override_rows(migrated_db) == [
        (ALEX, "feed.*", "allow", 0, OWNER, LATER)
    ]


def test_wildcard_clears_several_rows(migrated_db):
    run(migrated_db, "grant", "feed.subscribe")
    run(migrated_db, "deny", "feed.remove")
    run(migrated_db, "deny", "cat.*")

    result = run(migrated_db, "deny", "*", now=LATER)

    assert result.message == (
        f"<@{ALEX}>: `*` is now denied (everywhere). Cleared 3 exceptions: "
        "`cat.*` deny, `feed.remove` deny, `feed.subscribe` allow."
    )
    assert override_rows(migrated_db) == [(ALEX, "*", "deny", 0, OWNER, LATER)]
    cleared_notes = [row[8] for row in audit_rows(migrated_db)[-3:]]
    assert cleared_notes == ["cleared by /perm deny *"] * 3


def test_wildcard_clears_only_its_scope(migrated_db):
    run(migrated_db, "deny", "feed.remove", scope=SERVER)

    result = run(migrated_db, "grant", "feed.*")

    assert "Cleared" not in result.message
    assert (ALEX, "feed.remove", "deny", GUILD, OWNER, NOW) in override_rows(
        migrated_db
    )


def test_same_effect_wildcard_still_clears(migrated_db):
    run(migrated_db, "grant", "feed.*")
    run(migrated_db, "deny", "feed.remove", now=LATER)
    # A row beneath the wildcard was added after it, so granting the
    # wildcard again clears that row.
    result = run(migrated_db, "grant", "feed.*", now=LATER)

    assert result.message == (
        f"<@{ALEX}>: `feed.*` is now allowed (everywhere). Cleared 1 "
        "exception: `feed.remove` deny."
    )


def test_reset_wildcard_lists_cleared_rows(migrated_db):
    run(migrated_db, "grant", "*")
    run(migrated_db, "deny", "feed.remove")
    run(migrated_db, "deny", "cat.view")
    announce = Announcer()

    result = run(migrated_db, "reset", "*", announce=announce, now=LATER)

    assert result.message == (
        f"<@{ALEX}>: `*` exception removed (everywhere). Cleared 2 "
        "exceptions: `cat.view` deny, `feed.remove` deny."
    )
    assert announce.lines == [
        f"Override removed: <@{ALEX}> `*` (everywhere) by <@{OWNER}>. "
        "Cleared 2 exceptions: `cat.view` deny, `feed.remove` deny."
    ]
    assert override_rows(migrated_db) == []


# Notes for rows in other scopes

OTHER_GUILD = 6000


def test_global_grant_notes_server_deny(migrated_db):
    run(migrated_db, "deny", "cat.view", scope=SERVER)
    announce = Announcer()

    result = run(migrated_db, "grant", "cat.view", announce=announce)

    assert result.message == (
        f"<@{ALEX}>: `cat.view` is now allowed (everywhere). "
        "Note: `cat.view` is still denied (this server)."
    )
    # The announce line is unchanged: no note.
    assert announce.lines == [
        f"Override set: <@{ALEX}> `cat.view` allow (everywhere) by <@{OWNER}>."
    ]


def test_server_reset_notes_global_row(migrated_db):
    run(migrated_db, "deny", "*")
    run(migrated_db, "grant", "*", scope=SERVER)
    announce = Announcer()

    result = run(migrated_db, "reset", "*", scope=SERVER, announce=announce)

    assert result.message == (
        f"<@{ALEX}>: `*` exception removed (this server). "
        "Note: `*` is still denied (everywhere)."
    )
    assert announce.lines == [
        f"Override removed: <@{ALEX}> `*` (this server) by <@{OWNER}>."
    ]


def test_global_reset_notes_server_row(migrated_db):
    run(migrated_db, "grant", "*")
    run(migrated_db, "deny", "*", scope=SERVER)
    announce = Announcer()

    result = run(migrated_db, "reset", "*", announce=announce)

    assert result.message == (
        f"<@{ALEX}>: `*` exception removed (everywhere). "
        "Note: `*` is still denied (this server)."
    )
    assert announce.lines == [
        f"Override removed: <@{ALEX}> `*` (everywhere) by <@{OWNER}>."
    ]
    assert override_rows(migrated_db) == [
        (ALEX, "*", "deny", GUILD, OWNER, NOW)
    ]


def test_row_in_another_server_is_named_by_id(migrated_db):
    run(migrated_db, "deny", "cat.view", scope=SERVER, guild_id=OTHER_GUILD)

    result = run(migrated_db, "grant", "cat.view")

    assert result.message == (
        f"<@{ALEX}>: `cat.view` is now allowed (everywhere). "
        f"Note: `cat.view` is still denied (server {OTHER_GUILD})."
    )


def test_in_a_dm_a_server_row_is_named_by_id(migrated_db):
    run(migrated_db, "deny", "cat.view", scope=SERVER)

    result = run(migrated_db, "grant", "cat.view", guild_id=0)

    assert result.message.endswith(
        f"Note: `cat.view` is still denied (server {GUILD})."
    )


def test_two_other_rows_global_first(migrated_db):
    run(migrated_db, "grant", "cat.view")
    run(migrated_db, "deny", "cat.view", scope=SERVER, guild_id=OTHER_GUILD)

    result = run(migrated_db, "deny", "cat.view", scope=SERVER)

    assert result.message == (
        f"<@{ALEX}>: `cat.view` is now denied (this server). "
        "Note: `cat.view` is still allowed (everywhere). "
        f"Note: `cat.view` is still denied (server {OTHER_GUILD})."
    )


def test_server_rows_ordered_by_id(migrated_db):
    run(migrated_db, "deny", "cat.view", scope=SERVER, guild_id=OTHER_GUILD)
    run(migrated_db, "grant", "cat.view", scope=SERVER)

    result = run(migrated_db, "deny", "cat.view")

    assert result.message == (
        f"<@{ALEX}>: `cat.view` is now denied (everywhere). "
        "Note: `cat.view` is still allowed (this server). "
        f"Note: `cat.view` is still denied (server {OTHER_GUILD})."
    )


def test_no_note_without_other_rows(migrated_db):
    result = run(migrated_db, "grant", "cat.view")
    assert "Note:" not in result.message


def test_note_follows_cleared_rows(migrated_db):
    run(migrated_db, "deny", "feed.remove")
    run(migrated_db, "deny", "feed.*", scope=SERVER)

    result = run(migrated_db, "grant", "feed.*")

    assert result.message == (
        f"<@{ALEX}>: `feed.*` is now allowed (everywhere). Cleared 1 "
        "exception: `feed.remove` deny. "
        "Note: `feed.*` is still denied (this server)."
    )


def test_already_reply_has_note(migrated_db):
    run(migrated_db, "grant", "cat.view")
    run(migrated_db, "deny", "cat.view", scope=SERVER)
    announce = Announcer()

    result = run(migrated_db, "grant", "cat.view", announce=announce)

    assert result.message == (
        f"<@{ALEX}>: `cat.view` is already allowed (everywhere). "
        "Note: `cat.view` is still denied (this server)."
    )
    assert announce.lines == []


def test_no_exception_reply_has_note(migrated_db):
    run(migrated_db, "deny", "cat.view", scope=SERVER)

    result = run(migrated_db, "reset", "cat.view")

    assert result.message == (
        f"<@{ALEX}>: `cat.view` has no exception (everywhere). "
        "Note: `cat.view` is still denied (this server)."
    )


def test_other_keys_and_people_are_not_noted(migrated_db):
    run(migrated_db, "deny", "feed.remove", scope=SERVER)
    run(migrated_db, "deny", "cat.*", scope=SERVER)
    run(migrated_db, "deny", "cat.view", scope=SERVER, target_id=SAM)

    result = run(migrated_db, "grant", "cat.view")

    assert result.message == (
        f"<@{ALEX}>: `cat.view` is now allowed (everywhere)."
    )


# Unchanged cases


@pytest.mark.parametrize(
    ("command", "word"), [("grant", "allowed"), ("deny", "denied")]
)
def test_set_unchanged(migrated_db, command, word):
    run(migrated_db, command, "cat.view")
    before = audit_rows(migrated_db)
    announce = Announcer()

    result = run(migrated_db, command, "cat.view", announce=announce)

    assert result.success is True
    assert result.message == (
        f"<@{ALEX}>: `cat.view` is already {word} (everywhere)."
    )
    assert audit_rows(migrated_db) == before
    assert announce.lines == []


def test_reset_with_no_row(migrated_db):
    announce = Announcer()

    result = run(
        migrated_db, "reset", "cat.view", scope=SERVER, announce=announce
    )

    assert result.success is True
    assert result.message == (
        f"<@{ALEX}>: `cat.view` has no exception (this server)."
    )
    assert audit_rows(migrated_db) == []
    assert announce.lines == []


def test_reset_of_owner_only_key_has_no_exception(migrated_db):
    result = run(migrated_db, "reset", "config.get")
    assert result.message == (
        f"<@{ALEX}>: `config.get` has no exception (everywhere)."
    )


# Refusals


@pytest.mark.parametrize("command", ["grant", "deny", "reset"])
def test_bot_target(migrated_db, command):
    result = run(
        migrated_db, command, "cat.view", target_id=BOT_ID, target_is_bot=True
    )

    assert result.failure_kind is FailureKind.INVALID
    assert result.message == "Bots can't have permission exceptions."
    assert override_rows(migrated_db) == []


@pytest.mark.parametrize("command", ["grant", "deny", "reset"])
def test_owner_target(migrated_db, command):
    result = run(migrated_db, command, "cat.view", target_id=OWNER)

    assert result.failure_kind is FailureKind.INVALID
    assert result.message == "The owner passes every check."
    assert override_rows(migrated_db) == []


@pytest.mark.parametrize("command", ["grant", "deny", "reset"])
@pytest.mark.parametrize("key", ["feed.nope", "nope.*", "nope.view"])
def test_unknown_key_is_not_found(migrated_db, command, key):
    result = run(migrated_db, command, key)

    assert result.failure_kind is FailureKind.NOT_FOUND
    assert result.message == f"I don't know a permission key called `{key}`."
    assert audit_rows(migrated_db) == []


def test_backtick_in_pattern_is_escaped(migrated_db):
    result = run(migrated_db, "grant", "feed.`x")

    assert result.failure_kind is FailureKind.INVALID
    assert result.message.startswith("'feed.\\`x' is not a permission key")


@pytest.mark.parametrize("command", ["grant", "deny"])
def test_malformed_pattern_is_invalid(migrated_db, command):
    result = run(migrated_db, command, "Feed Remove")

    assert result.failure_kind is FailureKind.INVALID
    assert result.message == (
        "'feed remove' is not a permission key (area.action), an area "
        "wildcard (area.\\*) or \\*."
    )


@pytest.mark.parametrize(
    ("command", "word"), [("grant", "allowed"), ("deny", "denied")]
)
@pytest.mark.parametrize(
    "typed", ["Feed.Remove", "  feed.remove  ", " FEED.* "]
)
def test_typed_key_is_stripped_and_lowercased(
    migrated_db, command, word, typed
):
    announce = Announcer()
    key = typed.strip().lower()

    result = run(migrated_db, command, typed, announce=announce)

    assert result.success is True
    assert result.message == f"<@{ALEX}>: `{key}` is now {word} (everywhere)."
    assert override_rows(migrated_db)[0][1] == key
    assert audit_rows(migrated_db)[0][4] == key
    assert f"`{key}`" in announce.lines[0]


def test_reset_with_mixed_case_and_spaces(migrated_db):
    run(migrated_db, "deny", "cat.view")

    result = run(migrated_db, "reset", "  Cat.View ")

    assert result.message == (
        f"<@{ALEX}>: `cat.view` exception removed (everywhere)."
    )
    assert override_rows(migrated_db) == []


def test_mixed_case_unknown_key_is_not_found_in_lowercase(migrated_db):
    result = run(migrated_db, "grant", " Feed.Nope ")

    assert result.failure_kind is FailureKind.NOT_FOUND
    assert result.message == (
        "I don't know a permission key called `feed.nope`."
    )


@pytest.mark.parametrize("command", ["grant", "deny"])
def test_owner_only_key_is_invalid(migrated_db, command):
    result = run(migrated_db, command, "config.get")

    assert result.failure_kind is FailureKind.INVALID
    assert result.message == (
        "config.get is owner-only and cannot be granted or denied."
    )
    assert override_rows(migrated_db) == []


@pytest.mark.parametrize("command", ["grant", "deny"])
def test_owner_only_area_is_invalid(migrated_db, command):
    result = run(migrated_db, command, "config.*")

    assert result.failure_kind is FailureKind.INVALID
    assert result.message == (
        "Keys in the config area are owner-only and cannot be granted or "
        "denied."
    )


def test_protected_key_denied_is_invalid(migrated_db):
    result = run(migrated_db, "deny", "memory.view")

    assert result.failure_kind is FailureKind.INVALID
    assert result.message == (
        "memory.view is protected: it is always allowed for everyone, so "
        "it cannot be granted or denied."
    )
    assert override_rows(migrated_db) == []


def test_wildcard_deny_skips_protected_and_owner_only_keys(migrated_db):
    run(migrated_db, "deny", "*")

    assert decision(migrated_db, "cat.view").source is DecisionSource.OVERRIDE
    assert decision(migrated_db, "cat.view").allowed is False
    protected = decision(migrated_db, "memory.view")
    assert protected.allowed is True
    assert protected.source is DecisionSource.PROTECTED
    owner_only = decision(migrated_db, "config.get")
    assert owner_only.allowed is False
    assert owner_only.source is DecisionSource.OWNER_ONLY_KEY


def test_wildcard_grant_does_not_open_owner_only_keys(migrated_db):
    run(migrated_db, "grant", "*")

    owner_only = decision(migrated_db, "config.get")
    assert owner_only.allowed is False
    assert owner_only.source is DecisionSource.OWNER_ONLY_KEY


# One transaction


def test_rows_and_audit_written_together(migrated_db, monkeypatch):
    run(migrated_db, "deny", "feed.remove")
    rows_before = override_rows(migrated_db)
    audit_before = audit_rows(migrated_db)

    def broken_audit(*args, **kwargs):
        raise RuntimeError("audit write failed")

    monkeypatch.setattr(
        "maika.permission_store.write_audit_entry", broken_audit
    )

    with pytest.raises(RuntimeError, match="audit write failed"):
        run(migrated_db, "grant", "feed.*", now=LATER)

    assert override_rows(migrated_db) == rows_before
    assert audit_rows(migrated_db) == audit_before
    assert not migrated_db.in_transaction


# Announce


def test_announce_failure_keeps_success(migrated_db, caplog):
    def broken(line):
        raise RuntimeError("router down")

    result = run(migrated_db, "grant", "cat.view", announce=broken)

    assert result.success is True
    assert len(override_rows(migrated_db)) == 1
    assert "Announcing an override change failed." in caplog.messages


# /perm show


def test_show_reflects_a_new_override(migrated_db):
    def show():
        return perm_show(
            migrated_db,
            REGISTRY,
            target_id=ALEX,
            owner_id=OWNER,
            guild_id=GUILD,
        ).message.splitlines()

    assert "`feed.remove`: allowed" not in "\n".join(show())

    run(migrated_db, "grant", "feed.*", scope=SERVER)

    lines = show()
    assert (
        "`feed.remove`: allowed (override: allow override on feed.\\*, "
        "this server)"
    ) in lines
    assert (
        "`feed.subscribe`: allowed (override: allow override on feed.\\*, "
        "this server)"
    ) in lines


# Registry entries


@pytest.mark.parametrize(
    ("name", "key"),
    [
        ("perm grant", "perm.grant"),
        ("perm deny", "perm.deny"),
        ("perm reset", "perm.reset"),
    ],
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


# Key autocomplete


def test_permission_patterns():
    assert permission_patterns(REGISTRY) == (
        "*",
        "cat.*",
        "cat.view",
        "chat.*",
        "chat.talk",
        "feed.*",
        "feed.remove",
        "feed.subscribe",
    )


def test_patterns_leave_out_keys_without_exceptions():
    registry = PermissionRegistry(
        [
            PermissionKey("feed.subscribe", Level.MEMBER),
            PermissionKey("feed.export", Level.ADMIN, owner_only=True),
            PermissionKey("memory.view", Level.EVERYONE, protected=True),
            PermissionKey("memory.forget", Level.EVERYONE, protected=True),
            PermissionKey("config.get", Level.ADMIN, owner_only=True),
        ]
    )

    patterns = permission_patterns(registry)

    assert patterns == ("*", "feed.*", "feed.subscribe")
    # An owner-only key in a normal area, an owner-only area, and an
    # area with only protected keys are all left out.
    assert "feed.export" not in patterns
    assert "config.get" not in patterns
    assert "config.*" not in patterns
    assert "memory.view" not in patterns
    assert "memory.*" not in patterns
    offered = [c.value for c in permission_pattern_choices(registry, "")]
    assert offered == list(patterns)


def test_real_registry_offers_star_and_memory_keys():
    registry = build_command_registry().permission_registry()
    # The protected memory.view and memory.forget are left out.
    assert permission_patterns(registry) == (
        "*",
        "memory.*",
        "memory.add",
        "memory.manage",
    )


def test_pattern_choices_match_typed_text():
    choices = permission_pattern_choices(REGISTRY, "FEED")

    assert [(c.name, c.value) for c in choices] == [
        ("feed.*", "feed.*"),
        ("feed.remove", "feed.remove"),
        ("feed.subscribe", "feed.subscribe"),
    ]


def test_pattern_choices_for_star_are_the_wildcards():
    values = [c.value for c in permission_pattern_choices(REGISTRY, "*")]
    assert values == ["*", "cat.*", "chat.*", "feed.*"]


def test_pattern_choices_no_match():
    assert permission_pattern_choices(REGISTRY, "zzz") == []


def test_pattern_choices_limit():
    registry = PermissionRegistry(
        [PermissionKey(f"many.key_{n:02d}", Level.MEMBER) for n in range(30)]
    )

    choices = permission_pattern_choices(registry, "many")

    assert len(choices) == AUTOCOMPLETE_CHOICE_LIMIT == 25
    assert choices[0].value == "many.*"
    assert choices[-1].value == "many.key_23"


def autocomplete_interaction(user_id):
    return SimpleNamespace(user=SimpleNamespace(id=user_id))


def test_pattern_autocomplete_answers_owner():
    autocomplete = owner_permission_autocomplete(OWNER, REGISTRY)

    choices = asyncio.run(autocomplete(autocomplete_interaction(OWNER), "cat"))

    assert [c.value for c in choices] == ["cat.*", "cat.view"]


def test_pattern_autocomplete_empty_for_non_owner():
    autocomplete = owner_permission_autocomplete(OWNER, REGISTRY)

    choices = asyncio.run(autocomplete(autocomplete_interaction(ALEX), ""))

    assert choices == []


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


def scope_choice(value):
    names = {EVERYWHERE: "Everywhere", SERVER: "This server"}
    return app_commands.Choice(name=names[value], value=value)


@pytest.mark.parametrize("name", ["grant", "deny", "reset"])
def test_override_parameters(migrated_db, name):
    client = make_client(migrated_db)

    command = subcommand(client, name)

    user, key, scope = command.parameters
    assert user.type is discord.AppCommandOptionType.user
    assert user.required is True
    assert key.type is discord.AppCommandOptionType.string
    assert key.required is True
    assert key.autocomplete is True
    assert scope.type is discord.AppCommandOptionType.string
    assert scope.required is False
    assert [(c.name, c.value) for c in scope.choices] == [
        ("Everywhere", "everywhere"),
        ("This server", "server"),
    ]
    assert 1 <= len(command.description) < 100
    for parameter in command.parameters:
        assert 1 <= len(parameter.description) < 100


def test_grant_callback_default_scope_is_everywhere(migrated_db):
    client = make_client(migrated_db)
    interaction = fake_interaction(OWNER, now=LATER)

    asyncio.run(
        subcommand(client, "grant").callback(
            interaction, user=target(ALEX), key="*"
        )
    )

    assert reply(interaction) == (
        f"<@{ALEX}>: `*` is now allowed (everywhere).",
        True,
    )
    assert override_rows(migrated_db) == [
        (ALEX, "*", "allow", 0, OWNER, LATER)
    ]
    assert client.log_router.posts == [
        (
            LogStream.PERMISSIONS,
            LogLevel.INFO,
            f"Override set: <@{ALEX}> `*` allow (everywhere) by <@{OWNER}>.",
        )
    ]


def test_deny_callback_this_server(migrated_db):
    client = make_client(migrated_db)
    interaction = fake_interaction(OWNER)

    asyncio.run(
        subcommand(client, "deny").callback(
            interaction,
            user=target(ALEX),
            key="*",
            scope=scope_choice(SERVER),
        )
    )

    assert reply(interaction) == (
        f"<@{ALEX}>: `*` is now denied (this server).",
        True,
    )
    assert override_rows(migrated_db) == [
        (ALEX, "*", "deny", GUILD, OWNER, NOW)
    ]


def test_reset_callback_in_dm_with_server_scope(migrated_db):
    client = make_client(migrated_db)
    interaction = fake_interaction(OWNER, guild_id=None)

    asyncio.run(
        subcommand(client, "reset").callback(
            interaction,
            user=target(ALEX),
            key="*",
            scope=scope_choice(SERVER),
        )
    )

    assert reply(interaction) == (
        "Use this in a server, or choose Everywhere.",
        True,
    )


def test_grant_callback_bot_target(migrated_db):
    client = make_client(migrated_db)
    interaction = fake_interaction(OWNER)

    asyncio.run(
        subcommand(client, "grant").callback(
            interaction, user=target(BOT_ID, bot=True), key="*"
        )
    )

    assert reply(interaction) == (
        "Bots can't have permission exceptions.",
        True,
    )
    assert override_rows(migrated_db) == []


@pytest.mark.parametrize(
    "command_name", ["perm grant", "perm deny", "perm reset"]
)
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


@pytest.mark.parametrize("name", ["grant", "deny", "reset"])
def test_non_owner_callback_writes_nothing(migrated_db, name):
    client = make_client(migrated_db)
    interaction = fake_interaction(ALEX)

    asyncio.run(
        subcommand(client, name).callback(
            interaction, user=target(SAM), key="*"
        )
    )

    assert reply(interaction) == (DENIED_TEXT, True)
    assert override_rows(migrated_db) == []
    assert audit_rows(migrated_db) == []
    assert all(
        stream is not LogStream.PERMISSIONS
        for stream, _, _ in client.log_router.posts
    )
