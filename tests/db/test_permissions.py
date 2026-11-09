import pytest

from maika.permissions import (
    DEFAULT_LEVEL,
    OWNER_ONLY_AREAS,
    OWNER_ONLY_KEYS,
    Decision,
    DecisionSource,
    Level,
    PermissionKey,
    PermissionRegistry,
    UnknownPermissionKeyError,
    check_permission,
    override_patterns,
)

NOW = 1_800_000_000
OWNER = 1
ALEX = 100
SAM = 200
GUILD = 5000
OTHER_GUILD = 6000

REGISTRY = PermissionRegistry(
    [
        PermissionKey("feed.list", Level.EVERYONE),
        PermissionKey("feed.subscribe", Level.MEMBER),
        PermissionKey("feed.notify", Level.EVERYONE),
        PermissionKey("cat.retag", Level.TRUSTED),
        PermissionKey("feed.remove", Level.ADMIN),
        PermissionKey("chat.talk", Level.EVERYONE),
        PermissionKey("help.view", Level.EVERYONE),
        PermissionKey("perm.level", Level.ADMIN, owner_only=True),
        PermissionKey("memory.view", Level.EVERYONE, protected=True),
    ]
)


def check(connection, key, user_id=ALEX, guild_id=GUILD):
    return check_permission(
        connection,
        REGISTRY,
        owner_id=OWNER,
        user_id=user_id,
        key=key,
        guild_id=guild_id,
    )


def set_level(connection, user_id, level):
    # Level 0 is the absence of a row (design 9.2).
    if level == Level.EVERYONE:
        return
    connection.execute(
        "INSERT INTO users (discord_id, level, updated_at) VALUES (?, ?, ?)",
        (user_id, int(level), NOW),
    )


def add_block(connection, kind, user_id=ALEX, guild_id=0):
    connection.execute(
        "INSERT INTO blocks (user_id, guild_id, kind, set_by, set_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (user_id, guild_id, kind, OWNER, NOW),
    )


def add_override(connection, key, effect, user_id=ALEX, guild_id=0):
    connection.execute(
        "INSERT INTO permission_overrides "
        "(user_id, key, effect, guild_id, set_by, set_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, key, effect, guild_id, OWNER, NOW),
    )


# PermissionKey and registry validation


@pytest.mark.parametrize(
    "key",
    [
        "nodot",
        "two.dots.here",
        ".action",
        "area.",
        ".",
        "",
        "Feed.list",
        "feed.List",
        "feed-x.list",
        "feed.li st",
        "feed .list",
        "féed.list",
    ],
)
def test_key_rejects_bad_form(key):
    with pytest.raises(ValueError, match="area.action"):
        PermissionKey(key, Level.EVERYONE)


@pytest.mark.parametrize("key", ["feed.*", "*", "*.list", "fe*ed.list"])
def test_key_rejects_wildcard(key):
    with pytest.raises(ValueError, match="must not contain"):
        PermissionKey(key, Level.EVERYONE)


@pytest.mark.parametrize("key", ["feed.list", "cat_2.re_tag", "a.b"])
def test_key_accepts_good_form(key):
    PermissionKey(key, Level.EVERYONE)


def test_key_rejects_owner_only_and_protected():
    with pytest.raises(ValueError, match="both owner-only and protected"):
        PermissionKey("memory.view", Level.EVERYONE, True, True)


def test_key_rejects_plain_int_level():
    with pytest.raises(ValueError, match="Level"):
        PermissionKey("feed.list", 0)


def test_registry_rejects_duplicate():
    with pytest.raises(ValueError, match="feed.list"):
        PermissionRegistry(
            [
                PermissionKey("feed.list", Level.EVERYONE),
                PermissionKey("feed.list", Level.MEMBER),
            ]
        )


def test_registry_unknown_key():
    with pytest.raises(UnknownPermissionKeyError, match="feed.missing"):
        REGISTRY.get("feed.missing")


def test_check_unknown_key_raises_even_for_owner(migrated_db):
    with pytest.raises(UnknownPermissionKeyError):
        check(migrated_db, "feed.missing", user_id=OWNER)


