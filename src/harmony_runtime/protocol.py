"""Opt-in protocol and agent verification against the current physical device."""
from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .device import find_hdc

_PROTOCOL_TARGETS = {1: "tcp:8012", 2: "localabstract:uitest_socket"}
_PROBE_API = "Driver.findComponents"
_PROBE_BUILDER_API = "On.text"
_PROBE_BY_SEED = "On#seed"
_PROBE_SELECTOR_TEXT = "__codex_protocol_probe_absent_20260919__"
_REMOTE_REF_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*#[A-Za-z0-9_-]+$")
_MAX_RESPONSE_BYTES = 256 * 1024
_KEY_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,96}$")


def _sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _probe_message(
    api: str = _PROBE_API,
    this_ref: str = "Driver#0",
    args: list[Any] | None = None,
) -> str:
    request_id = datetime.now().strftime("%Y%m%d%H%M%S%f")
    return json.dumps({
        "module": "com.ohos.devicetest.hypiumApiHelper",
        "method": "callHypiumApi",
        "params": {
            "api": api,
            "this": this_ref,
            "args": args or [],
            "message_type": "hypium",
        },
        "request_id": request_id,
    }, ensure_ascii=False, separators=(",", ":"))


def _response_metadata(raw: Any) -> dict[str, Any]:
    """Return shape-only metadata without exposing device response content."""
    if not isinstance(raw, str):
        return {"status": "invalid", "code": "non_text_response"}
    encoded = raw.encode("utf-8", errors="replace")
    if not encoded or len(encoded) > _MAX_RESPONSE_BYTES:
        return {"status": "invalid", "code": "empty_or_oversized_response"}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {"status": "invalid", "code": "non_json_response"}
    if not isinstance(value, dict):
        return {"status": "invalid", "code": "non_object_response"}
    keys = sorted(k for k in value if isinstance(k, str) and _KEY_RE.fullmatch(k))
    if len(keys) != len(value):
        return {"status": "invalid", "code": "unsafe_response_keys"}
    status_keys = [k for k in ("code", "error", "exception", "message", "result", "success") if k in value]
    metadata = {"top_level_keys": keys[:32], "top_level_key_count": len(keys),
                "status_fields_present": status_keys}
    if "result" in value:
        result_value = value["result"]
        metadata["result_type"] = type(result_value).__name__
        if isinstance(result_value, list):
            metadata["result_length"] = len(result_value)
    # A syntactically valid JSON object is not necessarily a successful RPC.
    # The device-side Hypium bridge reports business failures as an
    # ``exception`` object, and some versions use ``error``/``success``/``code``
    # instead.  Keep only shape information: response values may contain
    # private device or page data.
    if "exception" in value:
        metadata.update({"status": "device_exception", "code": "device_exception"})
    elif "error" in value:
        metadata.update({"status": "device_error", "code": "device_error"})
    elif value.get("success") is False:
        metadata.update({"status": "device_error", "code": "unsuccessful_response"})
    elif "code" in value and value.get("code") not in (0, "0", None):
        metadata.update({"status": "device_error", "code": "nonzero_response_code"})
    elif any(field in value for field in ("result", "success", "code")):
        metadata["status"] = "ok"
    else:
        metadata.update({"status": "unexpected_shape", "code": "missing_success_indicator"})
    return metadata


def _result_reference(raw: Any) -> str | None:
    if not isinstance(raw, str):
        return None
    if len(raw.encode("utf-8", errors="replace")) > _MAX_RESPONSE_BYTES:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(value, dict):
        return None
    reference = value.get("result")
    if not isinstance(reference, str):
        return None
    return reference if _REMOTE_REF_RE.fullmatch(reference) else None


def _semantic_probe(device: Any) -> dict[str, Any]:
    builder_raw = device.rpc_call(
        _probe_message(
            _PROBE_BUILDER_API,
            _PROBE_BY_SEED,
            [_PROBE_SELECTOR_TEXT, 0],
        )
    )
    builder_metadata = _response_metadata(builder_raw)
    if builder_metadata.get("status") != "ok":
        builder_metadata["phase"] = "selector_builder"
        return builder_metadata

    by_ref = _result_reference(builder_raw)
    if by_ref is None:
        return {
            "status": "invalid",
            "code": "invalid_selector_builder_result",
            "phase": "selector_builder",
        }

    result_raw = device.rpc_call(
        _probe_message(_PROBE_API, "Driver#0", [by_ref])
    )
    metadata = _response_metadata(result_raw)
    metadata["api"] = _PROBE_API
    metadata["phase"] = "query"
    metadata["selector_builder"] = "ok"
    return metadata


