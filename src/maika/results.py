"""The result object every command function returns.

See ``docs/design.md`` section 1.3 (rules for testability). A command
function returns a CommandResult and never final text: the command
lane or the personality renderer turns it into a reply.
"""

import enum
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Self


class FailureKind(enum.Enum):
    """Why a command failed (design section 1.3).

    The four named kinds have a pool of fixed lines (3.9). INVALID
    (a value was rejected) and OTHER show the result's plain message.
    """

    DENIED = "denied"
    NOT_FOUND = "not_found"
    ALREADY_EXISTS = "already_exists"
    MISSING_ARGUMENT = "missing_argument"
    INVALID = "invalid"
    OTHER = "other"


@dataclass(frozen=True)
class Confirmation:
    """A question to confirm before a command's action runs.

    ``prompt`` is shown with Confirm and Cancel buttons. ``on_confirm``
    is the action to run on Confirm; the command lane runs it through
    the same wrapper as the command, so the permission is checked
    again and the action is logged.
    """

    prompt: str
    on_confirm: Callable[[], CommandResult]


@dataclass(frozen=True)
class CommandResult:
    """The outcome of one command.

    ``message`` is a plain default message. ``data`` carries anything
    the renderer needs. A success has no ``failure_kind``, and a
    failure must have one. ``confirmation`` is set when the command
    asks before acting. Build results with ``ok``, ``fail`` and
    ``ask``.
    """

    success: bool
    message: str
    data: Any = None
    failure_kind: FailureKind | None = None
    confirmation: Confirmation | None = None

    def __post_init__(self) -> None:
        """Raise ValueError when success and failure_kind disagree."""
        if self.success and self.failure_kind is not None:
            raise ValueError(
                "A successful result must not have a failure_kind, "
                f"but it has {self.failure_kind!r}."
            )
        if not self.success and self.failure_kind is None:
            raise ValueError("A failed result needs a failure_kind.")
        if self.failure_kind is not None and not isinstance(
            self.failure_kind, FailureKind
        ):
            raise ValueError(
                "failure_kind must be a FailureKind, not "
                f"{self.failure_kind!r}."
            )

    @classmethod
    def ok(cls, message: str, data: Any = None) -> Self:
        """Return a successful result."""
        return cls(success=True, message=message, data=data)

    @classmethod
    def fail(cls, kind: FailureKind, message: str) -> Self:
        """Return a failed result of the given kind."""
        return cls(success=False, message=message, failure_kind=kind)

    @classmethod
    def ask(cls, prompt: str, on_confirm: Callable[[], CommandResult]) -> Self:
        """Return a success that asks to confirm before acting.

        The message is the prompt, and ``on_confirm`` runs only when
        the user presses Confirm.
        """
        return cls(
            success=True,
            message=prompt,
            confirmation=Confirmation(prompt=prompt, on_confirm=on_confirm),
        )
