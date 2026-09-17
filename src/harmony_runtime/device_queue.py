"""FIFO device admission with bounded, cancellable waiting.

The ticket remains queued between cancellation checks, so polling cannot reorder
requests. This bounds queue waiting only; driver execution has its own budget.
"""
from collections import deque
import threading
import time


class DeviceQueue:
    def __init__(self):
        self._condition = threading.Condition()
        self._tickets = deque()
        self._owner = None

    @property
    def pending(self):
        with self._condition:
            return len(self._tickets)

    def acquire(self, timeout=-1, check=None):
        ticket = object()
        deadline = None if timeout < 0 else time.monotonic() + timeout
        ident = threading.get_ident()
        with self._condition:
            if self._owner == ident:
                raise RuntimeError("Device queue admission is not reentrant")
            self._tickets.append(ticket)
            try:
                while True:
                    if check:
                        check()
                    if deadline is not None and time.monotonic() >= deadline:
                        return False
                    if self._owner is None and self._tickets[0] is ticket:
                        self._tickets.popleft()
                        self._owner = ident
                        return True
                    remaining = .05 if deadline is None else min(.05, max(0, deadline-time.monotonic()))
                    self._condition.wait(remaining)
            finally:
                if ticket in self._tickets:
                    self._tickets.remove(ticket)
                    self._condition.notify_all()

    def release(self):
        with self._condition:
            if self._owner != threading.get_ident():
                raise RuntimeError("Device queue may only be released by its owner")
            self._owner = None
            self._condition.notify_all()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *args):
        self.release()
