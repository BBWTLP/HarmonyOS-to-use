from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Target(Contract):
    action_id: str | None = None
    text: str | None = None
    resource_id: str | None = None

    @model_validator(mode="after")
    def one_selector(self):
        if sum(v is not None for v in (self.action_id, self.text, self.resource_id)) != 1:
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
        if self.type != "stable" and "stable_ms" in self.model_fields_set and self.stable_ms != 300:
            raise ValueError("stable_ms applies only to stable")
        return self


class Action(Contract):
    kind: Literal["tap", "long_press", "swipe", "input_text", "back", "home", "launch"]
    target: Target | None = None
    text: str | None = Field(default=None, max_length=4096)
    bundle: str | None = Field(default=None, pattern=r"^[a-zA-Z][a-zA-Z0-9_.]+$")
    direction: Literal["up", "down", "left", "right"] | None = None

    @model_validator(mode="after")
    def valid_arguments(self):
        required = {"tap": "target", "long_press": "target", "input_text": "target", "launch": "bundle", "swipe": "direction"}
        if self.kind in required and getattr(self, required[self.kind]) is None:
            raise ValueError(f"{self.kind} requires {required[self.kind]}")
        if self.kind == "input_text" and self.text is None:
            raise ValueError("input_text requires text")
        allowed = {"tap": {"target"}, "long_press": {"target"}, "input_text": {"target", "text"}, "launch": {"bundle"}, "swipe": {"direction"}, "home": set(), "back": set()}[self.kind]
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
