"""Bounded environment diagnostics; never initialize a UI driver or expose serials."""
import json
import subprocess
import sys
from importlib.metadata import PackageNotFoundError, version
from .device import find_hdc
from .contracts import RuntimeFault


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
        add("device_discovery", "skipped", code="hdc_not_found")
    return {"status": "ok" if all(c["status"] == "ok" for c in checks) else "not_ready",
            "checks": checks, "phone_operation_verified": False}
