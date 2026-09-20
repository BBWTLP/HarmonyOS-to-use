"""Opt-in observation benchmark over stdio MCP, with metadata-only output.

No resident service is started here. Observation can wake/unlock a credential-free
screen; no application action is issued. First/subsequent are NOT cold/warm claims.
"""
import asyncio
import math
import sys
import time
from collections import Counter
from pathlib import Path

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

TIMING_KEYS = frozenset({
    "queue_ms", "worker_setup_ms", "request_ms", "screen_ready_before_ms",
    "foreground_before_ms", "tree_ms", "display_ms", "snapshot_ms",
    "screenshot_ms", "encode_image_ms", "tree_after_ms", "display_after_ms",
    "snapshot_after_ms", "foreground_after_ms", "screen_ready_after_ms",
    "annotation_ms", "encode_annotation_ms",
})
ERROR_CODES = frozenset({
    "runtime_unavailable", "device_unavailable", "device_busy", "device_not_found",
    "multiple_devices", "lease_conflict", "lease_expired", "session_invalid",
    "cancelled", "timeout", "screen_locked", "screen_off", "screen_state_unknown",
    "authentication_required", "runtime_stopped", "unauthorized",
    "unsupported_capability", "invalid_arguments",
})


def finite_ms(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def distribution(values):
    """Nearest-rank percentiles, no interpolation; empty samples stay null."""
    values = sorted(value for value in values if finite_ms(value))
    if not values:
        return {"count": 0, "p50_ms": None, "p95_ms": None, "min_ms": None, "max_ms": None}
    return {"count": len(values), "p50_ms": values[math.ceil(.50 * len(values))-1],
            "p95_ms": values[math.ceil(.95 * len(values))-1],
            "min_ms": values[0], "max_ms": values[-1]}


def error_code(result):
    data = result.structured_content or {}
    error = data.get("error", {})
    code = error.get("code") if isinstance(error, dict) else None
    return code if isinstance(code, str) and code in ERROR_CODES else "mcp_error"


def sample_metadata(result, index, elapsed_ms, mode, include_image):
    sample = {"index": index, "status": "not_ready", "client_ms": elapsed_ms}
    if result.is_error:
        sample["error_code"] = error_code(result)
        return sample
    data = result.structured_content or {}
    timing = data.get("timing", {})
    sample["timing"] = {key: value for key, value in timing.items()
                        if key in TIMING_KEYS and finite_ms(value)} if isinstance(timing, dict) else {}
    if finite_ms(data.get("capture_ms")):
        sample["capture_ms"] = data["capture_ms"]
    has_image = any(item.type == "image" for item in result.content)
    # Bounded booleans only: retain diagnostic uncertainty without UI/identifiers.
    sample["checks"] = {key: data[key] for key in (
        "actionable", "foreground_consistent", "image_tree_consistent",
        "image_dimensions_match") if type(data.get(key)) is bool}
    sample["checks"]["image_present"] = has_image
    som = data.get("som")
    if isinstance(som, dict) and type(som.get("available")) is bool:
        sample["checks"]["som_available"] = som["available"]
    if finite_ms(data.get("image_tree_skew_ms")):
        sample["image_tree_skew_ms"] = data["image_tree_skew_ms"]
    if data.get("blocking_dialog"):
        sample["error_code"] = "authentication_required"
    elif data.get("status") != "ok" or data.get("actionable") is not True:
        sample["error_code"] = "capture_unverified"
    elif (include_image or mode == "FULL") and (not has_image or data.get("image_tree_consistent") is not True):
        sample["error_code"] = "capture_unverified"
    elif mode == "FULL" and data.get("som", {}).get("available") is not True:
        sample["error_code"] = "capture_unverified"
    else:
        sample["status"] = "ok"
    return sample


def summarize(samples, requested, mode, include_image, terminal_error=None):
    successful = [sample for sample in samples if sample["status"] == "ok"]
    groups = {"all_attempts": samples, "successful": successful,
              "first_attempt": samples[:1], "subsequent_attempts": samples[1:]}
    metrics = {}
    for name, group in groups.items():
        metrics[name] = {"client": distribution([s["client_ms"] for s in group]),
                         "capture": distribution([s.get("capture_ms") for s in group]),
                         "phases": {key: distribution([s.get("timing", {}).get(key) for s in group])
                                    for key in sorted(TIMING_KEYS)
                                    if any(key in s.get("timing", {}) for s in group)}}
    report = {"schema_version": 1, "transport": "stdio_mcp", "mode": mode,
              "include_image": include_image or mode == "FULL", "requested_samples": requested,
              "attempted_samples": len(samples), "successful_samples": len(successful),
              "failed_samples": len(samples)-len(successful),
              "unattempted_samples": requested-len(samples),
              "status": "ok" if len(successful) == requested and not terminal_error else "not_ready",
              "error_counts": dict(Counter(s["error_code"] for s in samples if "error_code" in s)),
              "percentile_method": "nearest_rank", "thermal_state": "uncontrolled",
              "service_startup_included": False, "session_setup_included": False,
              "application_actions_issued": False, "screen_recovery_possible": True,
              "metrics": metrics, "samples": samples}
    if terminal_error:
        report["error_code"] = terminal_error
    return report


async def benchmark(root, samples=10, mode="FAST", include_image=False):
    if type(samples) is not int or not 1 <= samples <= 500:
        raise ValueError("samples must be an integer between 1 and 500")
    if mode not in ("FAST", "FULL"):
        raise ValueError("benchmark supports FAST or FULL")
    rows = []
    terminal_error = None
    params = StdioServerParameters(command=sys.executable, args=[
        "-m", "harmony_runtime.cli", "mcp", "--state-dir", str(Path(root).resolve())])
    try:
        async with stdio_client(params) as streams:
            async with ClientSession(*streams) as client:
                await client.initialize()
                opened = await client.call_tool("mobile_session", {"operation": "open"})
                if opened.is_error:
                    terminal_error = error_code(opened)
                else:
                    sid = opened.structured_content["session_id"]
                    try:
                        for index in range(1, samples + 1):
                            started = time.monotonic()
                            try:
                                result = await client.call_tool("mobile_observe", {
                                    "session_id": sid, "include_image": include_image, "mode": mode})
                            except Exception:
                                rows.append({"index": index, "status": "not_ready",
                                             "client_ms": round((time.monotonic()-started)*1000, 3),
                                             "error_code": "benchmark_transport_failed"})
                                terminal_error = "benchmark_transport_failed"
                                break
                            elapsed_ms = round((time.monotonic()-started)*1000, 3)
                            try:
                                row = sample_metadata(result, index, elapsed_ms, mode, include_image)
                            except (AttributeError, TypeError, ValueError):
                                # A response was received, so this attempt must not disappear
                                # from the denominator. Never include malformed private data.
                                row = {"index": index, "status": "not_ready",
                                       "client_ms": elapsed_ms, "error_code": "invalid_observation"}
                            rows.append(row)
                            # Dynamic captures may be sampled again, but do not retry device,
                            # session, transport or authentication faults in a tight loop.
                            if row.get("error_code") not in (None, "capture_unverified"):
                                terminal_error = row["error_code"]
                                break
                    finally:
                        try:
                            closed = await client.call_tool("mobile_session", {
                                "operation": "close", "session_id": sid})
                            if closed.is_error:
                                terminal_error = "session_close_failed"
                        except Exception:
                            terminal_error = "session_close_failed"
    except Exception:
        terminal_error = terminal_error or "benchmark_transport_failed"
    return summarize(rows, samples, mode, include_image, terminal_error)


def run(root, samples=10, mode="FAST", include_image=False):
    return asyncio.run(benchmark(root, samples, mode, include_image))
