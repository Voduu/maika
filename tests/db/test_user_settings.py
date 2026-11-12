import pytest

from maika.user_settings import (
    BIRTHDAY_KEY,
    TIMEZONE_KEY,
    USER_SETTINGS,
    UnknownUserSettingError,
    UserSettingError,
    UserSettingValueError,
    delete_user_setting,
    get_user_setting,
    set_user_setting,
)

NOW = 1_800_000_000
LATER = NOW + 3600
ALEX = 200
BLAIR = 300


def stored_row(connection, user_id, key):
    return connection.execute(
        "SELECT value, updated_at FROM user_settings "
        "WHERE user_id = ? AND key = ?",
        (user_id, key),
    ).fetchone()


def audit_count(connection):
    return connection.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]


def test_definitions_cover_both_keys():
    assert {d.key for d in USER_SETTINGS} == {TIMEZONE_KEY, BIRTHDAY_KEY}
    for definition in USER_SETTINGS:
        assert definition.description


# timezone


@pytest.mark.parametrize(
    "name",
    ["America/Los_Angeles", "Europe/London", "UTC", "Asia/Kolkata"],
)
def test_timezone_valid_names(migrated_db, name):
    assert set_user_setting(migrated_db, ALEX, TIMEZONE_KEY, name, NOW) == (
        name
    )
    assert get_user_setting(migrated_db, ALEX, TIMEZONE_KEY) == name


@pytest.mark.parametrize(
    ("typed", "canonical"),
    [
        ("america/los_angeles", "America/Los_Angeles"),
        ("AMERICA/NEW_YORK", "America/New_York"),
        ("  europe/london  ", "Europe/London"),
        ("utc", "UTC"),
    ],
)
def test_timezone_stored_canonically(migrated_db, typed, canonical):
    stored = set_user_setting(migrated_db, ALEX, TIMEZONE_KEY, typed, NOW)
    assert stored == canonical
    assert stored_row(migrated_db, ALEX, TIMEZONE_KEY)["value"] == canonical


@pytest.mark.parametrize(
    "name",
    ["Mars/Olympus_Mons", "America/Springfield", "../etc/passwd", "PST8"],
)
def test_timezone_unknown_name_rejected(migrated_db, name):
    with pytest.raises(UserSettingValueError) as caught:
        set_user_setting(migrated_db, ALEX, TIMEZONE_KEY, name, NOW)
    assert caught.value.key == TIMEZONE_KEY
    assert "I don't know the timezone" in caught.value.message
    assert stored_row(migrated_db, ALEX, TIMEZONE_KEY) is None


@pytest.mark.parametrize("text", ["", "   "])
def test_timezone_empty_rejected(migrated_db, text):
    with pytest.raises(UserSettingValueError) as caught:
        set_user_setting(migrated_db, ALEX, TIMEZONE_KEY, text, NOW)
    assert "Please give a timezone" in caught.value.message
    assert stored_row(migrated_db, ALEX, TIMEZONE_KEY) is None


# birthday


@pytest.mark.parametrize(
    ("typed", "stored"),
    [
        ("01-01", "01-01"),
        ("07-14", "07-14"),
        ("12-31", "12-31"),
        ("04-30", "04-30"),
        (" 03-05 ", "03-05"),
    ],
)
def test_birthday_valid_dates(migrated_db, typed, stored):
    assert set_user_setting(migrated_db, ALEX, BIRTHDAY_KEY, typed, NOW) == (
        stored
    )
    assert get_user_setting(migrated_db, ALEX, BIRTHDAY_KEY) == stored


def test_birthday_february_29_accepted(migrated_db):
    assert (
        set_user_setting(migrated_db, ALEX, BIRTHDAY_KEY, "02-29", NOW)
        == "02-29"
    )


@pytest.mark.parametrize(
    "text", ["02-30", "13-01", "00-10", "04-31", "06-00", "01-32"]
)
def test_birthday_impossible_dates_rejected(migrated_db, text):
    with pytest.raises(UserSettingValueError) as caught:
        set_user_setting(migrated_db, ALEX, BIRTHDAY_KEY, text, NOW)
    assert caught.value.key == BIRTHDAY_KEY
    assert "is not a real date" in caught.value.message
    assert stored_row(migrated_db, ALEX, BIRTHDAY_KEY) is None


@pytest.mark.parametrize(
    "text",
    [
        "",
        "2-29",
        "02/29",
        "0229",
        "2000-02-29",
        "02-29-2000",
        "Feb 29",
        "٠٢-٢٩",
    ],
)
def test_birthday_wrong_format_rejected(migrated_db, text):
    with pytest.raises(UserSettingValueError) as caught:
        set_user_setting(migrated_db, ALEX, BIRTHDAY_KEY, text, NOW)
    assert "MM-DD" in caught.value.message
    assert stored_row(migrated_db, ALEX, BIRTHDAY_KEY) is None


