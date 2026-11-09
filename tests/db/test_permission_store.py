import itertools
from unittest import mock

import pytest

from maika.permission_store import (
    BlockChange,
    LevelChange,
    PermissionChangeError,
    ScopedBlock,
    find_user_blocks,
    remove_block,
    set_block,
    set_level,
)
from maika.permissions import (
    DecisionSource,
    Level,
    PermissionKey,
    PermissionRegistry,
    check_permission,
)

NOW = 1_800_000_000
LATER = NOW + 60
OWNER = 1
ACTOR = 2
ALEX = 100
SAM = 200
GUILD = 5000

REGISTRY = PermissionRegistry(
    [
        PermissionKey("cat.retag", Level.TRUSTED),
        PermissionKey("feed.list", Level.EVERYONE),
        PermissionKey("memory.view", Level.EVERYONE, protected=True),
    ]
)

LEVEL_PAIRS = [
    (old, new) for old, new in itertools.product(Level, Level) if old != new
]


def change_level(connection, level, user_id=ALEX, now=NOW):
    return set_level(
        connection,
        user_id=user_id,
        level=level,
        owner_id=OWNER,
        actor_id=ACTOR,
        now=now,
    )


def block(connection, kind, user_id=ALEX, guild_id=0, now=NOW):
    return set_block(
        connection,
        user_id=user_id,
        kind=kind,
        guild_id=guild_id,
        owner_id=OWNER,
        actor_id=ACTOR,
        now=now,
    )


def unblock(connection, user_id=ALEX, guild_id=0, now=NOW):
    return remove_block(
        connection,
        user_id=user_id,
        guild_id=guild_id,
        actor_id=ACTOR,
        now=now,
    )


def users_rows(connection):
    return [
        tuple(row)
        for row in connection.execute(
            "SELECT discord_id, level, updated_at FROM users "
            "ORDER BY discord_id"
        )
    ]


def block_rows(connection):
    return [
        tuple(row)
        for row in connection.execute(
            "SELECT user_id, guild_id, kind, set_by, set_at FROM blocks "
            "ORDER BY user_id, guild_id"
        )
    ]


def audit_rows(connection):
    return [
        tuple(row)
        for row in connection.execute(
            "SELECT logged_at, kind, actor_id, target_id, key, guild_id, "
            "old_value, new_value, note FROM audit_log ORDER BY id"
        )
    ]


def clear_audit(connection):
    connection.execute("DELETE FROM audit_log")


def check(connection, key, user_id=ALEX, guild_id=GUILD):
    return check_permission(
        connection,
        REGISTRY,
        owner_id=OWNER,
        user_id=user_id,
        key=key,
        guild_id=guild_id,
    )


def level_text(level):
    return None if level == Level.EVERYONE else str(level.value)


# set_level


@pytest.mark.parametrize(("old", "new"), LEVEL_PAIRS)
def test_set_level_every_transition(migrated_db, old, new):
    change_level(migrated_db, old)
    clear_audit(migrated_db)

    result = change_level(migrated_db, new, now=LATER)

    assert result == LevelChange(ALEX, old, new, changed=True)
    if new == Level.EVERYONE:
        assert users_rows(migrated_db) == []
    else:
        assert users_rows(migrated_db) == [(ALEX, new.value, LATER)]
    assert audit_rows(migrated_db) == [
        (
            LATER,
            "level",
            ACTOR,
            ALEX,
            None,
            0,
            level_text(old),
            level_text(new),
            None,
        )
    ]


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (Level.EVERYONE, Level.TRUSTED),
        (Level.TRUSTED, Level.EVERYONE),
        (Level.MEMBER, Level.ADMIN),
    ],
)
def test_set_level_named_transitions(migrated_db, old, new):
    change_level(migrated_db, old)

    result = change_level(migrated_db, new, now=LATER)

    assert result.old_level == old
    assert result.new_level == new
    assert result.changed is True


