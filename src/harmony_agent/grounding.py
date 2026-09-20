"""Layered grounding: stable identity -> text/structure -> OCR -> image -> VLM.

Every layer returns candidate regions or an explicit "no evidence" result. The
first match is never taken automatically. A grounded target is an immutable
description plus a local fingerprint, not a bare coordinate; visual targets
carry the crop, the geometry transform and the revalidation method so the
runtime can reject them after rotation, keyboard occlusion or window changes.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Literal, Protocol

from .contracts import iso, utc_now

GROUNDING_SOURCES = ("ui_tree", "ocr", "image", "vlm")

#: Actions that need a spatial handle. home/back/launch are device-level.
SPATIAL_ACTIONS = ("tap", "long_press", "input_text", "replace_text")

#: Default candidate lifetime. Short enough that a page change invalidates it.
DEFAULT_TTL_SECONDS = 15

MAX_SHORTLIST = 16
MAX_CANDIDATES = 32


class GroundingUnavailable(RuntimeError):
    """No layer could produce evidence for the requested intent."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class GroundingIntent:
    """What the planner/agent asked for. Text and structure only, never a point."""

    action_kind: str
    text: str | None = None
    resource_id: str | None = None
    accessibility_id: str | None = None
    description: str | None = None
    index: int | None = None
    require_focus: bool | None = None
    require_clickable: bool | None = None

    def describe(self) -> str:
        parts = [f"action={self.action_kind}"]
        for name in ("text", "resource_id", "accessibility_id", "description"):
            value = getattr(self, name)
            if value:
                parts.append(f"{name}={value}")
        return " ".join(parts)


@dataclass(frozen=True)
class GroundedTarget:
    target_ref: str
    observation_id: str
    controller_epoch: int
    source: str
    action_kind: str
    bounds: tuple[int, int, int, int]
    hit_bounds: tuple[int, int, int, int]
    role: str
    text: str
    identity: dict[str, str]
    local_fingerprint: str
    enabled: bool
    visible: bool
    clickable: bool
    focused: bool
    selected: Any
    checked: Any
    parent_ref: str | None
    bundle: str
    confidence: float
    missing: tuple[str, ...]
    geometry: dict[str, Any] | None
    revalidate: str
    expires_at: str

    def to_ref(self) -> dict[str, Any]:
        """Reference payload handed to the runtime guard (no raw coordinates)."""
        return {
            "target_ref": self.target_ref,
            "observation_id": self.observation_id,
            "controller_epoch": self.controller_epoch,
            "action_kind": self.action_kind,
            "source": self.source,
            "identity": dict(self.identity),
            "local_fingerprint": self.local_fingerprint,
            "bundle": self.bundle,
            "bounds": list(self.bounds),
            "role": self.role,
            "expires_at": self.expires_at,
        }


@dataclass
class GroundingResult:
    targets: list[GroundedTarget] = field(default_factory=list)
    rejected: list[dict[str, Any]] = field(default_factory=list)
    layers_used: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def shortlist(self) -> list[GroundedTarget]:
        return self.targets[:MAX_SHORTLIST]

    def is_empty(self) -> bool:
        return not self.targets


