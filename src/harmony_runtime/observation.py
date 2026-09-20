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

    def walk(node, inherited_bundle="", inherited_enabled=True, parent_action_id=None):
        a = node.get("attributes", {})
        # An explicitly hidden ancestor hides its subtree. Container bounds
        # alone do not prove clipping, so only intersect with the screen here.
        if str(a.get("visible", "true")).lower() == "false":
            return
        bundle = a.get("bundleName") or inherited_bundle
        enabled = inherited_enabled and str(a.get("enabled", "true")).lower() != "false"
        bounds = parse_bounds(a.get("bounds"))
        is_input = any(kind in a.get("type", "").lower() for kind in ("input", "textfield", "textarea"))
        text = (a.get("text") if isinstance(a.get("text"), str) else "") if is_input else (a.get("text") or a.get("description") or a.get("hint") or "")
        if bounds:
            x1, y1, x2, y2 = bounds
            hit_bounds = [max(0, x1), max(0, y1), min(width, x2), min(height, y2)]
            if hit_bounds[2] > hit_bounds[0] and hit_bounds[3] > hit_bounds[1]:
                if text or a.get("id") or a.get("clickable") in (True, "true") or is_input:
                    action_id = f"n{len(entries)}"
                    clickable = a.get("clickable") in (True, "true")
                    long_clickable = a.get("longClickable") in (True, "true")
                    node_type = a.get("type", "")
                    entries.append({"action_id": action_id, "text": text,
                        "text_observed": isinstance(a.get("text"), str),
                        "hint": a.get("hint", ""), "description": a.get("description", ""),
                        "accessibility_id": a.get("accessibilityId", ""),
                        "host_window_id": a.get("hostWindowId", ""),
                        "hierarchy": a.get("hierarchy", ""),
                        "resource_id": a.get("id", ""), "type": node_type,
                        "bounds": bounds, "hit_bounds": hit_bounds, "enabled": enabled,
                        "clickable": clickable,
                        "focused": a.get("focused") in (True, "true"),
                        "parent_action_id": parent_action_id,
                        "target_fingerprint": hashlib.sha256(canonical(node).encode()).hexdigest(),
                        "checked": a.get("checked"), "selected": a.get("selected"), "bundle": bundle})
                    # Only an actually actionable node becomes the parent for
                    # descendants. Text/id-only entries must not leak across
                    # siblings and masquerade as an ancestor of later nodes.
                    if clickable or long_clickable or "Input" in node_type:
                        parent_action_id = action_id
        for child in node.get("children", []):
            walk(child, bundle, enabled, parent_action_id)
    walk(tree)
    return entries


def input_value_matches(items, target, value):
    """Exact value on one anchored, visible, focused field; never placeholder text."""
    anchor = next((key for key in ("accessibility_id", "resource_id", "hierarchy") if target.get(key)), None)
    if anchor is None:
        return False
    keys = (anchor, "host_window_id", "bundle", "type", "bounds")
    candidates = [item for item in items if all(item.get(key) == target.get(key) for key in keys)]
    return (len(candidates) == 1 and candidates[0].get("enabled") is True
            and candidates[0].get("focused") is True and candidates[0].get("text_observed") is True
            and candidates[0].get("text") == value)


