"""Entry point: ``python -m maika``."""

import logging
import sys

import discord

from maika.bot import MaikaClient
from maika.config import ConfigError, load_config_from_env

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
LOG_LEVEL = logging.INFO

EXIT_OK = 0
EXIT_CONFIG_ERROR = 1
EXIT_LOGIN_FAILURE = 1

LOGIN_FAILURE_MESSAGE = (
    "Discord rejected the token. Check DISCORD_TOKEN in .env."
)


def configure_logging() -> None:
    """Send log records to the console in a simple format."""
    logging.basicConfig(format=LOG_FORMAT, level=LOG_LEVEL)


def main() -> int:
    """Load the config, run the bot and return the exit status.

    A configuration problem is printed to stderr and returns
    ``EXIT_CONFIG_ERROR`` without connecting to Discord. A token that
    Discord rejects prints ``LOGIN_FAILURE_MESSAGE`` to stderr and
    returns ``EXIT_LOGIN_FAILURE``.
    """
    configure_logging()

    try:
        config = load_config_from_env()
    except ConfigError as error:
        print("Maika cannot start. Configuration problems:", file=sys.stderr)
        for problem in error.problems:
            print(f"- {problem}", file=sys.stderr)
        return EXIT_CONFIG_ERROR

    client = MaikaClient(config)
    # log_handler=None stops discord.py from adding its own console
    # handler, so its records use the format set above.
    try:
        client.run(config.discord_token, log_handler=None)
    except discord.LoginFailure:
        # The exception is not logged or printed, so nothing that
        # could hold the token reaches the console.
        print(LOGIN_FAILURE_MESSAGE, file=sys.stderr)
        return EXIT_LOGIN_FAILURE
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
