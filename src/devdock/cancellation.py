"""The flag that asks a long operation to stop, shared by everything that honors it.

Kept in a module of its own with no imports of its own, because both ends of
the system need it: the command runner, which must abandon a child process,
and the wait engine, which must stop polling. Anywhere else it would make one
of those depend on the other.
"""

from __future__ import annotations


class CancelToken:
    """A one-way flag someone else sets to stop work at the next safe point.

    Deliberately not an event or a lock. It is set from a signal handler,
    where almost nothing is safe to call, and read from ordinary code; a
    single boolean assignment is atomic under the interpreter lock and needs
    no allocation, which an ``Event`` does not promise.
    """

    __slots__ = ("_cancelled",)

    def __init__(self) -> None:
        self._cancelled = False

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def cancel(self) -> None:
        self._cancelled = True