def navigation_tree(tree):
    """Normalize clock/battery text, numeric progress and decorative image bounds.

    Actionable geometry, shape, enabled/focus state and other attributes remain exact.
    Targeted actions also require an unchanged target subtree. This projection
    is never used for change/wait verification. It is not a general semantic page identity.
    """
    def visit(node, status_text=False):
        result = dict(node)
        attributes = dict(node.get("attributes", {}))
        status_text = status_text or attributes.get("id") in (
            "ClockStatusView", "BatteryComponent-batteryIcon_Text_batterySoc")
        for field in ("text", "originalText"):
            value = attributes.get(field)
            progress = (attributes.get("type") in ("Slider", "Progress") and isinstance(value, str)
                        and re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value) is not None)
            if field in attributes and (status_text or progress):
                attributes[field] = "<volatile-navigation-text>"
        # Feed and video surfaces can continuously animate decorative image
        # bounds while the page and its actionable controls remain unchanged.
        # Keep target geometry in the full fingerprint, but exclude bounds of
        # unlabelled, non-clickable visual nodes from page identity so a
        # navigation action is not rejected mid-animation.
        visual_only = (attributes.get("type") in ("Image", "ImageView")
                       and not any(attributes.get(k) for k in ("id", "text", "description", "hint"))
                       and attributes.get("clickable") not in (True, "true")
                       and attributes.get("longClickable") not in (True, "true"))
        if visual_only:
            for field in ("bounds", "origBounds"):
                if field in attributes:
                    attributes[field] = "<volatile-visual-bounds>"
        result["attributes"] = attributes
        if "children" in node:
            result["children"] = [visit(child, status_text) for child in node["children"]]
        return result
    return visit(tree)


def authentication_blocker(items):
    """Recognize the observed system credential surface, not arbitrary app copy.

    Require the system auth extension, its password title and credential input.
    No credential values or raw UI are included in the diagnostic metadata.
    """
    system = [item for item in items if item.get("bundle") == "com.ohos.sceneboard"]
    extension = any(item.get("type") == "UIExtensionComponent"
                    and "userauthuiextensionability" in item.get("resource_id", "").lower()
                    for item in system)
    title = any(item.get("resource_id") == "NumberPasswordTitleGroup"
                or item.get("text", "").strip() == "请输入隐私密码" for item in system)
    credential = any(item.get("type") == "TextInput"
                     and item.get("resource_id") == "pinSix" for item in system)
    if extension and title and credential:
        return {"code": "authentication_required", "source": "system_authentication_ui",
                "allowed_actions": ["back", "home", "launch"]}
    return None


def snapshot(tree, display, foreground=None):
    items = catalog(tree, display)
    fingerprint = hashlib.sha256(canonical([tree, display, foreground]).encode()).hexdigest()
    return {"observation_id":uuid.uuid4().hex,"captured_at":time.time(), "display":{"width":display[0],"height":display[1],"rotation":display[2]},"fingerprint":fingerprint,"foreground_bundle":foreground.get("bundle") if foreground else None,"foreground_evidence":foreground,"catalog":items,"blocking_dialog":authentication_blocker(items),
            "navigation_fingerprint": hashlib.sha256(canonical([navigation_tree(tree), display, foreground]).encode()).hexdigest()}


def resolve(observation, target):
    if getattr(target, "target_ref", None) is not None:
        # v2: the service registered this grounded target against one observed node.
        # Match the local fingerprint so duplicate labels cannot be confused.
        candidates = [x for x in observation["catalog"]
                      if x.get("target_fingerprint") == target.local_fingerprint]
        if not candidates:
            raise RuntimeFault("target_not_found", "Grounded target is no longer present")
        if len(candidates) != 1:
            raise RuntimeFault("target_ambiguous", "Grounded target fingerprint is not unique")
        if not candidates[0]["enabled"]:
            raise RuntimeFault("target_not_found", "Grounded target is disabled")
        return candidates[0]
    candidates = [x for x in observation["catalog"] if x["enabled"] and all(getattr(target,k) is None or getattr(target,k) == x[k] for k in ("action_id","text","resource_id"))]
    if not candidates: raise RuntimeFault("target_not_found", "No enabled target matches")
    if len(candidates) != 1: raise RuntimeFault("target_ambiguous", f"{len(candidates)} targets match; choose a current action_id")
    return candidates[0]


def matches(obs, expected, before=None):
    if obs.get("actionable") is False or obs.get("blocking_dialog"): return False
    if expected.text is not None and not any(x["text"] == expected.text for x in obs["catalog"]): return False
    if expected.bundle is not None and obs.get("foreground_bundle") != expected.bundle: return False
    if expected.changed and (before is None or obs["fingerprint"] == before): return False
    return True
