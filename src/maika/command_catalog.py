"""The list of every command the bot registers.

Each command module defines its CommandDefinition; this module
collects them into the one CommandRegistry the bot uses.
"""

from maika.commands import CommandRegistry
from maika.config_commands import (
    CONFIG_GET_COMMAND,
    CONFIG_LIST_COMMAND,
    CONFIG_RESET_COMMAND,
    CONFIG_SET_COMMAND,
)


def build_command_registry() -> CommandRegistry:
    """Return the registry of every command."""
    return CommandRegistry(
        [
            CONFIG_GET_COMMAND,
            CONFIG_SET_COMMAND,
            CONFIG_RESET_COMMAND,
            CONFIG_LIST_COMMAND,
        ]
    )
