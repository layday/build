from __future__ import annotations

import abc
import importlib.util
import os
import platform
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import typing

from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from functools import lru_cache, partial

from . import _ctx, _util
from ._exceptions import FailedProcessError


if typing.TYPE_CHECKING:
    from typing_extensions import Self

    _PipInstall = Callable[[str], None]

EnvImpl = typing.Literal['venv', 'virtualenv', 'uv']

ENV_IMPLS = typing.get_args(EnvImpl)


class IsolatedEnv(typing.Protocol):
    """Isolated build environment interface."""

    @property
    @abc.abstractmethod
    def python_executable(self) -> str:
        """The Python executable of the isolated environment."""

    @abc.abstractmethod
    def make_extra_environ(self) -> Mapping[str, str] | None:
        """Generate additional env vars specific to the isolated environment."""


@lru_cache(1)
def _has_virtualenv() -> bool:
    from packaging.requirements import Requirement

    # virtualenv might be incompatible if it was installed separately
    # from build. This verifies that virtualenv and all of its
    # dependencies are installed as specified by build.
    return importlib.util.find_spec('virtualenv') is not None and not any(
        Requirement(d[1]).name == 'virtualenv' for d in _util.check_dependency('build[virtualenv]') if len(d) > 1
    )


def _get_minimum_pip_version_str() -> str:
    if platform.system() == 'Darwin':
        release, _, machine = platform.mac_ver()
        if int(release[: release.find('.')]) >= 11:
            # macOS 11+ name scheme change requires 20.3. Intel macOS 11.0 can be
            # told to report 10.16 for backwards compatibility; but that also fixes
            # earlier versions of pip so this is only needed for 11+.
            is_apple_silicon_python = machine != 'x86_64'
            return '21.0.1' if is_apple_silicon_python else '20.3.0'

    # PEP-517 and manylinux1 was first implemented in 19.1
    return '19.1.0'


def _has_valid_pip(version_str: str | None = None, /, **distargs: object) -> bool:
    """
    Given a path, see if Pip is present and return True if the version is
    sufficient for build, False if it is not. ModuleNotFoundError is thrown if
    pip is not present.
    """

    from packaging.version import Version

    from ._compat import importlib

    name = 'pip'

    try:
        pip_distribution = next(iter(importlib.metadata.distributions(name=name, **distargs)))
    except StopIteration:
        raise ModuleNotFoundError(name) from None

    return Version(pip_distribution.version) >= Version(version_str or _get_minimum_pip_version_str())


@lru_cache(1)
def _has_valid_outer_pip() -> bool | None:
    """
    This checks for a valid outer pip. Returns None if pip is missing, False
    if pip is too old, and True if it can be used.
    """
    try:
        # Version to have added the `--python` option.
        return _has_valid_pip('22.3')
    except ModuleNotFoundError:
        return None


