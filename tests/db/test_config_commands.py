import datetime

import pytest

from maika import config_commands
from maika.config_commands import config_get, display_value
from maika.foundation_settings import LOGGING_CATEGORY
from maika.results import CommandResult, FailureKind
from maika.settings import (
    SettingDefinition,
    SettingLevel,
    SettingsRegistry,
    ValueType,
    describe_range,
)
from maika.settings_store import lookup_value, set_value

NOW = 1_800_000_000
OWNER = 1
GUILD = 5000


def definition(key, value_type, default, server_scope=False, **limits):
    return SettingDefinition(
        key=key,
        category=LOGGING_CATEGORY,
        level=SettingLevel.BASIC,
        server_scope=server_scope,
        value_type=value_type,
        default=default,
        description="A test setting.",
        **limits,
    )


TEST_SETTINGS = SettingsRegistry(
    [
        definition("test.flag", ValueType.BOOL, False),
        definition("test.names", ValueType.STR_LIST, ("maika", "mai")),
        definition("test.ids", ValueType.INT_LIST, ()),
        definition(
            "test.limit", ValueType.INT, 5, server_scope=True, maximum=10
        ),
        definition("test.ratio", ValueType.FLOAT, 0.5, minimum=0.0),
    ]
)


@pytest.fixture
def test_settings(monkeypatch):
    monkeypatch.setattr(config_commands, "SETTINGS", TEST_SETTINGS)
    return TEST_SETTINGS


def store(connection, key, text, registry=None, guild_id=0):
    set_value(
        connection,
        registry or config_commands.SETTINGS,
        key,
        text,
        guild_id=guild_id,
        actor_id=OWNER,
        now=NOW,
    )


def first_line(result):
    return result.message.splitlines()[0]


# config_get with the foundation settings


def test_default_value(migrated_db):
    assert config_get(migrated_db, "backups.days_kept", 0) == (
        CommandResult.ok(
            "`backups.days_kept = 30` (default)\n"
            "Days of daily backups kept.\n"
            "Type: int. Range: 1 to 365. Default: 30."
        )
    )


def test_changed_value(migrated_db):
    store(migrated_db, "backups.days_kept", "14")

    assert config_get(migrated_db, "backups.days_kept", 0) == (
        CommandResult.ok(
            "`backups.days_kept = 14` (changed)\n"
            "Days of daily backups kept.\n"
            "Type: int. Range: 1 to 365. Default: 30."
        )
    )


def test_global_setting_in_a_server_uses_global_row(migrated_db):
    store(migrated_db, "backups.days_kept", "14")

    result = config_get(migrated_db, "backups.days_kept", GUILD)

    assert first_line(result) == "`backups.days_kept = 14` (changed)"


def test_none_value_without_range(migrated_db):
    result = config_get(migrated_db, "log.channel.errors", 0)
    assert result.message == (
        "`log.channel.errors = none` (default)\n"
        "Channel for #errors. Empty falls back to #bot-log.\n"
        "Type: int. Default: none."
    )


def test_text_value_with_choices(migrated_db):
    result = config_get(migrated_db, "log.level.bot_log", 0)
    assert result.message.splitlines()[0] == (
        "`log.level.bot_log = info` (default)"
    )
    assert result.message.splitlines()[2] == (
        "Type: text. Choices: debug, info, warning, error. Default: info."
    )


def test_choices_setting_full_reply(migrated_db):
    store(migrated_db, "log.level.feeds", "warning")

    result = config_get(migrated_db, "log.level.feeds", 0)

    definition = config_commands.SETTINGS.get("log.level.feeds")
    assert result.message == (
        "`log.level.feeds = warning` (changed)\n"
        f"{definition.description}\n"
        "Type: text. Choices: debug, info, warning, error. Default: info."
    )


def test_no_range_and_no_choices_omits_part(migrated_db):
    result = config_get(migrated_db, "backups.time", 0)
    details = result.message.splitlines()[2]
    assert details == "Type: time. Default: 04:00."
    assert "Range" not in details
    assert "Choices" not in details


def test_time_value_uses_stored_form(migrated_db):
    store(migrated_db, "backups.time", "03:30")

    result = config_get(migrated_db, "backups.time", 0)

    assert result.message == (
        "`backups.time = 03:30` (changed)\n"
        "Daily database backup time.\n"
        "Type: time. Default: 04:00."
    )


def test_unknown_key(migrated_db):
    assert config_get(migrated_db, "backups.dayz_kept", 0) == (
        CommandResult.fail(
            FailureKind.NOT_FOUND,
            "I don't know a setting called `backups.dayz_kept`.",
        )
    )


def test_changed_value_equal_to_default_says_changed(migrated_db):
    store(migrated_db, "backups.days_kept", "30")

    result = config_get(migrated_db, "backups.days_kept", 0)

    assert first_line(result) == "`backups.days_kept = 30` (changed)"


