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

from maika.block_commands import (
    BLOCK_NAME,
    UNBLOCK_NAME,
    block_user,
    unblock_user,
)
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
from maika.perm_commands import (
    PERM_DENY_NAME,
    PERM_GRANT_NAME,
    PERM_LEVEL_NAME,
    PERM_RESET_NAME,
    PERM_SHOW_NAME,
    SCOPE_OPTION_EVERYWHERE,
    SCOPE_OPTION_SERVER,
    level_name,
    perm_deny,
    perm_grant,
    perm_level,
    perm_reset,
    perm_show,
)
from maika.permissions import (
    BLOCK_KIND_ALL,
    BLOCK_KIND_CHAT,
    KEY_SEPARATOR,
    WILDCARD,
    Level,
    PermissionRegistry,
)
from maika.results import CommandResult
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

PERM_GROUP_NAME = "perm"
PERM_GROUP_DESCRIPTION = "Permission levels and checks (owner only)"
PERM_LEVEL_SUBCOMMAND = "level"
PERM_LEVEL_DESCRIPTION = "Set a person's permission level"
PERM_SHOW_SUBCOMMAND = "show"
PERM_SHOW_DESCRIPTION = "Show what a person may use, and what decided it"
PERM_USER_DESCRIPTION = "The person"
PERM_LEVEL_PARAMETER_DESCRIPTION = "The new level"
PERM_GRANT_SUBCOMMAND = "grant"
PERM_GRANT_DESCRIPTION = "Allow a person a key, whatever their level"
PERM_DENY_SUBCOMMAND = "deny"
PERM_DENY_DESCRIPTION = "Deny a person a key, whatever their level"
PERM_RESET_SUBCOMMAND = "reset"
PERM_RESET_DESCRIPTION = "Remove a person's exception for a key"
PERM_KEY_DESCRIPTION = "A permission key, area.* or *"
PERM_SCOPE_DESCRIPTION = "Where it applies (default: Everywhere)"

BLOCK_DESCRIPTION = "Block a person from chat, or from chat and commands"
BLOCK_KIND_DESCRIPTION = "What to block"
UNBLOCK_DESCRIPTION = "Remove a person's block"

# One choice per level, named as the levels are shown in replies.
LEVEL_CHOICES = [
    app_commands.Choice(name=level_name(level), value=level.value)
    for level in Level
]

SCOPE_CHOICES = [
    app_commands.Choice(name="Everywhere", value=SCOPE_OPTION_EVERYWHERE),
    app_commands.Choice(name="This server", value=SCOPE_OPTION_SERVER),
]

BLOCK_KIND_CHOICES = [
    app_commands.Choice(name="Chat only", value=BLOCK_KIND_CHAT),
    app_commands.Choice(name="Chat and commands", value=BLOCK_KIND_ALL),
]


def _scope_value(scope: app_commands.Choice[str] | None) -> str:
    """Return the chosen scope, or everywhere when none was chosen."""
    if scope is None:
        return SCOPE_OPTION_EVERYWHERE
    return scope.value


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


def permission_patterns(registry: PermissionRegistry) -> tuple[str, ...]:
    """Return every pattern that can carry an exception, sorted.

    That is each key that is neither owner-only nor protected,
    ``area.*`` for each area with such a key, and ``*``. Owner-only
    keys and areas and protected keys are decided before overrides,
    so they are left out. Sorting puts ``*`` first and each
    ``area.*`` before the keys in its area.
    """
    keys = [
        key
        for key in registry.keys()
        if not registry.get(key).owner_only and not registry.get(key).protected
    ]
    areas = {key.split(KEY_SEPARATOR, maxsplit=1)[0] for key in keys}
    wildcards = {f"{area}{KEY_SEPARATOR}{WILDCARD}" for area in areas}
    return tuple(sorted({*keys, *wildcards, WILDCARD}))


def permission_pattern_choices(
    registry: PermissionRegistry, current: str
) -> Choices:
    """Return the patterns that contain the typed text.

    The match ignores case, and at most ``AUTOCOMPLETE_CHOICE_LIMIT``
    patterns are returned in ``permission_patterns`` order. The name
    and value of each choice are the pattern.
    """
    text = current.strip().lower()
    patterns = [
        pattern
        for pattern in permission_patterns(registry)
        if text in pattern.lower()
    ]
    return [
        app_commands.Choice(name=pattern, value=pattern)
        for pattern in patterns[:AUTOCOMPLETE_CHOICE_LIMIT]
    ]


