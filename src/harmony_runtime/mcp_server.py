"""Thin MCP frontend; initialize/list never touch Runtime or the phone."""
import asyncio
import json
from mcp.types import CallToolResult, TextContent, ImageContent
from typing import Literal
from mcp.server import MCPServer
from .service import Client
from .contracts import ActRequest, BurstRequest, Expected, WaitCondition, RuntimeFault

def flat_tree(tree):
    """Lossless tree topology with bounded JSON nesting for MCP serializers."""
    nodes = []
    pending = [(tree, None)]
    while pending:
        node, parent = pending.pop()
        node_id = f"t{len(nodes)}"
        nodes.append({"node_id": node_id, "parent_id": parent,
                      "data": {k: v for k, v in node.items() if k != "children"},
                      "has_children_field": "children" in node})
        pending.extend((child, node_id) for child in reversed(node.get("children", [])))
    return {"format": "flat_tree_v1", "root_id": "t0", "nodes": nodes}


def build(root=None):
    server = MCPServer(name="harmony-mobile", version="0.1.0.dev0")
    client = Client(root)
    async def call(method, session_id=None, **params):
        if session_id is not None: params["session_id"] = session_id
        try:
            return await asyncio.to_thread(client.call, method, **params)
        except RuntimeFault as error:
            data = {"status": "error", "error": {"code": error.code, "message": str(error)}}
            return CallToolResult(content=[TextContent(type="text", text=json.dumps(data, ensure_ascii=False))], structured_content=data, is_error=True)
        except asyncio.CancelledError:
            sid = session_id or params.get("arguments", {}).get("session_id")
            if sid:
                await asyncio.shield(asyncio.to_thread(client.call, "session", operation="pause", session_id=sid))
            raise
    def package(result):
        if isinstance(result, CallToolResult):
            return result
        result = dict(result)
        if "tree" in result:
            result["tree"] = flat_tree(result["tree"])
        image = result.pop("image", None)
        annotated = result.pop("annotated_image", None)
        frame_images = []
        if "frames" in result:
            result["frames"] = [dict(frame) for frame in result["frames"]]
            for frame in result["frames"]:
                frame_image = frame.pop("image", None)
                if frame_image:
                    frame_images.append((frame["observation_id"], frame["actual_offset_ms"], frame_image))
        content = [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]
        for observation_id, offset, frame_image in frame_images:
            content.append(TextContent(type="text", text=f"Historical frame {observation_id} at {offset}ms; not actionable"))
            content.append(ImageContent(type="image", data=frame_image["base64"], mime_type=frame_image["mime_type"]))
        if image:
            content.append(TextContent(type="text", text="Original screenshot"))
            content.append(ImageContent(type="image", data=image["base64"], mime_type=image["mime_type"]))
        if annotated:
            content.append(TextContent(type="text", text="UI-tree target labels; use action_id with this observation_id. Labels do not prove absence of occlusion."))
            content.append(ImageContent(type="image", data=annotated["base64"], mime_type=annotated["mime_type"]))
        return CallToolResult(content=content, structured_content=result)
    @server.tool()
    async def mobile_session(operation: Literal["open","status","pause","resume","close","recover","action_status","burst_status"], session_id: str | None = None, device_id: str | None = None, request_id: str | None = None) -> CallToolResult:
        """Acquire/release one device lease. Recover reconnects and observes but never clears unknown writes. Resume requires a fresh observation. action_status and burst_status read device-bound journal metadata by request_id without replaying; not_found is not proof of non-execution."""
        return package(await call("session", operation=operation, session_id=session_id, device_id=device_id, request_id=request_id))
    @server.tool()
    async def mobile_observe(session_id: str, include_image: bool = False, mode: Literal["FAST", "FULL", "TEMPORAL"] = "FAST") -> CallToolResult:
        """Observe current UI. FULL includes a flat_tree_v1 tree (ordered nodes with parent_id and original data), original screenshot and numbered tree targets when consistent. FAST optionally includes a screenshot. TEMPORAL samples up to five historical frames within 3000ms; frames cannot be used as action handles and do not arm a live watch. Unverified captures cannot be used for actions. OCR and visual-only grounding are not yet available."""
        result = await call("observe", session_id=session_id, include_image=include_image, mode=mode)
        return package(result)
    @server.tool()
    async def mobile_act(request: ActRequest) -> CallToolResult:
        """Dispatch one typed action. Unknown execution must never be blindly retried."""
        return package(await call("act", arguments=request.model_dump()))
    @server.tool()
    async def mobile_burst(request: BurstRequest) -> CallToolResult:
        """Execute 1–5 semantic actions under one shared budget (maximum 3000ms). Each step requires a postcondition, fresh target resolution, and risk checks. Optional watch_timeout_ms waits locally for that step target, then regrounds before dispatch; timeout or disappearance stops execution. The watch budget includes dispatch and verification. Stops on the first unverified or unknown result; never resumes or replays an interrupted sequence. Use mobile_session burst_status to inspect child request metadata."""
        return package(await call("burst", arguments=request.model_dump()))
    @server.tool()
    async def mobile_wait(session_id: str, expected: Expected | None = None, timeout_ms: int = 5000, poll_ms: int = 200, condition: WaitCondition | None = None) -> CallToolResult:
        """Wait using exactly one of legacy expected or typed condition. Change requires a fresh baseline observation_id. Stable means equal sampled UI trees, not continuous or pixel stability. Element presence includes disabled nodes; absence only describes the UI catalog, not visually hidden tree omissions. app_changed is currently unsupported. Timeout is not proof of success."""
        return package(await call("wait", session_id=session_id, expected=expected.model_dump() if expected else None, condition=condition.model_dump() if condition else None, timeout_ms=timeout_ms, poll_ms=poll_ms))
    @server.tool()
    async def mobile_history(session_id: str, limit: int = 20, before: int | None = None) -> CallToolResult:
        """Read device-bound durable action metadata, newest first. Use next_before for the next page. Includes incident closure separately from original action verification. Works while paused; never touches the phone or replays actions. Omits UI text, screenshots, input values and recovery conditions."""
        return package(await call("history", session_id=session_id, limit=limit, before=before))
    return server

def run(root=None):
    build(root).run(transport="stdio")