@pytest.mark.parametrize(
    "key",
    [
        "perm.level",
        "config.set",
        "event.reset",
        "persona.edit",
        "sleep.wake",
        "presence.set",
        "say.send",
        "mood.set",
        "mood.raw",
        "relationship.set",
        "relationship.raw",
        "state.reset",
    ],
)
def test_registry_requires_owner_only_from_design(key):
    with pytest.raises(ValueError, match=f"{key} must be flagged owner_only"):
        PermissionRegistry([PermissionKey(key, Level.ADMIN)])
    with pytest.raises(ValueError, match="design makes it owner-only"):
        PermissionRegistry([PermissionKey(key, Level.ADMIN, protected=True)])

    registry = PermissionRegistry(
        [PermissionKey(key, Level.ADMIN, owner_only=True)]
    )
    assert registry.get(key).owner_only


def test_owner_only_areas_and_keys_match_design():
    assert OWNER_ONLY_AREAS == {
        "perm",
        "config",
        "event",
        "persona",
        "sleep",
        "presence",
        "say",
    }
    assert OWNER_ONLY_KEYS == {
        "mood.set",
        "mood.raw",
        "relationship.set",
        "relationship.raw",
        "state.reset",
    }


@pytest.mark.parametrize("key", ["mood.view", "relationship.view"])
def test_other_keys_in_mixed_areas_need_no_flag(key):
    PermissionRegistry([PermissionKey(key, Level.EVERYONE)])


def test_owner_only_outside_lists_is_allowed():
    registry = PermissionRegistry(
        [PermissionKey("feed.purge", Level.ADMIN, owner_only=True)]
    )
    assert registry.get("feed.purge").owner_only


def test_override_patterns():
    assert override_patterns("feed.remove") == ("feed.remove", "feed.*", "*")


def test_default_level():
    assert DEFAULT_LEVEL is Level.EVERYONE


# Step 1: the owner


def test_owner_passes_owner_only_key(migrated_db):
    assert check(migrated_db, "perm.level", user_id=OWNER).allowed


def test_owner_passes_while_blocked(migrated_db):
    add_block(migrated_db, "all", user_id=OWNER, guild_id=GUILD)
    add_block(migrated_db, "all", user_id=OWNER, guild_id=0)
    decision = check(migrated_db, "feed.remove", user_id=OWNER)
    assert decision.allowed
    assert decision.source is DecisionSource.OWNER


def test_owner_passes_with_deny_override(migrated_db):
    add_override(migrated_db, "feed.remove", "deny", user_id=OWNER)
    add_override(migrated_db, "*", "deny", user_id=OWNER, guild_id=GUILD)
    decision = check(migrated_db, "feed.remove", user_id=OWNER)
    assert decision.allowed
    assert decision.source is DecisionSource.OWNER


# Step 2: owner-only keys


def test_owner_only_denied_at_level_3(migrated_db):
    set_level(migrated_db, ALEX, Level.ADMIN)
    decision = check(migrated_db, "perm.level")
    assert not decision.allowed
    assert decision.source is DecisionSource.OWNER_ONLY_KEY


def test_owner_only_denied_with_exact_allow(migrated_db):
    add_override(migrated_db, "perm.level", "allow", guild_id=GUILD)
    add_override(migrated_db, "perm.level", "allow", guild_id=0)
    decision = check(migrated_db, "perm.level")
    assert not decision.allowed
    assert decision.source is DecisionSource.OWNER_ONLY_KEY


def test_owner_only_denied_with_star_allow(migrated_db):
    add_override(migrated_db, "*", "allow")
    add_override(migrated_db, "perm.*", "allow")
    decision = check(migrated_db, "perm.level")
    assert not decision.allowed
    assert decision.source is DecisionSource.OWNER_ONLY_KEY


# Step 3: protected keys


def test_protected_allowed_at_level_0(migrated_db):
    decision = check(migrated_db, "memory.view")
    assert decision.allowed
    assert decision.source is DecisionSource.PROTECTED


