"""Deterministic mock device and runtime for offline practice (P3-01).

Practice waves must be able to exercise the real safety path — Runtime guard,
epoch checks, journal, candidate registry, checker — without a phone. This module
provides a scripted driver that the real :class:`harmony_runtime.runtime.Runtime`
can own, plus a small page graph so a practice task has a stable page semantics.

The mock is a test double, not evidence: a wave that ran only on this adapter can
never be reported as device-verified.
"""
from __future__ import annotations

import copy
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

MOCK_APP = "com.example.practice"


def node(**attributes: Any) -> dict[str, Any]:
    attributes.setdefault("bundleName", MOCK_APP)
    attributes.setdefault("visible", "true")
    return {"attributes": attributes, "children": []}


def tree(children: list[dict[str, Any]]) -> dict[str, Any]:
    return {"attributes": {"bundleName": MOCK_APP, "visible": "true",
                           "bounds": "[0,0][1080,2340]", "type": "Root"},
            "children": children}


def home_page(query: str = "") -> dict[str, Any]:
    return tree([
        node(type="Button", text="打开设置", bounds="[40,300][1040,420]",
             clickable="true", id="open_settings"),
        node(type="Button", text="搜索", bounds="[40,460][1040,580]",
             clickable="true", id="search_entry"),
        node(type="Text", text="首页推荐", bounds="[40,620][800,760]"),
    ])


def search_page(query: str = "") -> dict[str, Any]:
    return tree([
        node(type="TextInput", text=query, hint="搜索", bounds="[40,120][820,240]",
             clickable="true", focused="true", id="search_input"),
        node(type="Button", text="搜索", bounds="[840,120][1040,240]",
             clickable="true", id="search_confirm_btn"),
        node(type="Button", text="取消", bounds="[840,280][1040,400]",
             clickable="true", id="search_cancel"),
        node(type="Text", text="热搜榜", bounds="[40,620][600,720]"),
    ])


def results_page(query: str = "") -> dict[str, Any]:
    return tree([
        node(type="TextInput", text=query, bounds="[40,120][820,240]",
             clickable="true", id="search_input"),
        node(type="Text", text=f"{query} 的相关结果", bounds="[40,420][1000,600]"),
        node(type="Text", text="返回", bounds="[40,660][200,760]", clickable="true",
             id="result_back"),
    ])


def settings_page(query: str = "") -> dict[str, Any]:
    return tree([
        node(type="Button", text="关于手机", bounds="[40,300][1040,420]",
             clickable="true", id="about_entry"),
        node(type="Text", text="设置", bounds="[40,160][400,260]"),
    ])


def about_page(query: str = "") -> dict[str, Any]:
    return tree([
        node(type="Text", text="版本信息", bounds="[40,300][1040,420]"),
        node(type="Button", text="返回", bounds="[40,520][200,620]", clickable="true",
             id="about_back"),
    ])


PAGES: dict[str, Callable[[str], dict[str, Any]]] = {
    "home": home_page,
    "search": search_page,
    "results": results_page,
    "settings": settings_page,
    "about": about_page,
}

#: (page, action kind, identity) -> next page. Identity is the resource id when
#: the step names ``id:``, otherwise the exact text of the target.
DEFAULT_FLOW: dict[tuple[str, str, str], str] = {
    ("home", "tap", "id:open_settings"): "settings",
    ("home", "tap", "搜索"): "search",
    ("search", "tap", "id:search_confirm_btn"): "results",
    ("results", "tap", "id:result_back"): "search",
    ("results", "back", ""): "search",
    ("about", "tap", "id:about_back"): "settings",
    ("about", "back", ""): "settings",
    ("settings", "back", ""): "home",
}

#: Pages that ignore a tap once, so a practice task can produce a fragile pass
#: (verified, but only after a retry) instead of a clean one.
NOOP_TAP_PAGES = ("home",)


@dataclass
class FaultProfile:
    """Deterministic faults a practice branch may inject.

    * ``unknown_writes``: the next N dispatches lose their response -> the runtime
      records an unknown execution and the journal blocks further writes.
    * ``noop_taps``: the next N taps produce no page change -> the runner has to
      re-observe and retry, which is the "fragile success" practice trigger.
    * ``black_screen``: the mock reports a locked screen, so the runtime refuses
      before any dispatch.
    """

    unknown_writes: int = 0
    noop_taps: int = 0
    black_screen: bool = False


