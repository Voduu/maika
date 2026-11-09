from unittest import mock

import pytest

from maika.permission_store import (
    ClearedOverride,
    OverrideChange,
    PermissionChangeError,
    ScopedCoveredOverride,
    ScopedOverride,
    find_covered_in_other_scopes,
    find_covered_overrides,
    find_key_overrides,
    reset_override,
    set_override,
)
from maika.permissions import (
    EFFECT_ALLOW,
    EFFECT_DENY,
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
OTHER_GUILD = 6000

ALLOW = EFFECT_ALLOW
DENY = EFFECT_DENY

REGISTRY = PermissionRegistry(
    [
        PermissionKey("feed.list", Level.EVERYONE),
        PermissionKey("feed.notify", Level.EVERYONE),
        PermissionKey("feed.remove", Level.ADMIN),
        PermissionKey("cat.view", Level.EVERYONE),
        PermissionKey("cat.retag", Level.TRUSTED),
        PermissionKey("cat_photos.add", Level.MEMBER),
        PermissionKey("catxphotos.add", Level.MEMBER),
        PermissionKey("chat.talk", Level.EVERYONE),
        PermissionKey("mood.view", Level.EVERYONE),
        PermissionKey("mood.set", Level.ADMIN, owner_only=True),
        PermissionKey("perm.level", Level.ADMIN, owner_only=True),
        PermissionKey("memory.view", Level.EVERYONE, protected=True),
    ]
)

AUDIT_FAILURE = "maika.permission_store.write_audit_entry"


def grant(connection, pattern, user_id=ALEX, guild_id=0, now=NOW):
    return set_override(
        connection,
        REGISTRY,
        user_id=user_id,
        pattern=pattern,
        effect=ALLOW,
        guild_id=guild_id,
        owner_id=OWNER,
        actor_id=ACTOR,
        now=now,
    )


def deny(connection, pattern, user_id=ALEX, guild_id=0, now=NOW):
    return set_override(
        connection,
        REGISTRY,
        user_id=user_id,
        pattern=pattern,
        effect=DENY,
        guild_id=guild_id,
        owner_id=OWNER,
        actor_id=ACTOR,
        now=now,
    )


def reset(connection, pattern, user_id=ALEX, guild_id=0, now=NOW):
    return reset_override(
        connection,
        REGISTRY,
        user_id=user_id,
        pattern=pattern,
        guild_id=guild_id,
        owner_id=OWNER,
        actor_id=ACTOR,
        now=now,
    )


def covered(connection, pattern, user_id=ALEX, guild_id=0):
    return find_covered_overrides(
        connection, user_id=user_id, pattern=pattern, guild_id=guild_id
    )


def insert_row(connection, key, effect, user_id=ALEX, guild_id=0):
    connection.execute(
        "INSERT INTO permission_overrides "
        "(user_id, key, effect, guild_id, set_by, set_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, key, effect, guild_id, ACTOR, NOW),
    )


def override_rows(connection):
    return [
        tuple(row)
        for row in connection.execute(
            "SELECT user_id, key, effect, guild_id, set_by, set_at "
            "FROM permission_overrides ORDER BY user_id, guild_id, key"
        )
    ]


def keys_for(connection, user_id=ALEX, guild_id=0):
    return {
        row[1]: row[2]
        for row in override_rows(connection)
        if row[0] == user_id and row[3] == guild_id
    }


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


def audit(key, old, new, note=None, guild_id=0, now=LATER):
    return (now, "override", ACTOR, ALEX, key, guild_id, old, new, note)


def check(connection, key, user_id=ALEX, guild_id=GUILD):
    return check_permission(
        connection,
        REGISTRY,
        owner_id=OWNER,
        user_id=user_id,
        key=key,
        guild_id=guild_id,
    )


def snapshot(connection):
    return override_rows(connection), audit_rows(connection)


# Exact keys


@pytest.mark.parametrize("effect", [ALLOW, DENY])
def test_new_exact_row(migrated_db, effect):
    result = set_override(
        migrated_db,
        REGISTRY,
        user_id=ALEX,
        pattern="feed.remove",
        effect=effect,
        guild_id=GUILD,
        owner_id=OWNER,
        actor_id=SAM,
        now=LATER,
    )

    assert result == OverrideChange("feed.remove", effect, None, True, ())
    assert override_rows(migrated_db) == [
        (ALEX, "feed.remove", effect, GUILD, SAM, LATER)
    ]
    assert audit_rows(migrated_db) == [
        (
            LATER,
            "override",
            SAM,
            ALEX,
            "feed.remove",
            GUILD,
            None,
            effect,
            None,
        )
    ]


def test_allow_changed_to_deny(migrated_db):
    grant(migrated_db, "feed.remove")
    clear_audit(migrated_db)

    result = deny(migrated_db, "feed.remove", now=LATER)

    assert result == OverrideChange("feed.remove", DENY, ALLOW, True, ())
    assert override_rows(migrated_db) == [
        (ALEX, "feed.remove", DENY, 0, ACTOR, LATER)
    ]
    assert audit_rows(migrated_db) == [audit("feed.remove", ALLOW, DENY)]


@pytest.mark.parametrize("effect", [ALLOW, DENY])
def test_same_effect_again_is_noop(migrated_db, effect):
    set_override(
        migrated_db,
        REGISTRY,
        user_id=ALEX,
        pattern="feed.remove",
        effect=effect,
        owner_id=OWNER,
        actor_id=ACTOR,
        now=NOW,
    )
    before = snapshot(migrated_db)

    result = set_override(
        migrated_db,
        REGISTRY,
        user_id=ALEX,
        pattern="feed.remove",
        effect=effect,
        owner_id=OWNER,
        actor_id=ACTOR,
        now=LATER,
    )

    assert result == OverrideChange("feed.remove", effect, effect, False, ())
    assert snapshot(migrated_db) == before


def test_reset_removes_exact_row(migrated_db):
    deny(migrated_db, "feed.remove", guild_id=GUILD)
    clear_audit(migrated_db)

    result = reset(migrated_db, "feed.remove", guild_id=GUILD, now=LATER)

    assert result == OverrideChange("feed.remove", None, DENY, True, ())
    assert override_rows(migrated_db) == []
    assert audit_rows(migrated_db) == [
        audit("feed.remove", DENY, None, guild_id=GUILD)
    ]


def test_reset_with_no_row_is_noop(migrated_db):
    deny(migrated_db, "feed.remove", guild_id=GUILD)
    before = snapshot(migrated_db)

    result = reset(migrated_db, "feed.remove", guild_id=0, now=LATER)

    assert result == OverrideChange("feed.remove", None, None, False, ())
    assert snapshot(migrated_db) == before


def test_exact_key_clears_nothing(migrated_db):
    deny(migrated_db, "feed.*")
    grant(migrated_db, "feed.list")

    assert keys_for(migrated_db) == {"feed.*": DENY, "feed.list": ALLOW}


# find_covered_overrides


def test_find_covered_area(migrated_db):
    insert_row(migrated_db, "feed.list", ALLOW)
    insert_row(migrated_db, "feed.remove", DENY)
    insert_row(migrated_db, "feed.*", DENY)
    insert_row(migrated_db, "cat.view", ALLOW)
    insert_row(migrated_db, "*", DENY)

    assert covered(migrated_db, "feed.*") == (
        ClearedOverride("feed.list", ALLOW),
        ClearedOverride("feed.remove", DENY),
    )


def test_find_covered_all(migrated_db):
    insert_row(migrated_db, "feed.remove", DENY)
    insert_row(migrated_db, "cat.view", ALLOW)
    insert_row(migrated_db, "feed.*", ALLOW)
    insert_row(migrated_db, "*", DENY)

    assert covered(migrated_db, "*") == (
        ClearedOverride("cat.view", ALLOW),
        ClearedOverride("feed.*", ALLOW),
        ClearedOverride("feed.remove", DENY),
    )


def test_find_covered_exact_key(migrated_db):
    insert_row(migrated_db, "feed.remove", DENY)
    assert covered(migrated_db, "feed.remove") == ()


@pytest.mark.parametrize("pattern", ["feed.*", "*"])
def test_find_covered_ignores_other_users_and_guilds(migrated_db, pattern):
    insert_row(migrated_db, "feed.list", ALLOW, user_id=SAM)
    insert_row(migrated_db, "feed.list", ALLOW, guild_id=GUILD)
    insert_row(migrated_db, "feed.remove", DENY, guild_id=OTHER_GUILD)

    assert covered(migrated_db, pattern) == ()
    assert covered(migrated_db, pattern, guild_id=GUILD) == (
        ClearedOverride("feed.list", ALLOW),
    )


def elsewhere(connection, pattern, guild_id=0):
    return find_covered_in_other_scopes(
        connection, user_id=ALEX, pattern=pattern, guild_id=guild_id
    )


def test_covered_elsewhere_skips_the_given_scope(migrated_db):
    insert_row(migrated_db, "feed.list", ALLOW)
    insert_row(migrated_db, "feed.remove", DENY, guild_id=OTHER_GUILD)
    insert_row(migrated_db, "feed.notify", ALLOW, guild_id=GUILD)
    insert_row(migrated_db, "feed.list", DENY, guild_id=GUILD)

    assert elsewhere(migrated_db, "feed.*") == (
        ScopedCoveredOverride(GUILD, "feed.list", DENY),
        ScopedCoveredOverride(GUILD, "feed.notify", ALLOW),
        ScopedCoveredOverride(OTHER_GUILD, "feed.remove", DENY),
    )
    assert elsewhere(migrated_db, "feed.*", guild_id=GUILD) == (
        ScopedCoveredOverride(0, "feed.list", ALLOW),
        ScopedCoveredOverride(OTHER_GUILD, "feed.remove", DENY),
    )


def test_covered_elsewhere_uses_the_covering_rule(migrated_db):
    insert_row(migrated_db, "*", DENY, guild_id=GUILD)
    insert_row(migrated_db, "feed.*", DENY, guild_id=GUILD)
    insert_row(migrated_db, "cat.view", ALLOW, guild_id=GUILD)
    insert_row(migrated_db, "cat_photos.add", ALLOW, guild_id=GUILD)
    insert_row(migrated_db, "feed.list", ALLOW, user_id=SAM, guild_id=GUILD)

    assert elsewhere(migrated_db, "*") == (
        ScopedCoveredOverride(GUILD, "cat.view", ALLOW),
        ScopedCoveredOverride(GUILD, "cat_photos.add", ALLOW),
        ScopedCoveredOverride(GUILD, "feed.*", DENY),
    )
    assert elsewhere(migrated_db, "cat.*") == (
        ScopedCoveredOverride(GUILD, "cat.view", ALLOW),
    )
    assert elsewhere(migrated_db, "feed.*") == ()


def test_covered_elsewhere_exact_key_is_empty(migrated_db):
    insert_row(migrated_db, "feed.list", ALLOW, guild_id=GUILD)
    assert elsewhere(migrated_db, "feed.list") == ()


def test_covered_elsewhere_refuses_malformed_pattern(migrated_db):
    with pytest.raises(PermissionChangeError):
        elsewhere(migrated_db, "Feed List")


def test_find_covered_writes_nothing(migrated_db):
    insert_row(migrated_db, "feed.list", ALLOW)
    insert_row(migrated_db, "cat.view", DENY)
    before = snapshot(migrated_db)

    covered(migrated_db, "feed.*")
    covered(migrated_db, "*")

    assert snapshot(migrated_db) == before
    assert not migrated_db.in_transaction


def test_find_covered_refuses_malformed_pattern(migrated_db):
    with pytest.raises(PermissionChangeError):
        covered(migrated_db, "feed.%")


# Wildcards


def test_area_wildcard_clears_exact_rows_in_area(migrated_db):
    grant(migrated_db, "feed.list")
    deny(migrated_db, "feed.notify")
    grant(migrated_db, "cat.view")
    grant(migrated_db, "feed.list", user_id=SAM)
    grant(migrated_db, "feed.list", guild_id=GUILD)

    result = deny(migrated_db, "feed.*", now=LATER)

    assert result == OverrideChange(
        "feed.*",
        DENY,
        None,
        True,
        (
            ClearedOverride("feed.list", ALLOW),
            ClearedOverride("feed.notify", DENY),
        ),
    )
    assert keys_for(migrated_db) == {"feed.*": DENY, "cat.view": ALLOW}
    assert keys_for(migrated_db, user_id=SAM) == {"feed.list": ALLOW}
    assert keys_for(migrated_db, guild_id=GUILD) == {"feed.list": ALLOW}


def test_area_wildcard_keeps_other_area_wildcards(migrated_db):
    grant(migrated_db, "cat.*")

    result = deny(migrated_db, "feed.*", now=LATER)

    assert result.cleared == ()
    assert keys_for(migrated_db) == {"cat.*": ALLOW, "feed.*": DENY}


def test_all_wildcard_clears_exact_and_area_rows(migrated_db):
    grant(migrated_db, "feed.*")
    deny(migrated_db, "cat.retag")
    grant(migrated_db, "feed.remove")

    result = deny(migrated_db, "*", now=LATER)

    assert result.cleared == (
        ClearedOverride("cat.retag", DENY),
        ClearedOverride("feed.*", ALLOW),
        ClearedOverride("feed.remove", ALLOW),
    )
    assert keys_for(migrated_db) == {"*": DENY}


def test_unchanged_wildcard_still_clears(migrated_db):
    deny(migrated_db, "feed.*")
    grant(migrated_db, "feed.remove")
    clear_audit(migrated_db)

    result = deny(migrated_db, "feed.*", now=LATER)

    assert result == OverrideChange(
        "feed.*",
        DENY,
        DENY,
        True,
        (ClearedOverride("feed.remove", ALLOW),),
    )
    assert keys_for(migrated_db) == {"feed.*": DENY}
    # The pattern row keeps its time, and only the cleared row is
    # audited.
    assert override_rows(migrated_db) == [
        (ALEX, "feed.*", DENY, 0, ACTOR, NOW)
    ]
    assert audit_rows(migrated_db) == [
        audit("feed.remove", ALLOW, None, "cleared by /perm deny feed.*")
    ]


def test_wildcard_audit_order_and_notes(migrated_db):
    # Inserted directly: a "*" deny written through deny() would
    # already have cleared the exact rows.
    insert_row(migrated_db, "feed.remove", DENY, guild_id=GUILD)
    insert_row(migrated_db, "feed.list", ALLOW, guild_id=GUILD)
    insert_row(migrated_db, "*", DENY, guild_id=GUILD)

    grant(migrated_db, "feed.*", guild_id=GUILD, now=LATER)
    grant(migrated_db, "cat.view", guild_id=GUILD, now=LATER)
    allow_all = grant(migrated_db, "*", guild_id=GUILD, now=LATER)

    assert allow_all.old_effect == DENY
    note = "cleared by /perm grant *"
    assert audit_rows(migrated_db) == [
        audit("feed.*", None, ALLOW, guild_id=GUILD),
        audit(
            "feed.list",
            ALLOW,
            None,
            "cleared by /perm grant feed.*",
            guild_id=GUILD,
        ),
        audit(
            "feed.remove",
            DENY,
            None,
            "cleared by /perm grant feed.*",
            guild_id=GUILD,
        ),
        audit("cat.view", None, ALLOW, guild_id=GUILD),
        audit("*", DENY, ALLOW, guild_id=GUILD),
        audit("cat.view", ALLOW, None, note, guild_id=GUILD),
        audit("feed.*", ALLOW, None, note, guild_id=GUILD),
    ]


def test_deny_wildcard_note(migrated_db):
    grant(migrated_db, "feed.list")
    clear_audit(migrated_db)

    deny(migrated_db, "*", now=LATER)

    assert audit_rows(migrated_db)[-1][-1] == "cleared by /perm deny *"


def test_reset_wildcard_removes_row_and_covered(migrated_db):
    deny(migrated_db, "feed.*")
    grant(migrated_db, "feed.remove")
    grant(migrated_db, "cat.view")
    clear_audit(migrated_db)

    result = reset(migrated_db, "feed.*", now=LATER)

    assert result == OverrideChange(
        "feed.*",
        None,
        DENY,
        True,
        (ClearedOverride("feed.remove", ALLOW),),
    )
    assert keys_for(migrated_db) == {"cat.view": ALLOW}
    assert audit_rows(migrated_db) == [
        audit("feed.*", DENY, None),
        audit("feed.remove", ALLOW, None, "cleared by /perm reset feed.*"),
    ]


def test_reset_wildcard_with_only_covered_rows(migrated_db):
    grant(migrated_db, "feed.remove")
    grant(migrated_db, "cat.*")
    clear_audit(migrated_db)

    result = reset(migrated_db, "*", now=LATER)

    assert result.old_effect is None
    assert result.changed is True
    assert result.cleared == (
        ClearedOverride("cat.*", ALLOW),
        ClearedOverride("feed.remove", ALLOW),
    )
    assert override_rows(migrated_db) == []
    note = "cleared by /perm reset *"
    assert audit_rows(migrated_db) == [
        audit("cat.*", ALLOW, None, note),
        audit("feed.remove", ALLOW, None, note),
    ]


def test_reset_wildcard_with_nothing_is_noop(migrated_db):
    grant(migrated_db, "cat.view")
    before = snapshot(migrated_db)

    result = reset(migrated_db, "feed.*", now=LATER)

    assert result == OverrideChange("feed.*", None, None, False, ())
    assert snapshot(migrated_db) == before


# Design sequences, end to end


def test_deny_area_then_grant_one_key(migrated_db):
    deny(migrated_db, "feed.*")
    grant(migrated_db, "feed.remove")

    assert keys_for(migrated_db) == {"feed.*": DENY, "feed.remove": ALLOW}
    assert check(migrated_db, "feed.remove").allowed is True
    assert check(migrated_db, "feed.list").allowed is False
    assert check(migrated_db, "feed.notify").allowed is False
    assert check(migrated_db, "cat.view").allowed is True


def test_grant_key_then_deny_area(migrated_db):
    grant(migrated_db, "feed.remove")
    deny(migrated_db, "feed.*")

    assert keys_for(migrated_db) == {"feed.*": DENY}
    decision = check(migrated_db, "feed.remove")
    assert decision.allowed is False
    assert decision.source is DecisionSource.OVERRIDE


def test_grant_all_then_deny_one_key(migrated_db):
    grant(migrated_db, "*")
    deny(migrated_db, "cat.view")

    assert keys_for(migrated_db) == {"*": ALLOW, "cat.view": DENY}
    assert check(migrated_db, "cat.view").allowed is False
    assert check(migrated_db, "feed.remove").allowed is True


def test_deny_all_after_area_allow(migrated_db):
    grant(migrated_db, "feed.*")
    deny(migrated_db, "*")

    assert keys_for(migrated_db) == {"*": DENY}
    assert check(migrated_db, "feed.list").allowed is False
    assert check(migrated_db, "cat.view").allowed is False


def test_all_deny_leaves_protected_and_server_rows(migrated_db):
    grant(migrated_db, "feed.list", guild_id=GUILD)
    deny(migrated_db, "*")

    assert check(migrated_db, "memory.view").allowed is True
    # The server row is a different scope and is not cleared.
    assert check(migrated_db, "feed.list").allowed is True
    assert check(migrated_db, "feed.list", guild_id=0).allowed is False


# Underscores


def test_underscore_area_matches_exactly(migrated_db):
    insert_row(migrated_db, "cat_photos.add", ALLOW)
    insert_row(migrated_db, "catXphotos.add", ALLOW)
    insert_row(migrated_db, "catxphotos.add", ALLOW)

    assert covered(migrated_db, "cat_photos.*") == (
        ClearedOverride("cat_photos.add", ALLOW),
    )


def test_underscore_area_wildcard_clears_only_its_area(migrated_db):
    grant(migrated_db, "cat_photos.add")
    grant(migrated_db, "catxphotos.add")

    result = deny(migrated_db, "cat_photos.*", now=LATER)

    assert result.cleared == (ClearedOverride("cat_photos.add", ALLOW),)
    assert keys_for(migrated_db) == {
        "cat_photos.*": DENY,
        "catxphotos.add": ALLOW,
    }


# Validation


def test_owner_is_refused(migrated_db):
    with pytest.raises(PermissionChangeError, match="owner"):
        grant(migrated_db, "feed.remove", user_id=OWNER)
    with pytest.raises(PermissionChangeError, match="owner"):
        reset(migrated_db, "feed.remove", user_id=OWNER)
    assert snapshot(migrated_db) == ([], [])


@pytest.mark.parametrize(
    "pattern",
    [
        "",
        "feed",
        "feed.",
        ".remove",
        "Feed.remove",
        "feed.remove.x",
        "feed*",
        "feed.re*",
        "*.remove",
        "**",
        "feed.%",
        None,
    ],
)
def test_malformed_pattern_is_refused(migrated_db, pattern):
    with pytest.raises(PermissionChangeError, match="is not a permission"):
        grant(migrated_db, pattern)
    with pytest.raises(PermissionChangeError, match="is not a permission"):
        reset(migrated_db, pattern)
    assert snapshot(migrated_db) == ([], [])


def test_unknown_exact_key_is_refused(migrated_db):
    with pytest.raises(PermissionChangeError, match="no permission key"):
        deny(migrated_db, "feed.unknown")
    with pytest.raises(PermissionChangeError, match="no permission key"):
        reset(migrated_db, "feed.unknown")
    assert snapshot(migrated_db) == ([], [])


def test_area_with_no_keys_is_refused(migrated_db):
    with pytest.raises(PermissionChangeError, match="music area"):
        deny(migrated_db, "music.*")
    with pytest.raises(PermissionChangeError, match="music area"):
        reset(migrated_db, "music.*")
    assert snapshot(migrated_db) == ([], [])


@pytest.mark.parametrize("pattern", ["mood.set", "perm.level"])
@pytest.mark.parametrize("effect", [ALLOW, DENY])
def test_owner_only_key_is_refused(migrated_db, pattern, effect):
    with pytest.raises(PermissionChangeError, match="cannot be granted"):
        set_override(
            migrated_db,
            REGISTRY,
            user_id=ALEX,
            pattern=pattern,
            effect=effect,
            owner_id=OWNER,
            actor_id=ACTOR,
            now=NOW,
        )
    assert snapshot(migrated_db) == ([], [])


@pytest.mark.parametrize("effect", [ALLOW, DENY])
def test_owner_only_area_is_refused(migrated_db, effect):
    with pytest.raises(PermissionChangeError, match="perm area"):
        set_override(
            migrated_db,
            REGISTRY,
            user_id=ALEX,
            pattern="perm.*",
            effect=effect,
            owner_id=OWNER,
            actor_id=ACTOR,
            now=NOW,
        )
    assert snapshot(migrated_db) == ([], [])


def test_area_with_some_owner_only_keys_is_allowed(migrated_db):
    result = deny(migrated_db, "mood.*")
    assert result.changed is True
    assert check(migrated_db, "mood.view").allowed is False


@pytest.mark.parametrize("effect", [ALLOW, DENY])
def test_protected_key_is_refused(migrated_db, effect):
    with pytest.raises(PermissionChangeError, match="always allowed"):
        set_override(
            migrated_db,
            REGISTRY,
            user_id=ALEX,
            pattern="memory.view",
            effect=effect,
            owner_id=OWNER,
            actor_id=ACTOR,
            now=NOW,
        )
    assert snapshot(migrated_db) == ([], [])


@pytest.mark.parametrize("pattern", ["mood.set", "perm.*", "memory.view"])
def test_reset_of_fixed_pattern_is_noop(migrated_db, pattern):
    result = reset(migrated_db, pattern)

    assert result == OverrideChange(pattern, None, None, False, ())
    assert snapshot(migrated_db) == ([], [])


def test_negative_guild_is_refused(migrated_db):
    with pytest.raises(ValueError, match="guild_id"):
        grant(migrated_db, "feed.list", guild_id=-1)
    with pytest.raises(ValueError, match="guild_id"):
        reset(migrated_db, "feed.list", guild_id=-1)
    assert snapshot(migrated_db) == ([], [])


@pytest.mark.parametrize("effect", ["ALLOW", "grant", "", None])
def test_unknown_effect_is_refused(migrated_db, effect):
    with pytest.raises(ValueError, match="Unknown override effect"):
        set_override(
            migrated_db,
            REGISTRY,
            user_id=ALEX,
            pattern="feed.list",
            effect=effect,
            owner_id=OWNER,
            actor_id=ACTOR,
            now=NOW,
        )
    assert snapshot(migrated_db) == ([], [])


# A failing audit write


@pytest.mark.parametrize(
    ("pattern", "effect"),
    [("feed.remove", DENY), ("feed.*", DENY), ("*", ALLOW), ("feed.*", ALLOW)],
)
def test_set_override_audit_failure_leaves_rows(migrated_db, pattern, effect):
    deny(migrated_db, "feed.*")
    grant(migrated_db, "feed.remove")
    grant(migrated_db, "cat.view")
    before = snapshot(migrated_db)

    with mock.patch(AUDIT_FAILURE, side_effect=RuntimeError("audit failed")):
        with pytest.raises(RuntimeError, match="audit failed"):
            set_override(
                migrated_db,
                REGISTRY,
                user_id=ALEX,
                pattern=pattern,
                effect=effect,
                owner_id=OWNER,
                actor_id=ACTOR,
                now=LATER,
            )

    assert not migrated_db.in_transaction
    assert snapshot(migrated_db) == before


@pytest.mark.parametrize("pattern", ["feed.remove", "feed.*", "*"])
def test_reset_override_audit_failure_leaves_rows(migrated_db, pattern):
    deny(migrated_db, "feed.*")
    grant(migrated_db, "feed.remove")
    grant(migrated_db, "cat.view")
    before = snapshot(migrated_db)

    with mock.patch(AUDIT_FAILURE, side_effect=RuntimeError("audit failed")):
        with pytest.raises(RuntimeError, match="audit failed"):
            reset(migrated_db, pattern, now=LATER)

    assert not migrated_db.in_transaction
    assert snapshot(migrated_db) == before


# find_key_overrides


def test_find_key_overrides_every_scope_global_first(migrated_db):
    deny(migrated_db, "feed.*", guild_id=OTHER_GUILD)
    grant(migrated_db, "feed.*", guild_id=GUILD)
    deny(migrated_db, "feed.*")

    rows = find_key_overrides(migrated_db, user_id=ALEX, key="feed.*")

    assert rows == (
        ScopedOverride(0, DENY),
        ScopedOverride(GUILD, ALLOW),
        ScopedOverride(OTHER_GUILD, DENY),
    )


def test_find_key_overrides_matches_key_and_person_exactly(migrated_db):
    grant(migrated_db, "feed.remove")
    grant(migrated_db, "*")
    deny(migrated_db, "feed.*", user_id=SAM)

    assert find_key_overrides(migrated_db, user_id=ALEX, key="feed.*") == ()
    assert find_key_overrides(migrated_db, user_id=SAM, key="feed.*") == (
        ScopedOverride(0, DENY),
    )


def test_find_key_overrides_writes_nothing(migrated_db):
    grant(migrated_db, "cat.view")
    before = snapshot(migrated_db)

    find_key_overrides(migrated_db, user_id=ALEX, key="cat.view")

    assert snapshot(migrated_db) == before
    assert not migrated_db.in_transaction