# config_get with other value types


def test_bool_default_and_changed(migrated_db, test_settings):
    assert config_get(migrated_db, "test.flag", 0).message == (
        "`test.flag = false` (default)\n"
        "A test setting.\n"
        "Type: bool. Default: false."
    )

    store(migrated_db, "test.flag", "true")

    assert first_line(config_get(migrated_db, "test.flag", 0)) == (
        "`test.flag = true` (changed)"
    )


def test_list_value(migrated_db, test_settings):
    assert config_get(migrated_db, "test.names", 0).message == (
        "`test.names = maika, mai` (default)\n"
        "A test setting.\n"
        "Type: list[str]. Default: maika, mai."
    )


def test_int_list_value(migrated_db, test_settings):
    store(migrated_db, "test.ids", "[111, 222]")

    assert config_get(migrated_db, "test.ids", 0).message == (
        "`test.ids = 111, 222` (changed)\n"
        "A test setting.\n"
        "Type: list[int]. Default: empty list."
    )


def test_empty_list_value(migrated_db, test_settings):
    assert first_line(config_get(migrated_db, "test.ids", 0)) == (
        "`test.ids = empty list` (default)"
    )


def test_backtick_in_value(migrated_db, test_settings):
    store(migrated_db, "test.names", '["a`b", "c"]')

    assert first_line(config_get(migrated_db, "test.names", 0)) == (
        "`test.names = a'b, c` (changed)"
    )


def test_float_range(migrated_db, test_settings):
    lines = config_get(migrated_db, "test.ratio", 0).message.splitlines()
    assert lines[2] == "Type: float. Range: at least 0.0. Default: 0.5."


def test_server_row_applies_in_its_server(migrated_db, test_settings):
    store(migrated_db, "test.limit", "9", guild_id=GUILD)

    assert first_line(config_get(migrated_db, "test.limit", GUILD)) == (
        "`test.limit = 9` (changed)"
    )
    assert config_get(migrated_db, "test.limit", 0).message == (
        "`test.limit = 5` (default)\n"
        "A test setting.\n"
        "Type: int. Range: at most 10. Default: 5."
    )


def test_config_get_writes_nothing(migrated_db):
    before = migrated_db.total_changes
    config_get(migrated_db, "backups.days_kept", 0)
    config_get(migrated_db, "nope.nope", 0)
    assert migrated_db.total_changes == before


# display_value

ANY_TEXT = definition("test.any", ValueType.JSON, None)
A_TIME = definition("test.time", ValueType.TIME, datetime.time(4, 0))


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (True, "true"),
        (False, "false"),
        (None, "none"),
        (("a", "b"), "a, b"),
        ([1, 2, 3], "1, 2, 3"),
        ((), "empty list"),
        ([], "empty list"),
        (0, "0"),
        (1, "1"),
        (0.5, "0.5"),
        ("info", "info"),
        ("a`b`c", "a'b'c"),
        (["x`y"], "x'y"),
    ],
)
def test_display_value(value, text):
    assert display_value(ANY_TEXT, value) == text


def test_display_time():
    assert display_value(A_TIME, datetime.time(4, 0)) == "04:00"
    assert display_value(A_TIME, datetime.time(23, 5)) == "23:05"
    assert display_value(A_TIME, None) == "none"


# describe_range


@pytest.mark.parametrize(
    ("minimum", "maximum", "text"),
    [
        (1, 365, "1 to 365"),
        (1, None, "at least 1"),
        (None, 10, "at most 10"),
        (None, None, None),
    ],
)
def test_describe_range(minimum, maximum, text):
    setting = SettingDefinition(
        key="test.count",
        category=LOGGING_CATEGORY,
        level=SettingLevel.BASIC,
        server_scope=False,
        value_type=ValueType.INT,
        default=None,
        description="A test setting.",
        minimum=minimum,
        maximum=maximum,
    )
    assert describe_range(setting) == text


# lookup_value


def test_lookup_reports_default(migrated_db):
    lookup = lookup_value(
        migrated_db, config_commands.SETTINGS, "backups.days_kept"
    )
    assert (lookup.value, lookup.stored) == (30, False)


def test_lookup_reports_stored_row(migrated_db):
    store(migrated_db, "backups.days_kept", "14")
    lookup = lookup_value(
        migrated_db, config_commands.SETTINGS, "backups.days_kept"
    )
    assert (lookup.value, lookup.stored) == (14, True)


def test_lookup_bad_row_falls_back_to_default(migrated_db):
    migrated_db.execute(
        "INSERT INTO settings (key, guild_id, value, updated_at, "
        "updated_by) VALUES ('backups.days_kept', 0, 'abc', 0, 1)"
    )
    lookup = lookup_value(
        migrated_db, config_commands.SETTINGS, "backups.days_kept"
    )
    assert (lookup.value, lookup.stored) == (30, False)
