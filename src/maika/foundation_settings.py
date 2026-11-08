"""The foundation settings: log channels, log levels and backups.

These are the 20 definitions of ``docs/design.md`` section 10.15,
copied exactly. Other sections add their settings with the features
that use them.
"""

import datetime

from maika.settings import (
    SettingDefinition,
    SettingLevel,
    SettingsRegistry,
    ValueType,
)

LOGGING_CATEGORY = "logging"

LOG_LEVEL_CHOICES = ("debug", "info", "warning", "error")

FOUNDATION_SETTINGS: tuple[SettingDefinition, ...] = (
    SettingDefinition(
        key="log.channel.bot_log",
        category=LOGGING_CATEGORY,
        level=SettingLevel.BASIC,
        server_scope=False,
        value_type=ValueType.INT,
        default=None,
        minimum=1,
        description=(
            "Channel for #bot-log. Empty uses LOG_CHANNEL_ID from .env."
        ),
    ),
    SettingDefinition(
        key="log.channel.errors",
        category=LOGGING_CATEGORY,
        level=SettingLevel.BASIC,
        server_scope=False,
        value_type=ValueType.INT,
        default=None,
        minimum=1,
        description="Channel for #errors. Empty falls back to #bot-log.",
    ),
    SettingDefinition(
        key="log.channel.permissions",
        category=LOGGING_CATEGORY,
        level=SettingLevel.BASIC,
        server_scope=False,
        value_type=ValueType.INT,
        default=None,
        minimum=1,
        description="Channel for #permissions. Empty falls back to #bot-log.",
    ),
    SettingDefinition(
        key="log.channel.feeds",
        category=LOGGING_CATEGORY,
        level=SettingLevel.BASIC,
        server_scope=False,
        value_type=ValueType.INT,
        default=None,
        minimum=1,
        description="Channel for #feeds. Empty falls back to #bot-log.",
    ),
    SettingDefinition(
        key="log.channel.decisions",
        category=LOGGING_CATEGORY,
        level=SettingLevel.BASIC,
        server_scope=False,
        value_type=ValueType.INT,
        default=None,
        minimum=1,
        description="Channel for #decisions. Empty falls back to #bot-log.",
    ),
    SettingDefinition(
        key="log.channel.persona_log",
        category=LOGGING_CATEGORY,
        level=SettingLevel.BASIC,
        server_scope=False,
        value_type=ValueType.INT,
        default=None,
        minimum=1,
        description="Channel for #persona-log. Empty falls back to #bot-log.",
    ),
    SettingDefinition(
        key="log.channel.events",
        category=LOGGING_CATEGORY,
        level=SettingLevel.BASIC,
        server_scope=False,
        value_type=ValueType.INT,
        default=None,
        minimum=1,
        description="Channel for #events. Empty falls back to #bot-log.",
    ),
    SettingDefinition(
        key="log.channel.memory_review",
        category=LOGGING_CATEGORY,
        level=SettingLevel.BASIC,
        server_scope=False,
        value_type=ValueType.INT,
        default=None,
        minimum=1,
        description=(
            "Channel for #memory-review. Empty falls back to #bot-log."
        ),
    ),
    SettingDefinition(
        key="log.level.bot_log",
        category=LOGGING_CATEGORY,
        level=SettingLevel.ADVANCED,
        server_scope=False,
        value_type=ValueType.TEXT,
        default="info",
        choices=LOG_LEVEL_CHOICES,
        description="Detail level for #bot-log.",
    ),
    SettingDefinition(
        key="log.level.errors",
        category=LOGGING_CATEGORY,
        level=SettingLevel.ADVANCED,
        server_scope=False,
        value_type=ValueType.TEXT,
        default="info",
        choices=LOG_LEVEL_CHOICES,
        description="Detail level for #errors.",
    ),
    SettingDefinition(
        key="log.level.permissions",
        category=LOGGING_CATEGORY,
        level=SettingLevel.ADVANCED,
        server_scope=False,
        value_type=ValueType.TEXT,
        default="info",
        choices=LOG_LEVEL_CHOICES,
        description="Detail level for #permissions.",
    ),
    SettingDefinition(
        key="log.level.feeds",
        category=LOGGING_CATEGORY,
        level=SettingLevel.ADVANCED,
        server_scope=False,
        value_type=ValueType.TEXT,
        default="info",
        choices=LOG_LEVEL_CHOICES,
        description="Detail level for #feeds.",
    ),
    SettingDefinition(
        key="log.level.decisions",
        category=LOGGING_CATEGORY,
        level=SettingLevel.ADVANCED,
        server_scope=False,
        value_type=ValueType.TEXT,
        default="info",
        choices=LOG_LEVEL_CHOICES,
        description="Detail level for #decisions.",
    ),
    SettingDefinition(
        key="log.level.persona_log",
        category=LOGGING_CATEGORY,
        level=SettingLevel.ADVANCED,
        server_scope=False,
        value_type=ValueType.TEXT,
        default="info",
        choices=LOG_LEVEL_CHOICES,
        description="Detail level for #persona-log.",
    ),
    SettingDefinition(
        key="log.level.events",
        category=LOGGING_CATEGORY,
        level=SettingLevel.ADVANCED,
        server_scope=False,
        value_type=ValueType.TEXT,
        default="info",
        choices=LOG_LEVEL_CHOICES,
        description="Detail level for #events.",
    ),
    SettingDefinition(
        key="log.level.memory_review",
        category=LOGGING_CATEGORY,
        level=SettingLevel.ADVANCED,
        server_scope=False,
        value_type=ValueType.TEXT,
        default="info",
        choices=LOG_LEVEL_CHOICES,
        description="Detail level for #memory-review.",
    ),
    SettingDefinition(
        key="backups.time",
        category=LOGGING_CATEGORY,
        level=SettingLevel.BASIC,
        server_scope=False,
        value_type=ValueType.TIME,
        default=datetime.time(4, 0),
        description="Daily database backup time.",
    ),
    SettingDefinition(
        key="backups.days_kept",
        category=LOGGING_CATEGORY,
        level=SettingLevel.BASIC,
        server_scope=False,
        value_type=ValueType.INT,
        default=30,
        minimum=1,
        maximum=365,
        description="Days of daily backups kept.",
    ),
    SettingDefinition(
        key="backups.migration_copies",
        category=LOGGING_CATEGORY,
        level=SettingLevel.ADVANCED,
        server_scope=False,
        value_type=ValueType.INT,
        default=10,
        minimum=1,
        maximum=50,
        description="Pre-migration copies kept.",
    ),
    SettingDefinition(
        key="backups.offsite_copies_kept",
        category=LOGGING_CATEGORY,
        level=SettingLevel.ADVANCED,
        server_scope=False,
        value_type=ValueType.INT,
        default=4,
        minimum=1,
        maximum=52,
        description="Weekly offsite copies kept.",
    ),
)


def build_foundation_registry() -> SettingsRegistry:
    """Return a registry holding the foundation settings."""
    return SettingsRegistry(FOUNDATION_SETTINGS)
