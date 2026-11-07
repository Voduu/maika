"""Setting definitions, text conversion, validation and the registry.

Every tunable value is declared once in code as a SettingDefinition
(``docs/design.md`` section 9.6). The settings table stores only
overrides, as text, and each value is converted by its declared type
when read (section 9.1). This module has no database access.

``parse_value`` turns stored or typed text into a value, and
``format_value`` turns a value into the text that is stored, so
``parse_value(d, format_value(d, x)) == x`` for every valid value.
"""

import datetime
import enum
import json
import math
import re
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

# area.name style: lowercase letters, digits and underscores in parts
# joined by dots, with at least one dot.
KEY_PATTERN = re.compile(r"^[a-z0-9_]+(\.[a-z0-9_]+)+$")

MIN_DESCRIPTION_LENGTH = 1
MAX_DESCRIPTION_LENGTH = 100

# Text longer than this cannot be a 64-bit integer, so it is rejected
# before int() sees it.
MAX_INT_TEXT_LENGTH = 20

# The range of a SQLite INTEGER (a signed 64-bit integer).
MIN_INT64 = -(2**63)
MAX_INT64 = 2**63 - 1

# ASCII digits only. \d would also accept other scripts' digits.
INT_PATTERN = re.compile(r"^-?[0-9]+$")
FLOAT_PATTERN = re.compile(r"^-?[0-9]+(\.[0-9]+)?$")
TIME_PATTERN = re.compile(r"^([01][0-9]|2[0-3]):[0-5][0-9]$")
TIME_FORMAT = "%H:%M"

TRUE_WORDS = frozenset({"true", "on", "yes"})
FALSE_WORDS = frozenset({"false", "off", "no"})
STORED_TRUE = "true"
STORED_FALSE = "false"

# Compact JSON: no space after commas or colons.
JSON_SEPARATORS = (",", ":")


class ValueType(enum.Enum):
    """The type a setting's text is converted to."""

    BOOL = "bool"
    INT = "int"
    FLOAT = "float"
    TEXT = "text"
    TIME = "time"
    INT_LIST = "list[int]"
    STR_LIST = "list[str]"
    JSON = "json"


class SettingLevel(enum.Enum):
    """Where a setting is listed by ``/config list``."""

    BASIC = "basic"
    ADVANCED = "advanced"


class SettingValueError(Exception):
    """Raised when a value does not fit its setting.

    The message names the key and states the rule, for example
    "backups.days_kept must be a whole number from 1 to 365."
    """

    def __init__(self, key: str, rule: str) -> None:
        """Store the key and rule and build the message."""
        self.key = key
        self.rule = rule
        super().__init__(f"{key} {rule}.")


class UnknownSettingError(Exception):
    """Raised when a key has no definition in the registry."""

    def __init__(self, key: str) -> None:
        """Store the key and build the message."""
        self.key = key
        super().__init__(f"There is no setting named {key}.")


