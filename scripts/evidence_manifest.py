"""A01: freeze the current work tree, dependencies and device baseline.

Records the hash of every tracked-but-modified file and every new file under the
source roots, plus installed dependency versions and the read-only device
baseline. Writes JSON/YAML-free metadata only; no UI text or credentials.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOTS = ("src", "scripts", "tests", "services/decider", "evals", "docs")
SOURCE_SUFFIXES = (".py", ".json", ".md", ".csv", ".txt", ".ps1")
SKIP_PARTS = (".venv", "__pycache__", ".cache", "models", ".runtime", "build", "dist",
              "node_modules")
MAX_FILE_BYTES = 2 * 1024 * 1024


def git(*args: str) -> str:
    result = subprocess.run(["git", *args], cwd=REPO_ROOT, capture_output=True,
                            text=True, encoding="utf-8", errors="replace")
    return result.stdout.strip()


def file_digest(path: Path) -> dict:
    data = path.read_bytes()
    return {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}


def iter_files():
    for root in SOURCE_ROOTS:
        base = REPO_ROOT / root
        if not base.exists():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file() or path.suffix not in SOURCE_SUFFIXES:
                continue
            if any(part in SKIP_PARTS for part in path.parts):
                continue
            if path.stat().st_size > MAX_FILE_BYTES:
                continue
            yield path.relative_to(REPO_ROOT).as_posix(), path


def dependencies() -> dict:
    result = subprocess.run([sys.executable, "-m", "pip", "list", "--format=json"],
                            capture_output=True, text=True, encoding="utf-8",
                            errors="replace")
    try:
        packages = {item["name"].lower(): item["version"] for item in json.loads(result.stdout)}
    except Exception:
        packages = {}
    lock = REPO_ROOT / "requirements.lock"
    return {"python": sys.version.split()[0],
            "packages": dict(sorted(packages.items())),
            "lock_sha256": hashlib.sha256(lock.read_bytes()).hexdigest() if lock.exists() else None}


def device_baseline() -> dict:
    try:
        from harmony_runtime.device import read_device_baseline
        return read_device_baseline(timeout=45)
    except Exception as error:  # a missing device must not break the manifest
        return {"status": "unavailable", "error": type(error).__name__}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", default="docs/acceptance/2026-09-20/baseline-manifest.json")
    parser.add_argument("--skip-device", action="store_true")
    args = parser.parse_args()
    files = []
    for name, path in iter_files():
        files.append({"path": name, **file_digest(path)})
    status = git("status", "--porcelain")
    manifest = {
        "schema_version": 1,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "head": git("rev-parse", "HEAD"),
        "branch": git("rev-parse", "--abbrev-ref", "HEAD"),
        "dirty": bool(status),
        "modified_or_new": sorted(line[3:].strip() for line in status.splitlines() if line.strip()),
        "tracked_files": files,
        "file_count": len(files),
        "dependencies": dependencies(),
        "note": ("File hashes cover the working tree, not only HEAD: the implementation "
                 "contains uncommitted changes and the digest is what identifies it."),
    }
    if not args.skip_device:
        manifest["device"] = device_baseline()
    target = Path(args.report)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                      encoding="utf-8")
    print(json.dumps({key: manifest[key] for key in
                      ("generated_at", "head", "branch", "dirty", "file_count")},
                     ensure_ascii=False, indent=2))
    print(f"manifest written to {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
