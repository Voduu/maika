import asyncio
import logging

import pytest

from maika.reminder_delivery import DeliverySummary
from maika.reminder_loop import (
    MAX_BACKOFF_SECONDS,
    RETRY_WAIT_SECONDS,
    run_reminder_loop,
)

RETRIED = DeliverySummary(delivered=2, retried=1)
NO_RETRIES = DeliverySummary(delivered=2, failed=1)


class StopLoopError(Exception):
    """Raised by the fake sleep to end a test after enough waits."""


class Recorder:
    """A fake sleep and a fake cycle that share one event list.

    ``outcomes`` scripts the cycles: None succeeds, a DeliverySummary
    is returned, an exception instance is raised. The sleep raises
    StopLoopError once ``waits`` waits were recorded.
    """

    def __init__(self, *, outcomes=(), waits=5):
        self.events = []
        self.outcomes = list(outcomes)
        self.max_waits = waits

    @property
    def waits(self):
        return [event[1] for event in self.events if event[0] == "sleep"]

    async def sleep(self, seconds):
        self.events.append(("sleep", seconds))
        if len(self.waits) > self.max_waits:
            raise StopLoopError

    async def cycle(self):
        self.events.append(("cycle",))
        outcome = self.outcomes.pop(0) if self.outcomes else None
        if isinstance(outcome, DeliverySummary):
            return outcome
        if outcome is not None:
            raise outcome
        return DeliverySummary()


def run_loop(recorder, read_interval, **options):
    async def main():
        await run_reminder_loop(
            recorder.cycle,
            read_interval=read_interval,
            sleep=recorder.sleep,
            **options,
        )

    with pytest.raises(StopLoopError):
        asyncio.run(main())


def test_max_backoff_is_15_minutes():
    assert MAX_BACKOFF_SECONDS == 900


def test_first_wait_comes_before_first_cycle():
    recorder = Recorder(waits=2)

    run_loop(recorder, lambda: 60)

    assert recorder.events[:3] == [
        ("sleep", 60),
        ("cycle",),
        ("sleep", 60),
    ]


def test_changed_interval_applies_at_next_wait():
    # One read before each wait.
    intervals = iter([60, 30, 120, 120])
    recorder = Recorder(waits=3)

    run_loop(recorder, lambda: next(intervals))

    assert recorder.waits == [60, 30, 120, 120]


def test_failures_double_the_wait_up_to_the_cap(caplog):
    recorder = Recorder(outcomes=[RuntimeError("boom")] * 9, waits=10)

    with caplog.at_level(logging.ERROR, logger="maika.reminder_loop"):
        run_loop(recorder, lambda: 10)

    assert recorder.waits == [10, 20, 40, 80, 160, 320, 640, 900, 900, 900, 10]
    assert len(caplog.records) == 9
    for record in caplog.records:
        assert record.getMessage() == "The reminder check failed."
        assert record.exc_info is not None


def test_success_resets_the_wait():
    recorder = Recorder(
        outcomes=[RuntimeError(), RuntimeError(), None, RuntimeError()],
        waits=5,
    )

    run_loop(recorder, lambda: 10)

    assert recorder.waits[:5] == [10, 20, 40, 10, 20]


def test_retry_wait_is_15_seconds():
    assert RETRY_WAIT_SECONDS == 15


def test_retried_check_waits_at_least_15_seconds():
    recorder = Recorder(outcomes=[RETRIED, NO_RETRIES, RETRIED], waits=3)

    run_loop(recorder, lambda: 1)

    assert recorder.waits[:4] == [1, 15, 1, 15]


def test_check_without_retries_waits_the_interval():
    recorder = Recorder(outcomes=[NO_RETRIES, NO_RETRIES], waits=2)

    run_loop(recorder, lambda: 1)

    assert recorder.waits[:3] == [1, 1, 1]


def test_longer_interval_stays_after_retried_check():
    recorder = Recorder(outcomes=[RETRIED, RETRIED], waits=2)

    run_loop(recorder, lambda: 60)

    assert recorder.waits[:3] == [60, 60, 60]


def test_backoff_cap_can_be_set():
    recorder = Recorder(outcomes=[RuntimeError()] * 4, waits=4)

    run_loop(recorder, lambda: 10, max_backoff_seconds=25)

    assert recorder.waits[:5] == [10, 20, 25, 25, 25]


def test_failed_interval_read_keeps_the_loop_running(caplog):
    reads = iter([60, ValueError("bad setting"), 45, 45])
    recorder = Recorder(waits=3)

    def read_interval():
        value = next(reads)
        if isinstance(value, Exception):
            raise value
        return value

    with caplog.at_level(logging.ERROR, logger="maika.reminder_loop"):
        run_loop(recorder, read_interval)

    # The failed read keeps the last wait.
    assert recorder.waits[:3] == [60, 60, 45]
    assert caplog.records[0].getMessage() == (
        "Reading the reminder check interval failed."
    )


def test_cancelled_error_in_cycle_ends_the_loop(caplog):
    recorder = Recorder(outcomes=[asyncio.CancelledError()], waits=5)

    async def main():
        await run_reminder_loop(
            recorder.cycle, read_interval=lambda: 10, sleep=recorder.sleep
        )

    with caplog.at_level(logging.ERROR, logger="maika.reminder_loop"):
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(main())

    assert recorder.events == [("sleep", 10), ("cycle",)]
    assert caplog.records == []


def test_cancelling_the_task_ends_the_loop(caplog):
    cycles = []

    async def cycle():
        cycles.append("cycle")
        return DeliverySummary()

    async def main():
        task = asyncio.create_task(
            run_reminder_loop(cycle, read_interval=lambda: 0.001)
        )
        while len(cycles) < 2:
            await asyncio.sleep(0.001)
        task.cancel()
        await asyncio.wait({task})
        return task

    with caplog.at_level(logging.ERROR, logger="maika.reminder_loop"):
        task = asyncio.run(main())

    assert task.cancelled()
    assert caplog.records == []