def run() -> dict[str, Any]:
    """Execute a read-only agent RPC probe on exactly one connected device."""
    from devhelmkit.harmony.device.hdc import HdcDevice

    started = time.monotonic()
    result: dict[str, Any] = {
        "schema_version": 1, "status": "not_ready", "scope": "device_agent_protocol_execution",
        "started_at": datetime.now().astimezone().isoformat(), "phone_operation_verified": False,
        "acceptance_verified": False,
        "side_effects": ["may_deploy_or_reuse_the_pinned_local_agent_asset",
                          "may_start_the_device_uitest_daemon", "creates_and_cleans_an_hdc_port_forward",
                          "issues_one_read_only_driver_find_components_rpc"],
        "device_count": None, "predicted": {"abi": None, "protocol": None},
        "actual": {"protocol": None, "transport": None, "session_id_echo": None,
                   "port_forward_established": None, "transport_status": "not_attempted",
                   "rpc_probe": {"status": "not_attempted"}},
        "agent_asset": {"status": "not_measured",
                        "scope": "selected_local_packaged_asset_only; device_loaded_hash_not_independently_attested"},
    }
    device = None
    try:
        hdc = find_hdc()
        HdcDevice.set_hdc_path(hdc)
        targets = HdcDevice.list_targets()
        if not isinstance(targets, list) or any(not isinstance(t, str) or not t for t in targets):
            result["error_code"] = "discovery_failed"
            return result
        result["device_count"] = len(targets)
        if len(targets) != 1:
            result["error_code"] = "no_device" if not targets else "multiple_devices"
            return result
        # Serial is used only as an in-process selector and never returned.
        device = HdcDevice(targets[0])
        manager = device.agent
        abi, predicted_protocol = manager.detect_device_info()
        if not isinstance(abi, str) or not isinstance(predicted_protocol, int):
            result["error_code"] = "device_info_invalid"
            return result
        result["predicted"] = {"abi": abi, "protocol": predicted_protocol}
        try:
            asset_path = manager._select_agent_so(abi, predicted_protocol)
            result["agent_asset"] = {"status": "ok", "abi": abi, "predicted_protocol": predicted_protocol,
                                     "name": Path(asset_path).name, "sha256": _sha256(asset_path),
                                     "scope": "selected_local_packaged_asset_only; device_loaded_hash_not_independently_attested"}
        except Exception:
            result["agent_asset"] = {"status": "unavailable", "code": "selected_asset_unavailable",
                                     "scope": "selected_local_packaged_asset_only; device_loaded_hash_not_independently_attested"}
            result["error_code"] = "agent_asset_unavailable"
            return result
        result["actual"]["rpc_probe"] = _semantic_probe(device)
        actual_protocol = getattr(manager, "protocol_version", None)
        result["actual"]["protocol"] = actual_protocol if actual_protocol in _PROTOCOL_TARGETS else None
        result["actual"]["transport"] = _PROTOCOL_TARGETS.get(actual_protocol)
        result["actual"]["session_id_echo"] = getattr(device, "_sid_echo", None)
        result["actual"]["port_forward_established"] = bool(getattr(device, "_fport_established", False))
        transport_ok = (result["actual"]["protocol"] == predicted_protocol
                        and result["actual"]["transport"] is not None
                        and result["actual"]["session_id_echo"] is True
                        and result["actual"]["port_forward_established"] is True)
        result["actual"]["transport_status"] = "ok" if transport_ok else "failed"
        rpc_ok = result["actual"]["rpc_probe"].get("status") == "ok"
        if transport_ok and rpc_ok:
            result["status"] = "ok"
        elif transport_ok:
            # The wire protocol is usable, but the semantic probe returned a
            # device-side failure or unexpected shape.  Do not call this a
            # complete agent/RPC acceptance.
            result["status"] = "partial"
            result["error_code"] = "rpc_probe_failed"
        else:
            result["error_code"] = "protocol_probe_incomplete"
        return result
    except Exception:
        # Driver exceptions may contain serials, paths, or response data.
        result["error_code"] = "protocol_probe_failed"
        return result
    finally:
        if device is not None:
            try:
                device.close(stop_daemon=False)
            except Exception:
                result.setdefault("cleanup", {})["status"] = "failed"
        result["finished_at"] = datetime.now().astimezone().isoformat()
        result["duration_ms"] = round((time.monotonic() - started) * 1000, 3)


def report() -> dict[str, Any]:
    return run()
