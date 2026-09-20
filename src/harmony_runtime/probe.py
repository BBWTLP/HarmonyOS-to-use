"""Explicit read-only probe through the same stdio MCP path as an agent."""
import asyncio
import re
import sys
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from pathlib import Path


def failure(code):
    return {"status": "not_ready", "error_code": code,
            "phone_observation_verified": False, "phone_write_verified": False}


def summarize(result):
    data = result.structured_content or {}
    if result.is_error:
        code = data.get("error", {}).get("code", "mcp_error")
        if not isinstance(code, str) or not re.fullmatch(r"[a-z_]{1,64}", code):
            code = "mcp_error"
        return failure(code)
    types = [item.type for item in result.content]
    consistent = data.get("image_tree_consistent") is True
    verified = data.get("status") == "ok" and consistent and "image" in types
    return {"status": "ok" if verified else "not_ready",
            "phone_observation_verified": verified, "phone_write_verified": False,
            "catalog_count": len(data.get("catalog", [])),
            "capture_ms": data.get("capture_ms"),
            "image_tree_consistent": consistent, "content_types": types}


async def probe(root):
    # Launch only the thin frontend; the resident service owns the device.
    params = StdioServerParameters(command=sys.executable, args=[
        "-m", "harmony_runtime.cli", "mcp", "--state-dir", str(Path(root).resolve())])
    try:
        async with stdio_client(params) as streams:
            async with ClientSession(*streams) as client:
                await client.initialize()
                opened = await client.call_tool("mobile_session", {"operation": "open"})
                if opened.is_error:
                    return summarize(opened)
                sid = opened.structured_content["session_id"]
                try:
                    result = await client.call_tool("mobile_observe", {
                        "session_id": sid, "include_image": True, "mode": "FAST"})
                    report = summarize(result)
                    report["transport"] = "stdio_mcp"
                finally:
                    closed = await client.call_tool("mobile_session", {"operation":"close", "session_id":sid})
                if closed.is_error:
                    return failure("session_close_failed")
                return report
    except Exception:
        return failure("probe_transport_failed")


def run(root):
    return asyncio.run(probe(root))
