"""Versioned learning memory: append-only snapshots, hashes, freeze (P3-04).

The store is the offline half of the learning loop. It is deliberately small and
boring:每个快照是一个不可变文件, 带父 manifest hash 与自身 manifest hash; 提交只会追加
一个新版本, 从不就地修改历史。在线任务只能挂载 :class:`FrozenMemory`, 它没有写入
方法, 因此 "Phase 3 在线任务不写回 memory" 是结构性保证, 而不是纪律要求。

Snapshots live under ``<root>/snapshots/`` as ``v000001.json``. Each file holds the
manifest plus its entries; the manifest hash covers version, parent hash and the
ordered entry hashes, so a rewritten file is detectable by recomputation.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .contracts import canonical
from .experience import Experience, experience_content_hash

SNAPSHOT_VERSION = 1


class MemoryStoreError(RuntimeError):
    """Memory store failure with a stable code."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


class MemoryReadOnlyError(MemoryStoreError):
    """Raised when an online run tries to write to frozen memory."""

    def __init__(self, operation: str):
        super().__init__("memory_read_only",
                         f"frozen memory is read-only in an online run ({operation})")


def manifest_hash_for(version: int, parent_manifest_hash: str | None,
                      entry_hashes: list[str], frozen: bool = False) -> str:
    payload = canonical({"version": version, "parent_manifest_hash": parent_manifest_hash,
                         "entry_hashes": list(entry_hashes), "frozen": frozen})
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class MemoryManifest:
    version: int
    snapshot_id: str
    parent_manifest_hash: str | None
    manifest_hash: str
    entry_hashes: tuple[str, ...]
    created_at: float
    frozen: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"version": self.version, "snapshot_id": self.snapshot_id,
                "parent_manifest_hash": self.parent_manifest_hash,
                "manifest_hash": self.manifest_hash,
                "entry_hashes": list(self.entry_hashes),
                "created_at": self.created_at, "frozen": self.frozen}

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "MemoryManifest":
        return cls(version=int(payload["version"]),
                   snapshot_id=str(payload["snapshot_id"]),
                   parent_manifest_hash=payload.get("parent_manifest_hash"),
                   manifest_hash=str(payload["manifest_hash"]),
                   entry_hashes=tuple(payload["entry_hashes"]),
                   created_at=float(payload["created_at"]),
                   frozen=bool(payload.get("frozen", False)))

    def recompute(self) -> str:
        return manifest_hash_for(self.version, self.parent_manifest_hash,
                                 list(self.entry_hashes), self.frozen)

    def verify(self) -> bool:
        return self.recompute() == self.manifest_hash


@dataclass(frozen=True)
class FrozenMemory:
    """A read-only mount of one sealed snapshot, for an online task."""

    manifest: MemoryManifest
    entries: tuple[Experience, ...]
    read_only: bool = True

    @property
    def version(self) -> int:
        return self.manifest.version

    @property
    def manifest_hash(self) -> str:
        return self.manifest.manifest_hash

    def verify(self) -> bool:
        if not self.manifest.verify() or not self.manifest.frozen:
            return False
        return [entry.content_hash for entry in self.entries] == list(self.manifest.entry_hashes)

    def facts(self, limit: int = 8) -> list[str]:
        """Bounded, redacted fact strings an online run may use."""
        facts = []
        for entry in self.entries[:max(0, limit)]:
            if entry.outcome != "verified":
                continue
            facts.append(f"exp:{entry.trigger.goal_pattern}"
                         f"@{entry.scope.app}/{entry.scope.build}"
                         f" layers={','.join(entry.grounding.layers)}"
                         f" risk={entry.risk_class} hash={entry.content_hash[:12]}")
        return facts

    def reject_write_back(self, operation: str = "write") -> None:
        raise MemoryReadOnlyError(operation)

    def stage(self, *args, **kwargs) -> None:  # pragma: no cover - always raises
        self.reject_write_back("stage")

    def commit(self, *args, **kwargs) -> None:  # pragma: no cover - always raises
        self.reject_write_back("commit")


