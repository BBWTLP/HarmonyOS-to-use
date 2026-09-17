"""The sole physical device boundary. Import/setup is lazy."""
import os
import re
import shutil
from pathlib import Path
from .contracts import RuntimeFault


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


class HarmonyDevice:
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

    def screen_state(self):
        power = self.driver.shell("hidumper -s PowerManagerService -a '-a'")
        locked = self.driver.shell("hidumper -s ScreenlockService -a -all")
        return parse_screen_state(power, locked)

    def tree(self):
        result = self.driver.dump_hierarchy()
        if not isinstance(result, dict) or not result:
            raise RuntimeFault("device_unavailable", "Driver returned an empty or invalid hierarchy")
        return result

    def screenshot(self):
        return self.driver.screenshot()

    def display(self):
        return (*self.driver.get_display_size(), self.driver.get_display_rotation())

    def dispatch(self, action, target):
        if action.kind in ("tap", "long_press", "input_text"):
            x1,y1,x2,y2 = target["hit_bounds"]
            x,y = (x1+x2)//2,(y1+y2)//2
        if action.kind == "tap": self.driver.click(x,y)
        elif action.kind == "long_press": self.driver.long_click(x,y,1.5)
        elif action.kind == "input_text":
            if not target.get("focused"):
                raise RuntimeFault("focus_required", "Tap the field and observe its focus before input")
            self.driver.input_text_on_cursor(action.text)
        elif action.kind == "home": self.driver.go_home()
        elif action.kind == "back": self.driver.go_back()
        elif action.kind == "launch": self.driver.app_start(action.bundle, wait_time=0)
        elif action.kind == "swipe": self.driver.swipe_ext(action.direction, scale=0.5)

    def close(self):
        self.driver.close()