# round trips


def test_get_returns_none_when_unset(migrated_db):
    assert get_user_setting(migrated_db, ALEX, TIMEZONE_KEY) is None
    assert get_user_setting(migrated_db, ALEX, BIRTHDAY_KEY) is None


def test_set_writes_updated_at(migrated_db):
    set_user_setting(migrated_db, ALEX, TIMEZONE_KEY, "Europe/Paris", NOW)
    row = stored_row(migrated_db, ALEX, TIMEZONE_KEY)
    assert row["value"] == "Europe/Paris"
    assert row["updated_at"] == NOW


def test_set_replaces_value_and_updated_at(migrated_db):
    set_user_setting(migrated_db, ALEX, TIMEZONE_KEY, "Europe/Paris", NOW)
    set_user_setting(migrated_db, ALEX, TIMEZONE_KEY, "Asia/Tokyo", LATER)

    row = stored_row(migrated_db, ALEX, TIMEZONE_KEY)
    assert row["value"] == "Asia/Tokyo"
    assert row["updated_at"] == LATER
    count = migrated_db.execute(
        "SELECT COUNT(*) FROM user_settings WHERE user_id = ?", (ALEX,)
    ).fetchone()[0]
    assert count == 1


def test_invalid_value_keeps_old_row(migrated_db):
    set_user_setting(migrated_db, ALEX, BIRTHDAY_KEY, "07-14", NOW)
    with pytest.raises(UserSettingValueError):
        set_user_setting(migrated_db, ALEX, BIRTHDAY_KEY, "02-30", LATER)
    row = stored_row(migrated_db, ALEX, BIRTHDAY_KEY)
    assert row["value"] == "07-14"
    assert row["updated_at"] == NOW


def test_delete_removes_row(migrated_db):
    set_user_setting(migrated_db, ALEX, BIRTHDAY_KEY, "07-14", NOW)
    assert delete_user_setting(migrated_db, ALEX, BIRTHDAY_KEY) is True
    assert get_user_setting(migrated_db, ALEX, BIRTHDAY_KEY) is None


def test_delete_without_row_returns_false(migrated_db):
    assert delete_user_setting(migrated_db, ALEX, BIRTHDAY_KEY) is False


def test_two_people_rows_are_separate(migrated_db):
    set_user_setting(migrated_db, ALEX, TIMEZONE_KEY, "Europe/Paris", NOW)
    set_user_setting(migrated_db, BLAIR, TIMEZONE_KEY, "Asia/Tokyo", LATER)

    assert get_user_setting(migrated_db, ALEX, TIMEZONE_KEY) == "Europe/Paris"
    assert get_user_setting(migrated_db, BLAIR, TIMEZONE_KEY) == "Asia/Tokyo"

    assert delete_user_setting(migrated_db, ALEX, TIMEZONE_KEY) is True
    assert get_user_setting(migrated_db, ALEX, TIMEZONE_KEY) is None
    assert get_user_setting(migrated_db, BLAIR, TIMEZONE_KEY) == "Asia/Tokyo"


def test_keys_for_one_person_are_separate(migrated_db):
    set_user_setting(migrated_db, ALEX, TIMEZONE_KEY, "Europe/Paris", NOW)
    set_user_setting(migrated_db, ALEX, BIRTHDAY_KEY, "07-14", NOW)
    delete_user_setting(migrated_db, ALEX, TIMEZONE_KEY)
    assert get_user_setting(migrated_db, ALEX, BIRTHDAY_KEY) == "07-14"


def test_changes_are_not_audited(migrated_db):
    set_user_setting(migrated_db, ALEX, TIMEZONE_KEY, "Europe/Paris", NOW)
    set_user_setting(migrated_db, ALEX, BIRTHDAY_KEY, "07-14", NOW)
    delete_user_setting(migrated_db, ALEX, TIMEZONE_KEY)
    assert audit_count(migrated_db) == 0


# unknown key


@pytest.mark.parametrize("key", ["nickname", "Timezone", "", "tz"])
def test_unknown_key_rejected_by_set(migrated_db, key):
    with pytest.raises(UnknownUserSettingError) as caught:
        set_user_setting(migrated_db, ALEX, key, "UTC", NOW)
    assert caught.value.key == key
    assert "There is no personal setting named" in caught.value.message
    count = migrated_db.execute(
        "SELECT COUNT(*) FROM user_settings"
    ).fetchone()[0]
    assert count == 0


def test_unknown_key_rejected_by_get_and_delete(migrated_db):
    with pytest.raises(UnknownUserSettingError):
        get_user_setting(migrated_db, ALEX, "nickname")
    with pytest.raises(UnknownUserSettingError):
        delete_user_setting(migrated_db, ALEX, "nickname")


def test_errors_share_one_base_type():
    assert issubclass(UnknownUserSettingError, UserSettingError)
    assert issubclass(UserSettingValueError, UserSettingError)
