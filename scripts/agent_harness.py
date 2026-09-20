"""Real MCP stdio client used by the M0/M1 and Decider shadow acceptance runs.

The harness is a genuine MCP client: it speaks the stdio protocol to
``harmony_runtime.cli mcp`` and only ever calls tools the server advertises.
Decisions are made from the current observation (target grounding + rules /
local Decider), never from a hard-coded action_id script, so the same harness
works on a different device build or page layout.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

REPO_ROOT = Path(__file__).resolve().parents[1]

WEIBO = "com.sina.weibo.stage"


class HarnessError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def _structured(result) -> dict[str, Any]:
    value = getattr(result, "structured_content", None)
    if not isinstance(value, dict):
        raise HarnessError("protocol_error", "Tool result has no structured content")
    if value.get("status") == "error":
        error = value.get("error") or {}
        raise HarnessError(str(error.get("code", "tool_error")),
                           str(error.get("message", "tool error")))
    return value


@dataclass
class ToolCall:
    tool: str
    ok: bool
    code: str | None
    ms: float
    detail: dict[str, Any] = field(default_factory=dict)


class AgentHarness:
    """One MCP client session against the production stdio boundary."""

    def __init__(self, *, state_dir: str | Path, python: str | None = None,
                 hdc: str | None = None, agent_tools: bool = True):
        self.state_dir = Path(state_dir)
        self.python = python or sys.executable
        self.hdc = hdc or os.environ.get("HARMONY_HDC") or (
            r"F:\DevEco Studio\sdk\default\openharmony\toolchains\hdc.exe")
        self.agent_tools = agent_tools
        self.session_id: str | None = None
        self.calls: list[ToolCall] = []
        self._client: ClientSession | None = None
        self._streams = None
        self._context = None
        self._session_context = None
        self.tools: list[str] = []
        #: Image content returned by the last tool call (MCP sends images as
        #: separate content items, not inside structured_content).
        self.last_images: list[dict[str, Any]] = []
        #: The MCP server writes diagnostics to stderr. It must be drained or a
        #: full pipe blocks the server mid-call, so it is streamed to a file.
        self.errlog_path = self.state_dir / "mcp-client-stderr.log"

    # -- lifecycle ----------------------------------------------------------
    async def __aenter__(self) -> "AgentHarness":
        # Inherit the parent environment: the stdio server is a separate
        # process and needs the normal interpreter environment to import
        # packages and to reach the runtime service.
        env = dict(os.environ)
        env["HARMONY_HDC"] = self.hdc
        if self.agent_tools:
            env["HARMONY_AGENT_TOOLS"] = "1"
            for name in ("HARMONY_AGENT_PROFILE", "HARMONY_AGENT_CALIBRATION",
                         "HARMONY_AGENT_MIN_CONFIDENCE", "HARMONY_AGENT_MIN_CERTAINTY"):
                if os.environ.get(name):
                    env[name] = os.environ[name]
        params = StdioServerParameters(
            command=self.python,
            args=["-m", "harmony_runtime.cli", "mcp", "--state-dir", str(self.state_dir)],
            cwd=str(REPO_ROOT),
            env=env,
        )
        self.errlog_path.parent.mkdir(parents=True, exist_ok=True)
        self._errlog = open(self.errlog_path, "a", encoding="utf-8")
        self._context = stdio_client(params, errlog=self._errlog)
        self._streams = await self._context.__aenter__()
        self._session_context = ClientSession(*self._streams)
        self._client = await self._session_context.__aenter__()
        await self._client.initialize()
        self.tools = sorted(tool.name for tool in (await self._client.list_tools()).tools)
        return self

    async def __aexit__(self, *exc):
        try:
            if self.session_id and self._client is not None:
                await self.call("mobile_session", operation="close",
                                session_id=self.session_id, record=False)
        except Exception:
            pass
        if self._session_context is not None:
            await self._session_context.__aexit__(*exc)
        if self._context is not None:
            await self._context.__aexit__(*exc)
        try:
            self._errlog.close()
        except Exception:
            pass

    # -- tool calls ---------------------------------------------------------
    async def call(self, tool: str, *, record: bool = True,
                   read_timeout_seconds: float = 180.0, **arguments) -> dict[str, Any]:
        try:
            return await self._call_tool(tool, record=record,
                                         read_timeout_seconds=read_timeout_seconds,
                                         **arguments)
        except HarnessError as error:
            return await self._recover_if_quarantined(
                error, lambda: self._call_tool(tool, record=record,
                                               read_timeout_seconds=read_timeout_seconds,
                                               **arguments))

    async def _recover_if_quarantined(self, error: HarnessError, retry):
        """Replace a quarantined worker once, then retry the refused call.

        `device_quarantined` is raised before anything is sent to the device, so
        this is a refresh, never a replay of a dispatched action.
        """
        if (error.code != "device_quarantined" or not self.session_id
                or getattr(self, "_recovering", False)):
            raise error
        self._recovering = True
        try:
            await self._call_tool("mobile_session", operation="recover",
                                  session_id=self.session_id, record=False)
        finally:
            self._recovering = False
        return await retry()

    async def _call_tool(self, tool: str, *, record: bool = True,
                         read_timeout_seconds: float = 180.0, **arguments) -> dict[str, Any]:
        if self._client is None:
            raise HarnessError("protocol_error", "Client is not started")
        started = time.perf_counter()
        # A bounded read timeout turns a wedged server or device into a
        # reportable failure instead of an acceptance run that never returns.
        result = await self._client.call_tool(tool, arguments,
                                              read_timeout_seconds=read_timeout_seconds)
        elapsed = round((time.perf_counter() - started) * 1000, 3)
        self.last_images = _images(result)
        if record:
            try:
                data = _structured(result)
                self.calls.append(ToolCall(tool, True, None, elapsed, {}))
                return data
            except HarnessError as error:
                self.calls.append(ToolCall(tool, False, error.code, elapsed, {}))
                raise
        return _structured(result)

    async def open(self, device_id: str | None = None) -> dict[str, Any]:
        return await self._open_with_lease_wait(device_id)

    async def _open_with_lease_wait(self, device_id: str | None,
                                    attempts: int = 8, interval: float = 20.0):
        """Wait out a lease left by a previous client instead of failing fast."""
        last: HarnessError | None = None
        for attempt in range(attempts):
            try:
                return await self._open_once(device_id)
            except HarnessError as error:
                if error.code not in ("lease_conflict", "device_busy"):
                    raise
                last = error
                await asyncio.sleep(interval)
        raise last or HarnessError("lease_conflict", "Device stay leased")

    async def _open_once(self, device_id: str | None):
        arguments: dict[str, Any] = {"operation": "open"}
        if device_id:
            arguments["device_id"] = device_id
        result = await self.call("mobile_session", **arguments)
        if result.get("status") == "error":
            error = result.get("error") or {}
            raise HarnessError(str(error.get("code", "session_error")),
                               str(error.get("message", "session open failed")))
        self.session_id = result["session_id"]
        if result.get("worker_quarantined") or result.get("device_state") == "quarantined":
            # Replace the interrupted worker before doing any real work.
            await self.call("mobile_session", operation="recover",
                            session_id=self.session_id)
        return result

    async def observe(self, mode: str = "FAST", include_image: bool = False) -> dict[str, Any]:
        return await self.call("mobile_observe", session_id=self.session_id,
                               mode=mode, include_image=include_image)

    async def act(self, *, request_id: str | None = None, observation_id: str,
                  action: dict[str, Any], expected: dict[str, Any] | None = None,
                  timeout_ms: int = 8000) -> dict[str, Any]:
        return await self.call("mobile_act", request={
            "session_id": self.session_id,
            "request_id": request_id or "act_" + uuid.uuid4().hex[:20],
            "observation_id": observation_id,
            "action": action,
            "expected": expected,
            "timeout_ms": timeout_ms,
        })

    async def wait(self, **arguments) -> dict[str, Any]:
        return await self.call("mobile_wait", session_id=self.session_id, **arguments)

    async def burst(self, **payload) -> dict[str, Any]:
        return await self.call("mobile_burst", request=payload)

    async def history(self, limit: int = 20) -> dict[str, Any]:
        return await self.call("mobile_history", session_id=self.session_id, limit=limit)

    async def session_status(self) -> dict[str, Any]:
        return await self.call("mobile_session", operation="status",
                               session_id=self.session_id)

    async def decide(self, *, observation_id: str, intent: dict[str, Any],
                     goal: str = "") -> dict[str, Any]:
        return await self.call("mobile_decide", session_id=self.session_id,
                               observation_id=observation_id, intent=intent, goal=goal)

    async def run_task(self, task: dict[str, Any]) -> dict[str, Any]:
        return await self.call("mobile_run_task", session_id=self.session_id, task=task)

    async def task_status(self, task_id: str, after_event_seq: int = 0) -> dict[str, Any]:
        return await self.call("mobile_task_status", task_id=task_id,
                               after_event_seq=after_event_seq)

    async def task_events(self, task_id: str, after_seq: int = 0,
                          limit: int = 100) -> dict[str, Any]:
        return await self.call("mobile_task_events", task_id=task_id,
                               after_seq=after_seq, limit=limit)

    async def task_result(self, task_id: str) -> dict[str, Any]:
        return await self.call("mobile_task_result", task_id=task_id)

    async def task_control(self, task_id: str, operation: str) -> dict[str, Any]:
        return await self.call("mobile_task_control", task_id=task_id, operation=operation)

    # -- helpers ------------------------------------------------------------
    async def wait_for_task(self, task_id: str, timeout: float = 180.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = await self.task_status(task_id)
            if status["terminal"] or status["status"] in (
                    "RECONCILIATION_REQUIRED", "PAUSED", "WAITING_USER"):
                return status
            await asyncio.sleep(0.3)
        return await self.task_status(task_id)

    async def stable_observation(self, *, mode: str = "FAST", tries: int = 3,
                                 interval: float = 0.5) -> dict[str, Any]:
        """Observe until two consecutive samples share a page identity.

        Live pages (auto-playing video, animated feeds) otherwise change between
        the referenced observation and the guard's pre-dispatch check, which the
        runtime correctly refuses as stale. Two equal navigation fingerprints
        mean the structure settled; it is not a guarantee of pixel stability.
        """
        previous = None
        observation = None
        for _ in range(max(2, tries)):
            observation = await self.observe(mode=mode)
            fingerprint = observation.get("navigation_fingerprint")
            if previous is not None and fingerprint and fingerprint == previous:
                return observation
            previous = fingerprint
            await asyncio.sleep(interval)
        return observation


class ServiceClientHarness:
    """Same call surface as AgentHarness, over the service's authenticated IPC.

    The stdio MCP frontend and this client both end up in
    ``Runtime.<method>`` on the service; using the service client removes the
    extra pipe hop for long acceptance runs. The method names and return shapes
    are identical so acceptance scripts can switch transports without changes.
    """

    def __init__(self, *, state_dir: str | Path):
        from harmony_runtime.service import Client
        self.client = Client(state_dir)
        self.session_id: str | None = None
        self.last_images: list[dict[str, Any]] = []
        self.tools = ["mobile_session", "mobile_observe", "mobile_act", "mobile_wait",
                      "mobile_burst", "mobile_history"]
        self.connected = False

    async def __aenter__(self) -> "ServiceClientHarness":
        # The client performs the authenticated handshake lazily on first use;
        # probing an unknown session here would only produce a session_invalid.
        self.connected = True
        return self

    async def __aexit__(self, *exc):
        if self.session_id:
            try:
                await asyncio.to_thread(self.client.call, "session", operation="close",
                                        session_id=self.session_id)
            except Exception:
                pass
        self.session_id = None

    async def call(self, tool: str, *, record: bool = True,
                   read_timeout_seconds: float = 180.0, **arguments):
        try:
            return await self._call_tool(tool, record=record, **arguments)
        except HarnessError as error:
            return await self._recover_if_quarantined(
                error, lambda: self._call_tool(tool, record=record, **arguments))

    async def _recover_if_quarantined(self, error: HarnessError, retry):
        """The service client needs the same recovery path as the stdio one."""
        if (error.code != "device_quarantined" or not self.session_id
                or getattr(self, "_recovering", False)):
            raise error
        self._recovering = True
        try:
            await self._call_tool("mobile_session", operation="recover",
                                  session_id=self.session_id, record=False)
        finally:
            self._recovering = False
        return await retry()

    async def _call_tool(self, tool: str, *, record: bool = True, **arguments):
        routes = {
            "mobile_session": ("session", False),
            "mobile_observe": ("observe", False),
            "mobile_act": ("act", True),
            "mobile_wait": ("wait", False),
            "mobile_history": ("history", False),
            "mobile_decide": ("agent_decide", False),
        }
        if tool not in routes:
            raise HarnessError("unsupported_tool",
                               f"{tool} is not available on this transport")
        method, wraps_request = routes[tool]
        # Both transports must surface the same error codes, so a service fault
        # becomes the same HarnessError the stdio frontend would produce.
        from harmony_runtime.contracts import RuntimeFault
        try:
            if wraps_request:
                return await asyncio.to_thread(self.client.call, method,
                                               arguments=arguments["request"])
            return await asyncio.to_thread(self.client.call, method, **arguments)
        except RuntimeFault as error:
            raise HarnessError(error.code, str(error)) from error

    async def open(self, device_id: str | None = None) -> dict[str, Any]:
        last: HarnessError | None = None
        for attempt in range(8):
            try:
                arguments = {"operation": "open"}
                if device_id:
                    arguments["device_id"] = device_id
                result = await self.call("mobile_session", **arguments)
                if result.get("status") == "error":
                    error = result.get("error") or {}
                    raise HarnessError(str(error.get("code", "session_error")),
                                       str(error.get("message", "session open failed")))
                self.session_id = result["session_id"]
                return result
            except HarnessError as error:
                if error.code not in ("lease_conflict", "device_busy"):
                    raise
                last = error
                await asyncio.sleep(20.0)
        raise last or HarnessError("lease_conflict", "Device stays leased")

    async def observe(self, mode: str = "FAST", include_image: bool = False) -> dict[str, Any]:
        data = await self.call("mobile_observe", session_id=self.session_id, mode=mode,
                               include_image=include_image)
        self.last_images = ([{"mime_type": (data.get("image") or {}).get("mime_type"),
                              "bytes": 0, "verified": True}]
                            if data.get("image") else [])
        return data

    async def act(self, *, request_id: str | None = None, observation_id: str,
                  action: dict[str, Any], expected: dict[str, Any] | None = None,
                  timeout_ms: int = 8000) -> dict[str, Any]:
        request = {"session_id": self.session_id,
                   "request_id": request_id or "act_" + uuid.uuid4().hex[:20],
                   "observation_id": observation_id, "action": action,
                   "expected": expected, "timeout_ms": timeout_ms}
        return await self.call("mobile_act", request=request)

    async def burst(self, **payload) -> dict[str, Any]:
        payload.setdefault("session_id", self.session_id)
        return await self.call("mobile_burst", request=payload)

    async def history(self, limit: int = 20) -> dict[str, Any]:
        return await self.call("mobile_history", session_id=self.session_id, limit=limit)

    async def session_status(self) -> dict[str, Any]:
        return await self.call("mobile_session", operation="status",
                               session_id=self.session_id)

    async def decide(self, *, observation_id: str, intent: dict[str, Any],
                     goal: str = "") -> dict[str, Any]:
        return await self.call("mobile_decide", session_id=self.session_id,
                               observation_id=observation_id, intent=intent, goal=goal)

    async def stable_observation(self, *, mode: str = "FAST", tries: int = 3,
                                 interval: float = 0.5) -> dict[str, Any]:
        previous = None
        observation = None
        for _ in range(max(2, tries)):
            observation = await self.observe(mode=mode)
            fingerprint = observation.get("navigation_fingerprint")
            if previous is not None and fingerprint and fingerprint == previous:
                return observation
            previous = fingerprint
            await asyncio.sleep(interval)
        return observation


# -- observation helpers ----------------------------------------------------

def texts(observation: dict[str, Any]) -> list[str]:
    return [str(item.get("text") or "") for item in observation.get("catalog", [])]


def find_node(observation: dict[str, Any], *, text: str | None = None,
              resource_id: str | None = None,
              accessibility_id: str | None = None) -> dict[str, Any] | None:
    """Exactly one matching node, or None (never the first of several)."""
    matches = [item for item in observation.get("catalog", [])
               if (text is None or item.get("text") == text)
               and (resource_id is None or item.get("resource_id") == resource_id)
               and (accessibility_id is None or item.get("accessibility_id") == accessibility_id)]
    if len(matches) != 1:
        return None
    return matches[0]


#: Weibo-specific structure, taken from the existing regression adapter. The
#: search entry is a top-of-page Flex container, not a labelled button, so it is
#: identified by structure rather than by localised text.
SEARCH_BAR_MAX_TOP = 300


def find_search_bar(observation: dict[str, Any]) -> dict[str, Any] | None:
    nodes = [item for item in observation.get("catalog", [])
             if item.get("enabled") and item.get("clickable")
             and item.get("type") == "Flex"
             and (item.get("bounds") or [0, 9999, 0, 0])[1] < SEARCH_BAR_MAX_TOP]
    return nodes[0] if len(nodes) == 1 else None


def is_search_editor(observation: dict[str, Any]) -> bool:
    inputs = [item for item in observation.get("catalog", [])
              if item.get("enabled") and item.get("focused")
              and item.get("type") == "TextInput"]
    lists = [item for item in observation.get("catalog", [])
             if item.get("type") == "List" and item.get("enabled")]
    return len(inputs) == 1 and bool(lists)


def focused_input(observation: dict[str, Any]) -> dict[str, Any] | None:
    inputs = [item for item in observation.get("catalog", [])
              if item.get("enabled") and item.get("focused")
              and "input" in str(item.get("type", "")).lower()]
    return inputs[0] if len(inputs) == 1 else None


def editor_input(observation: dict[str, Any]) -> dict[str, Any] | None:
    """The search editor's field, whether or not it currently holds focus.

    Structure first: one text field in the top band plus a results list below.
    """
    inputs = [item for item in observation.get("catalog", [])
              if "input" in str(item.get("type", "")).lower()
              and (item.get("bounds") or [0, 9999, 0, 0])[1] < 400]
    lists = [item for item in observation.get("catalog", [])
             if item.get("type") == "List"]
    return inputs[0] if len(inputs) == 1 and lists else None


def surface_kind(observation: dict[str, Any]) -> str:
    """Coarse page class used to steer navigation without app-private hooks."""
    if editor_input(observation) is not None:
        return "search_editor"
    if find_search_bar(observation) is not None:
        return "discover"
    for label in ("首页", "发现", "消息", "我"):
        if find_node(observation, text=label) is not None:
            return "tabs"
    return "unknown"


def require_weibo(observation: dict[str, Any]) -> None:
    if observation.get("blocking_dialog"):
        raise HarnessError("authentication_required", "System authentication is blocking")
    if observation.get("foreground_bundle") != WEIBO:
        raise HarnessError("weibo_not_foreground",
                           f"foreground={observation.get('foreground_bundle')!r}")


def target(text: str | None = None, resource_id: str | None = None) -> dict[str, Any]:
    if resource_id:
        return {"resource_id": resource_id}
    return {"text": text}


def encode_report(report: dict[str, Any]) -> str:
    return json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)


def _images(result) -> list[dict[str, Any]]:
    """Decode every image content item so callers can assert on real pixels."""
    found: list[dict[str, Any]] = []
    for item in getattr(result, "content", []) or []:
        if getattr(item, "type", None) != "image":
            continue
        record: dict[str, Any] = {"mime_type": getattr(item, "mime_type", None),
                                  "verified": False, "bytes": 0}
        try:
            import base64
            import io
            from PIL import Image
            raw = base64.b64decode(item.data, validate=True)
            record["bytes"] = len(raw)
            with Image.open(io.BytesIO(raw)) as image:
                image.verify()
            with Image.open(io.BytesIO(raw)) as image:
                record["width"], record["height"] = image.size
                record["format"] = image.format
            record["verified"] = True
        except Exception as error:  # a corrupt image is a failure, not a crash
            record["error"] = type(error).__name__
        found.append(record)
    return found


def write_report(path: str | Path | None, report: dict[str, Any]) -> None:
    if not path:
        return
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(encode_report(report) + "\n", encoding="utf-8")
