from __future__ import annotations

import contextvars
import logging
import os
import subprocess
import typing

from collections.abc import Callable, Mapping, Sequence
from functools import wraps

from ._types import StrPath


if typing.TYPE_CHECKING:
    from typing_extensions import ParamSpec

    _P = ParamSpec('_P')

_T = typing.TypeVar('_T')


class _Logger(typing.Protocol):  # pragma: no cover
    def __call__(self, message: str, *, origin: tuple[str, ...] | None = None) -> None: ...


_package_name = __spec__.parent  # type: ignore[name-defined]
_default_logger = logging.getLogger(_package_name)


def _log_default(message: str, *, origin: tuple[str, ...] | None = None) -> None:
    if origin is None:
        _default_logger.log(logging.INFO, message, stacklevel=2)


LOGGER = contextvars.ContextVar('LOGGER', default=_log_default)
VERBOSITY = contextvars.ContextVar('VERBOSITY', default=0)


def log_subprocess_error(error: subprocess.CalledProcessError) -> None:
    log = LOGGER.get()

    log(subprocess.list2cmdline(error.cmd), origin=('subprocess', 'cmd'))

    for stream_name in ('stdout', 'stderr'):
        stream = getattr(error, stream_name)
        if stream:
            log(stream.decode() if isinstance(stream, bytes) else stream, origin=('subprocess', stream_name))


def run_subprocess(cmd: Sequence[StrPath], *, cwd: str | None = None, extra_env: Mapping[str, str] = {}) -> None:
    verbosity = VERBOSITY.get()

    env = {**os.environ, 'FORCE_COLOR': '1', **extra_env}

    if verbosity:
        log = LOGGER.get()

        with subprocess.Popen(
            cmd, cwd=cwd, encoding='utf-8', env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
        ) as process:
            if verbosity > 1:
                log(subprocess.list2cmdline(cmd), origin=('subprocess', 'cmd'))

            for line in process.stdout or []:
                log(line, origin=('subprocess', 'stdout'))

            code = process.wait()
            if code:
                raise subprocess.CalledProcessError(code, process.args)

    else:
        try:
            subprocess.run(cmd, capture_output=True, check=True, cwd=cwd, env=env)
        except subprocess.CalledProcessError as error:
            log_subprocess_error(error)
            raise


def with_new_context(function: Callable[_P, _T]) -> Callable[_P, _T]:
    @wraps(function)
    def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> _T:
        return contextvars.copy_context().run(function, *args, **kwargs)

    return wrapper


if typing.TYPE_CHECKING:
    log: _Logger
    verbosity: bool

else:

    def __getattr__(name):
        if name == 'log':
            return LOGGER.get()
        elif name == 'verbosity':
            return VERBOSITY.get()
        raise AttributeError(name)  # pragma: no cover


__all__ = [
    'log_subprocess_error',
    'log',
    'LOGGER',
    'run_subprocess',
    'verbosity',
    'VERBOSITY',
    'with_new_context',
]
