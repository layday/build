from __future__ import annotations

import contextlib
import contextvars
import enum
import logging
import typing

from collections.abc import Iterator


class LogMessageOrigin(enum.IntEnum):
    Logging = 0
    CommandInput = 1
    CommandOutput = 2


class _Logger(typing.Protocol):
    def __call__(self, message: str, *, origin: LogMessageOrigin = LogMessageOrigin.Logging) -> None:
        ...


_default_logger = logging.getLogger(__package__)


def _log_default(message: str, *, origin: LogMessageOrigin = LogMessageOrigin.Logging) -> None:
    if origin is LogMessageOrigin.Logging:
        _default_logger.log(logging.INFO, message, stacklevel=2)


LOGGER = contextvars.ContextVar('LOGGER', default=_log_default)
VERBOSITY = contextvars.ContextVar('VERBOSITY', default=0)


@contextlib.contextmanager
def capture_logging_pretend_command(logger_name: str | None = None) -> Iterator[None]:
    log = LOGGER.get()

    class CtxLoggerHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            log(self.format(record), origin=LogMessageOrigin.CommandOutput)

    handler = CtxLoggerHandler()

    logger = logging.getLogger(logger_name)
    logger.addHandler(handler)

    try:
        yield
    finally:
        logger.removeHandler(handler)


if typing.TYPE_CHECKING:
    log: _Logger
    verbosity: bool

else:

    def __getattr__(name):
        if name == 'log':
            return LOGGER.get()
        elif name == 'verbosity':
            return VERBOSITY.get()
        raise AttributeError(name)