@dataclass(frozen=True)
class SettingDefinition:
    """The single declaration of one setting.

    ``default`` is None when the setting has no default value.
    ``minimum`` and ``maximum`` apply to INT and FLOAT only, and
    ``choices`` (lowercase) to TEXT only. The constructor checks the
    definition and raises ValueError when it is wrong.
    """

    key: str
    category: str
    level: SettingLevel
    server_scope: bool
    value_type: ValueType
    default: Any
    description: str
    minimum: int | float | None = None
    maximum: int | float | None = None
    choices: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        """Raise ValueError when the definition is wrong."""
        if not KEY_PATTERN.match(self.key):
            raise ValueError(
                f"Setting key {self.key!r} must be lowercase area.name "
                "style: letters, digits and underscores, with at least "
                "one dot."
            )

        length = len(self.description)
        if not MIN_DESCRIPTION_LENGTH <= length <= MAX_DESCRIPTION_LENGTH:
            raise ValueError(
                f"{self.key}: the description must be "
                f"{MIN_DESCRIPTION_LENGTH} to {MAX_DESCRIPTION_LENGTH} "
                f"characters, not {length}."
            )

        self._check_range()
        self._check_choices()

        if self.default is not None:
            try:
                check_value(self, self.default)
            except SettingValueError as error:
                raise ValueError(
                    f"The default for {error.key} is invalid: it {error.rule}."
                ) from error

    def _check_range(self) -> None:
        """Check minimum and maximum against the type and each other."""
        has_range = self.minimum is not None or self.maximum is not None
        if not has_range:
            return
        if self.value_type not in (ValueType.INT, ValueType.FLOAT):
            raise ValueError(
                f"{self.key}: minimum and maximum apply to int and "
                f"float settings only, not {self.value_type.value}."
            )
        for bound in (self.minimum, self.maximum):
            if bound is not None and not _is_number(bound):
                raise ValueError(
                    f"{self.key}: minimum and maximum must be numbers."
                )
        if (
            self.minimum is not None
            and self.maximum is not None
            and self.minimum > self.maximum
        ):
            raise ValueError(
                f"{self.key}: minimum {self.minimum} is above maximum "
                f"{self.maximum}."
            )

    def _check_choices(self) -> None:
        """Check that choices are lowercase text on a TEXT setting."""
        if self.choices is None:
            return
        if self.value_type is not ValueType.TEXT:
            raise ValueError(
                f"{self.key}: choices apply to text settings only, not "
                f"{self.value_type.value}."
            )
        if not self.choices:
            raise ValueError(f"{self.key}: choices must not be empty.")
        for choice in self.choices:
            if (
                not isinstance(choice, str)
                or choice == ""
                or choice != choice.strip().lower()
            ):
                raise ValueError(
                    f"{self.key}: each choice must be lowercase text "
                    f"with no surrounding spaces, not {choice!r}."
                )
        if len(set(self.choices)) != len(self.choices):
            raise ValueError(f"{self.key}: choices must not repeat.")


def _is_number(value: object) -> bool:
    """Return True for an int or float that is not a bool."""
    return isinstance(value, int | float) and not isinstance(value, bool)


def _format_number(number: int | float) -> str:
    """Write a number the way it would be typed, with no exponent."""
    if isinstance(number, float):
        return _format_float(number)
    return str(number)


def _format_float(number: float) -> str:
    """Write a float as a plain decimal such as "0.00001".

    ``repr`` gives the shortest text that reads back as the same
    float, but it can use an exponent ("1e-05"). Decimal rewrites it
    without one and keeps the same digits.
    """
    text = format(Decimal(repr(number)), "f")
    if "." not in text:
        text += ".0"
    return text


def _int_bounds(definition: SettingDefinition) -> tuple[int, int]:
    """Return an INT setting's limits, inside the 64-bit range.

    A setting with no minimum or maximum is still limited to what a
    SQLite INTEGER can hold.
    """
    low = MIN_INT64
    high = MAX_INT64
    if definition.minimum is not None:
        low = max(low, math.ceil(definition.minimum))
    if definition.maximum is not None:
        high = min(high, math.floor(definition.maximum))
    return low, high


def _range_text(low: int | float | None, high: int | float | None) -> str:
    """Return the range part of a rule, such as " from 1 to 365"."""
    if low is not None and high is not None:
        return f" from {_format_number(low)} to {_format_number(high)}"
    if low is not None:
        return f" of at least {_format_number(low)}"
    if high is not None:
        return f" of at most {_format_number(high)}"
    return ""


def describe_range(definition: SettingDefinition) -> str | None:
    """Return a setting's range for display, such as "1 to 365".

    Only the definition's own minimum and maximum count. Return None
    when it has neither.
    """
    low = definition.minimum
    high = definition.maximum
    if low is not None and high is not None:
        return f"{_format_number(low)} to {_format_number(high)}"
    if low is not None:
        return f"at least {_format_number(low)}"
    if high is not None:
        return f"at most {_format_number(high)}"
    return None