def test_set_level_new_row_contents(migrated_db):
    change_level(migrated_db, Level.MEMBER)
    assert users_rows(migrated_db) == [(ALEX, 1, NOW)]


def test_set_level_change_updates_time(migrated_db):
    change_level(migrated_db, Level.MEMBER)
    change_level(migrated_db, Level.ADMIN, now=LATER)
    assert users_rows(migrated_db) == [(ALEX, 3, LATER)]


def test_set_level_zero_removes_row(migrated_db):
    change_level(migrated_db, Level.TRUSTED)
    change_level(migrated_db, Level.EVERYONE, now=LATER)
    assert users_rows(migrated_db) == []


def test_set_level_leaves_other_people(migrated_db):
    change_level(migrated_db, Level.MEMBER, user_id=SAM)
    change_level(migrated_db, Level.TRUSTED)
    change_level(migrated_db, Level.EVERYONE, now=LATER)
    assert users_rows(migrated_db) == [(SAM, 1, NOW)]


@pytest.mark.parametrize("level", list(Level))
def test_set_level_same_level_writes_nothing(migrated_db, level):
    change_level(migrated_db, level)
    users_before = users_rows(migrated_db)
    audit_before = audit_rows(migrated_db)

    result = change_level(migrated_db, level, now=LATER)

    assert result == LevelChange(ALEX, level, level, changed=False)
    assert users_rows(migrated_db) == users_before
    assert audit_rows(migrated_db) == audit_before


def test_set_level_zero_with_no_row_writes_nothing(migrated_db):
    result = change_level(migrated_db, Level.EVERYONE)

    assert result.changed is False
    assert audit_rows(migrated_db) == []


def test_set_level_refuses_owner(migrated_db):
    with pytest.raises(PermissionChangeError, match="owner"):
        change_level(migrated_db, Level.ADMIN, user_id=OWNER)

    assert users_rows(migrated_db) == []
    assert audit_rows(migrated_db) == []


def test_set_level_owner_error_is_value_error():
    assert issubclass(PermissionChangeError, ValueError)


@pytest.mark.parametrize("level", [2, "2", None])
def test_set_level_refuses_non_level(migrated_db, level):
    with pytest.raises(ValueError, match="must be a Level"):
        change_level(migrated_db, level)
    assert users_rows(migrated_db) == []


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (Level.EVERYONE, Level.MEMBER),
        (Level.MEMBER, Level.ADMIN),
        (Level.TRUSTED, Level.EVERYONE),
    ],
)
def test_set_level_audit_failure_leaves_users(migrated_db, old, new):
    change_level(migrated_db, old)
    users_before = users_rows(migrated_db)
    audit_before = audit_rows(migrated_db)

    with mock.patch(
        "maika.permission_store.write_audit_entry",
        side_effect=RuntimeError("audit failed"),
    ):
        with pytest.raises(RuntimeError, match="audit failed"):
            change_level(migrated_db, new, now=LATER)

    assert not migrated_db.in_transaction
    assert users_rows(migrated_db) == users_before
    assert audit_rows(migrated_db) == audit_before


# set_block


def test_set_block_inserts_chat_block(migrated_db):
    result = block(migrated_db, "chat")

    assert result == BlockChange(ALEX, 0, None, "chat", changed=True)
    assert block_rows(migrated_db) == [(ALEX, 0, "chat", ACTOR, NOW)]
    assert audit_rows(migrated_db) == [
        (NOW, "block", ACTOR, ALEX, None, 0, None, "chat", None)
    ]


def test_set_block_chat_to_all(migrated_db):
    block(migrated_db, "chat")
    clear_audit(migrated_db)

    result = block(migrated_db, "all", now=LATER)

    assert result == BlockChange(ALEX, 0, "chat", "all", changed=True)
    assert block_rows(migrated_db) == [(ALEX, 0, "all", ACTOR, LATER)]
    assert audit_rows(migrated_db) == [
        (LATER, "block", ACTOR, ALEX, None, 0, "chat", "all", None)
    ]


