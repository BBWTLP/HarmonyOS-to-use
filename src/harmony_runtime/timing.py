"""Monotonic phase timings; diagnostic only, never used for execution budgets."""
import time
from contextlib import contextmanager


class Timings:
    def __init__(self, clock=None):
        self.clock = clock or time.monotonic
        self.seconds = {}

    @contextmanager
    def phase(self, name):
        started = self.clock()
        try:
            yield
        finally:
            self.seconds[name] = self.seconds.get(name, 0) + max(0, self.clock() - started)

    def call(self, name, function, *args, **kwargs):
        with self.phase(name):
            return function(*args, **kwargs)

    def milliseconds(self):
        return {name + "_ms": round(value * 1000, 3) for name, value in self.seconds.items()}
