# SPDX-License-Identifier: MIT

from __future__ import annotations

import contextlib
import tempfile

from collections.abc import Iterator

import pyproject_hooks

from . import ProjectBuilder
from . import env as _env
from ._compat import importlib
from ._types import StrPath


def project_wheel_metadata(
    source_dir: StrPath,
    isolated: bool = True,
    *,
    runner: pyproject_hooks.SubprocessRunner = pyproject_hooks.quiet_subprocess_runner,
) -> importlib.metadata.PackageMetadata:
    """
    Return the wheel metadata for a project.

    Uses the ``prepare_metadata_for_build_wheel`` hook if available,
    otherwise ``build_wheel``.

    :param source_dir: Project source directory
    :param isolated: Whether or not to run invoke the backend in the current
                     environment or to create an isolated one and invoke it
                     there.
    :param runner: An alternative runner for backend subprocesses
    """

    @contextlib.contextmanager
    def prepare_builder() -> Iterator[ProjectBuilder]:
        if isolated:
            with _env.DefaultIsolatedEnv() as env:
                builder = ProjectBuilder.from_isolated_env(env, source_dir, runner)
                env.install(builder.build_system_requires)
                env.install(builder.get_requires_for_build('wheel'))
                yield builder
        else:
            yield ProjectBuilder(source_dir, runner=runner)

    with tempfile.TemporaryDirectory() as temp_dir, \
            prepare_builder() as builder:  # fmt: skip
        return importlib.metadata.Distribution.at(
            builder.metadata_path(temp_dir),
        ).metadata


__all__ = [
    'project_wheel_metadata',
]