def test_set_block_records_set_by_and_set_at(migrated_db):
    set_block(
        migrated_db,
        user_id=ALEX,
        kind="all",
        guild_id=GUILD,
        owner_id=OWNER,
        actor_id=SAM,
        now=LATER,
    )
    assert block_rows(migrated_db) == [(ALEX, GUILD, "all", SAM, LATER)]


@pytest.mark.parametrize("kind", ["chat", "all"])
def test_set_block_same_kind_writes_nothing(migrated_db, kind):
    block(migrated_db, kind)
    blocks_before = block_rows(migrated_db)
    audit_before = audit_rows(migrated_db)

    result = block(migrated_db, kind, now=LATER)

    assert result == BlockChange(ALEX, 0, kind, kind, changed=False)
    assert block_rows(migrated_db) == blocks_before
    assert audit_rows(migrated_db) == audit_before


def test_set_block_server_and_global_are_separate(migrated_db):
    block(migrated_db, "chat", guild_id=0)
    result = block(migrated_db, "all", guild_id=GUILD, now=LATER)

    assert result == BlockChange(ALEX, GUILD, None, "all", changed=True)
    assert block_rows(migrated_db) == [
        (ALEX, 0, "chat", ACTOR, NOW),
        (ALEX, GUILD, "all", ACTOR, LATER),
    ]
    assert audit_rows(migrated_db)[-1] == (
        LATER,
        "block",
        ACTOR,
        ALEX,
        None,
        GUILD,
        None,
        "all",
        None,
    )


def test_set_block_refuses_negative_guild(migrated_db):
    with pytest.raises(ValueError, match="guild_id"):
        block(migrated_db, "chat", guild_id=-1)
    assert block_rows(migrated_db) == []
    assert audit_rows(migrated_db) == []


@pytest.mark.parametrize("kind", ["mute", "", "ALL", None])
def test_set_block_refuses_unknown_kind(migrated_db, kind):
    with pytest.raises(ValueError, match="Unknown block kind"):
        block(migrated_db, kind)
    assert block_rows(migrated_db) == []
    assert audit_rows(migrated_db) == []


def test_set_block_refuses_owner(migrated_db):
    with pytest.raises(PermissionChangeError, match="owner"):
        block(migrated_db, "all", user_id=OWNER)
    assert block_rows(migrated_db) == []
    assert audit_rows(migrated_db) == []


@pytest.mark.parametrize("old_kind", [None, "chat"])
def test_set_block_audit_failure_leaves_blocks(migrated_db, old_kind):
    if old_kind is not None:
        block(migrated_db, old_kind)
    blocks_before = block_rows(migrated_db)
    audit_before = audit_rows(migrated_db)

    with mock.patch(
        "maika.permission_store.write_audit_entry",
        side_effect=RuntimeError("audit failed"),
    ):
        with pytest.raises(RuntimeError, match="audit failed"):
            block(migrated_db, "all", now=LATER)

    assert not migrated_db.in_transaction
    assert block_rows(migrated_db) == blocks_before
    assert audit_rows(migrated_db) == audit_before


# remove_block


def test_remove_block_server_row_only(migrated_db):
    block(migrated_db, "chat", guild_id=0)
    block(migrated_db, "all", guild_id=GUILD)

    result = unblock(migrated_db, guild_id=GUILD, now=LATER)

    assert result == BlockChange(ALEX, GUILD, "all", None, changed=True)
    assert block_rows(migrated_db) == [(ALEX, 0, "chat", ACTOR, NOW)]


def test_remove_block_global_row_only(migrated_db):
    block(migrated_db, "chat", guild_id=0)
    block(migrated_db, "all", guild_id=GUILD)

    result = unblock(migrated_db, guild_id=0, now=LATER)

    assert result == BlockChange(ALEX, 0, "chat", None, changed=True)
    assert block_rows(migrated_db) == [(ALEX, GUILD, "all", ACTOR, NOW)]