def describe_rule(definition: SettingDefinition) -> str:
    """Return the rule a value must follow, used in error messages."""
    value_type = definition.value_type
    if value_type is ValueType.BOOL:
        return "must be true or false (on, off, yes and no also work)"
    if value_type is ValueType.INT:
        low, high = _int_bounds(definition)
        return f"must be a whole number{_range_text(low, high)}"
    if value_type is ValueType.FLOAT:
        range_text = _range_text(definition.minimum, definition.maximum)
        return f"must be a decimal number such as 0.5{range_text}"
    if value_type is ValueType.TEXT:
        if definition.choices is not None:
            return f"must be one of: {', '.join(definition.choices)}"
        return "must not be empty"
    if value_type is ValueType.TIME:
        return "must be a 24-hour time written HH:MM, such as 04:00"
    if value_type is ValueType.INT_LIST:
        return "must be a JSON list of whole numbers, such as [1, 2]"
    if value_type is ValueType.STR_LIST:
        return 'must be a JSON list of text, such as ["a", "b"]'
    return "must be valid JSON"


def _fail(definition: SettingDefinition) -> SettingValueError:
    """Return the error for a value that breaks the rule."""
    return SettingValueError(definition.key, describe_rule(definition))


def _in_range(definition: SettingDefinition, number: int | float) -> bool:
    """Return True when a number is inside the inclusive range."""
    if definition.minimum is not None and number < definition.minimum:
        return False
    if definition.maximum is not None and number > definition.maximum:
        return False
    return True


def _is_int64(value: object) -> bool:
    """Return True for an int (not a bool) in the 64-bit range."""
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and MIN_INT64 <= value <= MAX_INT64
    )


def _is_json_value(value: object) -> bool:
    """Return True when a value can be written as strict JSON."""
    try:
        json.dumps(value, allow_nan=False)
    except TypeError, ValueError:
        return False
    return True


def check_value(definition: SettingDefinition, value: object) -> None:
    """Raise SettingValueError when a typed value breaks the rules.

    This checks a value that is already converted, such as a default
    or the result of ``parse_value``: its Python type, the range for
    INT and FLOAT (INT values must also fit in a signed 64-bit
    integer), and the choices for TEXT.
    """
    value_type = definition.value_type
    if value_type is ValueType.BOOL:
        valid = isinstance(value, bool)
    elif value_type is ValueType.INT:
        valid = _is_int64(value) and _in_range(definition, value)
    elif value_type is ValueType.FLOAT:
        valid = (
            _is_number(value)
            and math.isfinite(value)
            and _in_range(definition, value)
        )
    elif value_type is ValueType.TEXT:
        valid = (
            isinstance(value, str) and value != "" and value == value.strip()
        )
        if valid and definition.choices is not None:
            valid = value in definition.choices
    elif value_type is ValueType.TIME:
        valid = (
            isinstance(value, datetime.time)
            and value.second == 0
            and value.microsecond == 0
            and value.tzinfo is None
        )
    elif value_type is ValueType.INT_LIST:
        valid = isinstance(value, tuple) and all(
            _is_int64(item) for item in value
        )
    elif value_type is ValueType.STR_LIST:
        valid = isinstance(value, tuple) and all(
            isinstance(item, str) for item in value
        )
    else:
        valid = _is_json_value(value)

    if not valid:
        raise _fail(definition)


def _reject_json_constant(name: str) -> None:
    """Refuse NaN and Infinity, which are not valid JSON."""
    raise ValueError(f"{name} is not valid JSON")


def _load_json(definition: SettingDefinition, text: str) -> Any:
    """Parse strict JSON text, raising SettingValueError on failure."""
    try:
        return json.loads(text, parse_constant=_reject_json_constant)
    except ValueError as error:
        raise _fail(definition) from error