def owner_permission_autocomplete(
    owner_id: int, registry: PermissionRegistry
) -> AutocompleteFunction:
    """Return a pattern autocomplete that answers the owner only."""

    async def autocomplete(
        interaction: discord.Interaction, current: str
    ) -> Choices:
        """Return the pattern choices for the owner, or none."""
        if interaction.user.id != owner_id:
            return []
        return permission_pattern_choices(registry, current)

    return autocomplete


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


def build_perm_group(
    *,
    connection: sqlite3.Connection,
    registry: CommandRegistry,
    owner_id: int,
    log_usage: Callable[[CommandUsage], None],
    log_router: LogRouter,
) -> app_commands.Group:
    """Return the /perm group with its subcommands.

    ``log_usage`` receives each command call's usage record. Level
    and override changes are announced to #permissions through
    ``log_router``.
    """
    group = app_commands.Group(
        name=PERM_GROUP_NAME, description=PERM_GROUP_DESCRIPTION
    )

    def announce(line: str) -> None:
        """Post a level or override change line to #permissions."""
        log_router.post(LogStream.PERMISSIONS, LogLevel.INFO, line)

    @group.command(
        name=PERM_LEVEL_SUBCOMMAND, description=PERM_LEVEL_DESCRIPTION
    )
    @app_commands.describe(
        user=PERM_USER_DESCRIPTION, level=PERM_LEVEL_PARAMETER_DESCRIPTION
    )
    @app_commands.choices(level=LEVEL_CHOICES)
    async def perm_level_command(
        interaction: discord.Interaction,
        user: discord.User,
        level: app_commands.Choice[int],
    ) -> None:
        """Run /perm level."""
        context = context_from_interaction(interaction)
        await handle_command(
            interaction,
            connection=connection,
            registry=registry,
            owner_id=owner_id,
            command_name=PERM_LEVEL_NAME,
            function=lambda: perm_level(
                connection,
                target_id=user.id,
                target_is_bot=user.bot,
                level=Level(level.value),
                owner_id=owner_id,
                actor_id=context.user_id,
                now=context.now,
                announce=announce,
            ),
            log_usage=log_usage,
        )

    @group.command(
        name=PERM_SHOW_SUBCOMMAND, description=PERM_SHOW_DESCRIPTION
    )
    @app_commands.describe(user=PERM_USER_DESCRIPTION)
    async def perm_show_command(
        interaction: discord.Interaction, user: discord.User
    ) -> None:
        """Run /perm show."""
        guild_id = guild_id_from_interaction(interaction)
        await handle_command(
            interaction,
            connection=connection,
            registry=registry,
            owner_id=owner_id,
            command_name=PERM_SHOW_NAME,
            function=lambda: perm_show(
                connection,
                registry.permission_registry(),
                target_id=user.id,
                owner_id=owner_id,
                guild_id=guild_id,
            ),
            log_usage=log_usage,
        )

    permission_registry = registry.permission_registry()
    pattern_autocomplete = owner_permission_autocomplete(
        owner_id, permission_registry
    )

    async def run_override_command(
        interaction: discord.Interaction,
        *,
        command_name: str,
        change: Callable[..., CommandResult],
        user: discord.User,
        key: str,
        scope: app_commands.Choice[str] | None,
    ) -> None:
        """Run grant, deny or reset through handle_command.

        With no scope chosen, the row applies everywhere.
        """
        context = context_from_interaction(interaction)
        scope_value = SCOPE_OPTION_EVERYWHERE
        if scope is not None:
            scope_value = scope.value
        await handle_command(
            interaction,
            connection=connection,
            registry=registry,
            owner_id=owner_id,
            command_name=command_name,
            function=lambda: change(
                connection,
                permission_registry,
                target_id=user.id,
                target_is_bot=user.bot,
                key=key,
                scope=scope_value,
                owner_id=owner_id,
                actor_id=context.user_id,
                guild_id=context.guild_id,
                now=context.now,
                announce=announce,
            ),
            log_usage=log_usage,
        )

    @group.command(
        name=PERM_GRANT_SUBCOMMAND, description=PERM_GRANT_DESCRIPTION
    )
    @app_commands.describe(
        user=PERM_USER_DESCRIPTION,
        key=PERM_KEY_DESCRIPTION,
        scope=PERM_SCOPE_DESCRIPTION,
    )
    @app_commands.choices(scope=SCOPE_CHOICES)
    @app_commands.autocomplete(key=pattern_autocomplete)
    async def perm_grant_command(
        interaction: discord.Interaction,
        user: discord.User,
        key: str,
        scope: app_commands.Choice[str] | None = None,
    ) -> None:
        """Run /perm grant."""
        await run_override_command(
            interaction,
            command_name=PERM_GRANT_NAME,
            change=perm_grant,
            user=user,
            key=key,
            scope=scope,
        )

    @group.command(
        name=PERM_DENY_SUBCOMMAND, description=PERM_DENY_DESCRIPTION
    )
    @app_commands.describe(
        user=PERM_USER_DESCRIPTION,
        key=PERM_KEY_DESCRIPTION,
        scope=PERM_SCOPE_DESCRIPTION,
    )
    @app_commands.choices(scope=SCOPE_CHOICES)
    @app_commands.autocomplete(key=pattern_autocomplete)
    async def perm_deny_command(
        interaction: discord.Interaction,
        user: discord.User,
        key: str,
        scope: app_commands.Choice[str] | None = None,
    ) -> None:
        """Run /perm deny."""
        await run_override_command(
            interaction,
            command_name=PERM_DENY_NAME,
            change=perm_deny,
            user=user,
            key=key,
            scope=scope,
        )

    @group.command(
        name=PERM_RESET_SUBCOMMAND, description=PERM_RESET_DESCRIPTION
    )
    @app_commands.describe(
        user=PERM_USER_DESCRIPTION,
        key=PERM_KEY_DESCRIPTION,
        scope=PERM_SCOPE_DESCRIPTION,
    )
    @app_commands.choices(scope=SCOPE_CHOICES)
    @app_commands.autocomplete(key=pattern_autocomplete)
    async def perm_reset_command(
        interaction: discord.Interaction,
        user: discord.User,
        key: str,
        scope: app_commands.Choice[str] | None = None,
    ) -> None:
        """Run /perm reset."""
        await run_override_command(
            interaction,
            command_name=PERM_RESET_NAME,
            change=perm_reset,
            user=user,
            key=key,
            scope=scope,
        )

    return group


