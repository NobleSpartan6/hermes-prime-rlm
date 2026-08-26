"""hermes-prime-rlm — a Hermes Agent plugin (standalone, community).

Exposes exactly one model-facing tool, ``prime_rlm_run``, which runs a bounded
Prime Agent RLM coding task inside a detached Git worktree ("candidate"),
runs host-observed checks, and returns an unsigned review receipt. The
ordinary source files are expected to remain untouched and the candidate is
never applied automatically; this plugin is not a security sandbox or an
adversarially trustworthy attestation system.

Runtime dependencies: Python standard library only.

This is an independent community integration. It is not an official Nous
Research or Prime Intellect product.
"""

from __future__ import annotations

PLUGIN_NAME = "prime-rlm"
PLUGIN_VERSION = "0.1.1"

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
