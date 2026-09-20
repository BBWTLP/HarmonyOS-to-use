"""Lazy construction of the optional fast ``DecisionProvider``.

The runtime stays usable when the Decider package, service or model is absent.
Nothing here imports the Decider adapter at module import time: the import
happens inside :func:`resolve_fast_provider`, and only for the two profiles that
are allowed to consult a model at all (``local_shadow`` and ``local_canary``).

``rules_only`` and ``local_off`` never import and never construct a provider, so
they cannot fail because of one.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .providers.base import DecisionProvider


#: Profiles that may construct a fast provider.
FAST_PROVIDER_PROFILES = ("local_shadow", "local_canary")

#: Profiles that must run on deterministic rules only.
RULES_ONLY_PROFILES = ("rules_only", "local_off")

#: Every profile the router understands, in v3.2 policy order.
PROFILES = RULES_ONLY_PROFILES + FAST_PROVIDER_PROFILES


@dataclass(frozen=True)
class ProviderConfig:
    """Everything needed to build the optional fast provider, and nothing else."""

    profile: str = "local_off"
    requested: bool = True
    base_url: str | None = None
    token_file: str | Path | None = None
    revision: str | None = None
    timeout_seconds: float | None = None
    repo_root: str | Path | None = None

    def wants_fast_provider(self) -> bool:
        """True only when this configuration may construct a provider."""
        return self.requested and self.profile in FAST_PROVIDER_PROFILES


@dataclass(frozen=True)
class ProviderBuild:
    """The outcome of a provider resolution, including why it is absent."""

    provider: DecisionProvider | None = None
    provider_name: str = "none"
    status: str = "not_requested"
    reason: str | None = None

    def available(self) -> bool:
        return self.provider is not None

    def as_dict(self) -> dict[str, Any]:
        return {"provider_name": self.provider_name,
                "provider_available": self.available(),
                "provider_status": self.status,
                "provider_reason": self.reason}


def resolve_fast_provider(profile: str, config: ProviderConfig | None = None) -> ProviderBuild:
    """Resolve the optional fast provider for ``profile``.

    Returns a build record instead of raising: an absent Decider module, an
    unusable token file or a malformed configuration all downgrade to a
    rules-only build so the runtime keeps working.
    """
    effective = config or ProviderConfig(profile=profile)
    if effective.profile != profile:
        effective = replace(effective, profile=profile)
    if profile in RULES_ONLY_PROFILES:
        return ProviderBuild(provider_name="none", status="rules_only",
                             reason=f"profile_{profile}")
    if profile not in FAST_PROVIDER_PROFILES:
        return ProviderBuild(provider_name="none", status="unknown_profile",
                             reason=f"unknown_profile_{profile}")
    if not effective.requested:
        return ProviderBuild(provider_name="none", status="not_requested",
                             reason="fast_provider_disabled")
    try:
        from .providers.decider import (DEFAULT_BASE_URL, DEFAULT_REVISION,
                                        DEFAULT_TOKEN_FILE, DeciderProvider)
    except Exception as error:  # module absent, broken or partially installed
        return ProviderBuild(provider_name="decider", status="module_missing",
                             reason=type(error).__name__)
    token_file = effective.token_file
    if token_file is None:
        token_file = (Path(effective.repo_root) / DEFAULT_TOKEN_FILE
                      if effective.repo_root else DEFAULT_TOKEN_FILE)
    try:
        provider = DeciderProvider(
            base_url=effective.base_url or DEFAULT_BASE_URL,
            token_file=token_file,
            revision=effective.revision or DEFAULT_REVISION,
            timeout_seconds=(effective.timeout_seconds
                             if effective.timeout_seconds is not None else 15.0),
        )
    except Exception as error:  # construction must never be fatal to the host
        return ProviderBuild(provider_name="decider", status="unavailable",
                             reason=type(error).__name__)
    return ProviderBuild(provider=provider, provider_name="decider", status="ready")


def build_fast_provider(profile: str,
                        config: ProviderConfig | None = None) -> DecisionProvider | None:
    """Convenience wrapper returning only the provider (``None`` when absent)."""
    return resolve_fast_provider(profile, config).provider


def config_from_env(profile: str, repo_root: str | Path | None = None) -> ProviderConfig:
    """Read operator overrides for the optional fast provider.

    Only the two fast profiles are affected; ``rules_only`` and ``local_off``
    ignore every value here because they never construct a provider.
    """
    enabled = os.environ.get("HARMONY_AGENT_FAST_PROVIDER", "auto").strip().lower()
    requested = enabled not in ("0", "off", "false", "no", "disabled")
    raw_timeout = os.environ.get("HARMONY_AGENT_PROVIDER_TIMEOUT", "").strip()
    try:
        timeout_seconds = float(raw_timeout) if raw_timeout else None
    except ValueError:
        timeout_seconds = None
    return ProviderConfig(
        profile=profile,
        requested=requested,
        base_url=os.environ.get("HARMONY_DECIDER_URL") or None,
        token_file=os.environ.get("HARMONY_DECIDER_TOKEN_FILE") or None,
        revision=os.environ.get("HARMONY_DECIDER_REVISION") or None,
        timeout_seconds=timeout_seconds,
        repo_root=repo_root,
    )
