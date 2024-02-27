# SPDX-License-Identifier: MIT

from __future__ import annotations

import argparse
import contextlib
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import traceback
import warnings

from collections.abc import Callable, Iterator, Sequence
from functools import partial
from typing import NoReturn, TextIO

import build

from . import ProjectBuilder, _ctx
from . import env as _env
from ._exceptions import BuildBackendException, BuildException, FailedProcessError, UnmetDependenciesError
from ._types import ConfigSettings, Distribution, StrPath


_COLORS = {
    'red': '\33[91m',
    'green': '\33[92m',
    'yellow': '\33[93m',
    'bold': '\33[1m',
    'dim': '\33[2m',
    'underline': '\33[4m',
    'reset': '\33[0m',
}
_NO_COLORS = {color: '' for color in _COLORS}


def _init_colors() -> dict[str, str]:
    if 'NO_COLOR' in os.environ:
        if 'FORCE_COLOR' in os.environ:
            warnings.warn('Both NO_COLOR and FORCE_COLOR environment variables are set, disabling color', stacklevel=2)
        return _NO_COLORS
    elif 'FORCE_COLOR' in os.environ or sys.stdout.isatty():
        return _COLORS
    return _NO_COLORS


_STYLES = _init_colors()


def _cprint(fmt: str = '', msg: str = '') -> None:
    print(fmt.format(msg, **_STYLES), flush=True)


def _showwarning(
    message: Warning | str,
    category: type[Warning],
    filename: str,
    lineno: int,
    file: TextIO | None = None,
    line: str | None = None,
) -> None:  # pragma: no cover
    _cprint('{yellow}WARNING{reset} {}', str(message))


_max_terminal_width = min(shutil.get_terminal_size().columns - 2, 127)


_fill = partial(textwrap.fill, width=_max_terminal_width)


def _log_to_stdout(message: str, *, origin: _ctx.LogMessageOrigin = _ctx.LogMessageOrigin.Logging) -> None:
    if origin is _ctx.LogMessageOrigin.Logging:
        for idx, line in enumerate(message.splitlines()):
            _cprint(
                '{bold}{}{reset}',
                _fill(line, initial_indent='* ' if idx == 0 else '  ', subsequent_indent='  '),
            )
    elif origin in {_ctx.LogMessageOrigin.CommandInput, _ctx.LogMessageOrigin.CommandOutput}:
        initial_indent = '< ' if origin is _ctx.LogMessageOrigin.CommandOutput else '> '
        for line in message.splitlines():
            _cprint(
                '{dim}{}{reset}',
                _fill(line, initial_indent=initial_indent, subsequent_indent='  '),
            )


def _setup_cli(*, verbosity: int) -> None:
    warnings.showwarning = _showwarning

    if sys.platform.startswith('win32'):
        try:
            import colorama

            colorama.init()
        except ModuleNotFoundError:
            pass

    _ctx.LOGGER.set(_log_to_stdout)

    _ctx.VERBOSITY.set(verbosity)
    if _ctx.verbosity:
        import logging

        logging.root.setLevel(logging.DEBUG if _ctx.verbosity > 1 else logging.INFO)


def _format_dep_chain(dep_chain: Sequence[str]) -> str:
    return ' -> '.join(dep.partition(';')[0].strip() for dep in dep_chain)


def _build_in_isolated_env(
    source_dir: StrPath,
    distribution: Distribution,
    *,
    output_dir: StrPath,
    config_settings: ConfigSettings | None,
    env_impl: _env.EnvImpl | None,
) -> str:
    with _env.DefaultIsolatedEnv(env_impl) as env:
        builder = ProjectBuilder.from_isolated_env(env, source_dir)
        # first install the build dependencies
        env.install(builder.build_system_requires)
        # then get the extra required dependencies from the backend (which was installed in the call above :P)
        env.install(builder.get_requires_for_build(distribution, config_settings or {}))
        return builder.build(distribution, output_dir, config_settings or {})


def _build_in_current_env(
    source_dir: StrPath,
    distribution: Distribution,
    *,
    output_dir: StrPath,
    config_settings: ConfigSettings | None,
    skip_dependency_check: bool,
) -> str:
    builder = ProjectBuilder(source_dir)

    if not skip_dependency_check:
        missing = builder.check_dependencies(distribution, config_settings or {})
        if missing:
            raise UnmetDependenciesError(missing)

    return builder.build(distribution, output_dir, config_settings or {})


