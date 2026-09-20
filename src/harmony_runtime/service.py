"""Authenticated loopback IPC. Device construction remains lazy."""
import json
import os
import secrets
import socket
import threading
from http.client import HTTPException
from urllib.error import URLError
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen
from pydantic import ValidationError
from .contracts import RuntimeFault
from .runtime import Runtime

MAX_REQUEST = 65536

def default_state():
    return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "HarmonyMobileRuntime"

class Singleton:
    def __init__(self, root):
        self.path = Path(root) / "runtime.lock"
    def __enter__(self):
        self.file = self.path.open("a+b")
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                if self.path.stat().st_size == 0:
                    self.file.write(b"0"); self.file.flush(); self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            self.file.close()
            raise
        return self
    def __exit__(self, *args):
        self.file.close()

def private_directory(root):
    root.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        import subprocess
        sid = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True, text=True, check=True).stdout
        import csv
        user_sid = next(csv.reader([sid.strip()]))[1]
        subprocess.run(["icacls", str(root), "/inheritance:r", "/grant:r", f"*{user_sid}:(OI)(CI)F"], capture_output=True, check=True)
    else:
        root.chmod(0o700)

def serve(root=None, runtime_factory=Runtime, ready=None, request_read_timeout=5, host_factory=None):
    if request_read_timeout <= 0:
        raise ValueError("Request read timeout must be positive")
    root = Path(root or default_state())
    private_directory(root)
    with Singleton(root):
        runtime = runtime_factory(root)
        if host_factory is None:
            from harmony_agent.host import host_from_env
            host = host_from_env(runtime, root, Path(__file__).resolve().parents[2])
        else:
            host = host_factory(runtime, root)
        token = secrets.token_urlsafe(32)
        owners = set()
        guard = threading.Lock()
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def handle(self):
                # A total header/body deadline also bounds slow trickle senders;
                # a socket idle timeout alone would reset on every received byte.
                self.connection.settimeout(request_read_timeout)
                def expire():
                    try: self.connection.shutdown(socket.SHUT_RDWR)
                    except OSError: pass
                self.read_timer = threading.Timer(request_read_timeout, expire)
                self.read_timer.daemon = True
                self.read_timer.start()
                try:
                    super().handle()
                except OSError:
                    pass
                finally:
                    self.read_timer.cancel()
                    self.read_timer.join()
            def do_POST(self):
                if self.path != "/rpc" or not secrets.compare_digest(self.headers.get("Authorization", ""), "Bearer " + token):
                    self.send_error(403); return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= MAX_REQUEST:
                        self.send_error(413); return
                    raw = self.rfile.read(length)
                    self.read_timer.cancel()
                    self.read_timer.join()
                    if len(raw) != length:
                        self.close_connection = True
                        return
                    body = json.loads(raw)
                    if not isinstance(body, dict):
                        raise ValueError("Request must be an object")
                    op, args = body["method"], body.get("params", {})
                    if op == "hello":
                        owner = secrets.token_urlsafe(24)
                        with guard: owners.add(owner)
                        result = {"owner": owner, "protocol": 1}
                    else:
                        owner = body.get("owner")
                        with guard: valid = owner in owners
                        if not valid: raise RuntimeFault("client_invalid", "Reconnect frontend")
                        methods = {"session": runtime.session, "observe": runtime.observe, "act": runtime.act, "burst": runtime.burst, "wait": runtime.wait, "history": runtime.history}
                        if host is not None:
                            methods.update({
                                "agent_run_task": host.run_task,
                                "agent_task_status": host.task_status,
                                "agent_task_control": host.task_control,
                                "agent_task_events": host.task_events,
                                "agent_task_result": host.task_result,
                                "agent_decide": host.decide,
                                "agent_artifacts_index": host.artifacts_index,
                                "agent_artifacts_sweep": host.artifacts_sweep,
                                "agent_artifacts_export": host.artifacts_export,
                                "agent_diagnostics": host.diagnostics,
                            })
                        if op not in methods: raise RuntimeFault("invalid_arguments", "Unknown method")
                        result = methods[op](owner, **args)
                    response = {"result": result}
                except (TimeoutError, ConnectionError):
                    self.close_connection = True
                    return
                except RuntimeFault as e:
                    response = {"error": {"code": e.code, "message": str(e)}}
                except (ValueError, TypeError, KeyError, ValidationError):
                    response = {"error": {"code": "invalid_arguments", "message": "Request does not match the contract"}}
                except Exception:
                    response = {"error": {"code": "internal_error", "message": "Operation failed; inspect journal before retrying a write"}}
                data = json.dumps(response, ensure_ascii=False).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                try: self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError): pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = False
        endpoint = root / "endpoint.json"
        temporary = root / "endpoint.tmp"
        temporary.write_text(json.dumps({"port":server.server_port,"token":token,"pid":os.getpid(),"protocol":1}), encoding="utf-8")
        temporary.replace(endpoint)
        if ready: ready(server)
        try: server.serve_forever(poll_interval=.2)
        finally:
            # Cancel active work before server_close joins request threads.
            # Workers must still be able to persist uncertain write outcomes.
            try:
                if host is not None:
                    host.close()
                runtime.close()
            finally:
                try:
                    server.server_close()
                finally:
                    endpoint.unlink(missing_ok=True)

