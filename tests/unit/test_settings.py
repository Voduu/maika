import datetime

import pytest

from maika.foundation_settings import (
    BACKUPS_CATEGORY,
    COMMANDS_CATEGORY,
    CONFIRM_TIMEOUT_KEY,
    FOUNDATION_SETTINGS,
    LOGGING_CATEGORY,
    REMINDERS_CATEGORY,
    build_foundation_registry,
)
from maika.settings import (
    SettingDefinition,
    SettingLevel,
    SettingsRegistry,
    SettingValueError,
    UnknownSettingError,
    ValueType,
    check_value,
    format_value,
    parse_value,
)


def make(value_type, key="test.value", **overrides):
    fields = {
        "key": key,
        "category": "test",
        "level": SettingLevel.BASIC,
        "server_scope": False,
        "value_type": value_type,
        "default": None,
        "description": "A setting used by the tests.",
    }
    fields.update(overrides)
    return SettingDefinition(**fields)


def assert_round_trip(definition, value):
    assert parse_value(definition, format_value(definition, value)) == value


# BOOL

BOOL = make(ValueType.BOOL)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("true", True),
        ("TRUE", True),
        ("On", True),
        ("yes", True),
        ("false", False),
        ("False", False),
        ("OFF", False),
        ("no", False),
        ("  yes  ", True),
    ],
)
def test_bool_valid(text, expected):
    assert parse_value(BOOL, text) is expected


@pytest.mark.parametrize("text", ["", "1", "0", "y", "n", "truee", "enabled"])
def test_bool_invalid(text):
    with pytest.raises(SettingValueError, match=r"^test\.value must be"):
        parse_value(BOOL, text)


def test_bool_stored_text():
    assert format_value(BOOL, True) == "true"
    assert format_value(BOOL, False) == "false"


@pytest.mark.parametrize("value", [True, False])
def test_bool_round_trip(value):
    assert_round_trip(BOOL, value)


# INT

INT = make(ValueType.INT)
DAYS = make(
    ValueType.INT, key="backups.days_kept", minimum=1, maximum=365, default=30
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("0", 0),
        ("42", 42),
        ("-7", -7),
        ("007", 7),
        (" 12 ", 12),
    ],
)
def test_int_valid(text, expected):
    assert parse_value(INT, text) == expected


MAX_INT64 = 2**63 - 1
MIN_INT64 = -(2**63)
INT64_RULE = (
    "test.value must be a whole number from "
    "-9223372036854775808 to 9223372036854775807."
)


@pytest.mark.parametrize("value", [MAX_INT64, MIN_INT64])
def test_int_accepts_64_bit_limits(value):
    assert parse_value(INT, str(value)) == value
    check_value(INT, value)


@pytest.mark.parametrize("value", [MAX_INT64 + 1, MIN_INT64 - 1])
def test_int_parse_rejects_beyond_64_bits(value):
    with pytest.raises(SettingValueError) as caught:
        parse_value(INT, str(value))
    assert str(caught.value) == INT64_RULE


@pytest.mark.parametrize("value", [MAX_INT64 + 1, MIN_INT64 - 1])
def test_int_check_rejects_beyond_64_bits(value):
    with pytest.raises(SettingValueError) as caught:
        check_value(INT, value)
    assert str(caught.value) == INT64_RULE


def test_int_beyond_64_bits_rejected_even_with_wider_range():
    wide = make(ValueType.INT, minimum=MIN_INT64 - 10, maximum=MAX_INT64 + 10)
    with pytest.raises(SettingValueError):
        parse_value(wide, str(MAX_INT64 + 1))


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "+5",
        "1_000",
        "1 000",
        "- 5",
        "5.0",
        "1e3",
        "0x10",
        "abc",
        "--5",
        "٣",  # Arabic-Indic digit three
        "1" * 21,
        "-" + "1" * 20,
    ],
)
def test_int_invalid(text):
    with pytest.raises(SettingValueError):
        parse_value(INT, text)


def test_int_message_names_key_and_rule():
    with pytest.raises(SettingValueError) as caught:
        parse_value(DAYS, "lots")
    assert str(caught.value) == (
        "backups.days_kept must be a whole number from 1 to 365."
    )