@contextlib.contextmanager
def _handle_build_error() -> Iterator[None]:
    def error(msg: str, code: int = 1) -> NoReturn:  # pragma: no cover
        """
        Print an error message and exit. Will color the output when writing to a TTY.

        :param msg: Error message
        :param code: Error code
        """
        _cprint('{red}ERROR{reset} {}', msg)
        raise SystemExit(code)

    try:
        yield

    except UnmetDependenciesError as e:
        dependencies = ''.join(
            '\n\t' + dep for deps in e.unmet_dependencies for dep in (deps[0], _format_dep_chain(deps[1:])) if dep
        )
        _cprint()
        error('Missing dependencies:' + dependencies)

    except (BuildException, FailedProcessError) as e:
        error(str(e))

    except BuildBackendException as e:
        if isinstance(e.exception, subprocess.CalledProcessError):
            _cprint()
            error(str(e))

        if e.exc_info:
            tb_lines = traceback.format_exception(e.exc_info[0], e.exc_info[1], e.exc_info[2], limit=-1)
            tb = ''.join(tb_lines)
        else:
            tb = traceback.format_exc(-1)
        _cprint('\n{dim}{}{reset}\n', tb.strip('\n'))
        error(str(e))

    except Exception as e:  # pragma: no cover
        tb = traceback.format_exc().strip('\n')
        _cprint('\n{dim}{}{reset}\n', tb)
        error(str(e))


def _natural_language_list(elements: Sequence[str]) -> str:
    if len(elements) == 0:
        msg = 'no elements'
        raise IndexError(msg)
    elif len(elements) == 1:
        return elements[0]
    else:
        return '{} and {}'.format(
            ', '.join(elements[:-1]),
            elements[-1],
        )


def build_package(
    source_dir: StrPath,
    distributions: Sequence[str],
    build: Callable[[StrPath, str], str],
) -> Sequence[str]:
    """
    Run the build process.

    :param source_dir: Source directory
    :param distribution: Distribution to build (sdist or wheel)
    :param build: Builder callback
    """
    return [os.path.basename(build(source_dir, d)) for d in distributions]


def build_package_via_sdist(
    source_dir: StrPath,
    distributions: Sequence[str],
    build: Callable[[StrPath, str], str],
) -> Sequence[str]:
    """
    Build a sdist and then the specified distributions from it.

    :param source_dir: Source directory
    :param distribution: Distribution to build (only wheel)
    :param build: Builder callback
    """
    from ._compat import tarfile

    if 'sdist' in distributions:
        msg = 'Only binary distributions are allowed but sdist was specified'
        raise ValueError(msg)

    sdist = build(source_dir, 'sdist')

    sdist_name = os.path.basename(sdist)
    built = [sdist_name]

    if distributions:
        # extract sdist
        with tempfile.TemporaryDirectory(prefix='build-via-sdist-') as sdist_dir, \
                tarfile.TarFile.open(sdist) as sdist_tar:  # fmt: skip
            sdist_tar.extractall(sdist_dir)

            _ctx.log(f'Preparing to build {_natural_language_list(distributions)} from sdist')

            source_dir = os.path.join(sdist_dir, sdist_name[: -len('.tar.gz')])
            for distribution in distributions:
                out = build(source_dir, distribution)
                built.append(os.path.basename(out))

    return built


def _map_config_settings(config_settings_list: list[str] | None) -> ConfigSettings:
    config_settings: dict[str, str | list[str]] = {}

    if config_settings_list:
        for arg in config_settings_list:
            setting, _, value = arg.partition('=')
            if setting not in config_settings:
                config_settings[setting] = value
            elif isinstance((existing_setting := config_settings[setting]), list):
                existing_setting.append(value)
            else:
                config_settings[setting] = [setting]

    return config_settings


