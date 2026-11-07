import logging

import pytest

from maika.log_router import (
    REDACTED_TEXT,
    LogLevel,
    LogStream,
    RouterLogHandler,
)

TOKEN = "secret-token-value"


class FakeRouter:
    def __init__(self):
        self.posts = []

    def post(self, stream, level, text):
        self.posts.append((stream, level, text))


class FailingRouter:
    def post(self, stream, level, text):
        raise RuntimeError("router broke")


@pytest.fixture
def attach():
    """Attach a handler to the maika logger and remove it afterwards."""
    attached = []

    def _attach(router, secrets=()):
        handler = RouterLogHandler(router, secrets=secrets)
        logging.getLogger("maika").addHandler(handler)
        attached.append(handler)
        return handler

    yield _attach
    for handler in attached:
        logging.getLogger("maika").removeHandler(handler)


@pytest.fixture(autouse=True)
def maika_level_debug():
    logger = logging.getLogger("maika")
    old_level = logger.level
    logger.setLevel(logging.DEBUG)
    yield
    logger.setLevel(old_level)


@pytest.mark.parametrize(
    ("record_level", "expected"),
    [
        (logging.WARNING, LogLevel.WARNING),
        (logging.ERROR, LogLevel.ERROR),
        (logging.CRITICAL, LogLevel.ERROR),
    ],
)
def test_forwards_warning_and_above(attach, record_level, expected):
    router = FakeRouter()
    attach(router)

    logging.getLogger("maika.feeds").log(record_level, "Feed %s broke.", 7)

    assert router.posts == [
        (LogStream.ERRORS, expected, "maika.feeds: Feed 7 broke.")
    ]


@pytest.mark.parametrize("record_level", [logging.DEBUG, logging.INFO])
def test_ignores_below_warning(attach, record_level):
    router = FakeRouter()
    attach(router)

    logging.getLogger("maika.feeds").log(record_level, "quiet")

    assert router.posts == []


def test_exception_has_class_and_message_without_traceback(attach):
    router = FakeRouter()
    attach(router)

    try:
        raise ValueError("bad value")
    except ValueError:
        logging.getLogger("maika.bot").exception("Something failed.")

    [(stream, level, text)] = router.posts
    assert stream is LogStream.ERRORS
    assert level is LogLevel.ERROR
    assert text == "maika.bot: Something failed.\nValueError: bad value"
    assert "Traceback" not in text


def test_ignores_router_logger(attach):
    router = FakeRouter()
    attach(router)

    logging.getLogger("maika.log_router").error("Sending failed.")

    assert router.posts == []


def test_does_not_forward_records_logged_during_a_forward(attach):
    class LoggingRouter(FakeRouter):
        def post(self, stream, level, text):
            super().post(stream, level, text)
            logging.getLogger("maika.settings_store").warning("nested")

    router = LoggingRouter()
    attach(router)

    logging.getLogger("maika.bot").warning("outer")

    assert [text for _, _, text in router.posts] == ["maika.bot: outer"]


def test_failing_router_does_not_raise(attach, monkeypatch):
    monkeypatch.setattr(logging, "raiseExceptions", False)
    attach(FailingRouter())

    logging.getLogger("maika.bot").error("Still fine.")


def test_failing_router_does_not_block_later_records(attach, monkeypatch):
    monkeypatch.setattr(logging, "raiseExceptions", False)
    attach(FailingRouter())
    router = FakeRouter()
    attach(router)

    logging.getLogger("maika.bot").error("first")
    logging.getLogger("maika.bot").error("second")

    assert [text for _, _, text in router.posts] == [
        "maika.bot: first",
        "maika.bot: second",
    ]


def test_token_is_redacted(attach):
    router = FakeRouter()
    attach(router, secrets=(TOKEN,))

    try:
        raise RuntimeError(f"login with {TOKEN} failed")
    except RuntimeError:
        logging.getLogger("maika.bot").exception("Token %s rejected.", TOKEN)

    [(_, _, text)] = router.posts
    assert TOKEN not in text
    assert text == (
        f"maika.bot: Token {REDACTED_TEXT} rejected.\n"
        f"RuntimeError: login with {REDACTED_TEXT} failed"
    )


def test_empty_secret_is_ignored(attach):
    router = FakeRouter()
    attach(router, secrets=("",))

    logging.getLogger("maika.bot").warning("plain text")

    assert router.posts[0][2] == "maika.bot: plain text"
