"""Resident device subprocess with deadline-bound local IPC and quarantine.

Terminating a host worker cannot undo an operation already sent to the phone.
An interrupted worker is never automatically replaced or used for another write.
"""
import contextlib
import io
import multiprocessing
import os
import pickle
import threading
import time
from .contracts import RuntimeFault
from .device import HarmonyDevice

_MAX_REPLY = 32 * 1024 * 1024
_METHODS = frozenset((
    "tree", "display", "screen_state", "screenshot", "foreground",
    "screen_on", "wake_up_display", "unlock",
    "batch_probe", "dispatch", "close",
))


def _device_main(connection, factory, serial):
    device = None
    # Driver diagnostics must never leak into a hosting stdio MCP connection.
    with open(os.devnull, "w") as sink, contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
        try:
            while True:
                method, args = connection.recv()
                if method not in _METHODS:
                    raise ValueError("Unknown device operation")
                try:
                    if device is None:
                        device = factory(serial)
                    result = getattr(device, method)(*args)
                    if method == "screenshot":
                        output = io.BytesIO()
                        result.save(output, format="PNG")
                        result = output.getvalue()
                    response = ("ok", result)
                except RuntimeFault as exc:
                    response = ("fault", (exc.code, str(exc)))
                except Exception as exc:
                    response = ("fault", ("device_unavailable", type(exc).__name__))
                payload = pickle.dumps(response, protocol=5)
                if len(payload) > _MAX_REPLY:
                    payload = pickle.dumps(("fault", ("device_response_too_large", "Device reply exceeds local IPC limit")))
                connection.send_bytes(payload)
                if method == "close":
                    return
        except (EOFError, BrokenPipeError, OSError):
            return
        finally:
            connection.close()


class ProcessDevice:
    """All methods are called under the Runtime's per-device FIFO lock."""
    supports_foreground = True
    def __init__(self, serial, factory=HarmonyDevice):
        self.serial = serial
        self.factory = factory
        self.process = None
        self.connection = None
        self.deadline = None
        self.check = None
        self.quarantined = False
        self.closed = False

    @contextlib.contextmanager
    def budget(self, deadline, check):
        previous = self.deadline, self.check
        self.deadline, self.check = min(deadline, self.deadline) if self.deadline is not None else deadline, check
        try:
            yield
        finally:
            self.deadline, self.check = previous

    def _stop(self):
        # Bounded host cleanup. A live survivor remains quarantined.
        if self.process is not None:
            if self.process.is_alive():
                self.process.terminate()
            self.process.join(.2)
            if self.process.is_alive():
                self.process.kill()
                self.process.join(.2)
        if self.connection is not None:
            self.connection.close()

    def _call(self, method, *args):
        if self.closed:
            raise RuntimeFault("device_unavailable", "Device worker is closed")
        if self.quarantined:
            raise RuntimeFault("device_quarantined", "Previous device call was interrupted; recovery is required before further operations")
        if self.deadline is None:
            raise RuntimeFault("internal_error", "Device calls require an operation deadline")
        if self.check:
            self.check()
        if time.monotonic() >= self.deadline:
            raise RuntimeFault("timeout", "Device budget expired before call")
        if self.process is None:
            context = multiprocessing.get_context("spawn")
            self.connection, child = context.Pipe()
            self.process = context.Process(target=_device_main, args=(child, self.factory, self.serial), daemon=True)
            try:
                self.process.start()
            except Exception:
                self.connection.close()
                self.process = None
                raise
            finally:
                child.close()
        # Process creation may consume the budget or overlap a session pause.
        # Recheck before a transport thread can enqueue a physical operation.
        try:
            if self.check:
                self.check()
            if time.monotonic() >= self.deadline:
                raise RuntimeFault("timeout", "Device budget expired during worker startup")
        except BaseException:
            self.quarantined = True
            self._stop()
            raise
        done = threading.Event()
        reply = []
        connection = self.connection
        def exchange():
            try:
                # Thread scheduling can itself consume the remaining budget.
                if self.check:
                    self.check()
                if time.monotonic() >= self.deadline:
                    raise RuntimeFault("timeout", "Device budget expired before transport send")
                connection.send((method, args))
                reply.append(pickle.loads(connection.recv_bytes(_MAX_REPLY)))
            except RuntimeFault as exc:
                reply.append(("fault", (exc.code, str(exc))))
            except Exception:
                reply.append(("transport_error", None))
            finally:
                done.set()
        transport = threading.Thread(target=exchange, daemon=True)
        transport.start()
        try:
            while True:
                if self.check:
                    self.check()
                remaining = self.deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeFault("timeout", "Device call exceeded operation deadline")
                if done.wait(min(.02, remaining)):
                    if self.check:
                        self.check()
                    if time.monotonic() >= self.deadline:
                        raise RuntimeFault("timeout", "Device reply arrived after operation deadline")
                    break
            kind, value = reply[0]
            if kind == "transport_error":
                raise RuntimeFault("device_worker_lost", "Device worker disconnected during a call")
        except BaseException:
            self.quarantined = True
            self._stop()
            transport.join(.1)
            raise
        if kind == "fault":
            raise RuntimeFault(*value)
        return value

    def tree(self): return self._call("tree")
    def display(self): return self._call("display")
    def foreground(self): return self._call("foreground")
    def screen_state(self): return self._call("screen_state")
    def screen_on(self): return self._call("screen_on")
    def wake_up_display(self): return self._call("wake_up_display")
    def unlock(self): return self._call("unlock")
    def dispatch(self, action, target): return self._call("dispatch", action, target)
    def batch_probe(self, script): return self._call("batch_probe", script)
    def screenshot(self):
        from PIL import Image
        image = Image.open(io.BytesIO(self._call("screenshot")))
        image.load()
        return image

    def close(self):
        if self.closed:
            return
        try:
            if self.process is not None and not self.quarantined:
                with self.budget(time.monotonic() + .5, None):
                    self._call("close")
        except Exception:
            pass
        finally:
            self.closed = True
            self._stop()
