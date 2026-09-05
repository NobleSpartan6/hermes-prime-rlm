"""hermes-prime-rlm — a Hermes Agent plugin (standalone, community).

Exposes exactly one model-facing tool, ``prime_agent``. Its only v0.2 action,
``run``, drives an ephemeral Prime Agent v0.8.1 RPC session inside a detached
candidate, then leaves verification and acceptance authority with Hermes.

Runtime dependencies: Python standard library only.

This is an independent community integration. It is not an official Nous
Research or Prime Intellect product.
"""

from __future__ import annotations

PLUGIN_NAME = "prime-rlm"
PLUGIN_VERSION = "0.2.0"

__all__ = ["register", "create_desktop_controller", "PLUGIN_NAME", "PLUGIN_VERSION"]


def register(ctx) -> None:
    """Register one model tool plus operator-only setup surfaces with Hermes.

    Import-light by design: no subprocesses, no network, no filesystem writes,
    no Prime probing. All heavy lifting happens when the tool handler runs.
    """
    from . import tools, ui_cli

    tools.register_tools(ctx)
    ui_cli.register_operator_commands(ctx)


def create_desktop_controller(ctx, **options):
    """Create an operator-only controller; the backend must reuse one per profile.

    This does not register another model tool or launch a worker. See
    docs/desktop-rlm-v1.md before connecting a renderer to this host API.
    """
    from .desktop import DesktopRunController

    return DesktopRunController(ctx, **options)


# Hermes' loader calls ``register_tools(ctx)`` on the package's tools.py for
# plugins declaring provides_tools; the package-level register() covers
# manager-driven loads. Both paths land in the same registration function.