def test_int_range_is_inclusive():
    assert parse_value(DAYS, "1") == 1
    assert parse_value(DAYS, "365") == 365
    with pytest.raises(SettingValueError):
        parse_value(DAYS, "0")
    with pytest.raises(SettingValueError):
        parse_value(DAYS, "366")


def test_int_stored_text():
    assert format_value(INT, -15) == "-15"


@pytest.mark.parametrize("value", [0, 1, -1, 2**63 - 1, -(2**63)])
def test_int_round_trip(value):
    assert_round_trip(INT, value)


def test_int_format_rejects_bool():
    with pytest.raises(SettingValueError):
        format_value(INT, True)


# FLOAT

FLOAT = make(ValueType.FLOAT)
FRACTION = make(ValueType.FLOAT, minimum=0, maximum=1)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("0.5", 0.5),
        ("-2", -2.0),
        ("10.25", 10.25),
        ("0", 0.0),
        (" 3.0 ", 3.0),
    ],
)
def test_float_valid(text, expected):
    value = parse_value(FLOAT, text)
    assert value == expected
    assert isinstance(value, float)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "1e3",
        "1E3",
        "2.5e-1",
        "nan",
        "NaN",
        "inf",
        "-inf",
        "Infinity",
        "1_000.5",
        "1.0_0",
        "+0.5",
        ".5",
        "5.",
        "0x1p3",
        "1,5",
        "9" * 400,  # converts to inf
    ],
)
def test_float_invalid(text):
    with pytest.raises(SettingValueError):
        parse_value(FLOAT, text)


def test_float_range_is_inclusive():
    assert parse_value(FRACTION, "0") == 0.0
    assert parse_value(FRACTION, "1") == 1.0
    with pytest.raises(SettingValueError, match="from 0 to 1"):
        parse_value(FRACTION, "-0.01")
    with pytest.raises(SettingValueError):
        parse_value(FRACTION, "1.0001")


def test_float_stored_text():
    assert format_value(FLOAT, 0.5) == "0.5"
    assert format_value(FLOAT, 2.0) == "2.0"
    assert format_value(FLOAT, 1e-05) == "0.00001"
    assert format_value(FLOAT, 1e16) == "10000000000000000.0"


@pytest.mark.parametrize(
    "value", [0.0, 0.1, -2.5, 1e-05, 1e16, 123456.789, 2 / 3]
)
def test_float_round_trip(value):
    assert_round_trip(FLOAT, value)


@pytest.mark.parametrize("value", [float("nan"), float("inf")])
def test_float_format_rejects_non_finite(value):
    with pytest.raises(SettingValueError):
        format_value(FLOAT, value)


# TEXT

TEXT = make(ValueType.TEXT)
LEVEL = make(
    ValueType.TEXT,
    choices=("debug", "info", "warning", "error"),
    default="info",
)


def test_text_is_stripped():
    assert parse_value(TEXT, "  hello there  ") == "hello there"


@pytest.mark.parametrize("text", ["", "   ", "\t\n"])
def test_text_rejects_empty(text):
    with pytest.raises(SettingValueError, match="must not be empty"):
        parse_value(TEXT, text)


@pytest.mark.parametrize(
    "text", ["warning", "WARNING", "Warning", " wArNiNg "]
)
def test_choices_accept_any_case(text):
    assert parse_value(LEVEL, text) == "warning"


def test_choices_reject_other_text():
    with pytest.raises(SettingValueError) as caught:
        parse_value(LEVEL, "verbose")
    assert str(caught.value) == (
        "test.value must be one of: debug, info, warning, error."
    )


def test_text_stored_text():
    assert format_value(TEXT, "hello") == "hello"
    assert format_value(LEVEL, "error") == "error"


@pytest.mark.parametrize("value", ["hello", "マイカ", "a b"])
def test_text_round_trip(value):
    assert_round_trip(TEXT, value)


@pytest.mark.parametrize("value", ["debug", "info", "warning", "error"])
def test_choice_round_trip(value):
    assert_round_trip(LEVEL, value)


def test_choice_format_rejects_uppercase():
    with pytest.raises(SettingValueError):
        format_value(LEVEL, "INFO")


# TIME

