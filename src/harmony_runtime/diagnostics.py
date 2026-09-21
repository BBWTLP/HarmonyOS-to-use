"""Bounded environment diagnostics; never initialize a UI driver or expose serials."""
import json
import socket
import subprocess
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from .device import find_hdc
from .contracts import RuntimeFault
from .service import default_state


def service_report(state_dir=None, *, connect_timeout: float = 2.0) -> dict:
    """Read-only health of the resident loopback service (P7-02).

    Two failure modes cost real device time before this existed: a batch started
    with no service at all, and a batch started against a *stale* ``endpoint.json``
    whose process had already exited. Both look like a silent stall from the
    outside. This reports the endpoint file, the port and the recorded pid
    separately, and never sends a runtime request, so it cannot dispatch.

    ``status`` is ``ok`` only when the endpoint is well-formed *and* something is
    listening on its loopback port.
    """
    root = Path(state_dir) if state_dir else Path(default_state())
    endpoint_path = root / "endpoint.json"
    details: dict = {"state_dir": str(root), "endpoint_file": str(endpoint_path)}
    try:
        endpoint = json.loads(endpoint_path.read_text(encoding="utf-8"))
        port, token, pid = endpoint["port"], endpoint["token"], endpoint.get("pid")
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("invalid port")
        if not isinstance(token, str) or not token:
            raise ValueError("invalid token")
    except FileNotFoundError:
        return {"status": "not_ready", "code": "service_endpoint_missing",
                **details,
                "message": "Start `harmony_runtime.cli serve --state-dir <dir>` first; "
                           "the stdio frontend cannot reach a device without it"}
    except (OSError, ValueError, TypeError, KeyError):
        return {"status": "not_ready", "code": "service_endpoint_invalid", **details,
                "message": "endpoint.json is unreadable or malformed; restart the service"}
    details.update(port=port, recorded_pid=pid if type(pid) is int else None)
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=connect_timeout):
            details["port_listening"] = True
    except OSError:
        return {"status": "not_ready", "code": "service_endpoint_stale", **details,
                "port_listening": False,
                "message": f"Nothing is listening on 127.0.0.1:{port}; the recorded "
                           "endpoint is stale. Restart the service (it rewrites the file)"}
    return {"status": "ok", "code": "service_reachable", **details,
            "port_listening": True,
            "message": "Loopback service is reachable. This check does not open a "
                       "session, touch the phone or prove device readiness"}


def report(timeout=10):
    checks = []
    def add(name, status, **details):
        checks.append(dict(name=name, status=status, **details))
    add("python", "ok" if sys.version_info >= (3, 11) else "error",
        version=".".join(map(str, sys.version_info[:3])))
    for package in ("devhelmkit", "mcp", "pydantic", "pillow"):
        try:
            add(package, "ok", version=version(package))
        except PackageNotFoundError:
            add(package, "error", code="dependency_missing")
    try:
        hdc = find_hdc()
        add("hdc", "ok", path=hdc)
    except RuntimeFault:
        add("hdc", "error", code="hdc_not_found")
        hdc = None
    if hdc:
        # The service check comes before device discovery: it is local, fast and
        # independent of the phone, and a stale endpoint is the first thing a
        # device batch trips over.
        service = service_report()
        add("service", service["status"], code=service["code"],
            port=service.get("port"), port_listening=service.get("port_listening", False))
        # The child imports only discovery; connect()/driver setup is never called.
        code = ("from harmony_runtime.device import HarmonyDevice; import json; "
                "print(json.dumps({'device_count': len(HarmonyDevice.discover())}))")
        try:
            result = subprocess.run([sys.executable, "-c", code], capture_output=True,
                                    text=True, timeout=timeout)
            if result.returncode:
                add("device_discovery", "error", code="discovery_failed")
            else:
                data = json.loads(result.stdout)
                count = data["device_count"]
                if type(count) is not int or count < 0:
                    raise ValueError("Invalid count")
                add("device_discovery", "ok" if count else "error", device_count=count,
                    code="devices_detected" if count else "no_device")
        except subprocess.TimeoutExpired:
            add("device_discovery", "error", code="discovery_timeout")
        except (OSError, ValueError, KeyError, TypeError):
            add("device_discovery", "error", code="discovery_failed")
    else:
        service = service_report()
        add("service", service["status"], code=service["code"],
            port=service.get("port"), port_listening=service.get("port_listening", False))
        add("device_discovery", "skipped", code="hdc_not_found")
    return {"status": "ok" if all(c["status"] == "ok" for c in checks) else "not_ready",
            "checks": checks, "service": service, "phone_operation_verified": False}
