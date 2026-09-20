from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class VisualRegion(Contract):
    """A visual target proposal. Never a device write; always revalidated first.

    `crop_digest` is the runtime's own content digest of `region`, produced by
    `harmony_runtime.visual.region_digest`. The label is what the visual layer
    matched, kept so the runtime policy can inspect it before dispatching.
    """

    region: tuple[int, int, int, int]
    crop_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    source: Literal["ocr", "image", "vlm"]
    label: str = Field(min_length=1, max_length=512)
    display_width: int = Field(gt=0)
    display_height: int = Field(gt=0)
    rotation: int


class Target(Contract):
    action_id: str | None = None
    text: str | None = None
    resource_id: str | None = None
    # v2 grounded-target handle: issued by the CandidateRegistry, never by a model.
    target_ref: str | None = Field(default=None, pattern=r"^gt_[0-9a-f]{16,64}$")
    observation_id: str | None = None
    local_fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    # v3.2 visual region handle: only valid next to a target_ref, and only after
    # the runtime re-derives the same crop digest from the pre-dispatch image.
    visual: VisualRegion | None = None

    @model_validator(mode="after")
    def one_selector(self):
        legacy = (self.action_id, self.text, self.resource_id)
        if self.target_ref is not None:
            if any(value is not None for value in legacy):
                raise ValueError("Provide either a v1 selector or a v2 target_ref, not both")
            if self.local_fingerprint is None:
                raise ValueError("target_ref requires the grounded local_fingerprint")
            if self.visual is not None and self.visual.crop_digest != self.local_fingerprint:
                raise ValueError("A visual target must carry the same crop digest as its handle")
            return self
        if self.visual is not None:
            raise ValueError("A visual region is only valid together with a target_ref")
        if self.local_fingerprint is not None:
            raise ValueError("local_fingerprint is only valid together with target_ref")
        if sum(v is not None for v in legacy) != 1:
            raise ValueError("Provide exactly one target selector")
        return self


class Expected(Contract):
    text: str | None = None
    bundle: str | None = None
    changed: bool = False

    @model_validator(mode="after")
    def condition(self):
        if not self.text and not self.bundle and not self.changed:
            raise ValueError("At least one observable condition is required")
        return self


class WaitCondition(Contract):
    type: Literal["change", "stable", "text_present", "text_absent", "element_present", "element_absent", "app_changed", "fingerprint_changed"]
    value: str | None = Field(default=None, min_length=1)
    target: Target | None = None
    observation_id: str | None = None
    stable_ms: int = Field(default=300, ge=100, le=10000)

    @model_validator(mode="after")
    def parameters(self):
        text = self.type in ("text_present", "text_absent")
        element = self.type in ("element_present", "element_absent")
        change = self.type in ("change", "fingerprint_changed", "app_changed")
        if text != (self.value is not None):
            raise ValueError("Only text conditions require value")
        if element != (self.target is not None):
            raise ValueError("Only element conditions require target")
        if change != (self.observation_id is not None):
            raise ValueError("Change conditions require a baseline observation_id")
        if self.target and self.target.action_id is not None:
            raise ValueError("Wait target must use text or resource_id")
        if self.target and self.target.target_ref is not None:
            raise ValueError("Wait target must use text or resource_id")
        if self.type != "stable" and "stable_ms" in self.model_fields_set and self.stable_ms != 300:
            raise ValueError("stable_ms applies only to stable")
        return self


class Action(Contract):
    kind: Literal["tap", "long_press", "swipe", "input_text", "replace_text", "back", "home", "launch"]
    target: Target | None = None
    text: str | None = Field(default=None, max_length=4096)
    bundle: str | None = Field(default=None, pattern=r"^[a-zA-Z][a-zA-Z0-9_.]+$")
    direction: Literal["up", "down", "left", "right"] | None = None

    @model_validator(mode="after")
    def valid_arguments(self):
        required = {"tap": "target", "long_press": "target", "input_text": "target", "replace_text": "target", "launch": "bundle", "swipe": "direction"}
        if self.kind in required and getattr(self, required[self.kind]) is None:
            raise ValueError(f"{self.kind} requires {required[self.kind]}")
        if self.kind in ("input_text", "replace_text") and self.text is None:
            raise ValueError(f"{self.kind} requires text")
        allowed = {"tap": {"target"}, "long_press": {"target"}, "input_text": {"target", "text"}, "replace_text": {"target", "text"}, "launch": {"bundle"}, "swipe": {"direction"}, "home": set(), "back": set()}[self.kind]
        for k in ("target", "text", "bundle", "direction"):
            if getattr(self, k) is not None and k not in allowed:
                raise ValueError(f"Unexpected argument {k} for {self.kind}")
        return self


class ActRequest(Contract):
    session_id: str
    request_id: str = Field(min_length=1, max_length=128)
    observation_id: str
    action: Action
    expected: Expected | None = None
    timeout_ms: int = Field(default=5000, ge=100, le=30000)


class BurstStep(Contract):
    action: Action
    expected: Expected
    watch_timeout_ms: int = Field(default=0, ge=0, le=3000)

    @model_validator(mode="after")
    def semantic_target(self):
        if self.watch_timeout_ms and self.action.target is None:
            raise ValueError("Watch requires a semantic action target")
        if self.action.target and self.action.target.action_id is not None:
            raise ValueError("Burst targets must use text or resource_id and are resolved anew for each step")
        if self.action.target and self.action.target.target_ref is not None:
            raise ValueError("Burst targets must use text or resource_id and are resolved anew for each step")
        return self


class BurstRequest(Contract):
    session_id: str
    request_id: str = Field(min_length=1, max_length=128)
    observation_id: str
    steps: list[BurstStep] = Field(min_length=1, max_length=5)
    timeout_ms: int = Field(default=3000, ge=100, le=3000)


class RuntimeFault(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)