TIME = make(ValueType.TIME)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("00:00", datetime.time(0, 0)),
        ("04:00", datetime.time(4, 0)),
        ("13:45", datetime.time(13, 45)),
        ("23:59", datetime.time(23, 59)),
    ],
)
def test_time_valid(text, expected):
    assert parse_value(TIME, text) == expected


@pytest.mark.parametrize(
    "text",
    ["4:00", "04:0", "24:00", "12:60", "0400", "04:00:00", "4pm", "", "04.00"],
)
def test_time_invalid(text):
    with pytest.raises(SettingValueError, match="HH:MM"):
        parse_value(TIME, text)


def test_time_stored_text():
    assert format_value(TIME, datetime.time(4, 5)) == "04:05"


def test_time_format_rejects_seconds():
    with pytest.raises(SettingValueError):
        format_value(TIME, datetime.time(4, 5, 6))


@pytest.mark.parametrize(
    "value", [datetime.time(0, 0), datetime.time(9, 7), datetime.time(23, 59)]
)
def test_time_round_trip(value):
    assert_round_trip(TIME, value)


# INT_LIST

INT_LIST = make(ValueType.INT_LIST)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("[]", ()),
        ("[1, 2, 3]", (1, 2, 3)),
        ("[-5]", (-5,)),
        (f"[{2**63 - 1}, {-(2**63)}]", (2**63 - 1, -(2**63))),
    ],
)
def test_int_list_valid(text, expected):
    assert parse_value(INT_LIST, text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "1, 2",
        "[1, 2",
        '{"a": 1}',
        "5",
        "[true]",
        "[1.0]",
        "[1.5]",
        '["1"]',
        "[null]",
        "[[1]]",
        f"[{2**63}]",
        f"[{-(2**63) - 1}]",
        "[NaN]",
    ],
)
def test_int_list_invalid(text):
    with pytest.raises(SettingValueError):
        parse_value(INT_LIST, text)


def test_int_list_stored_text():
    assert format_value(INT_LIST, (1, 22, 333)) == "[1,22,333]"


@pytest.mark.parametrize("value", [(), (1,), (123456789012345678, 0, -1)])
def test_int_list_round_trip(value):
    assert_round_trip(INT_LIST, value)


# STR_LIST

STR_LIST = make(ValueType.STR_LIST)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("[]", ()),
        ('["Maika", "マイカ"]', ("Maika", "マイカ")),
        ('[""]', ("",)),
    ],
)
def test_str_list_valid(text, expected):
    assert parse_value(STR_LIST, text) == expected


@pytest.mark.parametrize(
    "text", ["", "Maika", '["a", 1]', "[null]", '"a"', '{"a": "b"}', "[[]]"]
)
def test_str_list_invalid(text):
    with pytest.raises(SettingValueError):
        parse_value(STR_LIST, text)


def test_str_list_stored_text():
    assert format_value(STR_LIST, ("Maika", "マイカ")) == '["Maika","マイカ"]'


@pytest.mark.parametrize("value", [(), ("a",), ("Maika", "マイカ", 'q"uote')])
def test_str_list_round_trip(value):
    assert_round_trip(STR_LIST, value)


# JSON

JSON = make(ValueType.JSON)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('{"b": 1, "a": [1, 2]}', {"a": [1, 2], "b": 1}),
        ("[1, 2]", [1, 2]),
        ('"text"', "text"),
        ("3", 3),
        ("null", None),
        ("true", True),
    ],
)
def test_json_valid(text, expected):
    assert parse_value(JSON, text) == expected


@pytest.mark.parametrize(
    "text", ["", "{", "{'a': 1}", "NaN", "[Infinity]", "-Infinity", "nope"]
)
def test_json_invalid(text):
    with pytest.raises(SettingValueError, match="valid JSON"):
        parse_value(JSON, text)


def test_json_stored_in_canonical_form():
    value = parse_value(JSON, '{ "b" : 1,  "a" : { "d": 2, "c": 3 } }')
    assert format_value(JSON, value) == '{"a":{"c":3,"d":2},"b":1}'


@pytest.mark.parametrize(
    "value", [{"a": 1, "b": [1, "x", None]}, [], "マイカ", 0.5, None]
)
def test_json_round_trip(value):
    assert_round_trip(JSON, value)


# Definition checks


