from pathlib import Path

import pytest

from maika.config import (
    DEFAULT_DATABASE_PATH,
    MAX_DISCORD_ID,
    Config,
    ConfigError,
    Environment,
    load_config,
)

TOKEN = "fake-token.abc123.secret-value"
REQUIRED_VARS = ["ENVIRONMENT", "DISCORD_TOKEN", "OWNER_ID"]

# Written out rather than built from MAX_DISCORD_ID, so a wrong
# constant fails these tests.
ID_RULE = "must be a positive integer up to 9223372036854775807."


def valid_environ(**overrides: str) -> dict[str, str]:
    environ = {
        "ENVIRONMENT": "test",
        "DISCORD_TOKEN": TOKEN,
        "OWNER_ID": "123456789012345678",
        "LISTEN_CHANNELS": "111, 222",
        "LOG_CHANNEL_ID": "333",
        "DATABASE_PATH": "data/test.db",
    }
    environ.update(overrides)
    return environ


def minimal_environ() -> dict[str, str]:
    return {
        "ENVIRONMENT": "prod",
        "DISCORD_TOKEN": TOKEN,
        "OWNER_ID": "42",
    }


def config_error(environ: dict[str, str]) -> ConfigError:
    with pytest.raises(ConfigError) as caught:
        load_config(environ)
    return caught.value


# Valid configuration


def test_fully_valid_mapping():
    config = load_config(valid_environ())

    assert config == Config(
        environment=Environment.TEST,
        discord_token=TOKEN,
        owner_id=123456789012345678,
        listen_channels=(111, 222),
        log_channel_id=333,
        database_path=Path("data/test.db"),
    )


def test_prod_environment():
    config = load_config(valid_environ(ENVIRONMENT="prod"))

    assert config.environment is Environment.PROD


def test_environment_values():
    assert Environment.TEST.value == "test"
    assert Environment.PROD.value == "prod"


def test_optional_variables_missing():
    config = load_config(minimal_environ())

    assert config.listen_channels == ()
    assert config.log_channel_id is None
    assert config.database_path == DEFAULT_DATABASE_PATH


def test_database_path_default():
    assert DEFAULT_DATABASE_PATH == Path("var/maika.db")
    assert load_config(minimal_environ()).database_path == Path("var/maika.db")


def test_empty_optional_variables():
    environ = valid_environ(
        LISTEN_CHANNELS="", LOG_CHANNEL_ID="", DATABASE_PATH=""
    )

    config = load_config(environ)

    assert config.listen_channels == ()
    assert config.log_channel_id is None
    assert config.database_path == DEFAULT_DATABASE_PATH


def test_whitespace_around_values_is_stripped():
    environ = valid_environ(
        ENVIRONMENT=" test ",
        DISCORD_TOKEN=f"  {TOKEN}\n",
        OWNER_ID=" 42 ",
        LOG_CHANNEL_ID=" 333 ",
    )

    config = load_config(environ)

    assert config.environment is Environment.TEST
    assert config.discord_token == TOKEN
    assert config.owner_id == 42
    assert config.log_channel_id == 333


def test_config_is_frozen():
    config = load_config(valid_environ())

    with pytest.raises(AttributeError):
        config.owner_id = 1  # type: ignore[misc]


# Required variables


@pytest.mark.parametrize("name", REQUIRED_VARS)
def test_required_variable_missing(name):
    environ = valid_environ()
    del environ[name]

    error = config_error(environ)

    assert error.problems == (f"{name} is required but is not set.",)


@pytest.mark.parametrize("name", REQUIRED_VARS)
@pytest.mark.parametrize("empty", ["", "   ", "\t\n"])
def test_required_variable_empty(name, empty):
    error = config_error(valid_environ(**{name: empty}))

    assert error.problems == (f"{name} is required but is empty.",)


# Invalid values


@pytest.mark.parametrize("value", ["production", "TEST", "dev", "Prod"])
def test_invalid_environment(value):
    error = config_error(valid_environ(ENVIRONMENT=value))

    assert error.problems == ('ENVIRONMENT must be "test" or "prod".',)


@pytest.mark.parametrize("value", ["abc", "12.5", "12 34", "0x1F"])
def test_non_integer_owner_id(value):
    error = config_error(valid_environ(OWNER_ID=value))

    assert error.problems == (f"OWNER_ID {ID_RULE}",)