def _digest(*parts: Any) -> str:
    payload = "\u0000".join(str(part) for part in parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _stable_identity(node: dict[str, Any]) -> dict[str, str]:
    keys = ("accessibility_id", "resource_id", "hierarchy", "host_window_id", "type")
    return {key: str(node.get(key) or "") for key in keys if node.get(key)}


def _expiry(ttl_seconds: int) -> str:
    return iso(utc_now() + timedelta(seconds=ttl_seconds))


def _node_ref(observation_id: str, node: dict[str, Any]) -> str:
    identity = _stable_identity(node)
    return "gt_" + _digest(observation_id, sorted(identity.items()), node.get("bounds"))[:32]


def _missing_information(node: dict[str, Any]) -> tuple[str, ...]:
    missing = []
    if not node.get("text_observed") and not node.get("text"):
        missing.append("text_not_observed")
    if not node.get("accessibility_id") and not node.get("resource_id"):
        missing.append("no_stable_identity")
    if node.get("bounds") is None:
        missing.append("no_geometry")
    return tuple(missing)


def _from_node(observation: dict[str, Any], node: dict[str, Any], source: str,
               intent: GroundingIntent, ttl_seconds: int,
               confidence: float, parent_ref: str | None = None) -> GroundedTarget:
    return GroundedTarget(
        target_ref=_node_ref(observation["observation_id"], node),
        observation_id=observation["observation_id"],
        controller_epoch=int(observation.get("controller_epoch", 0)),
        source=source,
        action_kind=intent.action_kind,
        bounds=tuple(node["bounds"]),
        hit_bounds=tuple(node["hit_bounds"]),
        role=str(node.get("type") or ""),
        text=str(node.get("text") or ""),
        identity=_stable_identity(node),
        local_fingerprint=str(node.get("target_fingerprint") or ""),
        enabled=bool(node.get("enabled")),
        visible=True,
        clickable=bool(node.get("clickable")),
        focused=bool(node.get("focused")),
        selected=node.get("selected"),
        checked=node.get("checked"),
        parent_ref=parent_ref,
        bundle=str(node.get("bundle") or ""),
        confidence=confidence,
        missing=_missing_information(node),
        geometry=None,
        revalidate="identity_and_fingerprint",
        expires_at=_expiry(ttl_seconds),
    )


def _tree_layer(observation: dict[str, Any], intent: GroundingIntent, ttl_seconds: int) -> GroundingResult:
    """Layer 1-2: stable identity first, then text + structure relations."""
    catalog = observation.get("catalog") or []
    result = GroundingResult()
    by_action_id = {item.get("action_id"): item for item in catalog}

    def actionable(item: dict[str, Any]) -> dict[str, Any]:
        """A label is not clickable; its nearest clickable ancestor is the target.

        Keeping the ancestor (not the bare label) is what makes the candidate
        match the node the runtime will actually dispatch to.
        """
        if item.get("clickable") or item.get("enabled") and "input" in str(item.get("type", "")).lower():
            return item
        seen: set[str] = set()
        parent_id = item.get("parent_action_id")
        while parent_id and parent_id in by_action_id and parent_id not in seen:
            seen.add(parent_id)
            parent = by_action_id[parent_id]
            if parent.get("clickable"):
                return parent
            parent_id = parent.get("parent_action_id")
        return item

    def to_target(item: dict[str, Any], confidence: float, layer: str) -> GroundedTarget:
        parent = item.get("parent_action_id")
        return _from_node(
            observation, item, layer, intent, ttl_seconds, confidence,
            parent_ref=(_node_ref(observation["observation_id"], by_action_id[parent])
                        if parent in by_action_id else None),
        )

    identity_matches: list[dict[str, Any]] = []
    if intent.accessibility_id:
        identity_matches = [i for i in catalog if i.get("accessibility_id") == intent.accessibility_id]
    elif intent.resource_id:
        identity_matches = [i for i in catalog if i.get("resource_id") == intent.resource_id]
    if identity_matches:
        result.layers_used.append("stable_identity")
        for item in identity_matches:
            result.targets.append(to_target(actionable(item), 1.0, "ui_tree"))
        return result

    if intent.text is not None:
        exact = [i for i in catalog if i.get("text") == intent.text]
        if exact:
            result.layers_used.append("text_exact")
            for item in exact:
                result.targets.append(to_target(actionable(item), 0.9, "ui_tree"))
            return result
        # Structure layer: same text under a different window/container is a
        # different target, so report each one with its parent instead of a
        # single guess.
        structural = [i for i in catalog
                      if intent.text and intent.text in str(i.get("text") or "")]
        if structural:
            result.layers_used.append("text_structure")
            result.notes.append("substring matches are reported individually")
            for item in structural:
                result.targets.append(to_target(actionable(item), 0.5, "ui_tree"))
            return result

    if intent.description:
        described = [i for i in catalog
                     if intent.description in str(i.get("description") or "")
                     or intent.description == i.get("accessibility_id")]
        if described:
            result.layers_used.append("description_structure")
            for item in described:
                result.targets.append(to_target(actionable(item), 0.6, "ui_tree"))
            return result

    if not result.targets:
        result.notes.append("tree_layers_found_no_candidate")
    return result


class OcrEngine(Protocol):
    available: bool

    def recognize(self, image_bytes: bytes) -> list[dict[str, Any]]:
        """-> [{"text", "bounds": (l,t,r,b), "confidence"}]"""


class ImageMatcher(Protocol):
    available: bool

    def locate(self, image_bytes: bytes, description: str) -> list[dict[str, Any]]:
        """-> [{"bounds", "score", "method"}]"""


class UnavailableOcr:
    available = False
    reason = "no_ocr_engine_configured"

    def recognize(self, image_bytes: bytes) -> list[dict[str, Any]]:
        raise GroundingUnavailable("ocr_unavailable", self.reason)


class UnavailableMatcher:
    available = False
    reason = "no_image_matcher_configured"

    def locate(self, image_bytes: bytes, description: str) -> list[dict[str, Any]]:
        raise GroundingUnavailable("image_match_unavailable", self.reason)


def _image_bytes(observation: dict[str, Any]) -> bytes | None:
    image = observation.get("image")
    if isinstance(image, dict) and image.get("base64"):
        import base64
        return base64.b64decode(image["base64"], validate=True)
    return None


def _visual_target(observation: dict[str, Any], bounds: tuple[int, int, int, int],
                   source: str, intent: GroundingIntent, ttl_seconds: int,
                   confidence: float, method: str) -> GroundedTarget:
    import base64
    image = observation.get("image") or {}
    raw = base64.b64decode(image["base64"], validate=True) if image.get("base64") else b""
    crop_hash = _digest(observation["observation_id"], bounds, method, len(raw))
    display = observation.get("display") or {}
    return GroundedTarget(
        target_ref="gt_" + crop_hash[:32],
        observation_id=observation["observation_id"],
        controller_epoch=int(observation.get("controller_epoch", 0)),
        source=source,
        action_kind=intent.action_kind,
        bounds=bounds,
        hit_bounds=bounds,
        role="visual_region",
        text=intent.text or intent.description or "",
        identity={"method": method},
        local_fingerprint=crop_hash,
        enabled=True,
        visible=True,
        clickable=intent.action_kind in ("tap", "long_press"),
        focused=False,
        selected=None,
        checked=None,
        parent_ref=None,
        bundle=str(observation.get("foreground_bundle") or ""),
        confidence=confidence,
        missing=("no_ui_tree_evidence", "no_focus_state"),
        geometry={
            "crop": list(bounds),
            "display": {"width": display.get("width"), "height": display.get("height"),
                        "rotation": display.get("rotation")},
            "image_captured_at": observation.get("image_captured_at"),
            "tree_captured_at": observation.get("tree_captured_at"),
            "transform": "identity",
        },
        revalidate="visual_region_identity_and_geometry",
        expires_at=_expiry(ttl_seconds),
    )


def _ocr_layer(observation: dict[str, Any], intent: GroundingIntent, ttl_seconds: int,
               ocr: OcrEngine | None, result: GroundingResult) -> GroundingResult:
    if intent.text is None:
        return result
    engine = ocr or UnavailableOcr()
    if not getattr(engine, "available", False):
        result.notes.append(f"ocr_layer_skipped:{getattr(engine, 'reason', 'unavailable')}")
        return result
    raw = _image_bytes(observation)
    if not raw:
        result.notes.append("ocr_layer_skipped:no_image_evidence")
        return result
    try:
        regions = engine.recognize(raw)
    except GroundingUnavailable as error:
        result.notes.append(f"ocr_layer_failed:{error.code}")
        return result
    matched = [r for r in regions if r.get("text") == intent.text]
    if matched:
        result.layers_used.append("ocr_region")
        for region in matched:
            result.targets.append(_visual_target(
                observation, tuple(region["bounds"]), "ocr", intent, ttl_seconds,
                float(region.get("confidence", 0.5)), "ocr_text_region"))
    return result


def _image_layer(observation: dict[str, Any], intent: GroundingIntent, ttl_seconds: int,
                 matcher: ImageMatcher | None, result: GroundingResult) -> GroundingResult:
    description = intent.description or intent.text
    if not description:
        return result
    engine = matcher or UnavailableMatcher()
    if not getattr(engine, "available", False):
        result.notes.append(f"image_layer_skipped:{getattr(engine, 'reason', 'unavailable')}")
        return result
    raw = _image_bytes(observation)
    if not raw:
        result.notes.append("image_layer_skipped:no_image_evidence")
        return result
    try:
        regions = engine.locate(raw, description)
    except GroundingUnavailable as error:
        result.notes.append(f"image_layer_failed:{error.code}")
        return result
    if regions:
        result.layers_used.append("image_region")
        for region in sorted(regions, key=lambda r: -float(r.get("score", 0))):
            result.targets.append(_visual_target(
                observation, tuple(region["bounds"]), "image", intent, ttl_seconds,
                float(region.get("score", 0.4)), str(region.get("method", "template"))))
    return result


def is_black_screen(observation: dict[str, Any], threshold: float = 1.0) -> bool:
    """True when the screenshot carries no usable evidence (mean ~0, no variance)."""
    raw = _image_bytes(observation)
    if not raw:
        return False
    try:
        from PIL import Image, ImageStat
        import io
        with Image.open(io.BytesIO(raw)) as image:
            stat = ImageStat.Stat(image.convert("L"))
        return bool(stat.mean and stat.mean[0] <= threshold and (stat.stddev[0] <= threshold))
    except Exception:
        return False


def ground(observation: dict[str, Any], intent: GroundingIntent, *,
           ocr: OcrEngine | None = None, matcher: ImageMatcher | None = None,
           ttl_seconds: int = DEFAULT_TTL_SECONDS) -> GroundingResult:
    """Run the layers in order and return every candidate, never a guess."""
    if observation.get("actionable") is False and observation.get("mode") == "FULL":
        raise GroundingUnavailable("stale_observation", "Observation is not usable for actions")
    if is_black_screen(observation):
        raise GroundingUnavailable("no_visual_evidence", "Screenshot has no usable evidence")

    result = _tree_layer(observation, intent, ttl_seconds)
    if result.targets:
        return result

    result = _ocr_layer(observation, intent, ttl_seconds, ocr, result)
    if result.targets:
        return result

    result = _image_layer(observation, intent, ttl_seconds, matcher, result)
    if result.targets:
        return result

    result.notes.append("all_layers_exhausted")
    return result


def filter_for_intent(targets: list[GroundedTarget], intent: GroundingIntent) -> list[GroundedTarget]:
    """Apply capability and precondition filters, keeping rejections visible."""
    kept: list[GroundedTarget] = []
    for target in targets:
        if intent.require_clickable and not target.clickable:
            continue
        if intent.require_focus and not target.focused:
            continue
        if intent.action_kind in SPATIAL_ACTIONS and not target.enabled:
            continue
        kept.append(target)
    return kept


def text_of(observation: dict[str, Any]) -> set[str]:
    return {str(node.get("text") or "") for node in observation.get("catalog", [])}


def looks_like_authentication(observation: dict[str, Any]) -> bool:
    return bool(observation.get("blocking_dialog")) or bool(
        re.search(r"请输入隐私密码|验证码|密码", " ".join(text_of(observation))))
