"""The sole physical device boundary. Import/setup is lazy."""
import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from .contracts import RuntimeFault
from .foreground import parse_foreground


def find_hdc() -> str:
    candidates = [os.environ.get("HARMONY_HDC"), shutil.which("hdc"), r"F:\DevEco Studio\sdk\default\openharmony\toolchains\hdc.exe"]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(Path(candidate).resolve())
    raise RuntimeFault("device_unavailable", "Set HARMONY_HDC to your SDK hdc executable")


def parse_screen_state(power_output, lock_output):
    """Use explicit service fields only; absent/conflicting evidence is unknown."""
    power = set(re.findall(r"Current State:\s*([A-Z_]+)\b", power_output))
    locked = set(re.findall(r"^\s*\*?\s*screenLocked\s+(true|false)\b", lock_output, re.MULTILINE))
    return {
        "screen_on": True if power == {"AWAKE"} else False if power == {"SLEEP"} else None,
        "screen_locked": True if locked == {"true"} else False if locked == {"false"} else None,
    }


def screen_sleep_confirmed(state):
    """True only when the display is explicitly off.

    On a credential-free device the lock flag may be true or false after sleep;
    it is never required and never invented here. Unknown is not asleep.
    """
    return isinstance(state, dict) and state.get("screen_on") is False


def screen_ready_confirmed(state):
    """True only for a confirmed awake, unlocked screen."""
    return (
        isinstance(state, dict)
        and state.get("screen_on") is True
        and state.get("screen_locked") is False
    )


def parse_diagnostic_sections(raw, marker, count):
    """Reject truncated, duplicated, reordered or failed diagnostic commands."""
    if not isinstance(raw, str):
        raise RuntimeFault("device_unavailable", "Incomplete diagnostic batch")
    text = raw.replace("\r", "")
    pattern = r"\s*"
    for index in range(count):
        prefix = re.escape(f"{marker}:{index}")
        pattern += prefix + r":begin\n(.*?)\n" + prefix + r":end:0\n\s*"
    match = re.fullmatch(pattern, text, re.DOTALL)
    if match is None or any(marker in part for part in match.groups()):
        # Never echo raw diagnostic output or the device identity in errors.
        raise RuntimeFault("device_unavailable", "Incomplete diagnostic batch")
    return match.groups()