def test_protected_allowed_under_all_block(migrated_db):
    add_block(migrated_db, "all", guild_id=GUILD)
    add_block(migrated_db, "all", guild_id=0)
    decision = check(migrated_db, "memory.view")
    assert decision.allowed
    assert decision.source is DecisionSource.PROTECTED


def test_protected_allowed_under_star_deny(migrated_db):
    add_override(migrated_db, "*", "deny", guild_id=GUILD)
    add_override(migrated_db, "memory.view", "deny")
    decision = check(migrated_db, "memory.view")
    assert decision.allowed
    assert decision.source is DecisionSource.PROTECTED


# Step 4: blocks


def test_chat_block_refuses_only_chat_talk(migrated_db):
    add_block(migrated_db, "chat")

    decision = check(migrated_db, "chat.talk")
    assert not decision.allowed
    assert decision.source is DecisionSource.BLOCK

    other = check(migrated_db, "feed.list")
    assert other.allowed
    assert other.source is DecisionSource.LEVEL


def test_all_block_refuses_normal_key(migrated_db):
    add_block(migrated_db, "all")
    for key in ("feed.list", "chat.talk", "help.view"):
        decision = check(migrated_db, key)
        assert not decision.allowed
        assert decision.source is DecisionSource.BLOCK


def test_block_beats_allow_override(migrated_db):
    add_block(migrated_db, "all", guild_id=GUILD)
    add_override(migrated_db, "feed.remove", "allow", guild_id=GUILD)
    decision = check(migrated_db, "feed.remove")
    assert not decision.allowed
    assert decision.source is DecisionSource.BLOCK


def test_global_all_with_server_chat_applies_all(migrated_db):
    add_block(migrated_db, "chat", guild_id=GUILD)
    add_block(migrated_db, "all", guild_id=0)

    # The server's chat block does not loosen the global all block.
    for key in ("feed.list", "chat.talk"):
        decision = check(migrated_db, key)
        assert not decision.allowed
        assert decision.source is DecisionSource.BLOCK
        assert decision.detail == "all block, everywhere"


def test_global_chat_with_server_all_applies_all(migrated_db):
    add_block(migrated_db, "all", guild_id=GUILD)
    add_block(migrated_db, "chat", guild_id=0)

    decision = check(migrated_db, "feed.list")
    assert not decision.allowed
    assert decision.detail == f"all block, server {GUILD}"

    # In another server only the global chat block applies.
    elsewhere = check(migrated_db, "feed.list", guild_id=OTHER_GUILD)
    assert elsewhere.allowed
    assert elsewhere.source is DecisionSource.LEVEL
    talk = check(migrated_db, "chat.talk", guild_id=OTHER_GUILD)
    assert talk.detail == "chat block, everywhere"


def test_both_chat_applies_chat_and_names_server(migrated_db):
    add_block(migrated_db, "chat", guild_id=GUILD)
    add_block(migrated_db, "chat", guild_id=0)

    talk = check(migrated_db, "chat.talk")
    assert not talk.allowed
    assert talk.detail == f"chat block, server {GUILD}"

    other = check(migrated_db, "feed.list")
    assert other.allowed
    assert other.source is DecisionSource.LEVEL


def test_both_all_names_server(migrated_db):
    add_block(migrated_db, "all", guild_id=GUILD)
    add_block(migrated_db, "all", guild_id=0)
    decision = check(migrated_db, "feed.list")
    assert decision.detail == f"all block, server {GUILD}"


@pytest.mark.parametrize("kind", ["chat", "all"])
def test_only_server_row(migrated_db, kind):
    add_block(migrated_db, kind, guild_id=GUILD)

    talk = check(migrated_db, "chat.talk")
    assert not talk.allowed
    assert talk.detail == f"{kind} block, server {GUILD}"
    assert check(migrated_db, "feed.list").allowed is (kind == "chat")


@pytest.mark.parametrize("kind", ["chat", "all"])
def test_only_global_row(migrated_db, kind):
    add_block(migrated_db, kind, guild_id=0)

    talk = check(migrated_db, "chat.talk")
    assert not talk.allowed
    assert talk.detail == f"{kind} block, everywhere"
    assert check(migrated_db, "feed.list").allowed is (kind == "chat")