@pytest.mark.parametrize("value", ["abc", "12.5", "#bot-log"])
def test_non_integer_log_channel_id(value):
    error = config_error(valid_environ(LOG_CHANNEL_ID=value))

    assert error.problems == (f"LOG_CHANNEL_ID {ID_RULE}",)


def test_non_integer_listen_channel_item():
    error = config_error(valid_environ(LISTEN_CHANNELS="111, abc, 333"))

    assert error.problems == (f"LISTEN_CHANNELS item 2 {ID_RULE}",)


def test_every_bad_listen_channel_item_reported():
    error = config_error(valid_environ(LISTEN_CHANNELS="x, 222, 3.5"))

    assert error.problems == (
        f"LISTEN_CHANNELS item 1 {ID_RULE}",
        f"LISTEN_CHANNELS item 3 {ID_RULE}",
    )


# IDs must be positive and written with ASCII digits only

LARGE_ID = "1234567890123456789"

# Each of these is accepted by int() or is not positive.
REJECTED_ID_FORMS = [
    pytest.param("+5", id="plus-sign"),
    pytest.param("1_000", id="underscore"),
    pytest.param("0", id="zero"),
    pytest.param("000", id="zeros"),
    pytest.param("-5", id="negative"),
    pytest.param("-0", id="negative-zero"),
    pytest.param("²", id="superscript-two"),
    pytest.param("12²", id="trailing-superscript"),
    pytest.param("١٢٣", id="arabic-indic-digits"),
    pytest.param("１２３", id="fullwidth-digits"),
]


# The exact-message checks below also prove the value is not
# repeated. A substring check cannot be used for these short forms,
# because "0" appears inside the limit printed in the message.


@pytest.mark.parametrize("value", REJECTED_ID_FORMS)
def test_owner_id_rejected_forms(value):
    error = config_error(valid_environ(OWNER_ID=value))

    assert error.problems == (f"OWNER_ID {ID_RULE}",)


@pytest.mark.parametrize("value", REJECTED_ID_FORMS)
def test_log_channel_id_rejected_forms(value):
    error = config_error(valid_environ(LOG_CHANNEL_ID=value))

    assert error.problems == (f"LOG_CHANNEL_ID {ID_RULE}",)


@pytest.mark.parametrize("value", REJECTED_ID_FORMS)
def test_listen_channels_item_rejected_forms(value):
    error = config_error(valid_environ(LISTEN_CHANNELS=f"111, {value}"))

    assert error.problems == (f"LISTEN_CHANNELS item 2 {ID_RULE}",)


# The upper bound: the largest value a SQLite INTEGER holds

LARGEST_ID = "9223372036854775807"
ONE_ABOVE_LARGEST_ID = "9223372036854775808"
# More digits than int() accepts by default (4300).
HUGE_ID = "9" * 5000

TOO_LARGE_IDS = [
    pytest.param(ONE_ABOVE_LARGEST_ID, id="one-above-largest"),
    pytest.param(HUGE_ID, id="5000-digits"),
]


def test_max_discord_id_constant():
    assert MAX_DISCORD_ID == 9223372036854775807
    assert str(MAX_DISCORD_ID) == LARGEST_ID


def test_largest_id_accepted():
    environ = valid_environ(
        OWNER_ID=LARGEST_ID,
        LOG_CHANNEL_ID=LARGEST_ID,
        LISTEN_CHANNELS=f"1, {LARGEST_ID}",
    )

    config = load_config(environ)

    assert config.owner_id == 9223372036854775807
    assert config.log_channel_id == 9223372036854775807
    assert config.listen_channels == (1, 9223372036854775807)


@pytest.mark.parametrize("value", TOO_LARGE_IDS)
def test_owner_id_too_large(value):
    error = config_error(valid_environ(OWNER_ID=value))

    assert error.problems == (f"OWNER_ID {ID_RULE}",)
    assert value not in str(error)


@pytest.mark.parametrize("value", TOO_LARGE_IDS)
def test_log_channel_id_too_large(value):
    error = config_error(valid_environ(LOG_CHANNEL_ID=value))

    assert error.problems == (f"LOG_CHANNEL_ID {ID_RULE}",)
    assert value not in str(error)


@pytest.mark.parametrize("value", TOO_LARGE_IDS)
def test_listen_channels_item_too_large(value):
    error = config_error(valid_environ(LISTEN_CHANNELS=f"111, {value}"))

    assert error.problems == (f"LISTEN_CHANNELS item 2 {ID_RULE}",)
    assert value not in str(error)