class HarmonyDevice:
    supports_foreground = True

    def __init__(self, serial: str):
        import devhelmkit
        from devhelmkit.harmony.config import HarmonyDriverConfig
        from devhelmkit.harmony.device.hdc import HdcDevice
        hdc = find_hdc()
        # connect() discovers devices before applying HarmonyDriverConfig.
        HdcDevice.set_hdc_path(hdc)
        self.driver = devhelmkit.connect(serial=serial, config=HarmonyDriverConfig(hdc_path=hdc))

    @staticmethod
    def discover():
        from devhelmkit.harmony.device.hdc import HdcDevice
        HdcDevice.set_hdc_path(find_hdc())
        return HdcDevice.list_targets()

    def _diagnostics(self, kind):
        """One HDC round trip, ordered read-only queries, strict exit-code framing.

        Never accepts caller-supplied shell text. No caching, parallel reads or
        fallback retries: every guard gets fresh evidence within its IPC budget.
        """
        commands = {
            "foreground": ("hidumper -s WindowManagerService -a '-a'",
                           "hidumper -s AbilityManagerService -a '-l'",
                           "hidumper -s WindowManagerService -a '-a'"),
            "screen": ("hidumper -s PowerManagerService -a '-a'",
                       "hidumper -s ScreenlockService -a -all"),
        }[kind]
        marker = "HRT_" + uuid.uuid4().hex
        parts = []
        for index, command in enumerate(commands):
            parts.append(f"printf '\n{marker}:{index}:begin\n'; "
                         f"{{ {command}; }} 2>&1; rc=$?; "
                         f"printf '\n{marker}:{index}:end:%s\n' \"$rc\"")
        raw = self.driver.shell("; ".join(parts))
        return parse_diagnostic_sections(raw, marker, len(commands))

    def foreground(self):
        before, missions, after = self._diagnostics("foreground")
        return parse_foreground(before, missions, after)

    def screen_state(self):
        power, locked = self._diagnostics("screen")
        return parse_screen_state(power, locked)

    def screen_on(self):
        self.driver.screen_on()

    def wake_up_display(self):
        # Keep the semantic wake-up operation explicit for auditability.
        self.driver.wake_up_display()

    def unlock(self):
        # This uses the driver's no-credential swipe/enter path.
        self.driver.unlock()

    def tree(self):
        result = self.driver.dump_hierarchy()
        if not isinstance(result, dict) or not result:
            raise RuntimeFault("device_unavailable", "Driver returned an empty or invalid hierarchy")
        return result

    def batch_probe(self, script):
        """Run one read-only device-side transaction over a single HDC round trip.

        The script is assembled by ``harmony_runtime.snapshot`` from a fixed
        command vocabulary plus a random marker and derived temp paths; it never
        contains caller-supplied shell text. The call performs no write.
        """
        started = time.monotonic()
        raw = self.driver.shell(script)
        if not isinstance(raw, str):
            raise RuntimeFault("device_unavailable", "Device transaction returned no text")
        return {"raw": raw, "device_ms": round((time.monotonic() - started) * 1000, 3)}

    def screenshot(self):
        return self.driver.screenshot()

    def display(self):
        return (*self.driver.get_display_size(), self.driver.get_display_rotation())

    def dispatch(self, action, target):
        if action.kind in ("tap", "long_press", "input_text", "replace_text"):
            x1,y1,x2,y2 = target["hit_bounds"]
            x,y = (x1+x2)//2,(y1+y2)//2
        if action.kind == "tap": self.driver.click(x,y)
        elif action.kind == "long_press": self.driver.long_click(x,y,1.5)
        elif action.kind in ("input_text", "replace_text"):
            if not target.get("focused"):
                raise RuntimeFault("focus_required", "Tap the field and observe its focus before input")
            if action.kind == "replace_text":
                # replace_text is explicit: clear the observed focused field, then
                # write the requested value. Keep both operations inside one
                # journaled device action so partial execution remains covered by
                # the existing unknown-write barrier.
                from devhelmkit.model.keys import KeyCode
                from .observation import catalog, input_value_matches
                display = self.display()
                items = catalog(self.tree(), display)
                candidates = [item for item in items if item.get("target_fingerprint") == target.get("target_fingerprint")
                              and item.get("bundle") == target.get("bundle") and item.get("bounds") == target.get("bounds")]
                if len(candidates) != 1 or not input_value_matches(items, candidates[0], candidates[0]["text"]):
                    raise RuntimeFault("input_evidence_missing", "Focused field identity or value is not observable")
                field = candidates[0]
                self.driver.press_combination_key(int(KeyCode.CTRL_LEFT), int(KeyCode.A))
                self.driver.press_keycode(int(KeyCode.DEL))
                if not input_value_matches(catalog(self.tree(), display), field, ""):
                    raise RuntimeFault("input_clear_unverified", "Clear was not observed; text was not appended")
                if action.text:
                    self.driver.input_text_on_cursor(action.text)
                if not input_value_matches(catalog(self.tree(), display), field, action.text):
                    raise RuntimeFault("input_replace_unverified", "Replacement value was not observed; do not replay")
            else:
                self.driver.input_text_on_cursor(action.text)
        elif action.kind == "home": self.driver.go_home()
        elif action.kind == "back": self.driver.go_back()
        elif action.kind == "launch": self.driver.app_start(action.bundle, wait_time=0)
        elif action.kind == "swipe": self.driver.swipe_ext(action.direction, scale=0.5)

    def close(self):
        self.driver.close()


# Explicit read-only properties, not a parameter dump. No account/serial/UDID keys.
BASELINE_PROPERTIES = {
    "model": "const.product.model",
    "software_version": "const.product.software.version",
    "os_full_name": "const.ohos.fullname",
    "incremental_version": "const.product.incremental.version",
    "api_version": "const.ohos.apiversion",
    "security_patch": "const.ohos.version.security_patch",
    "abi_list": "const.product.cpu.abilist",
}


