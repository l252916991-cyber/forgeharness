"""Execution backends: where a tool's files live and where its commands run.

The package exists so the answer to that question is a constructor argument. The
protocol is in :mod:`forgeharness.backends.protocol`; the three implementations are the
host filesystem, a container sandbox, and a prefix router over both.
"""

from forgeharness.backends.composite import CompositeBackend, composite_factory
from forgeharness.backends.container import (
    CONTAINER_WORKDIR,
    ContainerBackend,
    ContainerLimits,
    WarmContainerPool,
    container_argv,
    container_backend_factory,
    docker_runner,
    exec_argv,
    pooled_argv,
)
from forgeharness.backends.local import LocalBackend, local_backend
from forgeharness.backends.protocol import (
    BackendError,
    BackendFactory,
    FileBackend,
    FileConflict,
    FileEntry,
    FileTooLarge,
    NotRegularFile,
    ReadResult,
    WriteResult,
)

__all__ = [
    "CONTAINER_WORKDIR",
    "BackendError",
    "BackendFactory",
    "CompositeBackend",
    "ContainerBackend",
    "ContainerLimits",
    "FileBackend",
    "FileConflict",
    "FileEntry",
    "FileTooLarge",
    "LocalBackend",
    "NotRegularFile",
    "ReadResult",
    "WarmContainerPool",
    "WriteResult",
    "composite_factory",
    "container_argv",
    "container_backend_factory",
    "docker_runner",
    "exec_argv",
    "local_backend",
    "pooled_argv",
]
