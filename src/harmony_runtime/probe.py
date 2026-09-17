"""Explicit read-only probe through the same stdio MCP path as an agent."""
import asyncio
import re
import sys
from pathlib import Path
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


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
    # Reuse the selected resident service and its lease; never start a competing
    # device runtime. Initialization and tool discovery remain side-effect free.
    parameters = StdioServerParameters(command=sys.executable,
        args=["-m", "harmony_runtime.cli", "mcp", "--state-dir", str(root)],
        env={"PYTHONPATH": str(Path(__file__).resolve().parents[1])})
    try:
        async with stdio_client(parameters) as streams:
            async with ClientSession(*streams) as client:
                await client.initialize()
                opened = await client.call_tool("mobile_session", {"operation": "open"})
                if opened.is_error:
                    return summarize(opened)
                sid = opened.structured_content["session_id"]
                try:
                    observed = await client.call_tool("mobile_observe",
                        {"session_id": sid, "include_image": True})
                    result = summarize(observed)
                finally:
                    closed = await client.call_tool("mobile_session",
                        {"operation": "close", "session_id": sid})
                if closed.is_error:
                    result.update(status="not_ready", error_code="session_cleanup_failed")
                return result
    except Exception:
        # Raw transport errors may include endpoint credentials or device data.
        return failure("probe_transport_failed")


def run(root):
    return asyncio.run(probe(root))
