"""Runtime adapters that isolate local tool invocation from command handlers."""

from devdock.adapters.base import (
    CommandResult,
    CommandRunner,
    ComposeCapabilities,
    ConfigValidation,
    ContainerRecord,
    DeclaredDependencies,
    DeclaredDependency,
    DeclaredServices,
    NetworkRecord,
    OperationOutcome,
    PublishedPort,
    ResourceInventory,
    RuntimeAdapter,
    ServiceObservation,
    ServiceObservations,
)
from devdock.adapters.compose import ComposeAdapter, run_command

__all__ = [
    "CommandResult",
    "CommandRunner",
    "ComposeAdapter",
    "ComposeCapabilities",
    "ConfigValidation",
    "ContainerRecord",
    "DeclaredDependencies",
    "DeclaredDependency",
    "DeclaredServices",
    "NetworkRecord",
    "PublishedPort",
    "ResourceInventory",
    "RuntimeAdapter",
    "ServiceObservation",
    "ServiceObservations",
    "OperationOutcome",
    "run_command",
]
