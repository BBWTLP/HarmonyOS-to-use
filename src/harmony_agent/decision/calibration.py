"""Calibration provenance for the optional fast provider.

A canary profile may only route a model answer to `execute` when the deployment
can show *which* calibration was used, against which provider revision and with
which thresholds. A bare non-empty string is not evidence, so `local_canary`
fails closed unless a complete artifact loads.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: Fields a calibration artifact must carry to be usable.
REQUIRED_FIELDS = ("calibration_id", "provider_revision", "confidence_threshold",
                   "certainty_threshold", "dataset_sha256")

STATUS_READY = "ready"
STATUS_MISSING = "artifact_missing"
STATUS_UNREADABLE = "artifact_unreadable"
STATUS_INCOMPLETE = "artifact_incomplete"
STATUS_REVISION_MISMATCH = "provider_revision_mismatch"


@dataclass(frozen=True)
class Calibration:
    calibration_id: str
    provider_revision: str
    confidence_threshold: float
    certainty_threshold: float
    dataset_sha256: str

    def as_dict(self) -> dict[str, Any]:
        return {"calibration_id": self.calibration_id,
                "provider_revision": self.provider_revision,
                "confidence_threshold": self.confidence_threshold,
                "certainty_threshold": self.certainty_threshold,
                "dataset_sha256": self.dataset_sha256}


def _threshold(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        return None
    if not 0.0 <= number <= 1.0:
        return None
    return number


def load_calibration(path: str | Path | None, *,
                     expected_revision: str | None = None) -> tuple[Calibration | None, str]:
    """Load and validate a calibration artifact.

    Returns ``(record, status)``. Any problem yields ``(None, status)`` so the
    caller fails closed instead of treating a string as proof of calibration.
    """
    if path is None or str(path).strip() == "":
        return None, STATUS_MISSING
    artifact = Path(path)
    if not artifact.is_file():
        return None, STATUS_MISSING
    try:
        payload = json.loads(artifact.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None, STATUS_UNREADABLE
    if not isinstance(payload, dict) or any(key not in payload for key in REQUIRED_FIELDS):
        return None, STATUS_INCOMPLETE
    confidence = _threshold(payload.get("confidence_threshold"))
    certainty = _threshold(payload.get("certainty_threshold"))
    calibration_id = str(payload.get("calibration_id") or "").strip()
    revision = str(payload.get("provider_revision") or "").strip()
    digest = str(payload.get("dataset_sha256") or "").strip().lower()
    if (confidence is None or certainty is None or not calibration_id or not revision
            or len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest)):
        return None, STATUS_INCOMPLETE
    if expected_revision is not None and revision != expected_revision:
        return None, STATUS_REVISION_MISMATCH
    return Calibration(calibration_id=calibration_id, provider_revision=revision,
                       confidence_threshold=confidence,
                       certainty_threshold=certainty,
                       dataset_sha256=digest), STATUS_READY


def dataset_digest(records: list[dict[str, Any]]) -> str:
    """Stable digest of a calibration dataset, for the artifact field."""
    canonical = json.dumps(records, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()
