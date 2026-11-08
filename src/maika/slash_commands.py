"""The discord.py slash commands, one thin callback per command.

Each callback gathers its arguments and calls
``discord_adapter.handle_command`` with a function that runs the
command. The permission check, the usage log and the reply all
happen there.

The ``key`` and ``category`` parameters of /config autocomplete from
the settings registry, for the owner only.
"""

import sqlite3
from collections.abc import Awaitable, Callable

import discord
from discord import app_commands

from maika.command_runner import CommandUsage
from maika.commands import CommandRegistry
from maika.config_commands import (
    CONFIG_GET_NAME,
    CONFIG_LIST_NAME,
    CONFIG_RESET_NAME,
    CONFIG_SET_NAME,
    SETTINGS,
    config_get,
    config_list,
    config_reset,
    config_set,
    setting_categories,
)
from maika.discord_adapter import (
    context_from_interaction,
    guild_id_from_interaction,
    handle_command,
)
from maika.log_router import LogLevel, LogRouter, LogStream
from maika.settings import SettingsRegistry

type Choices = list[app_commands.Choice[str]]
type AutocompleteFunction = Callable[
    [discord.Interaction, str], Awaitable[Choices]
]

# Discord shows at most 25 autocomplete choices, and a choice name is
# at most 100 characters.
AUTOCOMPLETE_CHOICE_LIMIT = 25
CHOICE_NAME_LIMIT = 100
CHOICE_CUT_MARK = "…"
KEY_CHOICE_NAME = "{key}: {description}"

CONFIG_GROUP_NAME = "config"
CONFIG_GROUP_DESCRIPTION = "View and change settings (owner only)"
CONFIG_GET_SUBCOMMAND = "get"
CONFIG_GET_DESCRIPTION = "Show a setting's current value"
CONFIG_KEY_DESCRIPTION = "The setting name, for example backups.days_kept"
CONFIG_SET_SUBCOMMAND = "set"
CONFIG_SET_DESCRIPTION = "Change a setting's value everywhere"
CONFIG_VALUE_DESCRIPTION = "The new value, for example 14"
CONFIG_RESET_SUBCOMMAND = "reset"
CONFIG_RESET_DESCRIPTION = "Put a setting back to its default"
CONFIG_LIST_SUBCOMMAND = "list"
CONFIG_LIST_DESCRIPTION = "List setting categories, or the settings in one"
CONFIG_CATEGORY_DESCRIPTION = "A settings category, for example logging"
CONFIG_ALL_NAME = "all"
CONFIG_ALL_DESCRIPTION = "Include Advanced settings (default: Basic only)"


def _cut_choice_name(text: str) -> str:
    """Return text cut to ``CHOICE_NAME_LIMIT`` with an ellipsis."""
    if len(text) <= CHOICE_NAME_LIMIT:
        return text
    kept_length = CHOICE_NAME_LIMIT - len(CHOICE_CUT_MARK)
    return text[:kept_length] + CHOICE_CUT_MARK


def key_choices(registry: SettingsRegistry, current: str) -> Choices:
    """Return the setting keys that match the typed text.

    The text is matched without regard to case against each key and
    its description. Keys that start with the text come first, then
    the other matches, each group in definition order, up to
    ``AUTOCOMPLETE_CHOICE_LIMIT`` choices. A choice is named
    "<key>: <description>" (cut to ``CHOICE_NAME_LIMIT``) and its
    value is the key.
    """
    text = current.strip().lower()
    starts: list[str] = []
    contains: list[str] = []
    for key in registry.keys():
        description = registry.get(key).description
        if key.lower().startswith(text):
            starts.append(key)
        elif text in key.lower() or text in description.lower():
            contains.append(key)

    keys = (starts + contains)[:AUTOCOMPLETE_CHOICE_LIMIT]
    return [
        app_commands.Choice(
            name=_cut_choice_name(
                KEY_CHOICE_NAME.format(
                    key=key, description=registry.get(key).description
                )
            ),
            value=key,
        )
        for key in keys
    ]


def category_choices(registry: SettingsRegistry, current: str) -> Choices:
    """Return the category names that contain the typed text.

    The match ignores case, and at most ``AUTOCOMPLETE_CHOICE_LIMIT``
    categories are returned, in the order they first appear.
    """
    text = current.strip().lower()
    names = [
        name for name in setting_categories(registry) if text in name.lower()
    ]
    return [
        app_commands.Choice(name=name, value=name)
        for name in names[:AUTOCOMPLETE_CHOICE_LIMIT]
    ]


