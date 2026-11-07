import datetime
import logging

import pytest

from maika import audit
from maika.foundation_settings import FOUNDATION_SETTINGS
from maika.settings import (
    SettingDefinition,
    SettingLevel,
    SettingsRegistry,
    SettingValueError,
    UnknownSettingError,
    ValueType,
)
from maika.settings_store import (
    SettingChange,
    get_value,
    reset_value,
    set_value,
)

NOW = 1_800_000_000
LATER = NOW + 60
OWNER = 111
OTHER_ADMIN = 222
GUILD = 333
OTHER_GUILD = 444

SERVER_KEY = "test.server_count"
GLOBAL_KEY = "backups.days_kept"

SERVER_SETTING = SettingDefinition(
    key=SERVER_KEY,
    category="test",
    level=SettingLevel.BASIC,
    server_scope=True,
    value_type=ValueType.INT,
    default=5,
    minimum=1,
    maximum=100,
    description="A server-scope setting used by the tests.",
)


@pytest.fixture
def registry():
    return SettingsRegistry([*FOUNDATION_SETTINGS, SERVER_SETTING])


def insert_row(connection, key, guild_id, value):
    connection.execute(
        "INSERT INTO settings (key, guild_id, value, updated_at, updated_by) "
        "VALUES (?, ?, ?, ?, ?)",
        (key, guild_id, value, NOW, OWNER),
    )


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


# get_value


def test_get_default_with_no_row(migrated_db, registry):
    assert get_value(migrated_db, registry, GLOBAL_KEY) == 30
    assert get_value(migrated_db, registry, "backups.time") == (
        datetime.time(4, 0)
    )
    assert get_value(migrated_db, registry, "log.channel.errors") is None


def test_get_global_row(migrated_db, registry):
    insert_row(migrated_db, GLOBAL_KEY, 0, "60")
    assert get_value(migrated_db, registry, GLOBAL_KEY) == 60


def test_get_server_row_beats_global(migrated_db, registry):
    insert_row(migrated_db, SERVER_KEY, 0, "10")
    insert_row(migrated_db, SERVER_KEY, GUILD, "20")
    assert get_value(migrated_db, registry, SERVER_KEY, GUILD) == 20
    assert get_value(migrated_db, registry, SERVER_KEY, OTHER_GUILD) == 10
    assert get_value(migrated_db, registry, SERVER_KEY) == 10


def test_get_server_falls_back_to_default(migrated_db, registry):
    assert get_value(migrated_db, registry, SERVER_KEY, GUILD) == 5


def test_get_ignores_server_row_for_global_setting(migrated_db, registry):
    insert_row(migrated_db, GLOBAL_KEY, 0, "60")
    insert_row(migrated_db, GLOBAL_KEY, GUILD, "90")
    assert get_value(migrated_db, registry, GLOBAL_KEY, GUILD) == 60


def test_get_unknown_key(migrated_db, registry):
    with pytest.raises(UnknownSettingError):
        get_value(migrated_db, registry, "no.such_key")


@pytest.mark.parametrize("bad_text", ["secret-banana", "9999"])
def test_get_corrupt_text_returns_default_and_warns(
    migrated_db, registry, caplog, bad_text
):
    insert_row(migrated_db, GLOBAL_KEY, 0, bad_text)

    with caplog.at_level(logging.WARNING):
        assert get_value(migrated_db, registry, GLOBAL_KEY) == 30

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert GLOBAL_KEY in message
    assert "guild 0" in message
    assert bad_text not in caplog.text


def test_get_corrupt_server_text_names_the_guild(
    migrated_db, registry, caplog
):
    insert_row(migrated_db, SERVER_KEY, GUILD, "lots")

    with caplog.at_level(logging.WARNING):
        assert get_value(migrated_db, registry, SERVER_KEY, GUILD) == 5

    assert f"guild {GUILD}" in caplog.text
    assert "lots" not in caplog.text


def test_get_corrupt_server_row_falls_back_to_global(
    migrated_db, registry, caplog
):
    insert_row(migrated_db, SERVER_KEY, 0, "10")
    insert_row(migrated_db, SERVER_KEY, GUILD, "lots")

    with caplog.at_level(logging.WARNING):
        assert get_value(migrated_db, registry, SERVER_KEY, GUILD) == 10

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert f"guild {GUILD}" in warnings[0].getMessage()
    assert "lots" not in caplog.text