def build_block_commands(
    *,
    connection: sqlite3.Connection,
    registry: CommandRegistry,
    owner_id: int,
    log_usage: Callable[[CommandUsage], None],
    log_router: LogRouter,
) -> list[app_commands.Command]:
    """Return the top-level /block and /unblock commands.

    ``log_usage`` receives each command call's usage record. Block
    changes are announced to #permissions through ``log_router``.
    With no scope chosen, the block applies everywhere.
    """

    def announce(line: str) -> None:
        """Post a block change line to #permissions."""
        log_router.post(LogStream.PERMISSIONS, LogLevel.INFO, line)

    @app_commands.command(name=BLOCK_NAME, description=BLOCK_DESCRIPTION)
    @app_commands.describe(
        user=PERM_USER_DESCRIPTION,
        kind=BLOCK_KIND_DESCRIPTION,
        scope=PERM_SCOPE_DESCRIPTION,
    )
    @app_commands.choices(kind=BLOCK_KIND_CHOICES, scope=SCOPE_CHOICES)
    async def block_command(
        interaction: discord.Interaction,
        user: discord.User,
        kind: app_commands.Choice[str],
        scope: app_commands.Choice[str] | None = None,
    ) -> None:
        """Run /block."""
        context = context_from_interaction(interaction)
        scope_value = _scope_value(scope)
        await handle_command(
            interaction,
            connection=connection,
            registry=registry,
            owner_id=owner_id,
            command_name=BLOCK_NAME,
            function=lambda: block_user(
                connection,
                target_id=user.id,
                target_is_bot=user.bot,
                kind=kind.value,
                scope=scope_value,
                owner_id=owner_id,
                actor_id=context.user_id,
                guild_id=context.guild_id,
                now=context.now,
                announce=announce,
            ),
            log_usage=log_usage,
        )

    @app_commands.command(name=UNBLOCK_NAME, description=UNBLOCK_DESCRIPTION)
    @app_commands.describe(
        user=PERM_USER_DESCRIPTION, scope=PERM_SCOPE_DESCRIPTION
    )
    @app_commands.choices(scope=SCOPE_CHOICES)
    async def unblock_command(
        interaction: discord.Interaction,
        user: discord.User,
        scope: app_commands.Choice[str] | None = None,
    ) -> None:
        """Run /unblock."""
        context = context_from_interaction(interaction)
        scope_value = _scope_value(scope)
        await handle_command(
            interaction,
            connection=connection,
            registry=registry,
            owner_id=owner_id,
            command_name=UNBLOCK_NAME,
            function=lambda: unblock_user(
                connection,
                target_id=user.id,
                target_is_bot=user.bot,
                scope=scope_value,
                owner_id=owner_id,
                actor_id=context.user_id,
                guild_id=context.guild_id,
                now=context.now,
                announce=announce,
            ),
            log_usage=log_usage,
        )

    return [block_command, unblock_command]