def _parse_typed(definition: SettingDefinition, text: str) -> Any:
    """Convert text to the definition's type, before range checks."""
    value_type = definition.value_type
    stripped = text.strip()

    if value_type is ValueType.BOOL:
        word = stripped.lower()
        if word in TRUE_WORDS:
            return True
        if word in FALSE_WORDS:
            return False
        raise _fail(definition)

    if value_type is ValueType.INT:
        if len(stripped) > MAX_INT_TEXT_LENGTH:
            raise _fail(definition)
        if not INT_PATTERN.match(stripped):
            raise _fail(definition)
        return int(stripped)

    if value_type is ValueType.FLOAT:
        if not FLOAT_PATTERN.match(stripped):
            raise _fail(definition)
        number = float(stripped)
        if not math.isfinite(number):
            raise _fail(definition)
        return number

    if value_type is ValueType.TEXT:
        if stripped == "":
            raise _fail(definition)
        if definition.choices is not None:
            return stripped.lower()
        return stripped

    if value_type is ValueType.TIME:
        if not TIME_PATTERN.match(stripped):
            raise _fail(definition)
        hours, minutes = stripped.split(":")
        return datetime.time(int(hours), int(minutes))

    if value_type in (ValueType.INT_LIST, ValueType.STR_LIST):
        loaded = _load_json(definition, stripped)
        if not isinstance(loaded, list):
            raise _fail(definition)
        return tuple(loaded)

    return _load_json(definition, stripped)


def parse_value(definition: SettingDefinition, text: str) -> Any:
    """Convert text to a checked value for the definition.

    Spaces around the text are ignored. Raise SettingValueError,
    naming the key and the rule, when the text does not fit.

    Types: BOOL returns bool, INT int, FLOAT float, TEXT str, TIME
    ``datetime.time``, INT_LIST and STR_LIST a tuple, and JSON the
    parsed value.
    """
    value = _parse_typed(definition, text)
    check_value(definition, value)
    return value


def format_value(definition: SettingDefinition, value: object) -> str:
    """Return the text stored for a value.

    The value is checked first, and SettingValueError is raised when
    it breaks the definition's rules.
    """
    check_value(definition, value)
    value_type = definition.value_type

    if value_type is ValueType.BOOL:
        return STORED_TRUE if value else STORED_FALSE
    if value_type is ValueType.INT:
        return str(value)
    if value_type is ValueType.FLOAT:
        return _format_float(float(value))
    if value_type is ValueType.TEXT:
        return str(value)
    if value_type is ValueType.TIME:
        return value.strftime(TIME_FORMAT)
    if value_type in (ValueType.INT_LIST, ValueType.STR_LIST):
        return json.dumps(
            list(value), separators=JSON_SEPARATORS, ensure_ascii=False
        )
    return json.dumps(
        value,
        sort_keys=True,
        separators=JSON_SEPARATORS,
        ensure_ascii=False,
        allow_nan=False,
    )


class SettingsRegistry:
    """All setting definitions, looked up by key."""

    def __init__(self, definitions: Iterable[SettingDefinition]) -> None:
        """Index the definitions by key, refusing duplicate keys."""
        self._definitions: dict[str, SettingDefinition] = {}
        for definition in definitions:
            if definition.key in self._definitions:
                raise ValueError(
                    f"Setting {definition.key} is defined more than once."
                )
            self._definitions[definition.key] = definition

    def get(self, key: str) -> SettingDefinition:
        """Return the definition for a key.

        Raise UnknownSettingError when there is none.
        """
        try:
            return self._definitions[key]
        except KeyError:
            raise UnknownSettingError(key) from None

    def keys(self) -> tuple[str, ...]:
        """Return every key, in the order the definitions were given."""
        return tuple(self._definitions)

    def definitions_in_category(
        self, category: str
    ) -> tuple[SettingDefinition, ...]:
        """Return a category's definitions, in their given order."""
        return tuple(
            definition
            for definition in self._definitions.values()
            if definition.category == category
        )