class DefaultIsolatedEnv(IsolatedEnv):
    """
    An isolated environment which offers a choice between: venv or virtualenv
    with pip; and uv with uv pip.
    """

    env_impl: typing.Final[EnvImpl | None]

    def __init__(
        self,
        env_impl: EnvImpl | None = None,
    ) -> None:
        self.env_impl = env_impl

    def __enter__(self) -> Self:
        if hasattr(self, '_path'):
            msg = f'{self.__class__} is not re-entrant'
            raise RuntimeError(msg)

        try:
            self._path = tempfile.mkdtemp(prefix='build-env-')

            msg_tpl = 'Creating isolated environment: {}...'

            # uv is opt-in only.
            if self.env_impl == 'uv':
                _ctx.log(msg_tpl.format('uv'))
                self._venv_paths, self._pip_install = _create_isolated_env_uv(
                    self._path,
                )

            # Use virtualenv when available and the user hasn't explicitly opted into
            # venv (as seeding pip is faster than with venv).
            elif self.env_impl == 'virtualenv' or (self.env_impl is None and _has_virtualenv()):
                _ctx.log(msg_tpl.format('virtualenv'))
                self._venv_paths, self._pip_install = _create_isolated_env_virtualenv(
                    self._path,
                )

            else:
                _ctx.log(msg_tpl.format('venv'))
                self._venv_paths, self._pip_install = _create_isolated_env_venv(
                    # Call ``realpath`` to prevent spurious warning from being emitted
                    # that the venv location has changed on Windows. The username is
                    # DOS-encoded in the output of tempfile - the location is the same
                    # but the representation of it is different, which confuses venv.
                    # Ref: https://bugs.python.org/issue46171
                    os.path.realpath(self._path),
                )

        except Exception:  # cleanup folder if creation fails
            self.__exit__(*sys.exc_info())
            raise

        return self

    def __exit__(self, *args: object) -> None:
        if os.path.exists(self._path):  # in case the user already deleted skip remove
            shutil.rmtree(self._path)

    @property
    def path(self) -> str:
        """The location of the isolated build environment."""
        return self._path

    @property
    def python_executable(self) -> str:
        """The python executable of the isolated build environment."""
        return self._venv_paths.python_executable

    def make_extra_environ(self) -> dict[str, str]:
        return {
            'PATH': os.pathsep.join(p for p in [self._venv_paths.scripts, os.environ.get('PATH')] if p is not None),
        }

    def install(self, requirements: Collection[str]) -> None:
        """
        Install packages from PEP 508 requirements in the isolated build environment.

        :param requirements: PEP 508 requirement specification to install

        :note: Passing non-PEP 508 strings will result in undefined behavior, you *should not* rely on it. It is
               merely an implementation detail, it may change any time without warning.
        """
        if not requirements:
            return

        # pip does not honour environment markers in command line arguments
        # but it does for requirements from a file
        with tempfile.NamedTemporaryFile('w', prefix='build-reqs-', suffix='.txt', delete=False, encoding='utf-8') as req_file:
            req_file.write(os.linesep.join(requirements))

        _ctx.log('Installing packages in isolated environment:\n' + '\n'.join(f'- {r}' for r in sorted(requirements)))

        try:
            self._pip_install(req_file.name)
        finally:
            os.unlink(req_file.name)


def _subprocess_run(
    cmd: list[str],
    extra_environ: Mapping[str, str] = {},
) -> None:
    """Invoke subprocess and output stdout and stderr if it fails."""

    with subprocess.Popen(
        cmd, env={**os.environ, **extra_environ}, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
    ) as proc:
        if _ctx.verbosity:
            _ctx.log(subprocess.list2cmdline(cmd), origin=_ctx.LogMessageOrigin.CommandInput)

        stdout, _ = proc.communicate()
        if _ctx.verbosity:
            _ctx.log(stdout, origin=_ctx.LogMessageOrigin.CommandOutput)

        code = proc.poll()
        if code:
            if not _ctx.verbosity:
                _ctx.log(stdout, origin=_ctx.LogMessageOrigin.CommandOutput)
            raise subprocess.CalledProcessError(code, cmd, stdout)


@dataclass
class _VenvPaths:
    scripts: str
    python_executable: str
    purelib: str


def _install_with_pip(python_executable: str, req_file_path: str) -> None:
    if _has_valid_outer_pip():
        pip_cmd = [sys.executable, '-m', 'pip', '--python', python_executable]
    else:
        pip_cmd = [python_executable, '-Im', 'pip']

    if _ctx.verbosity > 1:
        pip_cmd = [*pip_cmd, f'-{'v' * (_ctx.verbosity - 1)}']

    _subprocess_run([*pip_cmd, 'install', '--use-pep517', '--no-warn-script-location', '-r', req_file_path])


def _create_isolated_env_uv(path: str) -> tuple[_VenvPaths, _PipInstall]:
    uv = shutil.which('uv')
    if uv:
        cmd = [uv, 'venv', path]
        if _ctx.verbosity > 1:
            cmd += ['-v']
        _subprocess_run(cmd)

        def install(req_file_path: str) -> None:
            pip_cmd = [uv, 'pip']
            if _ctx.verbosity > 1:
                # uv doesn't support doubling up -v.
                pip_cmd = [*pip_cmd, '-v']

            _subprocess_run([*pip_cmd, 'install', '-r', req_file_path], extra_environ={'VIRTUAL_ENV': path})

        return _find_venv_paths(path), install
    else:
        msg = 'isolated env backend not found: uv'
        raise RuntimeError(msg)