def main_parser() -> argparse.ArgumentParser:
    """
    Construct the main parser.
    """
    parser = argparse.ArgumentParser(
        description=textwrap.indent(
            textwrap.dedent(
                """
                A simple, correct Python build frontend.

                By default, a source distribution (sdist) is built from {srcdir}
                and a binary distribution (wheel) is built from the sdist.
                This is recommended as it will ensure the sdist can be used
                to build wheels.

                Pass -s/--sdist and/or -w/--wheel to build a specific distribution.
                If you do this, the default behavior will be disabled, and all
                artifacts will be built from {srcdir} (even if you combine
                -w/--wheel with -s/--sdist, the wheel will be built from {srcdir}).
                """
            ).strip(),
            '    ',
        ),
        formatter_class=partial(
            argparse.RawDescriptionHelpFormatter,
            # Prevent argparse from taking up the entire width of the terminal window
            # which impedes readability.
            width=_max_terminal_width,
        ),
    )
    parser.add_argument(
        'srcdir',
        type=str,
        nargs='?',
        default=os.getcwd(),
        help='source directory (defaults to current directory)',
    )
    parser.add_argument(
        '--version',
        '-V',
        action='version',
        version=f"build {build.__version__} ({','.join(build.__path__)})",
    )
    parser.add_argument(
        '--verbose',
        '-v',
        action='count',
        default=0,
        help='increase verbosity',
    )
    parser.add_argument(
        '--sdist',
        '-s',
        dest='distributions',
        action='append_const',
        const='sdist',
        help='build a source distribution (disables the default behavior)',
    )
    parser.add_argument(
        '--wheel',
        '-w',
        dest='distributions',
        action='append_const',
        const='wheel',
        help='build a wheel (disables the default behavior)',
    )
    parser.add_argument(
        '--outdir',
        '-o',
        type=str,
        help=f'output directory (defaults to {{srcdir}}{os.sep}dist)',
        metavar='PATH',
    )
    parser.add_argument(
        '--skip-dependency-check',
        '-x',
        action='store_true',
        help='do not check that build dependencies are installed',
    )
    env_group = parser.add_mutually_exclusive_group()
    env_group.add_argument(
        '--no-isolation',
        '-n',
        action='store_true',
        help='disable building the project in an isolated virtual environment. '
        'Build dependencies must be installed separately when this option is used',
    )
    env_group.add_argument(
        '--env-impl',
        choices=_env.ENV_IMPLS,
        help='isolated environment implementation to use.  Defaults to virtualenv if installed, '
        ' otherwise venv.  uv support is experimental.',
    )
    parser.add_argument(
        '--config-setting',
        '-C',
        dest='config_settings',
        action='append',
        help='settings to pass to the backend.  Multiple settings can be provided. '
        'Settings beginning with a hyphen will erroneously be interpreted as options to build if separated '
        'by a space character; use ``--config-setting=--my-setting -C--my-other-setting``',
        metavar='KEY[=VALUE]',
    )
    return parser


def main(cli_args: Sequence[str], prog: str | None = None) -> None:
    """
    Parse the CLI arguments and invoke the build process.

    :param cli_args: CLI arguments
    :param prog: Program name to show in help text
    """

    parser = main_parser()
    if prog:
        parser.prog = prog
    args = parser.parse_args(cli_args)

    _setup_cli(verbosity=args.verbose)

    build = partial(
        partial(
            _build_in_current_env,
            skip_dependency_check=args.skip_dependency_check,
        )
        if args.no_isolation
        else partial(
            _build_in_isolated_env,
            env_impl=args.env_impl,
        ),
        # outdir is relative to srcdir only if omitted.
        output_dir=os.path.join(args.srcdir, 'dist') if args.outdir is None else args.outdir,
        config_settings=_map_config_settings(args.config_settings),
    )
    build_all = partial(
        partial(
            build_package,
            distributions=args.distributions,
        )
        if args.distributions
        else partial(
            build_package_via_sdist,
            distributions=['wheel'],
        ),
        args.srcdir,
        build=build,
    )

    with _handle_build_error():
        built = build_all()
        artifact_list = _natural_language_list(
            ['{underline}{}{reset}{bold}{green}'.format(artifact, **_STYLES) for artifact in built]
        )
        _cprint('{bold}{green}Successfully built {}{reset}', artifact_list)


def entrypoint() -> None:
    main(sys.argv[1:])


if __name__ == '__main__':  # pragma: no cover
    main(sys.argv[1:], 'python -m build')


__all__ = [
    'main',
    'main_parser',
]
