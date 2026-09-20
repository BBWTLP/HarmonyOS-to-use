"""Bounded, quarantining invocation of an optional visual provider backend.

Ownership of the deadline is explicit:

``backend.deadline_bounded is True``
    The backend's own transport enforces the timeout (connection read deadline,
    subprocess kill, and so on). It is called directly and is the only shape that
    is considered production-ready.

``backend.deadline_bounded is False`` (the default)
    Python cannot kill a thread, so this helper provides the *bounded caller*
    that the runtime needs: the call runs in one daemon thread and the caller
    stops waiting at the deadline. A backend that never returns therefore costs
    at most one stuck thread, after which the provider is **quarantined** and
    every later call fails fast without spawning another thread. That bound is
    what keeps a long-running service from accumulating zombies, and it is why
    such a backend is reported as not production-ready rather than pretending to
    have a hard timeout.
"""
from __future__ import annotations

import threading
from typing import Any, Callable


class DeadlineExceeded(RuntimeError):
    """The backend did not answer within its deadline."""


class BoundedCaller:
    def __init__(self, name: str, timeout_seconds: float):
        self.name = name
        self.timeout_seconds = float(timeout_seconds)
        self.quarantined = False
        self.timeouts = 0
        self.calls = 0
        self._lock = threading.Lock()

    @property
    def reason(self) -> str:
        return f"{self.name}_quarantined" if self.quarantined else "ready"

    def call(self, function: Callable[[], Any]) -> Any:
        """Run ``function`` with a deadline; raise DeadlineExceeded on timeout."""
        with self._lock:
            if self.quarantined:
                raise DeadlineExceeded(
                    f"{self.name} is quarantined after a timeout; no further call is attempted")
            self.calls += 1
        result: dict[str, Any] = {}

        def run() -> None:
            try:
                result["value"] = function()
            except BaseException as error:  # reported to the caller verbatim
                result["error"] = error

        worker = threading.Thread(target=run, name=f"{self.name}-caller", daemon=True)
        worker.start()
        worker.join(self.timeout_seconds)
        if worker.is_alive():
            with self._lock:
                self.quarantined = True
                self.timeouts += 1
            raise DeadlineExceeded(
                f"{self.name} exceeded {self.timeout_seconds}s; the provider is quarantined")
        if "error" in result:
            raise result["error"]
        return result.get("value")
