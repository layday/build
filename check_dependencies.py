# Adapted from ``src/build/__init__.py``.

from typing import AbstractSet, Iterator, List, Optional, Tuple

from packaging.requirements import Requirement
from typing_extensions import Protocol


class MetadataCallback(Protocol):
    def __call__(self, dist_name):
        # type: (str) -> Optional[Tuple[str, Optional[List[str]]]]
        """
        Given a distribution name, return a version and requirements two-tuple
        from distribution metadata, or ``None``.
        """


def extract_dist_version_and_requires(dist_name):
    # type: (str) -> Optional[Tuple[str, Optional[List[str]]]]
    # Not implemented in ``packaging``
    import sys

    if sys.version_info >= (3, 8):
        from importlib import metadata as importlib_metadata
    else:
        import importlib_metadata

    try:
        dist = importlib_metadata.distribution(dist_name)
    except importlib_metadata.PackageNotFoundError:
        # dependency is not installed in the environment.
        return None
    else:
        return dist.version, dist.requires


def check_dependency(
    req_string,
    callback,
    _ancestral_req_strings=(),
    _parent_extras=frozenset(),
):
    # type: (str, MetadataCallback, Tuple[str, ...], AbstractSet[str]) -> Iterator[Tuple[str, ...]]
    "Verify that a dependency and all of its dependencies are met."
    req = Requirement(req_string)

    if req.marker:
        extras = frozenset(('',)).union(_parent_extras)
        # a requirement can have multiple extras but ``evaluate`` can
        # only check one at a time.
        if all(not req.marker.evaluate(environment={'extra': e}) for e in extras):
            # if the marker conditions are not met, we pretend that the
            # dependency is satisfied.
            return

    metadata = callback(req.name)
    if metadata is None:
        yield _ancestral_req_strings + (req_string,)
        return

    version, reqs = metadata
    if req.specifier and version not in req.specifier:
        # the installed version is incompatible.
        yield _ancestral_req_strings + (req_string,)
    elif reqs:
        for other_req_string in reqs:
            for unmet_req in check_dependency(
                other_req_string, callback, _ancestral_req_strings + (req_string,), req.extras
            ):
                # a transitive dependency is not satisfied.
                yield unmet_req


check_dependency('packaging', extract_dist_version_and_requires)
