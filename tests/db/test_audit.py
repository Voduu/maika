import pytest

from maika.audit import (
    AUDIT_KINDS,
    PERMISSION_AUDIT_KINDS,
    SELECT_TARGET_ENTRIES_SQL,
    AuditEntry,
    AuditError,
    count_target_entries,
    find_target_entries,
    write_audit_entry,
)
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


# find_target_entries and count_target_entries


def write(connection, kind, now, target_id=TARGET, **fields):
    with transaction(connection):
        write_audit_entry(
            connection,
            kind=kind,
            actor_id=OWNER,
            now=now,
            target_id=target_id,
            **fields,
        )


def test_permission_kinds():
    assert PERMISSION_AUDIT_KINDS == ("level", "override", "block")
    assert set(PERMISSION_AUDIT_KINDS) <= set(AUDIT_KINDS)


def test_find_target_entries_newest_first(migrated_db):
    write(migrated_db, "level", NOW, new_value="2")
    write(migrated_db, "block", NOW + 20, guild_id=GUILD, new_value="all")
    write(migrated_db, "override", NOW + 10, key="cat.view")
    # Same second as the block: the one written later comes first.
    write(migrated_db, "level", NOW + 20, old_value="2")

    entries = find_target_entries(migrated_db, target_id=TARGET, limit=10)

    assert [(e.logged_at, e.kind) for e in entries] == [
        (NOW + 20, "level"),
        (NOW + 20, "block"),
        (NOW + 10, "override"),
        (NOW, "level"),
    ]
    assert entries[1] == AuditEntry(
        logged_at=NOW + 20,
        kind="block",
        actor_id=OWNER,
        target_id=TARGET,
        key=None,
        guild_id=GUILD,
        old_value=None,
        new_value="all",
        note=None,
    )


def test_find_target_entries_limit_and_count(migrated_db):
    for offset in range(5):
        write(migrated_db, "level", NOW + offset, new_value="1")

    entries = find_target_entries(migrated_db, target_id=TARGET, limit=2)

    assert [e.logged_at for e in entries] == [NOW + 4, NOW + 3]
    assert count_target_entries(migrated_db, target_id=TARGET) == 5


def test_find_target_entries_only_permission_kinds_and_person(migrated_db):
    write(migrated_db, "setting", NOW, key="backups.days_kept")
    write(migrated_db, "memory", NOW)
    write(migrated_db, "level", NOW, target_id=OWNER, new_value="1")
    write(migrated_db, "override", NOW, key="cat.view", new_value="deny")

    entries = find_target_entries(migrated_db, target_id=TARGET, limit=10)

    assert [e.kind for e in entries] == ["override"]
    assert count_target_entries(migrated_db, target_id=TARGET) == 1


def test_find_target_entries_none(migrated_db):
    assert find_target_entries(migrated_db, target_id=TARGET, limit=5) == ()
    assert count_target_entries(migrated_db, target_id=TARGET) == 0


def test_reads_write_nothing(migrated_db):
    write(migrated_db, "level", NOW, new_value="2")
    changes_before = migrated_db.total_changes

    find_target_entries(migrated_db, target_id=TARGET, limit=5)
    count_target_entries(migrated_db, target_id=TARGET)

    assert migrated_db.total_changes == changes_before
    assert not migrated_db.in_transaction


def test_find_target_entries_uses_target_index(migrated_db):
    plan = migrated_db.execute(
        "EXPLAIN QUERY PLAN " + SELECT_TARGET_ENTRIES_SQL,
        (TARGET, *PERMISSION_AUDIT_KINDS, 5),
    ).fetchall()

    assert any("audit_log_target" in row["detail"] for row in plan)