def metadata_value(raw, serial="", numeric=False):
    """Fail closed on missing, multi-line, error or identity-bearing responses."""
    if not isinstance(raw, str):
        return None
    value = raw.strip()
    if (not value or len(value) > 192 or (serial and serial in value)
            or not re.fullmatch(r"[A-Za-z0-9 ._()+,/:=-]+", value)
            or re.search(r"error|fail|denied|not found|not exist|unknown|invalid|default|usage|\[", value, re.I)):
        return None
    if numeric and not re.fullmatch(r"[0-9]{1,4}", value):
        return None
    return value


def read_device_baseline(timeout=45):
    """Read-only HDC metadata via devhelmkit, never connect/setup/forward/deploy.

    Discovery has the pinned driver's 20s timeout; a shared deadline then bounds
    all remaining commands. Keep timeout >= 20 rather than promising a shorter
    discovery deadline that the driver cannot honor.
    """
    if not 20 <= timeout <= 120:
        raise ValueError("baseline timeout must be between 20 and 120 seconds")
    from devhelmkit.harmony.device.hdc import HdcDevice
    from devhelmkit.harmony.agent.so_manager import PROTOCOL_V2_THRESHOLD, _compare_version
    deadline = time.monotonic() + timeout
    result = {"status": "not_ready", "device_count": None, "fields": {},
              "phone_operation_verified": False, "protocol_negotiated": False,
              "connection_transport": "not_measured"}
    try:
        hdc = find_hdc()
        HdcDevice.set_hdc_path(hdc)
        targets = HdcDevice.list_targets()
    except Exception:
        result["error_code"] = "discovery_failed"
        return result
    if not isinstance(targets, list) or any(not isinstance(t, str) or not t for t in targets):
        result["error_code"] = "discovery_failed"
        return result
    result["device_count"] = len(targets)
    if len(targets) != 1:
        result["error_code"] = "no_device" if not targets else "multiple_devices"
        return result
    serial = targets[0]
    # Construction allocates local state only. Never call setup/rpc/close here:
    # the diagnostic must not touch a daemon shared with a running runtime.
    device = HdcDevice(serial)
    def remaining():
        budget = deadline - time.monotonic()
        if budget <= 0:
            raise TimeoutError()
        return min(5.0, budget)
    for name, key in BASELINE_PROPERTIES.items():
        try:
            raw = device.shell('param get "' + key + '"', timeout=remaining())
            value = metadata_value(raw, serial, numeric=name == "api_version")
            result["fields"][name] = ({"status": "ok", "value": value} if value else
                                      {"status": "unavailable", "code": "missing_or_invalid"})
        except Exception:
            result["fields"][name] = {"status": "unavailable", "code": "read_failed_or_timeout"}
    try:
        raw = device.shell("uitest --version", timeout=remaining()).strip()
        match = re.fullmatch(r"(?:uitest\s+(?:version[: ]+)?|version[: ]+)?(\d+\.\d+\.\d+\.\d+)", raw, re.I)
        if not match or serial in raw:
            raise ValueError()
        value = match.group(1)
        result["uitest"] = {"status": "ok", "version": value,
                            "driver_predicted_protocol": 2 if _compare_version(value, PROTOCOL_V2_THRESHOLD) > 0 else 1}
    except Exception:
        result["uitest"] = {"status": "unavailable", "code": "version_unavailable"}
    try:
        child = subprocess.run([hdc, "-v"], capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=remaining(), check=True)
        value = metadata_value(child.stdout, serial)
        if not value or not re.fullmatch(r"(?:Ver: ?)?[0-9][A-Za-z0-9.()-]*", value):
            raise ValueError()
        result["hdc"] = {"status": "ok", "version": value}
    except Exception:
        result["hdc"] = {"status": "unavailable", "code": "version_unavailable"}
    required = ("model", "software_version", "os_full_name", "incremental_version", "api_version", "abi_list")
    result["status"] = "ok" if (all(result["fields"][k]["status"] == "ok" for k in required)
                                and result["uitest"]["status"] == "ok"
                                and result["hdc"]["status"] == "ok") else "not_ready"
    result["duration_ms"] = round((time.monotonic() - deadline + timeout) * 1000, 3)
    return result
