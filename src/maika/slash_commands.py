"""The discord.py slash commands, one thin callback per command.

Each callback gathers its arguments and calls
``discord_adapter.handle_command`` with a function that runs the
command. The permission check, the usage log and the reply all
happen there.
"""

import sqlite3

import discord
from discord import app_commands

from maika.commands import CommandRegistry
from maika.config_commands import CONFIG_GET_NAME, config_get
from maika.discord_adapter import guild_id_from_interaction, handle_command

CONFIG_GROUP_NAME = "config"
CONFIG_GROUP_DESCRIPTION = "View and change settings (owner only)"
CONFIG_GET_SUBCOMMAND = "get"
CONFIG_GET_DESCRIPTION = "Show a setting's current value"
CONFIG_KEY_DESCRIPTION = "The setting name, for example backups.days_kept"


def build_config_group(
    *,
    connection: sqlite3.Connection,
    registry: CommandRegistry,
    owner_id: int,
) -> app_commands.Group:
    """Return the /config group with its subcommands."""
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
        )

    return group
