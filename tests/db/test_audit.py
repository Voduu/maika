import pytest

from maika.audit import AUDIT_KINDS, AuditError, write_audit_entry
from maika.database import transaction

NOW = 1_800_000_000
OWNER = 111
TARGET = 222
GUILD = 333

DESIGN_KINDS = {
    "level",
    "override",
    "block",
    "setting",
    "photo",
    "memory",
    "relationship",
    "nickname",
    "say",
}


def audit_rows(connection):
    rows = connection.execute(
        "SELECT logged_at, kind, actor_id, target_id, key, guild_id, "
        "old_value, new_value, note FROM audit_log ORDER BY id"
    ).fetchall()
    return [tuple(row) for row in rows]


def test_optional_fields_stored_as_null(migrated_db):
    with transaction(migrated_db):
        write_audit_entry(migrated_db, kind="setting", actor_id=OWNER, now=NOW)

    assert audit_rows(migrated_db) == [
        (NOW, "setting", OWNER, None, None, 0, None, None, None)
    ]


def test_all_fields_stored(migrated_db):
    with transaction(migrated_db):
        write_audit_entry(
            migrated_db,
            kind="level",
            actor_id=OWNER,
            now=NOW,
            target_id=TARGET,
            key="perm.level",
            guild_id=GUILD,
            old_value="1",
            new_value="2",
            note="promoted",
        )

    assert audit_rows(migrated_db) == [
        (
            NOW,
            "level",
            OWNER,
            TARGET,
            "perm.level",
            GUILD,
            "1",
            "2",
            "promoted",
        )
    ]


def test_does_not_commit_the_callers_transaction(migrated_db):
    migrated_db.execute("BEGIN")
    write_audit_entry(migrated_db, kind="setting", actor_id=OWNER, now=NOW)
    assert migrated_db.in_transaction
    migrated_db.execute("ROLLBACK")
    assert audit_rows(migrated_db) == []


def test_rolled_back_with_callers_transaction(migrated_db):
    with pytest.raises(RuntimeError, match="caller failed"):
        with transaction(migrated_db):
            write_audit_entry(
                migrated_db, kind="setting", actor_id=OWNER, now=NOW
            )
            raise RuntimeError("caller failed")
    assert audit_rows(migrated_db) == []


def test_outside_a_transaction_raises(migrated_db):
    with pytest.raises(AuditError, match="must start one"):
        write_audit_entry(migrated_db, kind="setting", actor_id=OWNER, now=NOW)
    assert audit_rows(migrated_db) == []
    assert not migrated_db.in_transaction


def test_audit_kinds_match_design():
    assert set(AUDIT_KINDS) == DESIGN_KINDS
    assert len(AUDIT_KINDS) == len(DESIGN_KINDS)


@pytest.mark.parametrize("kind", AUDIT_KINDS)
def test_every_listed_kind_is_accepted(migrated_db, kind):
    with transaction(migrated_db):
        write_audit_entry(migrated_db, kind=kind, actor_id=OWNER, now=NOW)
    assert audit_rows(migrated_db)[0][1] == kind


@pytest.mark.parametrize("kind", ["settings", "Setting", "", "event_reset"])
def test_unknown_kind_raises(migrated_db, kind):
    with transaction(migrated_db):
        with pytest.raises(ValueError, match=repr(kind)):
            write_audit_entry(migrated_db, kind=kind, actor_id=OWNER, now=NOW)
    assert audit_rows(migrated_db) == []
