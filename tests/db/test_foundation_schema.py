import sqlite3

import pytest

from maika.migrate import find_migrations

FOUNDATION_TABLES = {
    "users",
    "permission_overrides",
    "blocks",
    "audit_log",
    "settings",
    "cooldowns",
    "bot_state",
}

NOW = 1_800_000_000
OWNER = 100
USER = 200
GUILD = 300


def names_of(connection, kind):
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = ?", (kind,)
    ).fetchall()
    return {row[0] for row in rows}


def test_real_migrations_pass_find_migrations(real_migrations_dir):
    found = find_migrations(real_migrations_dir)
    assert found
    assert found[0].version == 1
    assert found[0].name == "foundation"


def test_foundation_tables_exist(migrated_db):
    tables = names_of(migrated_db, "table")
    assert FOUNDATION_TABLES <= tables
    assert "schema_migrations" in tables


def test_audit_log_index_exists(migrated_db):
    row = migrated_db.execute(
        "SELECT tbl_name FROM sqlite_master "
        "WHERE type = 'index' AND name = 'audit_log_target'"
    ).fetchone()
    assert row is not None
    assert row["tbl_name"] == "audit_log"
    columns = [
        info["name"]
        for info in migrated_db.execute(
            "PRAGMA index_info(audit_log_target)"
        ).fetchall()
    ]
    assert columns == ["target_id", "logged_at"]


# users


def insert_user(connection, level, discord_id=USER):
    connection.execute(
        "INSERT INTO users (discord_id, level, updated_at) VALUES (?, ?, ?)",
        (discord_id, level, NOW),
    )


@pytest.mark.parametrize("level", [0, 4])
def test_users_rejects_level_out_of_range(migrated_db, level):
    with pytest.raises(sqlite3.IntegrityError):
        insert_user(migrated_db, level)


@pytest.mark.parametrize("level", [1, 2, 3])
def test_users_accepts_levels_1_to_3(migrated_db, level):
    insert_user(migrated_db, level)
    row = migrated_db.execute(
        "SELECT level FROM users WHERE discord_id = ?", (USER,)
    ).fetchone()
    assert row["level"] == level


# permission_overrides


def insert_override(connection, effect, key="cat.view", guild_id=GUILD):
    connection.execute(
        "INSERT INTO permission_overrides "
        "(user_id, key, effect, guild_id, set_by, set_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (USER, key, effect, guild_id, OWNER, NOW),
    )


@pytest.mark.parametrize("effect", ["allow", "deny"])
def test_overrides_accept_allow_and_deny(migrated_db, effect):
    insert_override(migrated_db, effect)


@pytest.mark.parametrize("effect", ["grant", "ALLOW", ""])
def test_overrides_reject_other_effects(migrated_db, effect):
    with pytest.raises(sqlite3.IntegrityError):
        insert_override(migrated_db, effect)


def test_overrides_reject_duplicate(migrated_db):
    insert_override(migrated_db, "allow")
    with pytest.raises(sqlite3.IntegrityError):
        insert_override(migrated_db, "deny")


def test_overrides_allow_same_key_in_other_guild(migrated_db):
    insert_override(migrated_db, "allow", guild_id=GUILD)
    insert_override(migrated_db, "deny", guild_id=0)


# blocks


def insert_block(connection, kind, guild_id=GUILD):
    connection.execute(
        "INSERT INTO blocks (user_id, guild_id, kind, set_by, set_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (USER, guild_id, kind, OWNER, NOW),
    )


@pytest.mark.parametrize("kind", ["chat", "all"])
def test_blocks_accept_chat_and_all(migrated_db, kind):
    insert_block(migrated_db, kind)


@pytest.mark.parametrize("kind", ["commands", "CHAT", ""])
def test_blocks_reject_other_kinds(migrated_db, kind):
    with pytest.raises(sqlite3.IntegrityError):
        insert_block(migrated_db, kind)


def test_blocks_one_row_per_user_and_guild(migrated_db):
    insert_block(migrated_db, "chat")
    with pytest.raises(sqlite3.IntegrityError):
        insert_block(migrated_db, "all")


# settings


def insert_setting(connection, guild_id=GUILD, value="10"):
    connection.execute(
        "INSERT INTO settings "
        "(key, guild_id, value, updated_at, updated_by) "
        "VALUES (?, ?, ?, ?, ?)",
        ("presence.idle_minutes", guild_id, value, NOW, OWNER),
    )


def test_settings_one_row_per_key_and_guild(migrated_db):
    insert_setting(migrated_db)
    with pytest.raises(sqlite3.IntegrityError):
        insert_setting(migrated_db, value="20")


def test_settings_same_key_global_and_guild(migrated_db):
    insert_setting(migrated_db, guild_id=GUILD)
    insert_setting(migrated_db, guild_id=0)


# guild_id and scope_id defaults


@pytest.mark.parametrize(
    ("insert_sql", "values", "select_sql"),
    [
        (
            "INSERT INTO permission_overrides "
            "(user_id, key, effect, set_by, set_at) VALUES (?, ?, ?, ?, ?)",
            (USER, "cat.view", "allow", OWNER, NOW),
            "SELECT guild_id FROM permission_overrides",
        ),
        (
            "INSERT INTO blocks (user_id, kind, set_by, set_at) "
            "VALUES (?, ?, ?, ?)",
            (USER, "chat", OWNER, NOW),
            "SELECT guild_id FROM blocks",
        ),
        (
            "INSERT INTO audit_log (logged_at, kind, actor_id) "
            "VALUES (?, ?, ?)",
            (NOW, "level", OWNER),
            "SELECT guild_id FROM audit_log",
        ),
        (
            "INSERT INTO settings (key, value, updated_at, updated_by) "
            "VALUES (?, ?, ?, ?)",
            ("presence.idle_minutes", "10", NOW, OWNER),
            "SELECT guild_id FROM settings",
        ),
        (
            "INSERT INTO cooldowns (scope, key, expires_at) VALUES (?, ?, ?)",
            ("global", "typo", NOW),
            "SELECT scope_id FROM cooldowns",
        ),
    ],
    ids=["overrides", "blocks", "audit_log", "settings", "cooldowns"],
)
def test_guild_and_scope_ids_default_to_zero(
    migrated_db, insert_sql, values, select_sql
):
    migrated_db.execute(insert_sql, values)
    assert migrated_db.execute(select_sql).fetchone()[0] == 0


# cooldowns


def test_cooldowns_accept_null_expires_at(migrated_db):
    migrated_db.execute(
        "INSERT INTO cooldowns (scope, scope_id, key, expires_at) "
        "VALUES (?, ?, ?, NULL)",
        ("user", USER, "nickname_use"),
    )
    row = migrated_db.execute("SELECT expires_at FROM cooldowns").fetchone()
    assert row["expires_at"] is None


# audit_log


def test_audit_log_accepts_null_optional_columns(migrated_db):
    migrated_db.execute(
        "INSERT INTO audit_log "
        "(logged_at, kind, actor_id, target_id, key, guild_id, "
        "old_value, new_value, note) "
        "VALUES (?, ?, ?, NULL, NULL, ?, NULL, NULL, NULL)",
        (NOW, "setting", OWNER, GUILD),
    )
    row = migrated_db.execute(
        "SELECT target_id, key, old_value, new_value, note FROM audit_log"
    ).fetchone()
    assert tuple(row) == (None, None, None, None, None)