@pytest.mark.parametrize(
    "key",
    [
        "nodot",
        "Upper.case",
        "log.Channel",
        "has space.x",
        "has-hyphen.x",
        ".leading",
        "trailing.",
        "double..dot",
        "",
    ],
)
def test_definition_rejects_bad_key(key):
    with pytest.raises(ValueError, match="area.name"):
        make(ValueType.INT, key=key)


@pytest.mark.parametrize("key", ["a.b", "log.channel.bot_log", "x1.y_2"])
def test_definition_accepts_good_key(key):
    make(ValueType.INT, key=key)


def test_definition_rejects_long_description():
    with pytest.raises(ValueError, match="description"):
        make(ValueType.INT, description="x" * 101)


def test_definition_accepts_100_character_description():
    make(ValueType.INT, description="x" * 100)


def test_definition_rejects_empty_description():
    with pytest.raises(ValueError, match="description"):
        make(ValueType.INT, description="")


@pytest.mark.parametrize("default", [0, 366])
def test_definition_rejects_default_outside_range(default):
    with pytest.raises(ValueError, match="default"):
        make(ValueType.INT, minimum=1, maximum=365, default=default)


def test_definition_rejects_default_not_a_choice():
    with pytest.raises(ValueError, match="default"):
        make(ValueType.TEXT, choices=("a", "b"), default="c")


@pytest.mark.parametrize(
    ("value_type", "default"),
    [
        (ValueType.INT, "30"),
        (ValueType.INT, 1.5),
        (ValueType.BOOL, "true"),
        (ValueType.TIME, "04:00"),
        (ValueType.INT_LIST, [1, 2]),
    ],
)
def test_definition_rejects_default_of_wrong_type(value_type, default):
    with pytest.raises(ValueError, match="default"):
        make(value_type, default=default)


def test_definition_rejects_minimum_above_maximum():
    with pytest.raises(ValueError, match="above maximum"):
        make(ValueType.INT, minimum=10, maximum=5)


def test_definition_accepts_equal_minimum_and_maximum():
    make(ValueType.INT, minimum=5, maximum=5, default=5)


@pytest.mark.parametrize(
    "value_type",
    [ValueType.BOOL, ValueType.TEXT, ValueType.TIME, ValueType.INT_LIST],
)
def test_definition_rejects_range_on_wrong_type(value_type):
    with pytest.raises(ValueError, match="int and float"):
        make(value_type, minimum=1)


@pytest.mark.parametrize(
    "value_type",
    [ValueType.INT, ValueType.FLOAT, ValueType.BOOL, ValueType.STR_LIST],
)
def test_definition_rejects_choices_on_wrong_type(value_type):
    with pytest.raises(ValueError, match="text settings only"):
        make(value_type, choices=("a", "b"))


@pytest.mark.parametrize("choices", [(), ("Info",), (" a",), ("a", "a")])
def test_definition_rejects_bad_choices(choices):
    with pytest.raises(ValueError):
        make(ValueType.TEXT, choices=choices)


def test_definition_with_no_default_is_allowed():
    definition = make(ValueType.INT, minimum=1, maximum=5)
    assert definition.default is None


def test_check_value_passes_valid_default():
    check_value(DAYS, 30)


# Registry


def test_registry_rejects_duplicate_keys():
    with pytest.raises(ValueError, match="test.value"):
        SettingsRegistry([make(ValueType.INT), make(ValueType.BOOL)])


def test_registry_get_and_unknown_key():
    registry = SettingsRegistry([BOOL, DAYS])
    assert registry.get("backups.days_kept") is DAYS
    with pytest.raises(UnknownSettingError, match="no.such"):
        registry.get("no.such")


def test_registry_keys_in_given_order():
    registry = SettingsRegistry([DAYS, BOOL])
    assert registry.keys() == ("backups.days_kept", "test.value")


def test_registry_category_listing():
    first = make(ValueType.INT, key="a.one", category="alpha")
    second = make(ValueType.INT, key="b.two", category="beta")
    third = make(ValueType.INT, key="a.three", category="alpha")
    registry = SettingsRegistry([first, second, third])
    assert registry.definitions_in_category("alpha") == (first, third)
    assert registry.definitions_in_category("beta") == (second,)
    assert registry.definitions_in_category("gamma") == ()


