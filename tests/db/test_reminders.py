import pytest

from maika.reminders import (
    CancelOutcome,
    CancelResult,
    cancel_reminder,
    count_pending,
    create_reminder,
    get_reminder,
    list_pending,
    list_recently_failed,
)

DAY = 86400
WEEKLY_RULE = "FREQ=WEEKLY;BYDAY=TU;BYHOUR=9;BYMINUTE=0;BYSECOND=0"

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


# list_pending


def ids(rows):
    return [row["id"] for row in rows]


def test_list_pending_only_own_pending_rows_in_due_order(migrated_db):
    later = add(migrated_db, due_at=NOW + 3 * DAY)
    sooner = add(migrated_db, due_at=NOW + DAY)
    same_time_first = add(migrated_db, due_at=NOW + 2 * DAY)
    same_time_second = add(migrated_db, due_at=NOW + 2 * DAY)
    for status in ("delivered", "failed", "cancelled"):
        set_status(migrated_db, add(migrated_db, due_at=NOW), status)
    add(migrated_db, user_id=SAM, due_at=NOW)

    assert ids(list_pending(migrated_db, ALEX)) == [
        sooner,
        same_time_first,
        same_time_second,
        later,
    ]


def test_list_pending_empty(migrated_db):
    add(migrated_db, user_id=SAM)
    assert list_pending(migrated_db, ALEX) == []


# list_recently_failed


def test_list_recently_failed_respects_cutoff(migrated_db):
    since = NOW - 7 * DAY
    too_old = add(migrated_db, due_at=since - 1)
    at_cutoff = add(migrated_db, due_at=since)
    recent = add(migrated_db, due_at=NOW - DAY)
    for reminder_id in (too_old, at_cutoff, recent):
        set_status(migrated_db, reminder_id, "failed")

    assert ids(list_recently_failed(migrated_db, ALEX, since=since)) == [
        at_cutoff,
        recent,
    ]


def test_list_recently_failed_ignores_other_statuses_and_people(
    migrated_db,
):
    since = NOW - 7 * DAY
    failed = add(migrated_db, due_at=NOW - DAY)
    set_status(migrated_db, failed, "failed")
    add(migrated_db, due_at=NOW - DAY)
    for status in ("delivered", "cancelled"):
        set_status(migrated_db, add(migrated_db, due_at=NOW - DAY), status)
    set_status(
        migrated_db, add(migrated_db, user_id=SAM, due_at=NOW - DAY), "failed"
    )

    assert ids(list_recently_failed(migrated_db, ALEX, since=since)) == [
        failed
    ]


def test_list_recently_failed_soonest_first(migrated_db):
    second = add(migrated_db, due_at=NOW - DAY)
    first = add(migrated_db, due_at=NOW - 2 * DAY)
    set_status(migrated_db, second, "failed")
    set_status(migrated_db, first, "failed")

    rows = list_recently_failed(migrated_db, ALEX, since=NOW - 7 * DAY)

    assert ids(rows) == [first, second]


# cancel_reminder


def cancel(connection, reminder_id, user_id=ALEX):
    return cancel_reminder(
        connection, reminder_id=reminder_id, user_id=user_id
    )


def test_cancel_own_pending(migrated_db):
    reminder_id = add(migrated_db)

    result = cancel(migrated_db, reminder_id)

    assert result == CancelResult(
        CancelOutcome.CANCELLED,
        message="submit assignment",
        repeat_rule=None,
    )
    assert get_reminder(migrated_db, reminder_id)["status"] == "cancelled"


def test_cancel_repeating(migrated_db):
    reminder_id = add(
        migrated_db,
        message="take out the trash",
        repeat_rule=WEEKLY_RULE,
        timezone="America/Los_Angeles",
    )

    result = cancel(migrated_db, reminder_id)

    assert result == CancelResult(
        CancelOutcome.CANCELLED,
        message="take out the trash",
        repeat_rule=WEEKLY_RULE,
    )
    assert get_reminder(migrated_db, reminder_id)["status"] == "cancelled"


def test_cancel_missing(migrated_db):
    assert cancel(migrated_db, 999) == CancelResult(CancelOutcome.NOT_FOUND)


def test_cancel_someone_elses(migrated_db):
    reminder_id = add(migrated_db, user_id=SAM)

    result = cancel(migrated_db, reminder_id)

    assert result == CancelResult(CancelOutcome.NOT_FOUND)
    assert get_reminder(migrated_db, reminder_id)["status"] == "pending"


@pytest.mark.parametrize("status", ["delivered", "failed", "cancelled"])
def test_cancel_not_pending(migrated_db, status):
    reminder_id = add(migrated_db)
    set_status(migrated_db, reminder_id, status)

    result = cancel(migrated_db, reminder_id)

    assert result == CancelResult(
        CancelOutcome.NOT_PENDING,
        message="submit assignment",
        repeat_rule=None,
    )
    assert get_reminder(migrated_db, reminder_id)["status"] == status


def test_cancel_changes_only_that_row(migrated_db):
    kept = add(migrated_db)
    cancelled = add(migrated_db)

    cancel(migrated_db, cancelled)

    assert get_reminder(migrated_db, kept)["status"] == "pending"
