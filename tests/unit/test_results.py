import dataclasses

import pytest

from maika.results import CommandResult, FailureKind


def test_ok_without_data():
    result = CommandResult.ok("Done.")

    assert result == CommandResult(True, "Done.", None, None)
    assert result.success is True
    assert result.failure_kind is None


def test_ok_with_data():
    result = CommandResult.ok("Added.", data={"photo_id": 214})
    assert result.data == {"photo_id": 214}


@pytest.mark.parametrize("kind", list(FailureKind))
def test_fail_each_kind(kind):
    result = CommandResult.fail(kind, "No.")

    assert result.success is False
    assert result.failure_kind is kind
    assert result.message == "No."
    assert result.data is None


def test_failure_kind_values():
    assert [kind.value for kind in FailureKind] == [
        "denied",
        "not_found",
        "already_exists",
        "missing_argument",
        "other",
    ]


@pytest.mark.parametrize("kind", list(FailureKind))
def test_success_with_failure_kind_raises(kind):
    with pytest.raises(ValueError, match="must not have a failure_kind"):
        CommandResult(True, "Done.", failure_kind=kind)


def test_failure_without_kind_raises():
    with pytest.raises(ValueError, match="needs a failure_kind"):
        CommandResult(False, "No.")


@pytest.mark.parametrize("kind", ["denied", 1])
def test_failure_kind_must_be_enum(kind):
    with pytest.raises(ValueError, match="must be a FailureKind"):
        CommandResult(False, "No.", failure_kind=kind)


def test_result_is_frozen():
    result = CommandResult.ok("Done.")
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.success = False