def _create_isolated_env_virtualenv(path: str) -> tuple[_VenvPaths, _PipInstall]:
    """
    We optionally can use the virtualenv package to provision a virtual environment.

    :param path: The path where to create the isolated build environment
    :return: The Python executable and script folder
    """
    import virtualenv

    cmd = [path, '--activators', '']
    if _has_valid_outer_pip():
        cmd += ['--no-seed']
    else:
        cmd += ['--no-setuptools', '--no-wheel']

    with _ctx.capture_logging_pretend_command():
        result = virtualenv.cli_run(cmd, setup_logging=False)

    venv_paths = _VenvPaths(
        scripts=str(result.creator.bin_dir), python_executable=str(result.creator.exe), purelib=str(result.creator.purelib)
    )

    return venv_paths, partial(_install_with_pip, venv_paths.python_executable)


def _create_isolated_env_venv(path: str) -> tuple[_VenvPaths, _PipInstall]:
    """
    On Python 3 we use the venv package from the standard library.

    :param path: The path where to create the isolated build environment
    :return: The Python executable and script folder
    """
    import venv as _venv

    try:
        venv = _venv.EnvBuilder(with_pip=not _has_valid_outer_pip(), symlinks=os.name != 'nt')
        with _ctx.capture_logging_pretend_command(_venv.__name__):
            venv.create(path)

    except subprocess.CalledProcessError as exc:
        raise FailedProcessError(exc, 'Failed to create venv. Maybe try installing virtualenv.') from None

    venv_paths = _find_venv_paths(path)

    if venv.with_pip:
        # Get the version of pip in the environment
        if not _has_valid_pip(path=[venv_paths.purelib]):
            _subprocess_run([venv_paths.python_executable, '-m', 'pip', 'install', f'pip>={_get_minimum_pip_version_str()}'])

        if sys.version_info < (3, 12):
            # Avoid implicitly depending on distutils/setuptools.
            _subprocess_run([venv_paths.python_executable, '-m', 'pip', 'uninstall', '-y', 'setuptools'])

    return venv_paths, partial(_install_with_pip, venv_paths.python_executable)


def _find_venv_paths(path: str) -> _VenvPaths:
    """
    Detect the Python executable and script folder of a virtual environment.

    :param path: The location of the virtual environment
    :return: The Python executable, script folder, and purelib folder
    """
    config_vars = sysconfig.get_config_vars().copy()  # globally cached, copy before altering it
    config_vars['base'] = path

    scheme_names = sysconfig.get_scheme_names()
    if 'venv' in scheme_names:
        # Python distributors with custom default installation scheme can set a
        # scheme that can't be used to expand the paths in a venv.
        # This can happen if build itself is not installed in a venv.
        # The distributors are encouraged to set a "venv" scheme to be used for this.
        # See https://bugs.python.org/issue45413
        # and https://github.com/pypa/virtualenv/issues/2208
        paths = sysconfig.get_paths(scheme='venv', vars=config_vars)
    elif 'posix_local' in scheme_names:
        # The Python that ships on Debian/Ubuntu varies the default scheme to
        # install to /usr/local
        # But it does not (yet) set the "venv" scheme.
        # If we're the Debian "posix_local" scheme is available, but "venv"
        # is not, we use "posix_prefix" instead which is venv-compatible there.
        paths = sysconfig.get_paths(scheme='posix_prefix', vars=config_vars)
    elif 'osx_framework_library' in scheme_names:
        # The Python that ships with the macOS developer tools varies the
        # default scheme depending on whether the ``sys.prefix`` is part of a framework.
        # But it does not (yet) set the "venv" scheme.
        # If the Apple-custom "osx_framework_library" scheme is available but "venv"
        # is not, we use "posix_prefix" instead which is venv-compatible there.
        paths = sysconfig.get_paths(scheme='posix_prefix', vars=config_vars)
    else:
        paths = sysconfig.get_paths(vars=config_vars)

    executable = os.path.join(paths['scripts'], 'python' + sysconfig.get_config_var('EXE'))
    if not os.path.exists(executable):
        msg = f'Virtual environment creation failed, executable {executable} missing'
        raise RuntimeError(msg)

    return _VenvPaths(scripts=paths['scripts'], python_executable=executable, purelib=paths['purelib'])


__all__ = [
    'IsolatedEnv',
    'DefaultIsolatedEnv',
]