class MemoryStore:
    """Append-only snapshot store with parent-hash chaining."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.snapshots_dir = self.root / "snapshots"
        self.snapshots_dir.mkdir(parents=True, exist_ok=True)
        self.marker_dir = self.root / "frozen"
        self.marker_dir.mkdir(parents=True, exist_ok=True)

    # -- read ---------------------------------------------------------------
    def _path(self, version: int) -> Path:
        return self.snapshots_dir / f"v{version:06d}.json"

    def versions(self) -> list[int]:
        found = []
        for path in sorted(self.snapshots_dir.glob("v*.json")):
            try:
                found.append(int(path.stem[1:]))
            except ValueError:  # pragma: no cover - defensive
                continue
        return found

    def load(self, version: int) -> tuple[MemoryManifest, list[Experience]]:
        path = self._path(version)
        if not path.exists():
            raise MemoryStoreError("snapshot_missing", f"no snapshot v{version}")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise MemoryStoreError("snapshot_unreadable",
                                   f"snapshot v{version} cannot be read") from error
        manifest = MemoryManifest.from_dict(payload["manifest"])
        if not manifest.verify():
            raise MemoryStoreError("manifest_hash_mismatch",
                                   f"snapshot v{version} does not match its manifest hash")
        try:
            entries = [Experience.model_validate(item) for item in payload["entries"]]
        except Exception as error:
            # A tampered or corrupt entry is a store failure, not a caller bug.
            raise MemoryStoreError("entry_invalid",
                                   f"snapshot v{version} holds an invalid entry") from error
        if [entry.content_hash for entry in entries] != list(manifest.entry_hashes):
            raise MemoryStoreError("entry_hash_mismatch",
                               f"snapshot v{version} entries do not match the manifest")
        return manifest, entries

    def latest(self) -> tuple[MemoryManifest, list[Experience]] | None:
        versions = self.versions()
        if not versions:
            return None
        return self.load(versions[-1])

    def default_parent(self) -> MemoryManifest | None:
        latest = self.latest()
        return latest[0] if latest else None

    # -- write --------------------------------------------------------------
    def commit(self, entries: Iterable[Experience], *,
               parent: MemoryManifest | None = None,
               expected_parent_hash: str | None = None) -> MemoryManifest:
        """Append one snapshot. Refuses to fork history or reuse a stale parent."""
        entries = list(entries)
        if not entries:
            raise MemoryStoreError("empty_snapshot", "a snapshot needs at least one entry")
        for entry in entries:
            if entry.outcome == "inconclusive":
                raise MemoryStoreError("inconclusive_not_committable",
                                   "an inconclusive experience may not enter memory")
        latest = self.latest()
        current_parent = latest[0] if latest else None
        if expected_parent_hash is not None:
            actual = current_parent.manifest_hash if current_parent else None
            if actual != expected_parent_hash:
                raise MemoryStoreError("stale_parent",
                                   "memory moved since the caller read it")
        elif parent is not None:
            if current_parent is None or current_parent.manifest_hash != parent.manifest_hash:
                raise MemoryStoreError("stale_parent", "the supplied parent is not the current tip")
        elif current_parent is not None and parent is None:
            raise MemoryStoreError("parent_required",
                               "committing onto existing history requires the current parent")
        version = (current_parent.version if current_parent else 0) + 1
        entry_hashes = [entry.content_hash for entry in entries]
        manifest = MemoryManifest(
            version=version,
            snapshot_id="snap_" + uuid.uuid4().hex[:24],
            parent_manifest_hash=current_parent.manifest_hash if current_parent else None,
            manifest_hash=manifest_hash_for(
                version, current_parent.manifest_hash if current_parent else None,
                entry_hashes),
            entry_hashes=tuple(entry_hashes),
            created_at=time.time())
        self._write(manifest, entries)
        return manifest

    def _write(self, manifest: MemoryManifest, entries: list[Experience]) -> None:
        path = self._path(manifest.version)
        if path.exists():
            raise MemoryStoreError("version_exists",
                               f"snapshot v{manifest.version} already exists")
        body = {"manifest": manifest.to_dict(),
                "entries": [entry.model_dump() for entry in entries]}
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(body, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)

    def freeze(self, version: int | None = None) -> FrozenMemory:
        """Seal a snapshot into a separate frozen artifact.

        The append-only snapshot file is never rewritten: freezing writes a new
        file under ``frozen/`` whose manifest hash covers the frozen flag, so a
        later edit of either file is detectable.
        """
        manifest, entries = (self.latest() if version is None else self.load(version))  # type: ignore[misc]
        frozen_manifest = MemoryManifest(
            version=manifest.version, snapshot_id=manifest.snapshot_id,
            parent_manifest_hash=manifest.parent_manifest_hash,
            manifest_hash=manifest_hash_for(manifest.version, manifest.parent_manifest_hash,
                                            list(manifest.entry_hashes), frozen=True),
            entry_hashes=manifest.entry_hashes, created_at=manifest.created_at, frozen=True)
        self._write_frozen(frozen_manifest, entries)
        return FrozenMemory(manifest=frozen_manifest, entries=tuple(entries))

    def _write_frozen(self, frozen: MemoryManifest, entries: list[Experience]) -> None:
        path = self.marker_dir / f"v{frozen.version:06d}.json"
        body = {"manifest": frozen.to_dict(),
                "entries": [entry.model_dump() for entry in entries]}
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(body, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)

    def load_frozen(self, version: int) -> FrozenMemory:
        path = self.marker_dir / f"v{version:06d}.json"
        if not path.exists():
            raise MemoryStoreError("not_frozen", f"snapshot v{version} is not frozen")
        payload = json.loads(path.read_text(encoding="utf-8"))
        manifest = MemoryManifest.from_dict(payload["manifest"])
        if not manifest.frozen or not manifest.verify():
            raise MemoryStoreError("frozen_manifest_hash_mismatch",
                                   f"frozen snapshot v{version} failed hash verification")
        entries = [Experience.model_validate(item) for item in payload["entries"]]
        if [entry.content_hash for entry in entries] != list(manifest.entry_hashes):
            raise MemoryStoreError("entry_hash_mismatch",
                                   f"frozen snapshot v{version} entries do not match the manifest")
        return FrozenMemory(manifest=manifest, entries=tuple(entries))

    def rollback(self, to_version: int, *, reason: str) -> MemoryManifest:
        """Roll back by *appending* the older entry set as a new version (P7-05).

        History is never rewritten: the rollback is a new snapshot whose entries
        are the selected older version's, and whose parent is the current tip.
        That keeps the audit trail (including the version being abandoned) intact
        and makes the rollback itself reviewable.
        """
        target_manifest, target_entries = self.load(to_version)
        latest = self.latest()
        if latest is None:
            raise MemoryStoreError("no_history", "there is nothing to roll back from")
        current = latest[0]
        if target_manifest.version == current.version:
            raise MemoryStoreError("already_at_version",
                                   f"v{to_version} is already the current snapshot")
        if target_manifest.version > current.version:
            raise MemoryStoreError("unknown_version",
                                   f"v{to_version} is newer than the current tip")
        manifest = self.commit(target_entries, parent=current)
        directory = self.root / "rollbacks"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"v{manifest.version:06d}.json").write_text(
            json.dumps({"from_version": current.version, "to_version": to_version,
                        "reason": reason, "manifest_hash": manifest.manifest_hash,
                        "at": time.time()}, ensure_ascii=False, indent=2),
            encoding="utf-8")
        return manifest

    def rollbacks(self) -> list[dict[str, Any]]:
        """Review record of every rollback: what was abandoned and why."""
        directory = self.root / "rollbacks"
        if not directory.exists():
            return []
        return [json.loads(path.read_text(encoding="utf-8"))
                for path in sorted(directory.glob("v*.json"))]

    def is_unchanged(self, manifest_hash: str) -> bool:
        latest = self.latest()
        if latest is None:
            return False
        manifest, entries = latest
        return manifest.manifest_hash == manifest_hash and all(
            entry.content_hash == expected
            for entry, expected in zip(entries, manifest.entry_hashes))


def merge_experiences(entries: Iterable[Experience]) -> list[Experience]:
    """Deterministic merge order: newest evidence last, duplicates collapsed.

    The wave barrier's fixed merge order matters: two branches that learned about
    the same page must not race into memory, so entries are de-duplicated by
    content hash and sorted by (goal pattern, experience id).
    """
    unique: dict[str, Experience] = {}
    for entry in entries:
        unique.setdefault(entry.content_hash, entry)
    return sorted(unique.values(),
                  key=lambda item: (item.trigger.goal_pattern, item.experience_id))
