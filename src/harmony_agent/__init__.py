"""General-purpose agent layer on top of the deterministic harmony_runtime.

The runtime keeps every device write; this package adds candidate registration,
grounding, decision routing (rules / local Decider), task supervision, memory
and read-only checking. Nothing here may talk to HDC directly.
"""

__all__ = ["__version__"]

__version__ = "0.1.0.dev0"
