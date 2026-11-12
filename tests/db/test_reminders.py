from maika.reminders import count_pending, create_reminder, get_reminder

NOW = 1_800_000_000
ALEX = 100
SAM = 200
CHANNEL = 7000


def add(connection, user_id=ALEX, **changes):
    fields = {
        "user_id": user_id,
        "message": "submit assignment",
        "due_at": NOW + 7200,
        "delivery": "dm",
        "channel_id": CHANNEL,
        "repeat_rule": None,
        "timezone": None,
        "now": NOW,
    }
    fields.update(changes)
    return create_reminder(connection, **fields)


def set_status(connection, reminder_id, status):
    connection.execute(
        "UPDATE reminders SET status = ? WHERE id = ?", (status, reminder_id)
    )


def test_create_returns_id_and_stores_fields(migrated_db):
    reminder_id = add(
        migrated_db,
        delivery="channel",
        repeat_rule="FREQ=DAILY;BYHOUR=9;BYMINUTE=0;BYSECOND=0",
        timezone="America/Los_Angeles",
    )

    row = get_reminder(migrated_db, reminder_id)
    assert dict(row) == {
        "id": reminder_id,
        "user_id": ALEX,
        "message": "submit assignment",
        "due_at": NOW + 7200,
        "delivery": "channel",
        "channel_id": CHANNEL,
        "created_at": NOW,
        "repeat_rule": "FREQ=DAILY;BYHOUR=9;BYMINUTE=0;BYSECOND=0",
        "timezone": "America/Los_Angeles",
        "status": "pending",
        "delivered_at": None,
        "delivered_via": None,
    }


def test_create_in_a_dm_has_no_channel(migrated_db):
    reminder_id = add(migrated_db, channel_id=None)
    assert get_reminder(migrated_db, reminder_id)["channel_id"] is None


def test_ids_differ(migrated_db):
    assert add(migrated_db) != add(migrated_db)


def test_get_missing_reminder(migrated_db):
    assert get_reminder(migrated_db, 999) is None


def test_count_pending_counts_only_own_pending_rows(migrated_db):
    add(migrated_db)
    add(migrated_db)
    for status in ("delivered", "failed", "cancelled"):
        set_status(migrated_db, add(migrated_db), status)
    add(migrated_db, user_id=SAM)

    assert count_pending(migrated_db, ALEX) == 2
    assert count_pending(migrated_db, SAM) == 1
    assert count_pending(migrated_db, 999) == 0