# Foundation settings

FOUNDATION_KEYS = {
    "log.channel.bot_log",
    "log.channel.errors",
    "log.channel.permissions",
    "log.channel.feeds",
    "log.channel.decisions",
    "log.channel.persona_log",
    "log.channel.events",
    "log.channel.memory_review",
    "log.level.bot_log",
    "log.level.errors",
    "log.level.permissions",
    "log.level.feeds",
    "log.level.decisions",
    "log.level.persona_log",
    "log.level.events",
    "log.level.memory_review",
    "backups.time",
    "backups.days_kept",
    "backups.migration_copies",
    "backups.offsite_copies_kept",
    "backups.offsite_enabled",
    "backups.offsite_day",
    "backups.offsite_remote",
    "commands.confirm_timeout_seconds",
    "reminders.default_time",
    "reminders.max_pending_per_person",
    "reminders.check_seconds",
}


def test_foundation_has_exactly_27_settings():
    assert len(FOUNDATION_SETTINGS) == 27


def test_foundation_keys_match_design():
    assert {d.key for d in FOUNDATION_SETTINGS} == FOUNDATION_KEYS


def test_foundation_defaults_are_valid():
    for definition in FOUNDATION_SETTINGS:
        if definition.default is not None:
            check_value(definition, definition.default)
            assert_round_trip(definition, definition.default)


def test_foundation_categories():
    assert LOGGING_CATEGORY == "logging"
    assert BACKUPS_CATEGORY == "backups"
    assert COMMANDS_CATEGORY == "commands"
    assert REMINDERS_CATEGORY == "reminders"
    assert {d.category for d in FOUNDATION_SETTINGS} == {
        "logging",
        "backups",
        "commands",
        "reminders",
    }


def test_foundation_all_global():
    assert not any(d.server_scope for d in FOUNDATION_SETTINGS)


def test_foundation_registry():
    registry = build_foundation_registry()
    assert set(registry.keys()) == FOUNDATION_KEYS
    assert len(registry.definitions_in_category("logging")) == 16
    assert all(
        d.key.startswith("log.")
        for d in registry.definitions_in_category("logging")
    )
    assert [d.key for d in registry.definitions_in_category("backups")] == [
        "backups.time",
        "backups.days_kept",
        "backups.migration_copies",
        "backups.offsite_copies_kept",
        "backups.offsite_enabled",
        "backups.offsite_day",
        "backups.offsite_remote",
    ]
    assert [d.key for d in registry.definitions_in_category("commands")] == [
        "commands.confirm_timeout_seconds"
    ]
    assert [d.key for d in registry.definitions_in_category("reminders")] == [
        "reminders.default_time",
        "reminders.max_pending_per_person",
        "reminders.check_seconds",
    ]


def test_reminder_settings():
    registry = build_foundation_registry()
    default_time = registry.get("reminders.default_time")
    max_pending = registry.get("reminders.max_pending_per_person")
    check_seconds = registry.get("reminders.check_seconds")

    assert default_time.value_type is ValueType.TIME
    assert default_time.default == datetime.time(8, 0)
    assert default_time.level is SettingLevel.BASIC
    assert default_time.server_scope is False
    assert default_time.description == (
        "Clock time used when a reminder gives a day but no time."
    )
    assert max_pending.value_type is ValueType.INT
    assert max_pending.default == 25
    assert (max_pending.minimum, max_pending.maximum) == (1, 500)
    assert max_pending.level is SettingLevel.ADVANCED
    assert max_pending.server_scope is False
    assert max_pending.description == "Pending reminders one person may hold."
    assert check_seconds.category == "reminders"
    assert check_seconds.value_type is ValueType.INT
    assert check_seconds.default == 1
    assert (check_seconds.minimum, check_seconds.maximum) == (1, 300)
    assert check_seconds.level is SettingLevel.ADVANCED
    assert check_seconds.server_scope is False
    assert check_seconds.description == (
        "Seconds between checks for due reminders."
    )


@pytest.mark.parametrize("text", ["1", "300"])
def test_check_seconds_accepts_range_ends(text):
    check_seconds = build_foundation_registry().get("reminders.check_seconds")
    assert parse_value(check_seconds, text) == int(text)


