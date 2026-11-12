"""The settings defined so far: logging, backups, commands, reminders.

These are the 23 definitions of ``docs/design.md`` section 10.15,
copied exactly, ``commands.confirm_timeout_seconds`` from 10.2 for
the confirmation buttons, and three reminder settings from 10.12 for
/remind add and reminder delivery. The log settings are in the
"logging" category, the seven ``backups.*`` settings in "backups"
and the reminder settings in "reminders". Other sections add their
settings with the features that use them.
"""

import datetime

from maika.settings import (
    SettingDefinition,
    SettingLevel,
    SettingsRegistry,
    ValueType,
)

LOGGING_CATEGORY = "logging"
BACKUPS_CATEGORY = "backups"
COMMANDS_CATEGORY = "commands"
REMINDERS_CATEGORY = "reminders"

LOG_LEVEL_CHOICES = ("debug", "info", "warning", "error")

# In date.weekday() order: Monday is 0 and Sunday is 6.
WEEKDAY_CHOICES = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)

OFFSITE_ENABLED_KEY = "backups.offsite_enabled"
OFFSITE_DAY_KEY = "backups.offsite_day"
OFFSITE_REMOTE_KEY = "backups.offsite_remote"

CONFIRM_TIMEOUT_KEY = "commands.confirm_timeout_seconds"
CONFIRM_TIMEOUT_DEFAULT_SECONDS = 60
CONFIRM_TIMEOUT_MIN_SECONDS = 10
CONFIRM_TIMEOUT_MAX_SECONDS = 600

REMINDER_DEFAULT_TIME_KEY = "reminders.default_time"
REMINDER_DEFAULT_TIME = datetime.time(8, 0)

MAX_PENDING_REMINDERS_KEY = "reminders.max_pending_per_person"
MAX_PENDING_REMINDERS_DEFAULT = 25
MAX_PENDING_REMINDERS_MIN = 1
MAX_PENDING_REMINDERS_MAX = 500

REMINDER_CHECK_SECONDS_KEY = "reminders.check_seconds"
REMINDER_CHECK_SECONDS_DEFAULT = 60
REMINDER_CHECK_SECONDS_MIN = 10
REMINDER_CHECK_SECONDS_MAX = 300

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
        category=BACKUPS_CATEGORY,
        level=SettingLevel.BASIC,
        server_scope=False,
        value_type=ValueType.TIME,
        default=datetime.time(4, 0),
        description=(
            "Daily database backup time (local time on the bot's machine)."
        ),
    ),
    SettingDefinition(
        key="backups.days_kept",
        category=BACKUPS_CATEGORY,
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
        category=BACKUPS_CATEGORY,
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
        category=BACKUPS_CATEGORY,
        level=SettingLevel.ADVANCED,
        server_scope=False,
        value_type=ValueType.INT,
        default=4,
        minimum=1,
        maximum=52,
        description="Weekly offsite copies kept.",
    ),
    SettingDefinition(
        key=OFFSITE_ENABLED_KEY,
        category=BACKUPS_CATEGORY,
        level=SettingLevel.BASIC,
        server_scope=False,
        value_type=ValueType.BOOL,
        default=False,
        description="Weekly offsite copy on or off.",
    ),
    SettingDefinition(
        key=OFFSITE_DAY_KEY,
        category=BACKUPS_CATEGORY,
        level=SettingLevel.BASIC,
        server_scope=False,
        value_type=ValueType.TEXT,
        default="sunday",
        choices=WEEKDAY_CHOICES,
        description="Weekday of the weekly offsite copy.",
    ),
    SettingDefinition(
        key=OFFSITE_REMOTE_KEY,
        category=BACKUPS_CATEGORY,
        level=SettingLevel.ADVANCED,
        server_scope=False,
        value_type=ValueType.TEXT,
        default="b2offsite:maika-backups",
        description="rclone remote and folder for the offsite copies.",
    ),
    SettingDefinition(
        key=CONFIRM_TIMEOUT_KEY,
        category=COMMANDS_CATEGORY,
        level=SettingLevel.ADVANCED,
        server_scope=False,
        value_type=ValueType.INT,
        default=CONFIRM_TIMEOUT_DEFAULT_SECONDS,
        minimum=CONFIRM_TIMEOUT_MIN_SECONDS,
        maximum=CONFIRM_TIMEOUT_MAX_SECONDS,
        description="Seconds before confirmation buttons stop working.",
    ),
    SettingDefinition(
        key=REMINDER_DEFAULT_TIME_KEY,
        category=REMINDERS_CATEGORY,
        level=SettingLevel.BASIC,
        server_scope=False,
        value_type=ValueType.TIME,
        default=REMINDER_DEFAULT_TIME,
        description="Clock time used when a reminder gives a day but no time.",
    ),
    SettingDefinition(
        key=MAX_PENDING_REMINDERS_KEY,
        category=REMINDERS_CATEGORY,
        level=SettingLevel.ADVANCED,
        server_scope=False,
        value_type=ValueType.INT,
        default=MAX_PENDING_REMINDERS_DEFAULT,
        minimum=MAX_PENDING_REMINDERS_MIN,
        maximum=MAX_PENDING_REMINDERS_MAX,
        description="Pending reminders one person may hold.",
    ),
    SettingDefinition(
        key=REMINDER_CHECK_SECONDS_KEY,
        category=REMINDERS_CATEGORY,
        level=SettingLevel.ADVANCED,
        server_scope=False,
        value_type=ValueType.INT,
        default=REMINDER_CHECK_SECONDS_DEFAULT,
        minimum=REMINDER_CHECK_SECONDS_MIN,
        maximum=REMINDER_CHECK_SECONDS_MAX,
        description="Seconds between checks for due reminders.",
    ),
)


def build_foundation_registry() -> SettingsRegistry:
    """Return a registry holding the foundation settings."""
    return SettingsRegistry(FOUNDATION_SETTINGS)