def test_remove_block_leaves_other_people(migrated_db):
    block(migrated_db, "all", user_id=SAM)
    block(migrated_db, "all")

    unblock(migrated_db)

    assert block_rows(migrated_db) == [(SAM, 0, "all", ACTOR, NOW)]


def test_remove_block_no_row_writes_nothing(migrated_db):
    block(migrated_db, "all", guild_id=GUILD)
    audit_before = audit_rows(migrated_db)

    result = unblock(migrated_db, guild_id=0)

    assert result == BlockChange(ALEX, 0, None, None, changed=False)
    assert block_rows(migrated_db) == [(ALEX, GUILD, "all", ACTOR, NOW)]
    assert audit_rows(migrated_db) == audit_before


def test_remove_block_audit_entry(migrated_db):
    block(migrated_db, "chat", guild_id=GUILD)
    clear_audit(migrated_db)

    unblock(migrated_db, guild_id=GUILD, now=LATER)

    assert audit_rows(migrated_db) == [
        (LATER, "block", ACTOR, ALEX, None, GUILD, "chat", None, None)
    ]


def test_remove_block_audit_failure_leaves_blocks(migrated_db):
    block(migrated_db, "chat")
    blocks_before = block_rows(migrated_db)
    audit_before = audit_rows(migrated_db)

    with mock.patch(
        "maika.permission_store.write_audit_entry",
        side_effect=RuntimeError("audit failed"),
    ):
        with pytest.raises(RuntimeError, match="audit failed"):
            unblock(migrated_db, now=LATER)

    assert not migrated_db.in_transaction
    assert block_rows(migrated_db) == blocks_before
    assert audit_rows(migrated_db) == audit_before


# find_user_blocks


def test_find_user_blocks_global_first_then_servers(migrated_db):
    block(migrated_db, "all", guild_id=GUILD + 1)
    block(migrated_db, "chat", guild_id=GUILD)
    block(migrated_db, "chat")
    block(migrated_db, "all", user_id=SAM)

    assert find_user_blocks(migrated_db, user_id=ALEX) == (
        ScopedBlock(0, "chat"),
        ScopedBlock(GUILD, "chat"),
        ScopedBlock(GUILD + 1, "all"),
    )


def test_find_user_blocks_none(migrated_db):
    assert find_user_blocks(migrated_db, user_id=ALEX) == ()


# End to end with check_permission


def test_level_two_passes_level_two_key(migrated_db):
    assert check(migrated_db, "cat.retag").allowed is False

    change_level(migrated_db, Level.TRUSTED)

    decision = check(migrated_db, "cat.retag")
    assert decision.allowed is True
    assert decision.source is DecisionSource.LEVEL


def test_level_back_to_zero_loses_key(migrated_db):
    change_level(migrated_db, Level.TRUSTED)
    change_level(migrated_db, Level.EVERYONE, now=LATER)

    assert check(migrated_db, "cat.retag").allowed is False


def test_all_block_denies_normal_key_keeps_protected(migrated_db):
    change_level(migrated_db, Level.TRUSTED)
    block(migrated_db, "all")

    retag = check(migrated_db, "cat.retag")
    assert retag.allowed is False
    assert retag.source is DecisionSource.BLOCK
    assert check(migrated_db, "feed.list").allowed is False

    memory = check(migrated_db, "memory.view")
    assert memory.allowed is True
    assert memory.source is DecisionSource.PROTECTED


def test_removing_block_restores_access(migrated_db):
    change_level(migrated_db, Level.TRUSTED)
    block(migrated_db, "all")

    unblock(migrated_db, now=LATER)

    decision = check(migrated_db, "cat.retag")
    assert decision.allowed is True
    assert decision.source is DecisionSource.LEVEL