class MockDevice:
    """A deterministic driver the real Runtime can own."""

    supports_foreground = True

    def __init__(self, serial: str = "mock-device", *,
                 flow: dict[tuple[str, str, str], str] | None = None,
                 start: str = "home", faults: FaultProfile | None = None,
                 query: str = ""):
        self.serial = serial
        self.flow = dict(flow or DEFAULT_FLOW)
        self.page = start
        self.query = query
        self.faults = faults or FaultProfile()
        self.writes: list[dict[str, Any]] = []
        self.closed = False
        self._lock = threading.RLock()

    # -- read path ----------------------------------------------------------
    def screen_state(self) -> dict[str, Any]:
        if self.faults.black_screen:
            return {"screen_on": False, "screen_locked": True}
        return {"screen_on": True, "screen_locked": False}

    def foreground(self) -> dict[str, Any]:
        return {"status": "ok", "bundle": MOCK_APP}

    def display(self) -> tuple[int, int, int]:
        return (1080, 2340, 0)

    def tree(self) -> dict[str, Any]:
        with self._lock:
            page = PAGES.get(self.page, home_page)
            return copy.deepcopy(page(self.query))

    def screenshot(self):
        from PIL import Image
        colour = (0, 0, 0) if self.faults.black_screen else (32, 32, 32)
        return Image.new("RGB", (1080, 2340), colour)

    # -- write path ---------------------------------------------------------
    def dispatch(self, action, target) -> None:
        with self._lock:
            if self.faults.unknown_writes > 0:
                self.faults.unknown_writes -= 1
                self.writes.append({"kind": action.kind, "lost": True})
                raise OSError("mock link lost after dispatch")
            kind = action.kind
            resource_id = (target or {}).get("resource_id")
            text = (target or {}).get("text")
            self.writes.append({"kind": kind, "resource_id": resource_id,
                                "text": text,
                                "input": getattr(action, "text", None)})
            if kind in ("input_text", "replace_text"):
                self.query = getattr(action, "text", "") or ""
                return
            if kind == "tap" and self.faults.noop_taps > 0:
                self.faults.noop_taps -= 1
                return
            nxt = None
            for identity in ([f"id:{resource_id}"] if resource_id else []) + \
                    ([text] if text else []):
                nxt = self.flow.get((self.page, kind, identity))
                if nxt:
                    break
            if nxt is None and kind == "back":
                nxt = self.flow.get((self.page, "back", ""))
            if nxt:
                self.page = nxt

    def close(self) -> None:
        self.closed = True


def mock_device_factory(faults: FaultProfile | None = None,
                        flow: dict[tuple[str, str, str], str] | None = None):
    """Build a Runtime factory that hands out one independent mock device.

    Faults are consumed by the first device that raises them, and each branch of a
    wave gets its own factory so one branch's injected fault cannot leak into
    another branch's run.
    """
    created: list[MockDevice] = []
    remaining = faults or FaultProfile()

    def factory(serial: str) -> MockDevice:
        device = MockDevice(serial, flow=flow,
                            faults=FaultProfile(unknown_writes=remaining.unknown_writes,
                                                noop_taps=remaining.noop_taps,
                                                black_screen=remaining.black_screen))
        created.append(device)
        return device

    factory.supports_foreground = True  # type: ignore[attr-defined]
    factory.created = created  # type: ignore[attr-defined]
    return factory


@dataclass
class PracticeRuntime:
    """A mock runtime plus the session an offline practice task runs against."""

    runtime: Any
    host: Any
    session_id: str
    root: Path
    devices: list[MockDevice] = field(default_factory=list)

    def close(self) -> None:
        try:
            self.host.close()
        finally:
            self.runtime.close()


def open_practice_runtime(root: str | Path, *, owner: str = "practice",
                          faults: FaultProfile | None = None,
                          frozen_memory: Any = None,
                          verifier: Any = None,
                          flow: dict[tuple[str, str, str], str] | None = None,
                          profile: str = "local_shadow") -> PracticeRuntime:
    """Open a real Runtime + AgentHost against the mock device.

    Everything except the driver is the production path: guard, journal, epoch,
    candidates, router, supervisor and checker are the same code an online task
    uses, so an offline practice wave is a genuine dry run of the safety chain.
    """
    from harmony_runtime.runtime import Runtime

    from .host import AgentHost

    root = Path(root)
    factory = mock_device_factory(faults, flow=flow)
    runtime = Runtime(root / "runtime", factory=factory, discover=lambda: ["mock-device"])
    session = runtime.session(owner, "open", device_id="mock-device")
    host = AgentHost(runtime, root / "agent", profile=profile,
                     repo_root=root, frozen_memory=frozen_memory, verifier=verifier)
    return PracticeRuntime(runtime=runtime, host=host, session_id=session["session_id"],
                           root=root, devices=factory.created)  # type: ignore[attr-defined]
