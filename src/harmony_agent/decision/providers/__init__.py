"""Decision providers. None of them may dispatch a device action.

The concrete adapters are imported lazily so that importing this package never
imports the Decider adapter. A build without the Decider package still gets the
protocol, the rules baseline and the health helper.
"""

from .base import (DecisionProvider, ProviderCapabilities, ProviderResult,
                   ProviderUnavailable, provider_health)

__all__ = [
    "DecisionProvider",
    "ProviderCapabilities",
    "ProviderResult",
    "ProviderUnavailable",
    "provider_health",
    "DeciderProvider",
    "RulesProvider",
]


def __getattr__(name: str):
    if name == "RulesProvider":
        from .rules import RulesProvider
        return RulesProvider
    if name == "DeciderProvider":
        from .decider import DeciderProvider
        return DeciderProvider
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__():
    return sorted(__all__)
