"""Read-only MCP client smoke for the first Agent-client integration.

This is an MCP client protocol check, not an autonomous Agent benchmark. It
does not dispatch phone actions. FULL observation may wake the current device
through the existing Runtime policy, so execution acknowledgement is explicit.
The report is metadata-only: no UI text, screenshots, device identifiers, or
endpoint credentials are written.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import io
import json
import sys
from pathlib import Path

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from PIL import Image


EXPECTED_TOOLS = {
    "mobile_session", "mobile_observe", "mobile_act", "mobile_burst",
    "mobile_wait", "mobile_history",
}


def _content_types(result):
    return [getattr(item, "type", None) for item in getattr(result, "content", [])]


def _validate_image_content(result):
    checked = 0
    for item in getattr(result, "content", []):
        if getattr(item, "type", None) != "image":
            continue
        raw = base64.b64decode(item.data, validate=True)
        with Image.open(io.BytesIO(raw)) as image:
            image.verify()
        checked += 1
    return checked


def _metadata(result):
    value = getattr(result, "structured_content", None)
    if not isinstance(value, dict):
        raise RuntimeError("structured_content_missing")
    return value


async def smoke(root: Path, python: str, hdc: str):
    params = StdioServerParameters(
        command=python,
        args=["-m", "harmony_runtime.cli", "mcp", "--state-dir", str(root)],
        cwd=str(Path(__file__).resolve().parents[1]),
        env={"HARMONY_HDC": hdc},
    )
    report = {
        "schema_version": 1,
        "transport": "stdio",
        "scope": "MCP client integration smoke; not autonomous planning or M1",
        "actions_issued": False,
        "tool_discovery": False,
        "structured_result": False,
        "images_decoded": 0,
        "cancellation_observed": False,
        "session_paused_after_cancel": False,
        "status_query_not_found": False,
        "status": "not_ready",
    }
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as client:
            await client.initialize()
            tools = await client.list_tools()
            names = {tool.name for tool in tools.tools}
            report["tool_discovery"] = names == EXPECTED_TOOLS
            if not report["tool_discovery"]:
                report["unexpected_tool_count"] = len(names)
                return report

            opened = await client.call_tool("mobile_session", {"operation": "open"})
            opened_data = _metadata(opened)
            sid = opened_data.get("session_id")
            if not isinstance(sid, str) or not sid:
                return report
            try:
                observed = await client.call_tool("mobile_observe", {
                    "session_id": sid, "mode": "FULL", "include_image": True,
                })
                observed_data = _metadata(observed)
                report["structured_result"] = isinstance(observed_data.get("tree"), dict)
                report["images_decoded"] = _validate_image_content(observed)

                status_query = await client.call_tool("mobile_session", {
                    "operation": "action_status", "session_id": sid,
                    "request_id": "agent-client-smoke-read-only",
                })
                status_data = _metadata(status_query)
                report["status_query_not_found"] = status_data.get("status") == "not_found"

                # Cancel an intentionally impossible read-only wait. The
                # server must pause the lease instead of leaving work running.
                wait_task = asyncio.create_task(client.call_tool("mobile_wait", {
                    "session_id": sid,
                    "expected": {"text": "__agent_client_smoke_never_present__"},
                    "timeout_ms": 5000,
                    "poll_ms": 200,
                }))
                await asyncio.sleep(0.25)
                wait_task.cancel()
                try:
                    await wait_task
                except asyncio.CancelledError:
                    report["cancellation_observed"] = True
                await asyncio.sleep(0.25)
                state = await client.call_tool("mobile_session", {
                    "operation": "status", "session_id": sid,
                })
                report["session_paused_after_cancel"] = (
                    _metadata(state).get("status") == "paused"
                )
            finally:
                await client.call_tool("mobile_session", {
                    "operation": "close", "session_id": sid,
                })
    report["status"] = "ok" if all((
        report["tool_discovery"], report["structured_result"],
        report["images_decoded"] > 0, report["cancellation_observed"],
        report["session_paused_after_cancel"], report["status_query_not_found"],
    )) else "not_ready"
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir", default=".runtime/acceptance")
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--hdc", default="F:/DevEco Studio/sdk/default/openharmony/toolchains/hdc.exe")
    parser.add_argument("--execute", action="store_true",
                        help="Acknowledge that FULL observe may wake/unlock the phone")
    parser.add_argument("--report")
    args = parser.parse_args()
    if not args.execute:
        parser.error("agent-client-smoke requires --execute")
    result = asyncio.run(smoke(Path(args.state_dir).resolve(), args.python, args.hdc))
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    print(rendered)
    if args.report:
        Path(args.report).write_text(rendered + "\n", encoding="utf-8")
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
