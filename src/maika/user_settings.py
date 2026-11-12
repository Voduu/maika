"""Per-person settings in the user_settings table.

See ``docs/design.md`` section 9.11. Each key is declared once in
code as a UserSettingDefinition. Only values a person has set are
stored, as text, and these keys have no default: a missing row means
the person has not set the key.

Changes to a person's own settings are not written to the audit log,
which covers owner changes (section 9.5).

Each function is a single statement and never starts a transaction,
so it can run inside the caller's ``transaction()`` block. Nothing
here reads the clock: the caller passes ``now`` in UTC Unix seconds.
"""

import calendar
import enum
import re
import sqlite3
import zoneinfo
from dataclasses import dataclass

TIMEZONE_KEY = "timezone"
BIRTHDAY_KEY = "birthday"

# Birthdays are checked against a leap year so that 02-29 passes.
LEAP_YEAR = 2000

FIRST_MONTH = 1
LAST_MONTH = 12
FIRST_DAY = 1

# Two ASCII digits, a hyphen, two ASCII digits. \d would also accept
# other scripts' digits.
MONTH_DAY_PATTERN = re.compile(r"^(?P<month>[0-9]{2})-(?P<day>[0-9]{2})$")

SELECT_VALUE_SQL = (
    "SELECT value FROM user_settings WHERE user_id = ? AND key = ?"
)

UPSERT_SQL = (
    "INSERT INTO user_settings (user_id, key, value, updated_at) "
    "VALUES (:user_id, :key, :value, :updated_at) "
    "ON CONFLICT (user_id, key) DO UPDATE SET "
    "value = excluded.value, "
    "updated_at = excluded.updated_at"
)

DELETE_SQL = "DELETE FROM user_settings WHERE user_id = ? AND key = ?"


class UserValueType(enum.Enum):
    """The kind of value a per-person setting holds."""

    TIMEZONE = "timezone"
    MONTH_DAY = "month-day"


class UserSettingError(Exception):
    """Raised when a per-person setting cannot be stored.

    ``message`` is a plain sentence a command can show the person.
    """

    def __init__(self, message: str) -> None:
        """Store the message."""
        self.message = message
        super().__init__(message)


class UnknownUserSettingError(UserSettingError):
    """Raised when a key has no per-person setting definition."""

    def __init__(self, key: str) -> None:
        """Store the key and build the message."""
        self.key = key
        super().__init__(f"There is no personal setting named {key}.")


class UserSettingValueError(UserSettingError):
    """Raised when a value does not fit its per-person setting."""

    def __init__(self, key: str, message: str) -> None:
        """Store the key and the message."""
        self.key = key
        super().__init__(message)


@dataclass(frozen=True)
class UserSettingDefinition:
    """The single declaration of one per-person setting."""

    key: str
    value_type: UserValueType
    description: str


USER_SETTINGS: tuple[UserSettingDefinition, ...] = (
    UserSettingDefinition(
        key=TIMEZONE_KEY,
        value_type=UserValueType.TIMEZONE,
        description="Your timezone, as an IANA name such as America/New_York.",
    ),
    UserSettingDefinition(
        key=BIRTHDAY_KEY,
        value_type=UserValueType.MONTH_DAY,
        description="Your birthday as month and day, written MM-DD.",
    ),
)

_DEFINITIONS: dict[str, UserSettingDefinition] = {
    definition.key: definition for definition in USER_SETTINGS
}


def _build_timezone_names() -> dict[str, str]:
    """Map each lowercase IANA name to its canonical spelling."""
    return {name.lower(): name for name in zoneinfo.available_timezones()}


# Built once: reading the timezone list scans the tzdata files.
_TIMEZONE_NAMES = _build_timezone_names()


def get_definition(key: str) -> UserSettingDefinition:
    """Return the definition for a key.

    Raise UnknownUserSettingError when there is none.
    """
    try:
        return _DEFINITIONS[key]
    except KeyError:
        raise UnknownUserSettingError(key) from None


def _canonical_timezone(key: str, text: str) -> str:
    """Return the canonical IANA name for text in any capitalization.

    The name is looked up in the list of known timezones, so text
    such as a file path never reaches ZoneInfo. ZoneInfo then loads
    the name to confirm it can be used.
    """
    name = text.strip()
    if name == "":
        raise UserSettingValueError(
            key, "Please give a timezone, such as America/New_York."
        )
    canonical = _TIMEZONE_NAMES.get(name.lower())
    if canonical is None:
        raise UserSettingValueError(
            key,
            f"I don't know the timezone {name}. Use an IANA name such "
            "as America/New_York or Europe/London.",
        )
    try:
        zoneinfo.ZoneInfo(canonical)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError) as error:
        raise UserSettingValueError(
            key, f"The timezone {canonical} could not be loaded."
        ) from error
    return canonical


def _canonical_month_day(key: str, text: str) -> str:
    """Return a month and day as MM-DD text after checking the date.

    The day is checked against a leap year, so 02-29 is accepted.
    """
    stripped = text.strip()
    match = MONTH_DAY_PATTERN.match(stripped)
    if match is None:
        raise UserSettingValueError(
            key,
            "Please write the date as MM-DD, for example 07-14 for July 14.",
        )
    month = int(match["month"])
    day = int(match["day"])
    if not FIRST_MONTH <= month <= LAST_MONTH:
        raise UserSettingValueError(
            key, f"{stripped} is not a real date: the month must be 01 to 12."
        )
    _, days_in_month = calendar.monthrange(LEAP_YEAR, month)
    if not FIRST_DAY <= day <= days_in_month:
        raise UserSettingValueError(
            key,
            f"{stripped} is not a real date: that month has "
            f"{days_in_month} days.",
        )
    return f"{month:02d}-{day:02d}"


def validate_user_setting(key: str, value: str) -> str:
    """Return the canonical text to store for a key and typed value.

    Raise UnknownUserSettingError for an unknown key and
    UserSettingValueError for a value that does not fit.
    """
    definition = get_definition(key)
    if definition.value_type is UserValueType.TIMEZONE:
        return _canonical_timezone(key, value)
    return _canonical_month_day(key, value)


def get_user_setting(
    connection: sqlite3.Connection, user_id: int, key: str
) -> str | None:
    """Return the stored text, or None when the person has not set it.

    Raise UnknownUserSettingError for an unknown key.
    """
    get_definition(key)
    row = connection.execute(SELECT_VALUE_SQL, (user_id, key)).fetchone()
    if row is None:
        return None
    return row["value"]


def set_user_setting(
    connection: sqlite3.Connection,
    user_id: int,
    key: str,
    value: str,
    now: int,
) -> str:
    """Validate and store a person's value, and return the stored text.

    The value is converted to its canonical text first, and nothing is
    written when it is invalid. One upsert statement writes the row
    with ``updated_at = now``, replacing any earlier value. No audit
    entry is written.

    Raise UnknownUserSettingError for an unknown key and
    UserSettingValueError for a value that does not fit.
    """
    stored = validate_user_setting(key, value)
    connection.execute(
        UPSERT_SQL,
        {
            "user_id": user_id,
            "key": key,
            "value": stored,
            "updated_at": now,
        },
    )
    return stored


def delete_user_setting(
    connection: sqlite3.Connection, user_id: int, key: str
) -> bool:
    """Delete a person's value. Return True when a row was removed.

    Raise UnknownUserSettingError for an unknown key.
    """
    get_definition(key)
    cursor = connection.execute(DELETE_SQL, (user_id, key))
    return cursor.rowcount == 1
