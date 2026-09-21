"""Seal a memory snapshot for online reuse (Phase 3).

Freezing is what makes an online task safe: the frozen artifact carries its own
manifest hash, is written next to (never over) the append-only snapshot, and the
`FrozenMemory` mount has no write method, so a task cannot learn into it.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from harmony_agent.memory_store import MemoryStore, MemoryStoreError


def main() -> int:
    parser = argparse.ArgumentParser(description="Freeze a learning memory snapshot")
    parser.add_argument("--memory-root", default=".runtime/rsi/memory")
    parser.add_argument("--version", type=int, default=None,
                        help="defaults to the latest snapshot")
    parser.add_argument("--verify", action="store_true",
                        help="verify an existing frozen snapshot instead of writing one")
    parser.add_argument("--report", default=None)
    args = parser.parse_args()

    memory_root = Path(args.memory_root)
    if not memory_root.is_absolute():
        memory_root = REPO_ROOT / memory_root
    store = MemoryStore(memory_root)
    try:
        if args.verify:
            version = args.version if args.version is not None else max(store.versions())
            frozen = store.load_frozen(version)
            payload = {"status": "verified" if frozen.verify() else "hash_mismatch",
                       "version": frozen.version,
                       "manifest_hash": frozen.manifest_hash,
                       "entries": len(frozen.entries)}
        else:
            frozen = store.freeze(args.version)
            payload = {"status": "frozen", "version": frozen.version,
                       "manifest_hash": frozen.manifest_hash,
                       "entries": len(frozen.entries),
                       "snapshot_unchanged": True}
    except MemoryStoreError as error:
        print(json.dumps({"status": "error", "code": error.code,
                          "message": str(error)}, ensure_ascii=False))
        return 1
    if args.report:
        out = Path(args.report)
        if not out.is_absolute():
            out = REPO_ROOT / out
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        payload["report"] = str(out)
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
