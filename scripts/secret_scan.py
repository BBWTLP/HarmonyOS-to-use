"""A02/P0-02: scan the git index for secrets, raw trees and screenshots.

Offline only. It reads the tracked file list (`git ls-files`) and the working-tree
content of those files; it never touches a device, the Decider service or the
network. Findings contain file names and rule ids, never the matched value.

Rules
-----
``binary-artifact``   tracked image/video/binary files that belong in the local
                      retention area, not in Git.
``private-key``       PEM private key blocks.
``service-token``     literal bearer/api tokens and secret-looking assignments.
``device-serial``     a hardware serial shaped token (mixed upper-case alnum).
``embedded-blob``     a very long base64 payload inlined in a text file.
``raw-tree``          a file that looks like a captured UI tree dump.

Exit code is 0 when clean, 1 when findings exist, 2 when the scan cannot run.
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

BINARY_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".mp4", ".mov",
                   ".avi", ".heic", ".wav", ".mp3", ".zip", ".7z", ".db", ".sqlite3",
                   ".pdf", ".docx", ".xlsx")

RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private-key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("service-token", re.compile(r"(?:sk|rk)-[A-Za-z0-9]{24,}")),
    ("service-token", re.compile(r"(?i)\b(?:api[_-]?token|access[_-]?token|"
                                 r"client[_-]?secret|password)\b\s*[:=]\s*['\"]"
                                 r"[^'\"\s]{12,}['\"]")),
    ("device-serial", re.compile(r"\b(?=[A-Z0-9]{14,17}\b)(?=(?:[^A-Z]*[A-Z]){2,})"
                                 r"(?=(?:[^0-9]*[0-9]){5,})[A-Z0-9]{14,17}\b")),
    ("embedded-blob", re.compile(r"(?:[A-Za-z0-9+/]{400,}={0,2})")),
)

#: A UI tree dump stores node attributes next to bounds/children.
RAW_TREE_MARKERS = ('"attributes"', '"bounds"', '"children"')

#: Suffixes whose content is code or prose: constructing a tree there is not a
#: captured dump, so the raw-tree rule does not apply.
SOURCE_SUFFIXES = (".py", ".ps1", ".sh", ".bat", ".toml", ".cfg", ".ini", ".yml",
                   ".yaml", ".md")

#: Files whose content is intentionally about these markers.
RULE_ALLOWLIST = (
    "scripts/secret_scan.py",
    "docs/acceptance/2026-09-20/secret-scan.json",
)

#: Findings that were reviewed and accepted, with the reason they stay.
#: Device serials appear only in acceptance/baseline documentation, which is the
#: project's record of which authorized phone produced an evidence file.
REVIEWED_ALLOWLIST: tuple[tuple[str, str, str], ...] = (
    ("README.md", "device-serial", "the quickstart documents the authorized test device"),
    ("docs/*.md", "device-serial", "device baseline / audit records"),
    ("docs/acceptance/*/*.json", "device-serial", "acceptance baseline metadata"),
    ("docs/acceptance/*/*.md", "device-serial", "acceptance narrative records"),
)


def tracked_files() -> list[Path]:
    completed = subprocess.run(["git", "ls-files"], cwd=REPO_ROOT, capture_output=True,
                               text=True, check=True)
    return [REPO_ROOT / line for line in completed.stdout.splitlines() if line.strip()]


def reviewed_reason(relative: str, rule: str) -> str | None:
    for pattern, allowed_rule, reason in REVIEWED_ALLOWLIST:
        if allowed_rule == rule and fnmatch.fnmatch(relative, pattern):
            return reason
    return None


def scan(files: list[Path]) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    findings: list[dict[str, object]] = []
    allowlisted: list[dict[str, object]] = []
    for path in files:
        relative = path.relative_to(REPO_ROOT).as_posix()
        if relative in RULE_ALLOWLIST:
            continue
        raw: list[dict[str, object]] = []
        if not path.exists():
            raw.append({"rule": "missing-file", "file": relative, "line": 0})
        elif path.suffix.lower() in BINARY_SUFFIXES:
            raw.append({"rule": "binary-artifact", "file": relative, "line": 0})
        else:
            try:
                text = path.read_text(encoding="utf-8", errors="strict")
            except (UnicodeDecodeError, OSError):
                raw.append({"rule": "binary-artifact", "file": relative, "line": 0})
                text = ""
            if text and (path.suffix.lower() not in SOURCE_SUFFIXES
                         and all(marker in text for marker in RAW_TREE_MARKERS)):
                raw.append({"rule": "raw-tree", "file": relative, "line": 0})
            elif text:
                for number, line in enumerate(text.splitlines(), start=1):
                    for rule, pattern in RULES:
                        if pattern.search(line):
                            raw.append({"rule": rule, "file": relative, "line": number})
                            break
        for item in raw:
            reason = reviewed_reason(relative, str(item["rule"]))
            if reason:
                allowlisted.append({**item, "reason": reason})
            else:
                findings.append(item)
    return findings, allowlisted


def main() -> int:
    parser = argparse.ArgumentParser(description="Scan tracked files for secrets and raw captures")
    parser.add_argument("--report", default=None,
                        help="write the JSON report to this path")
    parser.add_argument("--paths", nargs="*", default=None,
                        help="scan these paths instead of the git index")
    args = parser.parse_args()
    try:
        files = ([Path(item) if Path(item).is_absolute() else REPO_ROOT / item
                  for item in args.paths] if args.paths else tracked_files())
    except Exception as error:  # pragma: no cover - environment failure
        print(json.dumps({"status": "error", "error": str(error)}, ensure_ascii=False))
        return 2
    findings, allowlisted = scan(files)
    report = {
        "schema_version": 1,
        "scope": "git index + working tree content of tracked files",
        "files_scanned": len(files),
        "rules": sorted({rule for rule, _ in RULES} | {"binary-artifact", "raw-tree"}),
        "findings": findings,
        "finding_count": len(findings),
        "reviewed_allowlisted": allowlisted,
        "reviewed_count": len(allowlisted),
        "status": "clean" if not findings else "findings",
        "note": "findings list file names and rule ids only; no matched value is stored; "
                "reviewed_allowlisted entries were inspected and accepted with a reason",
    }
    if args.report:
        out = Path(args.report)
        if not out.is_absolute():
            out = REPO_ROOT / out
        out.parent.mkdir(parents=True, exist_ok=True)
        payload = dict(report)
        payload["index_sha256"] = hashlib.sha256(
            "\n".join(sorted(path.relative_to(REPO_ROOT).as_posix() for path in files))
            .encode("utf-8")).hexdigest()
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        report["report"] = str(out)
    print(json.dumps(report, ensure_ascii=False))
    return 0 if not findings else 1


if __name__ == "__main__":
    raise SystemExit(main())
