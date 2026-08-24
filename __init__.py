"""hermes-prime-rlm — a Hermes Agent plugin (standalone, community).

Exposes exactly one model-facing tool, ``prime_rlm_run``, which runs a bounded
Prime Agent RLM coding task inside a detached Git worktree ("candidate"),
independently verifies the result with host-executed checks, and returns a
deterministic receipt. The active source checkout is never modified and the
candidate is never applied automatically.

Runtime dependencies: Python standard library only.

This is an independent community integration. It is not an official Nous
Research or Prime Intellect product.
"""

from __future__ import annotations

PLUGIN_NAME = "prime-rlm"
PLUGIN_VERSION = "0.1.0"

__all__ = ["register", "PLUGIN_NAME", "PLUGIN_VERSION"]


def register(ctx) -> None:
    """Register the plugin's single tool with Hermes.

    Import-light by design: no subprocesses, no network, no filesystem writes,
    no Prime probing. All heavy lifting happens when the tool handler runs.
    """
    from . import tools

    tools.register_tools(ctx)


# Hermes' loader calls ``register_tools(ctx)`` on the package's tools.py for
# plugins declaring provides_tools; the package-level register() covers
# manager-driven loads. Both paths land in the same registration function.
