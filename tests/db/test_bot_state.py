import pytest

from maika.bot_state import (
    StateEntry,
    delete_state,
    get_entry,
    get_state,
    set_state,
)
from maika.database import transaction

NOW = 1_800_000_000
LATER = NOW + 60


def test_set_then_get(migrated_db):
    set_state(migrated_db, "heartbeat", "1800000000", NOW)

    assert get_state(migrated_db, "heartbeat") == "1800000000"
    assert get_entry(migrated_db, "heartbeat") == StateEntry(
        "heartbeat", "1800000000", NOW
    )


def test_second_set_replaces_value_and_time(migrated_db):
    set_state(migrated_db, "sleep.state", "awake", NOW)
    set_state(migrated_db, "sleep.state", "asleep", LATER)

    assert get_entry(migrated_db, "sleep.state") == StateEntry(
        "sleep.state", "asleep", LATER
    )
    count = migrated_db.execute("SELECT COUNT(*) FROM bot_state").fetchone()
    assert count[0] == 1


def test_missing_key_returns_none(migrated_db):
    assert get_state(migrated_db, "first_boot") is None
    assert get_entry(migrated_db, "first_boot") is None


def test_empty_text_value_is_stored(migrated_db):
    set_state(migrated_db, "sleep.early_woken", "", NOW)
    assert get_state(migrated_db, "sleep.early_woken") == ""


def test_delete_returns_true_then_false(migrated_db):
    set_state(migrated_db, "first_boot", "1", NOW)

    assert delete_state(migrated_db, "first_boot") is True
    assert get_state(migrated_db, "first_boot") is None
    assert delete_state(migrated_db, "first_boot") is False


@pytest.mark.parametrize("key", ["", "   "])
def test_empty_key_raises(migrated_db, key):
    with pytest.raises(ValueError, match="must not be empty"):
        set_state(migrated_db, key, "1", NOW)


def test_non_text_key_raises(migrated_db):
    with pytest.raises(TypeError, match="key must be text"):
        set_state(migrated_db, 5, "1", NOW)


@pytest.mark.parametrize("value", [1, 1.5, None, True, b"bytes"])
def test_non_text_value_raises(migrated_db, value):
    with pytest.raises(TypeError, match="must be text"):
        set_state(migrated_db, "heartbeat", value, NOW)
    assert get_state(migrated_db, "heartbeat") is None


def test_set_inside_transaction_commits(migrated_db):
    with transaction(migrated_db):
        set_state(migrated_db, "mood.energy", "0.4", NOW)
        set_state(migrated_db, "mood.happiness", "0.6", NOW)
        assert migrated_db.in_transaction

    assert get_state(migrated_db, "mood.energy") == "0.4"
    assert get_state(migrated_db, "mood.happiness") == "0.6"


def test_set_rolls_back_with_transaction(migrated_db):
    set_state(migrated_db, "mood.energy", "0.1", NOW)

    with pytest.raises(RuntimeError):
        with transaction(migrated_db):
            set_state(migrated_db, "mood.energy", "0.9", LATER)
            set_state(migrated_db, "mood.happiness", "0.9", LATER)
            raise RuntimeError("stop")

    assert get_entry(migrated_db, "mood.energy") == StateEntry(
        "mood.energy", "0.1", NOW
    )
    assert get_state(migrated_db, "mood.happiness") is None
