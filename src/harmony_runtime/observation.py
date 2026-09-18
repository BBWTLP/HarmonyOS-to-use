import hashlib
import json
import re
import time
import uuid
from .contracts import RuntimeFault


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def parse_bounds(raw):
    if isinstance(raw, str):
        match = re.fullmatch(r"\s*\[(-?\d+),\s*(-?\d+)\]\s*\[(-?\d+),\s*(-?\d+)\]\s*", raw)
        if not match:
            return None
        values = list(map(int, match.groups()))
    elif isinstance(raw, dict):
        values = [raw.get(key) for key in ("left", "top", "right", "bottom")]
    elif isinstance(raw, (list, tuple)) and len(raw) == 4:
        values = list(raw)
    else:
        return None
    if any(type(value) is not int for value in values):
        return None
    return values if values[2] > values[0] and values[3] > values[1] else None


def catalog(tree, display):
    width, height = display[:2]
    if any(type(value) is not int or value <= 0 for value in (width, height)):
        raise RuntimeFault("device_unavailable", "Display dimensions are invalid")
    entries = []

    def walk(node, inherited_bundle="", inherited_enabled=True):
        a = node.get("attributes", {})
        # An explicitly hidden ancestor hides its subtree. Container bounds
        # alone do not prove clipping, so only intersect with the screen here.
        if str(a.get("visible", "true")).lower() == "false":
            return
        bundle = a.get("bundleName") or inherited_bundle
        enabled = inherited_enabled and str(a.get("enabled", "true")).lower() != "false"
        bounds = parse_bounds(a.get("bounds"))
        text = a.get("text") or a.get("description") or a.get("hint") or ""
        if bounds:
            x1, y1, x2, y2 = bounds
            hit_bounds = [max(0, x1), max(0, y1), min(width, x2), min(height, y2)]
            if hit_bounds[2] > hit_bounds[0] and hit_bounds[3] > hit_bounds[1]:
                if text or a.get("id") or a.get("clickable") in (True, "true") or "Input" in a.get("type", ""):
                    entries.append({"action_id": f"n{len(entries)}", "text": text,
                        "resource_id": a.get("id", ""), "type": a.get("type", ""),
                        "bounds": bounds, "hit_bounds": hit_bounds, "enabled": enabled,
                        "clickable": a.get("clickable") in (True, "true"),
                        "focused": a.get("focused") in (True, "true"),
                        "checked": a.get("checked"), "selected": a.get("selected"), "bundle": bundle})
        for child in node.get("children", []):
            walk(child, bundle, enabled)
    walk(tree)
    return entries


def navigation_tree(tree):
    """Normalize only clock/battery text and numeric slider progress for navigation.

    Shape, geometry, enabled/focus state and all other attributes remain exact.
    This projection is never used for target-dependent taps or text input, nor
    for change/wait verification. It is not a general semantic page identity.
    """
    def visit(node, status_text=False):
        result = dict(node)
        attributes = dict(node.get("attributes", {}))
        status_text = status_text or attributes.get("id") in (
            "ClockStatusView", "BatteryComponent-batteryIcon_Text_batterySoc")
        for field in ("text", "originalText"):
            value = attributes.get(field)
            progress = (attributes.get("type") == "Slider" and isinstance(value, str)
                        and re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value) is not None)
            if field in attributes and (status_text or progress):
                attributes[field] = "<volatile-navigation-text>"
        result["attributes"] = attributes
        if "children" in node:
            result["children"] = [visit(child, status_text) for child in node["children"]]
        return result
    return visit(tree)


def snapshot(tree, display):
    items = catalog(tree, display)
    fingerprint = hashlib.sha256(canonical([tree, display]).encode()).hexdigest()
    return {"observation_id":uuid.uuid4().hex,"captured_at":time.time(), "display":{"width":display[0],"height":display[1],"rotation":display[2]},"fingerprint":fingerprint,"foreground_bundle":None,"catalog":items,
            "navigation_fingerprint": hashlib.sha256(canonical([navigation_tree(tree), display]).encode()).hexdigest()}


def resolve(observation, target):
    candidates = [x for x in observation["catalog"] if x["enabled"] and all(getattr(target,k) is None or getattr(target,k) == x[k] for k in ("action_id","text","resource_id"))]
    if not candidates: raise RuntimeFault("target_not_found", "No enabled target matches")
    if len(candidates) != 1: raise RuntimeFault("target_ambiguous", f"{len(candidates)} targets match; choose a current action_id")
    return candidates[0]


def matches(obs, expected, before=None):
    if expected.text is not None and not any(x["text"] == expected.text for x in obs["catalog"]): return False
    if expected.bundle is not None and obs.get("foreground_bundle") != expected.bundle: return False
    if expected.changed and (before is None or obs["fingerprint"] == before): return False
    return True