class Client:
    def __init__(self, root=None):
        self.root = Path(root or default_state())
        self.owner = None
        self.lock = threading.Lock()
    def _send(self, method, params):
        try:
            endpoint = json.loads((self.root / "endpoint.json").read_text(encoding="utf-8"))
            port, token = endpoint["port"], endpoint["token"]
            if type(port) is not int or not 1 <= port <= 65535 or not isinstance(token, str) or not token:
                raise ValueError("Invalid endpoint")
        except (OSError, ValueError, TypeError, KeyError):
            raise RuntimeFault("runtime_unavailable", "Runtime connection information is unavailable; start the local service") from None
        body = json.dumps({"method":method,"params":params,"owner":self.owner}).encode()
        request = Request(f"http://127.0.0.1:{port}/rpc", data=body, headers={"Authorization":"Bearer "+token,"Content-Type":"application/json"})
        # Never retry transport failures: a write may already have reached the device.
        try:
            with urlopen(request, timeout=65) as response:
                result = json.load(response)
            if not isinstance(result, dict):
                raise ValueError("Invalid response")
            if "error" in result:
                error = result["error"]
                if not isinstance(error, dict) or not all(isinstance(error.get(k), str) for k in ("code", "message")):
                    raise ValueError("Invalid error response")
                raise RuntimeFault(error["code"], error["message"])
            if "result" not in result or not isinstance(result["result"], dict):
                raise ValueError("Missing result")
            if method == "hello" and (not isinstance(result["result"].get("owner"), str) or not result["result"]["owner"] or result["result"].get("protocol") != 1):
                raise ValueError("Invalid handshake")
            return result["result"]
        except (OSError, URLError, HTTPException, ValueError, TypeError, KeyError):
            if method in ("act", "burst"):
                raise RuntimeFault("execution_unknown", "Action response was lost or invalid; do not repeat the action with a new request ID. Reconnect and reconcile the journal first") from None
            raise RuntimeFault("runtime_transport_error", "Runtime response was lost or invalid; reconnect and check session state") from None
    def call(self, method, **params):
        with self.lock:
            if self.owner is None: self.owner = self._send("hello", {})["owner"]
            owner = self.owner
        try:
            return self._send(method, params)
        except RuntimeFault as error:
            if error.code != "client_invalid":
                raise
            # The authenticated service rejected this identity before dispatch.
            # Forget only this generation; never replay the original call.
            with self.lock:
                if self.owner == owner:
                    self.owner = None
            raise RuntimeFault("session_reopen_required", "Runtime no longer recognizes this frontend. Open a new session, query original request IDs, and observe again. The rejected request was not retried") from None
