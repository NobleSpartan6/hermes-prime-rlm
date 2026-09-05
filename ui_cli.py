"""Operator-only GUI entry point; retains all existing setup/doctor commands."""

from __future__ import annotations


def configure_cli(parser) -> None:
    from . import setup_gate

    setup_gate.configure_cli(parser)
    parser.add_argument("--ui", action="store_true", help="Open the guided desktop launcher")
    parser.add_argument("--demo", action="store_true", help="Use --ui in a zero-model demo mode")


def handle_cli(args, *, ctx) -> int:
    from . import setup_gate

    ui = getattr(args, "ui", False)
    demo = getattr(args, "demo", False)
    if (demo and not ui) or (ui and getattr(args, "prime_action", None)):
        print("Use hermes prime --ui [--demo], without a setup or doctor subcommand.")
        return 2
    if ui:
        from .desktop_ui import launch_ui

        return launch_ui(ctx, demo=demo)
    return setup_gate.handle_cli(args, ctx=ctx)


def register_operator_commands(ctx) -> None:
    from . import setup_gate

    def cli_handler(args):
        return handle_cli(args, ctx=ctx)

    def slash_handler(raw_args):
        return setup_gate.handle_slash(raw_args, ctx=ctx)

    ctx.register_cli_command(
        name="prime", help="Prime RLM guided launcher, readiness, and setup",
        setup_fn=configure_cli, handler_fn=cli_handler,
        description="Launch the desktop form or inspect the profile-scoped Prime runtime.",
    )
    ctx.register_command(
        "prime-setup", slash_handler,
        description="Inspect Prime runtime setup (run repairs from a local terminal).",
        args_hint="",
    )