def owner_autocomplete(
    owner_id: int,
    choices_for: Callable[[SettingsRegistry, str], Choices],
) -> AutocompleteFunction:
    """Return an autocomplete callback that answers the owner only.

    Anyone else gets an empty list, so the setting names are not
    shown to people who cannot use the command.
    """

    async def autocomplete(
        interaction: discord.Interaction, current: str
    ) -> Choices:
        """Return the choices for the owner, or none."""
        if interaction.user.id != owner_id:
            return []
        return choices_for(SETTINGS, current)

    return autocomplete


def build_config_group(
    *,
    connection: sqlite3.Connection,
    registry: CommandRegistry,
    owner_id: int,
    log_usage: Callable[[CommandUsage], None],
    log_router: LogRouter,
) -> app_commands.Group:
    """Return the /config group with its subcommands.

    ``log_usage`` receives each command call's usage record. Set and
    reset announce each change to #bot-log through ``log_router``.
    """
    group = app_commands.Group(
        name=CONFIG_GROUP_NAME, description=CONFIG_GROUP_DESCRIPTION
    )
    key_autocomplete = owner_autocomplete(owner_id, key_choices)
    category_autocomplete = owner_autocomplete(owner_id, category_choices)

    @group.command(
        name=CONFIG_GET_SUBCOMMAND, description=CONFIG_GET_DESCRIPTION
    )
    @app_commands.describe(key=CONFIG_KEY_DESCRIPTION)
    @app_commands.autocomplete(key=key_autocomplete)
    async def config_get_command(
        interaction: discord.Interaction, key: str
    ) -> None:
        """Run /config get."""
        guild_id = guild_id_from_interaction(interaction)
        await handle_command(
            interaction,
            connection=connection,
            registry=registry,
            owner_id=owner_id,
            command_name=CONFIG_GET_NAME,
            function=lambda: config_get(connection, key, guild_id),
            log_usage=log_usage,
        )

    def announce(line: str) -> None:
        """Post a settings change line to #bot-log."""
        log_router.post(LogStream.BOT_LOG, LogLevel.INFO, line)

    @group.command(
        name=CONFIG_SET_SUBCOMMAND, description=CONFIG_SET_DESCRIPTION
    )
    @app_commands.describe(
        key=CONFIG_KEY_DESCRIPTION, value=CONFIG_VALUE_DESCRIPTION
    )
    @app_commands.autocomplete(key=key_autocomplete)
    async def config_set_command(
        interaction: discord.Interaction, key: str, value: str
    ) -> None:
        """Run /config set."""
        context = context_from_interaction(interaction)
        await handle_command(
            interaction,
            connection=connection,
            registry=registry,
            owner_id=owner_id,
            command_name=CONFIG_SET_NAME,
            function=lambda: config_set(
                connection,
                SETTINGS,
                key,
                value,
                actor_id=context.user_id,
                now=context.now,
                announce=announce,
            ),
            log_usage=log_usage,
        )

    @group.command(
        name=CONFIG_RESET_SUBCOMMAND, description=CONFIG_RESET_DESCRIPTION
    )
    @app_commands.describe(key=CONFIG_KEY_DESCRIPTION)
    @app_commands.autocomplete(key=key_autocomplete)
    async def config_reset_command(
        interaction: discord.Interaction, key: str
    ) -> None:
        """Run /config reset."""
        context = context_from_interaction(interaction)
        await handle_command(
            interaction,
            connection=connection,
            registry=registry,
            owner_id=owner_id,
            command_name=CONFIG_RESET_NAME,
            function=lambda: config_reset(
                connection,
                SETTINGS,
                key,
                actor_id=context.user_id,
                now=context.now,
                announce=announce,
            ),
            log_usage=log_usage,
        )

    @group.command(
        name=CONFIG_LIST_SUBCOMMAND, description=CONFIG_LIST_DESCRIPTION
    )
    @app_commands.rename(show_all=CONFIG_ALL_NAME)
    @app_commands.describe(
        category=CONFIG_CATEGORY_DESCRIPTION, show_all=CONFIG_ALL_DESCRIPTION
    )
    @app_commands.autocomplete(category=category_autocomplete)
    async def config_list_command(
        interaction: discord.Interaction,
        category: str | None = None,
        show_all: bool = False,
    ) -> None:
        """Run /config list."""
        await handle_command(
            interaction,
            connection=connection,
            registry=registry,
            owner_id=owner_id,
            command_name=CONFIG_LIST_NAME,
            function=lambda: config_list(
                connection, SETTINGS, category, show_all
            ),
            log_usage=log_usage,
        )

    return group