def test_block_in_another_server_does_not_apply(migrated_db):
    add_block(migrated_db, "all", guild_id=OTHER_GUILD)
    decision = check(migrated_db, "feed.list")
    assert decision.allowed
    assert decision.source is DecisionSource.LEVEL


def test_guild_zero_uses_only_global_block(migrated_db):
    add_block(migrated_db, "all", guild_id=GUILD)
    assert check(migrated_db, "feed.list", guild_id=0).allowed

    add_block(migrated_db, "all", guild_id=0)
    decision = check(migrated_db, "feed.list", guild_id=0)
    assert not decision.allowed
    assert decision.detail == "all block, everywhere"


def test_other_users_block_does_not_apply(migrated_db):
    add_block(migrated_db, "all", user_id=SAM)
    assert check(migrated_db, "feed.list").allowed


# Step 5: overrides


@pytest.mark.parametrize(
    ("winner", "loser"),
    [
        (("feed.remove", "allow"), ("feed.*", "deny")),
        (("feed.remove", "deny"), ("feed.*", "allow")),
        (("feed.*", "allow"), ("*", "deny")),
        (("feed.*", "deny"), ("*", "allow")),
        (("feed.remove", "allow"), ("*", "deny")),
        (("feed.remove", "deny"), ("*", "allow")),
    ],
)
def test_more_specific_pattern_wins(migrated_db, winner, loser):
    set_level(migrated_db, ALEX, Level.MEMBER)
    add_override(migrated_db, *loser)
    add_override(migrated_db, *winner)

    decision = check(migrated_db, "feed.remove")

    assert decision.allowed is (winner[1] == "allow")
    assert decision.source is DecisionSource.OVERRIDE
    assert decision.detail == (
        f"{winner[1]} override on {winner[0]}, everywhere"
    )


@pytest.mark.parametrize("pattern", ["feed.remove", "feed.*", "*"])
@pytest.mark.parametrize("effect", ["allow", "deny"])
def test_single_override_decides(migrated_db, pattern, effect):
    add_override(migrated_db, pattern, effect)
    decision = check(migrated_db, "feed.remove")
    assert decision.allowed is (effect == "allow")
    assert decision.source is DecisionSource.OVERRIDE


def test_first_row_found_decides_over_level(migrated_db):
    set_level(migrated_db, ALEX, Level.ADMIN)
    add_override(migrated_db, "*", "deny")
    decision = check(migrated_db, "feed.list")
    assert not decision.allowed
    assert decision.source is DecisionSource.OVERRIDE


def test_server_row_beats_global_row_in_same_pattern(migrated_db):
    add_override(migrated_db, "feed.remove", "deny", guild_id=0)
    add_override(migrated_db, "feed.remove", "allow", guild_id=GUILD)

    here = check(migrated_db, "feed.remove")
    assert here.allowed
    assert here.detail == f"allow override on feed.remove, server {GUILD}"

    elsewhere = check(migrated_db, "feed.remove", guild_id=OTHER_GUILD)
    assert not elsewhere.allowed
    assert elsewhere.detail == "deny override on feed.remove, everywhere"


def test_exact_global_beats_area_server(migrated_db):
    add_override(migrated_db, "feed.*", "allow", guild_id=GUILD)
    add_override(migrated_db, "feed.remove", "deny", guild_id=0)

    decision = check(migrated_db, "feed.remove")
    assert not decision.allowed
    assert decision.detail == "deny override on feed.remove, everywhere"


def test_other_servers_row_is_ignored(migrated_db):
    add_override(migrated_db, "feed.remove", "allow", guild_id=OTHER_GUILD)
    decision = check(migrated_db, "feed.remove")
    assert not decision.allowed
    assert decision.source is DecisionSource.LEVEL


def test_guild_zero_ignores_server_rows(migrated_db):
    add_override(migrated_db, "feed.remove", "allow", guild_id=GUILD)
    add_override(migrated_db, "*", "allow", guild_id=GUILD)

    decision = check(migrated_db, "feed.remove", guild_id=0)
    assert not decision.allowed
    assert decision.source is DecisionSource.LEVEL

    add_override(migrated_db, "feed.*", "allow", guild_id=0)
    assert check(migrated_db, "feed.remove", guild_id=0).allowed


