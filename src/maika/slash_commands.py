"""The discord.py slash commands, one thin callback per command.

Each callback gathers its arguments and calls
``discord_adapter.handle_command`` with a function that runs the
command. The permission check, the usage log and the reply all
happen there.
"""

import sqlite3
from collections.abc import Callable

import discord
from discord import app_commands

from maika.command_runner import CommandUsage
from maika.commands import CommandRegistry
from maika.config_commands import (
    CONFIG_GET_NAME,
    CONFIG_RESET_NAME,
    CONFIG_SET_NAME,
    SETTINGS,
    config_get,
    config_reset,
    config_set,
)
from maika.discord_adapter import (
    context_from_interaction,
    guild_id_from_interaction,
    handle_command,
)
from maika.log_router import LogLevel, LogRouter, LogStream

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

    @group.command(
        name=CONFIG_GET_SUBCOMMAND, description=CONFIG_GET_DESCRIPTION
    )
    @app_commands.describe(key=CONFIG_KEY_DESCRIPTION)
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

    return group
