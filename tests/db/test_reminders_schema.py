import shutil

from maika.database import open_database
from maika.migrate import apply_migrations, find_migrations

NOW = 1_800_000_000
LATER = NOW + 60
USER = 200


def names_of(connection, kind):
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = ?", (kind,)
    ).fetchall()
    return {row[0] for row in rows}


def versions_of(connection):
    rows = connection.execute(
        "SELECT version FROM schema_migrations ORDER BY version"
    ).fetchall()
    return [row[0] for row in rows]


def test_real_migrations_include_0002(real_migrations_dir):
    found = find_migrations(real_migrations_dir)
    assert [(m.version, m.name) for m in found[:2]] == [
        (1, "foundation"),
        (2, "reminders"),
    ]


def test_fresh_database_has_both_tables(migrated_db):
    tables = names_of(migrated_db, "table")
    assert {"reminders", "user_settings"} <= tables


def test_reminders_due_index_exists(migrated_db):
    row = migrated_db.execute(
        "SELECT tbl_name FROM sqlite_master "
        "WHERE type = 'index' AND name = 'reminders_due'"
    ).fetchone()
    assert row is not None
    assert row["tbl_name"] == "reminders"
    columns = [
        info["name"]
        for info in migrated_db.execute(
            "PRAGMA index_info(reminders_due)"
        ).fetchall()
    ]
    assert columns == ["status", "due_at"]


def test_schema_migrations_has_versions_1_and_2(migrated_db):
    assert versions_of(migrated_db)[:2] == [1, 2]


def test_reminder_status_defaults_to_pending(migrated_db):
    migrated_db.execute(
        "INSERT INTO reminders "
        "(user_id, message, due_at, delivery, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (USER, "submit assignment", LATER, "dm", NOW),
    )
    row = migrated_db.execute("SELECT status FROM reminders").fetchone()
    assert row["status"] == "pending"


def test_0002_applies_to_database_with_0001(tmp_path, real_migrations_dir):
    only_first = tmp_path / "only_first"
    only_first.mkdir()
    shutil.copy(
        real_migrations_dir / "0001_foundation.sql",
        only_first / "0001_foundation.sql",
    )
    database_path = tmp_path / "maika.db"
    backup_dir = tmp_path / "backups"

    connection = open_database(database_path)
    try:
        apply_migrations(connection, only_first, backup_dir, NOW)
        assert versions_of(connection) == [1]
        assert "user_settings" not in names_of(connection, "table")
        connection.execute(
            "INSERT INTO bot_state (key, value, updated_at) "
            "VALUES ('first_boot', 'x', ?)",
            (NOW,),
        )

        applied = apply_migrations(
            connection, real_migrations_dir, backup_dir, LATER
        )

        assert applied[0].version == 2
        assert versions_of(connection)[:2] == [1, 2]
        tables = names_of(connection, "table")
        assert {"reminders", "user_settings"} <= tables
        assert "reminders_due" in names_of(connection, "index")
        row = connection.execute(
            "SELECT value FROM bot_state WHERE key = 'first_boot'"
        ).fetchone()
        assert row["value"] == "x"
    finally:
        connection.close()

    backups = [path.name for path in backup_dir.iterdir()]
    assert len(backups) == 1
    assert backups[0].endswith("-before-0002.db")
