"""Load the bot's startup configuration from ``.env`` values.

The values that differ between the test bot and the real Maika (the
environment name, the token, the owner, the starting channels and the
database file) come from environment variables, usually read from a
``.env`` file. See ``docs/design.md`` section 5, "Test and production
copies".

``load_config`` checks every variable and reports all problems in one
``ConfigError``. No message ever contains the token's value.
"""

import enum
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ENVIRONMENT_VAR = "ENVIRONMENT"
DISCORD_TOKEN_VAR = "DISCORD_TOKEN"
OWNER_ID_VAR = "OWNER_ID"
LISTEN_CHANNELS_VAR = "LISTEN_CHANNELS"
LOG_CHANNEL_ID_VAR = "LOG_CHANNEL_ID"
DATABASE_PATH_VAR = "DATABASE_PATH"

DEFAULT_DATABASE_PATH = Path("var/maika.db")

# Relative to the working directory, as load_dotenv resolves it.
DOTENV_FILE = Path(".env")

LIST_SEPARATOR = ","

# The largest value a SQLite INTEGER holds (a signed 64-bit integer).
MAX_DISCORD_ID = 2**63 - 1

# Text longer than this cannot be an ID, so it is rejected before
# int() sees it. int() raises ValueError above 4300 digits.
MAX_DISCORD_ID_DIGITS = len(str(MAX_DISCORD_ID))

DISCORD_ID_RULE = f"must be a positive integer up to {MAX_DISCORD_ID}"


class Environment(enum.Enum):
    """Which copy of the bot is running."""

    TEST = "test"
    PROD = "prod"


@dataclass(frozen=True)
class Config:
    """Startup configuration read from the environment.

    ``discord_token`` is left out of the repr so that printing or
    logging a Config never shows the token.
    """

    environment: Environment
    discord_token: str = field(repr=False)
    owner_id: int
    listen_channels: tuple[int, ...]
    log_channel_id: int | None
    database_path: Path


class ConfigError(Exception):
    """Raised when the configuration has one or more problems.

    The message lists every problem, one per line. ``problems`` holds
    the same lines as a tuple.
    """

    def __init__(self, problems: list[str]) -> None:
        """Store the problems and build the message from them."""
        self.problems = tuple(problems)
        super().__init__("\n".join(self.problems))


def _read_required(
    environ: Mapping[str, str], name: str, problems: list[str]
) -> str | None:
    """Return the stripped value of a required variable.

    Record a problem and return None when the variable is missing or
    empty after stripping whitespace.
    """
    if name not in environ:
        problems.append(f"{name} is required but is not set.")
        return None
    value = environ[name].strip()
    if value == "":
        problems.append(f"{name} is required but is empty.")
        return None
    return value


def _read_optional(environ: Mapping[str, str], name: str) -> str | None:
    """Return the stripped value of an optional variable.

    Return None when the variable is missing or empty after stripping
    whitespace.
    """
    value = environ.get(name, "").strip()
    if value == "":
        return None
    return value


def _to_discord_id(text: str) -> int | None:
    """Convert text to a Discord ID, or return None if it is not one.

    A Discord ID is written with the ASCII digits 0-9 only, is greater
    than 0 and is no larger than ``MAX_DISCORD_ID``. ``int()`` alone
    would also accept a sign, ``_`` separators and non-ASCII digits,
    so the text is checked first. Text with more digits than
    ``MAX_DISCORD_ID`` is rejected before ``int()``, so a huge string
    never reaches it.
    """
    if not (text.isascii() and text.isdigit()):
        return None
    if len(text) > MAX_DISCORD_ID_DIGITS:
        return None
    number = int(text)
    if number <= 0 or number > MAX_DISCORD_ID:
        return None
    return number


