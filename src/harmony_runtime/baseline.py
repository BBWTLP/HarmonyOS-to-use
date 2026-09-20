"""Privacy-scoped, read-only baseline; not a phone-operation acceptance result."""
import hashlib
import json
import platform
import re
import subprocess
import sys
from datetime import datetime
from importlib.metadata import PackageNotFoundError, distribution, version
from pathlib import Path

from .device import find_hdc, read_device_baseline


def file_digest(path):
    path = Path(path)
    if path.is_symlink() or path.stat().st_size > 16 * 1024 * 1024:
        raise ValueError("Unsupported baseline asset")
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def source_metadata(root):
    """Hash an explicit code/config scope, never .runtime, screenshots or .env.

    Includes uncommitted/untracked code. This is not a Git tree hash or a
    reproducible-build attestation. Paths are used in the digest, not emitted.
    """
    root = Path(root)
    if not (root / "pyproject.toml").is_file() or not (root / "src/harmony_runtime").is_dir():
        return {"status": "unavailable", "code": "source_checkout_not_found"}
    try:
        files = [p for folder in ("src", "tests", "scripts")
                 for p in (root / folder).rglob("*.py") if "__pycache__" not in p.parts]
        files += [root / name for name in ("pyproject.toml", "requirements.lock")
                  if (root / name).is_file()]
        digests = []
        for path in sorted(files, key=lambda p: p.relative_to(root).as_posix()):
            # Also reject a symlink/junction ancestor pointing outside the checkout.
            if not path.resolve().is_relative_to(root.resolve()):
                raise ValueError()
            digests.append([path.relative_to(root).as_posix(), file_digest(path)])
        digest = hashlib.sha256(json.dumps(digests, separators=(",", ":")).encode()).hexdigest()
        result = {"status": "ok", "content_sha256": digest, "file_count": len(digests),
                  "scope": "src/tests/scripts Python files + pyproject.toml + requirements.lock",
                  "git": {"status": "unavailable"}}
        def git(*args):
            return subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                                  text=True, timeout=3, check=True).stdout.strip()
        try:
            sha = git("rev-parse", "--verify", "HEAD")
            if not re.fullmatch(r"[0-9a-f]{40,64}", sha):
                raise ValueError()
            dirty = bool(git("status", "--porcelain", "--untracked-files=normal"))
            result["git"] = {"status": "ok", "commit": sha, "dirty": dirty}
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
        return result
    except (OSError, ValueError):
        return {"status": "unavailable", "code": "source_hash_failed"}


def host_metadata(root):
    packages = {}
    for name in ("devhelmkit", "mcp", "pydantic", "pillow"):
        try:
            packages[name] = {"status": "ok", "version": version(name)}
        except PackageNotFoundError:
            packages[name] = {"status": "unavailable"}
    assets = []
    try:
        package = distribution("devhelmkit")
        for abi, name in (("arm64-v8a", "agent_v1.so"), ("arm64-v8a", "agent_v2.so"), ("x86_64", "agent.so")):
            path = package.locate_file("devhelmkit/assets/so/" + abi + "/" + name)
            try:
                assets.append({"abi": abi, "name": name, "status": "ok", "sha256": file_digest(path)})
            except (OSError, ValueError):
                assets.append({"abi": abi, "name": name, "status": "unavailable"})
    except PackageNotFoundError:
        pass
    try:
        hdc_metadata = {"status": "ok", "sha256": file_digest(find_hdc())}
    except Exception:
        hdc_metadata = {"status": "unavailable"}
    lock = Path(root) / "requirements.lock"
    try:
        lock_metadata = {"status": "ok", "sha256": file_digest(lock)}
    except (OSError, ValueError):
        lock_metadata = {"status": "unavailable"}
    return {"python": platform.python_version(), "python_design_target": "3.12",
            "python_design_target_met": tuple(sys.version_info[:2]) == (3, 12),
            "packages": packages, "hdc_binary": hdc_metadata, "local_agent_assets": assets,
            "agent_asset_scope": "local packaged bytes only; not deployed/loaded device verification",
            "requirements_lock": lock_metadata, "source": source_metadata(root)}


def report(root=None):
    root = Path(root) if root is not None else Path(__file__).resolve().parents[2]
    started = datetime.now().astimezone().isoformat()
    host = host_metadata(root)
    try:
        device = read_device_baseline()
    except Exception:
        # Never leak driver exception strings, which can contain device identities.
        device = {"status": "not_ready", "error_code": "metadata_collection_failed"}
    ready = (device["status"] == "ok" and all(v["status"] == "ok" for v in host["packages"].values()))
    result = {"schema_version": 1, "status": "ok" if ready else "not_ready",
              "scope": "device_and_host_core_metadata", "started_at": started,
              "finished_at": datetime.now().astimezone().isoformat(),
              "device": device, "host": host, "phone_operation_verified": False,
              "acceptance_verified": False,
              "not_measured": ["display_and_scaling", "app_versions", "foreground_and_authentication",
                               "connection_transport", "negotiated_protocol", "loaded_agent_hash",
                               "task_success_rate", "controlled_cold_warm_latency"]}
    result["baseline_id"] = hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest()
    return result