def test_other_users_rows_are_ignored(migrated_db):
    add_override(migrated_db, "feed.remove", "allow", user_id=SAM)
    add_override(migrated_db, "*", "deny", user_id=SAM, guild_id=GUILD)
    decision = check(migrated_db, "feed.remove")
    assert not decision.allowed
    assert decision.source is DecisionSource.LEVEL
    assert check(migrated_db, "feed.list").allowed


def test_area_wildcard_does_not_cover_other_area(migrated_db):
    add_override(migrated_db, "cat.*", "allow")
    decision = check(migrated_db, "feed.remove")
    assert not decision.allowed
    assert decision.source is DecisionSource.LEVEL


# Step 6: levels

LEVEL_KEYS = {
    Level.EVERYONE: "feed.list",
    Level.MEMBER: "feed.subscribe",
    Level.TRUSTED: "cat.retag",
    Level.ADMIN: "feed.remove",
}


@pytest.mark.parametrize(
    "required", [Level.MEMBER, Level.TRUSTED, Level.ADMIN]
)
def test_level_equal_passes_and_one_below_fails(migrated_db, required):
    key = LEVEL_KEYS[required]
    set_level(migrated_db, ALEX, required)
    set_level(migrated_db, SAM, Level(required - 1))

    assert check(migrated_db, key, user_id=ALEX).allowed
    below = check(migrated_db, key, user_id=SAM)
    assert not below.allowed
    assert below.source is DecisionSource.LEVEL


@pytest.mark.parametrize("level", list(Level)[1:])
def test_higher_level_passes_lower_keys(migrated_db, level):
    set_level(migrated_db, ALEX, level)
    for required, key in LEVEL_KEYS.items():
        assert check(migrated_db, key).allowed is (level >= required)


def test_no_users_row_means_level_0(migrated_db):
    assert check(migrated_db, "feed.list").allowed
    decision = check(migrated_db, "feed.subscribe")
    assert not decision.allowed
    assert (
        decision.detail == "level 0 (everyone), feed.subscribe needs level 1"
    )


def test_level_0_key_passes_for_everyone(migrated_db):
    for user_id in (ALEX, SAM, 999_999_999_999):
        assert check(migrated_db, "chat.talk", user_id=user_id).allowed


# Decision source and detail


def test_decision_for_each_source(migrated_db):
    set_level(migrated_db, SAM, Level.TRUSTED)
    add_block(migrated_db, "chat", user_id=ALEX, guild_id=GUILD)
    add_override(migrated_db, "feed.*", "deny", user_id=SAM, guild_id=GUILD)

    assert check(migrated_db, "feed.list", user_id=OWNER) == Decision(
        True, DecisionSource.OWNER, "owner"
    )
    assert check(migrated_db, "perm.level") == Decision(
        False, DecisionSource.OWNER_ONLY_KEY, "perm.level is owner-only"
    )
    assert check(migrated_db, "memory.view") == Decision(
        True, DecisionSource.PROTECTED, "memory.view is protected"
    )
    assert check(migrated_db, "chat.talk") == Decision(
        False, DecisionSource.BLOCK, f"chat block, server {GUILD}"
    )
    assert check(migrated_db, "feed.list", user_id=SAM) == Decision(
        False,
        DecisionSource.OVERRIDE,
        f"deny override on feed.*, server {GUILD}",
    )
    assert check(migrated_db, "cat.retag", user_id=SAM) == Decision(
        True,
        DecisionSource.LEVEL,
        "level 2 (trusted), cat.retag needs level 2",
    )


def test_check_writes_nothing(migrated_db):
    add_block(migrated_db, "chat")
    add_override(migrated_db, "feed.*", "allow")
    before = migrated_db.total_changes

    for key in ("feed.list", "chat.talk", "perm.level", "memory.view"):
        check(migrated_db, key)

    assert migrated_db.total_changes == before
    assert not migrated_db.in_transaction