def _parse_id(name: str, value: str, problems: list[str]) -> int | None:
    """Convert a value to a Discord ID.

    Record a problem and return None when the value is not a positive
    integer up to ``MAX_DISCORD_ID``. The message names the variable
    but does not repeat the value.
    """
    discord_id = _to_discord_id(value)
    if discord_id is None:
        problems.append(f"{name} {DISCORD_ID_RULE}.")
    return discord_id


def _parse_environment(value: str, problems: list[str]) -> Environment | None:
    """Convert a value to an Environment.

    Record a problem and return None when the value is not one of the
    allowed names.
    """
    try:
        return Environment(value)
    except ValueError:
        allowed = " or ".join(f'"{member.value}"' for member in Environment)
        problems.append(f"{ENVIRONMENT_VAR} must be {allowed}.")
        return None


def _parse_id_list(
    name: str, value: str, problems: list[str]
) -> tuple[int, ...]:
    """Convert a comma-separated value to a tuple of Discord IDs.

    Spaces around items and one trailing comma are allowed. An empty
    item anywhere else, or an item that is not a positive integer up
    to ``MAX_DISCORD_ID``, records a problem naming its position
    (counted from 1).
    """
    if value.endswith(LIST_SEPARATOR):
        value = value.removesuffix(LIST_SEPARATOR)

    numbers: list[int] = []
    for position, item in enumerate(value.split(LIST_SEPARATOR), start=1):
        item = item.strip()
        if item == "":
            problems.append(f"{name} item {position} is empty.")
            continue
        discord_id = _to_discord_id(item)
        if discord_id is None:
            problems.append(f"{name} item {position} {DISCORD_ID_RULE}.")
            continue
        numbers.append(discord_id)
    return tuple(numbers)


def load_config(environ: Mapping[str, str]) -> Config:
    """Build a Config from a mapping of environment variables.

    Every variable is checked before anything is raised, so a single
    ConfigError lists all the problems at once.
    """
    problems: list[str] = []

    environment = None
    environment_text = _read_required(environ, ENVIRONMENT_VAR, problems)
    if environment_text is not None:
        environment = _parse_environment(environment_text, problems)

    discord_token = _read_required(environ, DISCORD_TOKEN_VAR, problems)

    owner_id = None
    owner_id_text = _read_required(environ, OWNER_ID_VAR, problems)
    if owner_id_text is not None:
        owner_id = _parse_id(OWNER_ID_VAR, owner_id_text, problems)

    listen_channels: tuple[int, ...] = ()
    listen_channels_text = _read_optional(environ, LISTEN_CHANNELS_VAR)
    if listen_channels_text is not None:
        listen_channels = _parse_id_list(
            LISTEN_CHANNELS_VAR, listen_channels_text, problems
        )

    log_channel_id = None
    log_channel_id_text = _read_optional(environ, LOG_CHANNEL_ID_VAR)
    if log_channel_id_text is not None:
        log_channel_id = _parse_id(
            LOG_CHANNEL_ID_VAR, log_channel_id_text, problems
        )

    database_path = DEFAULT_DATABASE_PATH
    database_path_text = _read_optional(environ, DATABASE_PATH_VAR)
    if database_path_text is not None:
        database_path = Path(database_path_text)

    if problems:
        raise ConfigError(problems)

    # Every None path above records a problem, so this cannot happen.
    # The check tells the type checker the values are set, and unlike
    # assert it still runs under ``python -O``.
    if environment is None or discord_token is None or owner_id is None:
        raise RuntimeError(
            "load_config found no problems but a required value is "
            "missing. This is a bug in load_config."
        )

    return Config(
        environment=environment,
        discord_token=discord_token,
        owner_id=owner_id,
        listen_channels=listen_channels,
        log_channel_id=log_channel_id,
        database_path=database_path,
    )


def load_config_from_env() -> Config:
    """Read ``.env`` from the working directory, then load the config.

    Variables already set in the real environment win over ``.env``.
    A missing ``.env`` file is not an error, because production may set
    every variable directly.
    """
    load_dotenv(DOTENV_FILE, override=False)
    return load_config(os.environ)
