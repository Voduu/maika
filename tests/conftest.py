import logging

import pytest

from maika.log_names import LOGGER_NAME


@pytest.fixture(autouse=True)
def restore_maika_handlers():
    """Remove handlers a test (for example setup_hook) left behind."""
    logger = logging.getLogger(LOGGER_NAME)
    handlers = list(logger.handlers)
    yield
    logger.handlers[:] = handlers
