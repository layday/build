# SPDX-License-Identifier: MIT

from __future__ import annotations

import contextlib
import tempfile

from collections.abc import Iterator
from pathlib import Path

import packaging.metadata

from . import ProjectBuilder
from ._types import StrPath
from .env import DefaultIsolatedEnv, Installer


def get_wheel_metadata(
    source_dir: StrPath,
    *,
    installer: Installer | None = 'pip',
) -> packaging.metadata.Metadata:
    """Return a project's wheel METADATA.

    Uses the ``prepare_metadata_for_build_wheel`` hook if available,
    otherwise ``build_wheel``.

    :param source_dir: Project source directory
    :param installer: Dependency installer to use.  An installer value of
        ``None`` implies no isolation.
    """

    @contextlib.contextmanager
    def prepare_builder() -> Iterator[ProjectBuilder]:
        if installer:
            with DefaultIsolatedEnv(installer=installer) as env:
                builder = ProjectBuilder(source_dir, env=env)
                env.install_requirements(builder.build_system_requires)
                env.install_requirements(builder.get_requires_for_build('wheel'))
                yield builder

        else:
            yield ProjectBuilder(source_dir)

    with prepare_builder() as builder, tempfile.TemporaryDirectory() as temp_dir:
        distinfo_path = builder.metadata_path(temp_dir)
        metadata_path = Path(distinfo_path, 'METADATA')
        return packaging.metadata.Metadata.from_email(metadata_path.read_bytes())


__all__ = [
    'get_wheel_metadata',
]