def test_get_corrupt_server_and_global_rows_return_default(
    migrated_db, registry, caplog
):
    insert_row(migrated_db, SERVER_KEY, 0, "zero")
    insert_row(migrated_db, SERVER_KEY, GUILD, "lots")

    with caplog.at_level(logging.WARNING):
        assert get_value(migrated_db, registry, SERVER_KEY, GUILD) == 5

    messages = [
        r.getMessage() for r in caplog.records if r.levelno == logging.WARNING
    ]
    assert len(messages) == 2
    assert f"guild {GUILD}" in messages[0]
    assert "guild 0" in messages[1]
    assert "lots" not in caplog.text
    assert "zero" not in caplog.text


# set_value


def test_set_writes_row_and_audit(migrated_db, registry):
    change = set_value(
        migrated_db, registry, GLOBAL_KEY, " 45 ", actor_id=OWNER, now=NOW
    )

    assert change == SettingChange(GLOBAL_KEY, 0, None, "45", True)
    assert settings_rows(migrated_db) == [(GLOBAL_KEY, 0, "45", NOW, OWNER)]
    assert audit_rows(migrated_db) == [
        (NOW, "setting", OWNER, None, GLOBAL_KEY, 0, None, "45", None)
    ]
    assert get_value(migrated_db, registry, GLOBAL_KEY) == 45
    assert not migrated_db.in_transaction


def test_set_server_row(migrated_db, registry):
    change = set_value(
        migrated_db,
        registry,
        SERVER_KEY,
        "7",
        guild_id=GUILD,
        actor_id=OWNER,
        now=NOW,
    )

    assert change == SettingChange(SERVER_KEY, GUILD, None, "7", True)
    assert settings_rows(migrated_db) == [(SERVER_KEY, GUILD, "7", NOW, OWNER)]
    assert audit_rows(migrated_db) == [
        (NOW, "setting", OWNER, None, SERVER_KEY, GUILD, None, "7", None)
    ]


def test_second_set_records_old_text(migrated_db, registry):
    set_value(migrated_db, registry, GLOBAL_KEY, "45", actor_id=OWNER, now=NOW)

    change = set_value(
        migrated_db,
        registry,
        GLOBAL_KEY,
        "90",
        actor_id=OTHER_ADMIN,
        now=LATER,
    )

    assert change == SettingChange(GLOBAL_KEY, 0, "45", "90", True)
    assert settings_rows(migrated_db) == [
        (GLOBAL_KEY, 0, "90", LATER, OTHER_ADMIN)
    ]
    assert audit_rows(migrated_db)[1] == (
        LATER,
        "setting",
        OTHER_ADMIN,
        None,
        GLOBAL_KEY,
        0,
        "45",
        "90",
        None,
    )


def test_same_value_again_changes_nothing(migrated_db, registry):
    set_value(
        migrated_db,
        registry,
        "log.level.feeds",
        "warning",
        actor_id=OWNER,
        now=NOW,
    )

    # Different text, same stored form.
    change = set_value(
        migrated_db,
        registry,
        "log.level.feeds",
        "WARNING",
        actor_id=OTHER_ADMIN,
        now=LATER,
    )

    assert change == SettingChange(
        "log.level.feeds", 0, "warning", "warning", False
    )
    assert settings_rows(migrated_db) == [
        ("log.level.feeds", 0, "warning", NOW, OWNER)
    ]
    assert len(audit_rows(migrated_db)) == 1
    assert not migrated_db.in_transaction


@pytest.mark.parametrize("text", ["0", "366", "thirty", ""])
def test_set_invalid_text_changes_nothing(migrated_db, registry, text):
    with pytest.raises(SettingValueError, match=GLOBAL_KEY):
        set_value(
            migrated_db, registry, GLOBAL_KEY, text, actor_id=OWNER, now=NOW
        )
    assert settings_rows(migrated_db) == []
    assert audit_rows(migrated_db) == []


