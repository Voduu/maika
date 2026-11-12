"""The list of every command and permission key the bot registers.

Each command module defines its CommandDefinition; this module
collects them, plus the keys that have no command yet, into the one
CommandRegistry the bot uses.
"""

from maika.block_commands import BLOCK_COMMAND, UNBLOCK_COMMAND
from maika.commands import CommandRegistry
from maika.config_commands import (
    CONFIG_GET_COMMAND,
    CONFIG_LIST_COMMAND,
    CONFIG_RESET_COMMAND,
    CONFIG_SET_COMMAND,
)
from maika.perm_commands import (
    PERM_DENY_COMMAND,
    PERM_GRANT_COMMAND,
    PERM_LEVEL_COMMAND,
    PERM_RESET_COMMAND,
    PERM_SHOW_COMMAND,
)
from maika.perm_lookup_commands import PERM_HISTORY_COMMAND, PERM_KEYS_COMMAND
from maika.permissions import Level, PermissionKey
from maika.prefs_commands import (
    BIRTHDAY_SET_COMMAND,
    TIMEZONE_SET_COMMAND,
    TIMEZONE_SHOW_COMMAND,
)
from maika.remind_commands import (
    REMIND_ADD_COMMAND,
    REMIND_CANCEL_COMMAND,
    REMIND_LIST_COMMAND,
)

# The memory keys (design sections 1.2 and 2.5). The commands come at
# build step 13; the keys are registered now so overrides and
# /perm keys list them early.
MEMORY_KEYS = (
    PermissionKey(
        "memory.view",
        Level.EVERYONE,
        protected=True,
        description="View your own memories (/memories)",
    ),
    PermissionKey(
        "memory.add",
        Level.MEMBER,
        description=(
            "Save a memory about yourself or another person (/remember)"
        ),
    ),
    PermissionKey(
        "memory.forget",
        Level.EVERYONE,
        protected=True,
        description="Delete your own memories (/forget, /forget all)",
    ),
    PermissionKey(
        "memory.manage",
        Level.ADMIN,
        description="View and delete other people's memories",
    ),
)


def build_command_registry() -> CommandRegistry:
    """Return the registry of every command and command-less key."""
    return CommandRegistry(
        [
            CONFIG_GET_COMMAND,
            CONFIG_SET_COMMAND,
            CONFIG_RESET_COMMAND,
            CONFIG_LIST_COMMAND,
            PERM_LEVEL_COMMAND,
            PERM_SHOW_COMMAND,
            PERM_GRANT_COMMAND,
            PERM_DENY_COMMAND,
            PERM_RESET_COMMAND,
            PERM_HISTORY_COMMAND,
            PERM_KEYS_COMMAND,
            BLOCK_COMMAND,
            UNBLOCK_COMMAND,
            TIMEZONE_SET_COMMAND,
            TIMEZONE_SHOW_COMMAND,
            BIRTHDAY_SET_COMMAND,
            REMIND_ADD_COMMAND,
            REMIND_LIST_COMMAND,
            REMIND_CANCEL_COMMAND,
        ],
        extra_keys=MEMORY_KEYS,
    )