def test_huge_ids_everywhere_give_one_config_error():
    environ = valid_environ(
        OWNER_ID=HUGE_ID,
        LOG_CHANNEL_ID=HUGE_ID,
        LISTEN_CHANNELS=f"{HUGE_ID}, {HUGE_ID}",
    )

    error = config_error(environ)

    assert error.problems == (
        f"OWNER_ID {ID_RULE}",
        f"LISTEN_CHANNELS item 1 {ID_RULE}",
        f"LISTEN_CHANNELS item 2 {ID_RULE}",
        f"LOG_CHANNEL_ID {ID_RULE}",
    )


def test_large_ids_accepted():
    environ = valid_environ(
        OWNER_ID=LARGE_ID,
        LOG_CHANNEL_ID=LARGE_ID,
        LISTEN_CHANNELS=f"{LARGE_ID}, 1",
    )

    config = load_config(environ)

    assert config.owner_id == 1234567890123456789
    assert config.log_channel_id == 1234567890123456789
    assert config.listen_channels == (1234567890123456789, 1)


def test_leading_zeros_accepted():
    config = load_config(valid_environ(OWNER_ID="007"))

    assert config.owner_id == 7


# LISTEN_CHANNELS parsing


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("111", (111,)),
        ("111,222,333", (111, 222, 333)),
        ("111, 222, 333", (111, 222, 333)),
        ("  111 ,222  ,  333  ", (111, 222, 333)),
        ("111,", (111,)),
        ("111, 222,", (111, 222)),
        ("111, 222, ", (111, 222)),
        ("", ()),
        ("   ", ()),
    ],
)
def test_listen_channels_parsing(value, expected):
    config = load_config(valid_environ(LISTEN_CHANNELS=value))

    assert config.listen_channels == expected


def test_listen_channels_missing():
    environ = valid_environ()
    del environ["LISTEN_CHANNELS"]

    assert load_config(environ).listen_channels == ()


@pytest.mark.parametrize(
    ("value", "problem"),
    [
        ("111,,222", "LISTEN_CHANNELS item 2 is empty."),
        (",111", "LISTEN_CHANNELS item 1 is empty."),
        ("111,,", "LISTEN_CHANNELS item 2 is empty."),
        (",", "LISTEN_CHANNELS item 1 is empty."),
    ],
)
def test_listen_channels_empty_item(value, problem):
    error = config_error(valid_environ(LISTEN_CHANNELS=value))

    assert error.problems == (problem,)


# Several problems at once


def test_several_problems_in_one_error():
    environ = {
        "ENVIRONMENT": "staging",
        "OWNER_ID": "not-a-number",
        "LISTEN_CHANNELS": "1, two",
        "LOG_CHANNEL_ID": "log",
    }

    error = config_error(environ)

    expected = (
        'ENVIRONMENT must be "test" or "prod".',
        "DISCORD_TOKEN is required but is not set.",
        f"OWNER_ID {ID_RULE}",
        f"LISTEN_CHANNELS item 2 {ID_RULE}",
        f"LOG_CHANNEL_ID {ID_RULE}",
    )
    assert error.problems == expected
    assert str(error) == "\n".join(expected)


def test_empty_mapping_reports_every_required_variable():
    error = config_error({})

    assert error.problems == tuple(
        f"{name} is required but is not set." for name in REQUIRED_VARS
    )


# The token stays secret


def test_token_absent_from_repr():
    config = load_config(valid_environ())

    assert TOKEN not in repr(config)
    assert "discord_token" not in repr(config)
    assert TOKEN not in str(config)


@pytest.mark.parametrize(
    "overrides",
    [
        {"ENVIRONMENT": "staging"},
        {"OWNER_ID": "abc"},
        {"OWNER_ID": ""},
        {"LISTEN_CHANNELS": "1,,x"},
        {"LOG_CHANNEL_ID": "abc"},
        # The token pasted into the wrong variable is not echoed.
        {"OWNER_ID": TOKEN},
        {"LOG_CHANNEL_ID": TOKEN},
        {"LISTEN_CHANNELS": f"111, {TOKEN}"},
        {"ENVIRONMENT": TOKEN},
        {
            "ENVIRONMENT": "",
            "OWNER_ID": "x",
            "LISTEN_CHANNELS": "y",
            "LOG_CHANNEL_ID": "z",
        },
    ],
)
def test_token_absent_from_error_messages(overrides):
    error = config_error(valid_environ(**overrides))

    assert TOKEN not in str(error)
    assert TOKEN not in repr(error)
    for problem in error.problems:
        assert TOKEN not in problem