def test_set_server_scope_on_global_setting(migrated_db, registry):
    with pytest.raises(SettingValueError) as caught:
        set_value(
            migrated_db,
            registry,
            GLOBAL_KEY,
            "45",
            guild_id=GUILD,
            actor_id=OWNER,
            now=NOW,
        )
    assert str(caught.value) == (
        "backups.days_kept is a global setting and cannot be set for one "
        "server."
    )
    assert settings_rows(migrated_db) == []
    assert audit_rows(migrated_db) == []


def test_set_unknown_key(migrated_db, registry):
    with pytest.raises(UnknownSettingError):
        set_value(
            migrated_db, registry, "no.such_key", "1", actor_id=OWNER, now=NOW
        )


def test_audit_failure_leaves_no_settings_row(
    migrated_db, registry, monkeypatch
):
    def fail(*args, **kwargs):
        raise RuntimeError("audit write failed")

    monkeypatch.setattr(audit, "write_audit_entry", fail)

    with pytest.raises(RuntimeError, match="audit write failed"):
        set_value(
            migrated_db, registry, GLOBAL_KEY, "45", actor_id=OWNER, now=NOW
        )

    assert settings_rows(migrated_db) == []
    assert audit_rows(migrated_db) == []
    assert not migrated_db.in_transaction


def test_audit_failure_keeps_previous_value(
    migrated_db, registry, monkeypatch
):
    set_value(migrated_db, registry, GLOBAL_KEY, "45", actor_id=OWNER, now=NOW)

    def fail(*args, **kwargs):
        raise RuntimeError("audit write failed")

    monkeypatch.setattr(audit, "write_audit_entry", fail)

    with pytest.raises(RuntimeError):
        set_value(
            migrated_db, registry, GLOBAL_KEY, "90", actor_id=OWNER, now=LATER
        )

    assert settings_rows(migrated_db) == [(GLOBAL_KEY, 0, "45", NOW, OWNER)]
    assert len(audit_rows(migrated_db)) == 1


# reset_value


def test_reset_removes_row_and_audits(migrated_db, registry):
    set_value(migrated_db, registry, GLOBAL_KEY, "45", actor_id=OWNER, now=NOW)

    change = reset_value(
        migrated_db, registry, GLOBAL_KEY, actor_id=OTHER_ADMIN, now=LATER
    )

    assert change == SettingChange(GLOBAL_KEY, 0, "45", None, True)
    assert settings_rows(migrated_db) == []
    assert audit_rows(migrated_db)[1] == (
        LATER,
        "setting",
        OTHER_ADMIN,
        None,
        GLOBAL_KEY,
        0,
        "45",
        None,
        None,
    )
    assert get_value(migrated_db, registry, GLOBAL_KEY) == 30


def test_reset_with_no_row_writes_nothing(migrated_db, registry):
    change = reset_value(
        migrated_db, registry, GLOBAL_KEY, actor_id=OWNER, now=NOW
    )

    assert change == SettingChange(GLOBAL_KEY, 0, None, None, False)
    assert audit_rows(migrated_db) == []
    assert not migrated_db.in_transaction


def test_reset_server_row_keeps_global_row(migrated_db, registry):
    set_value(migrated_db, registry, SERVER_KEY, "10", actor_id=OWNER, now=NOW)
    set_value(
        migrated_db,
        registry,
        SERVER_KEY,
        "20",
        guild_id=GUILD,
        actor_id=OWNER,
        now=NOW,
    )

    reset_value(
        migrated_db,
        registry,
        SERVER_KEY,
        guild_id=GUILD,
        actor_id=OWNER,
        now=LATER,
    )

    assert get_value(migrated_db, registry, SERVER_KEY, GUILD) == 10
    assert settings_rows(migrated_db) == [(SERVER_KEY, 0, "10", NOW, OWNER)]


def test_reset_server_scope_on_global_setting(migrated_db, registry):
    insert_row(migrated_db, GLOBAL_KEY, GUILD, "45")
    with pytest.raises(SettingValueError, match="global setting"):
        reset_value(
            migrated_db,
            registry,
            GLOBAL_KEY,
            guild_id=GUILD,
            actor_id=OWNER,
            now=NOW,
        )
    assert settings_rows(migrated_db) == [
        (GLOBAL_KEY, GUILD, "45", NOW, OWNER)
    ]
    assert audit_rows(migrated_db) == []