@pytest.mark.parametrize("text", ["0", "301"])
def test_check_seconds_rejects_outside_range(text):
    check_seconds = build_foundation_registry().get("reminders.check_seconds")
    with pytest.raises(SettingValueError):
        parse_value(check_seconds, text)


def test_every_backups_setting_is_in_backups_category():
    backups = [d for d in FOUNDATION_SETTINGS if d.key.startswith("backups.")]
    assert len(backups) == 7
    assert all(d.category == BACKUPS_CATEGORY for d in backups)


def test_offsite_settings_definitions():
    registry = build_foundation_registry()

    enabled = registry.get("backups.offsite_enabled")
    assert enabled.value_type is ValueType.BOOL
    assert enabled.default is False
    assert enabled.level is SettingLevel.BASIC
    assert enabled.description == "Weekly offsite copy on or off."

    day = registry.get("backups.offsite_day")
    assert day.value_type is ValueType.TEXT
    assert day.default == "sunday"
    assert day.choices == (
        "monday",
        "tuesday",
        "wednesday",
        "thursday",
        "friday",
        "saturday",
        "sunday",
    )
    assert day.level is SettingLevel.BASIC
    assert day.description == "Weekday of the weekly offsite copy."

    remote = registry.get("backups.offsite_remote")
    assert remote.value_type is ValueType.TEXT
    assert remote.default == "b2offsite:maika-backups"
    assert remote.level is SettingLevel.ADVANCED
    assert remote.description == (
        "rclone remote and folder for the offsite copies."
    )


def test_confirm_timeout_definition():
    definition = build_foundation_registry().get(CONFIRM_TIMEOUT_KEY)

    assert CONFIRM_TIMEOUT_KEY == "commands.confirm_timeout_seconds"
    assert definition.category == "commands"
    assert definition.level is SettingLevel.ADVANCED
    assert definition.server_scope is False
    assert definition.value_type is ValueType.INT
    assert definition.default == 60
    assert definition.minimum == 10
    assert definition.maximum == 600
    assert definition.choices is None
    assert definition.description == (
        "Seconds before confirmation buttons stop working."
    )


@pytest.mark.parametrize("text", ["10", "60", "600"])
def test_confirm_timeout_accepts_range(text):
    definition = build_foundation_registry().get(CONFIRM_TIMEOUT_KEY)
    assert parse_value(definition, text) == int(text)


@pytest.mark.parametrize("text", ["9", "601", "0", "-5"])
def test_confirm_timeout_rejects_outside_range(text):
    definition = build_foundation_registry().get(CONFIRM_TIMEOUT_KEY)
    with pytest.raises(SettingValueError):
        parse_value(definition, text)


@pytest.mark.parametrize(
    ("key", "value_type", "default", "minimum", "maximum", "level"),
    [
        ("log.channel.bot_log", ValueType.INT, None, 1, None, "BASIC"),
        ("log.level.errors", ValueType.TEXT, "info", None, None, "ADVANCED"),
        (
            "backups.time",
            ValueType.TIME,
            datetime.time(4, 0),
            None,
            None,
            "BASIC",
        ),
        ("backups.days_kept", ValueType.INT, 30, 1, 365, "BASIC"),
        ("backups.migration_copies", ValueType.INT, 10, 1, 50, "ADVANCED"),
        ("backups.offsite_copies_kept", ValueType.INT, 4, 1, 52, "ADVANCED"),
    ],
)
def test_foundation_sample_definitions(
    key, value_type, default, minimum, maximum, level
):
    definition = build_foundation_registry().get(key)
    assert definition.value_type is value_type
    assert definition.default == default
    assert definition.minimum == minimum
    assert definition.maximum == maximum
    assert definition.level is SettingLevel[level]


def test_foundation_log_channels_start_at_one():
    channels = [
        definition
        for definition in FOUNDATION_SETTINGS
        if definition.key.startswith("log.channel.")
    ]
    assert len(channels) == 8
    for definition in channels:
        assert definition.minimum == 1
        assert definition.maximum is None


def test_foundation_log_level_choices():
    for definition in FOUNDATION_SETTINGS:
        if definition.key.startswith("log.level."):
            assert definition.choices == ("debug", "info", "warning", "error")
