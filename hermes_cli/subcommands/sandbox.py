"""``hermes sandbox`` subcommand — manage pre-built Docker sandbox images.

Sandbox builds (Cursor-inspired) bake ``terminal.docker_build_command`` into
a committed Docker image ahead of sessions so containers boot with their
dependencies already installed. This module carries both the parser and the
handlers; the heavy lifting lives in ``tools.sandbox_builds``.
"""

from __future__ import annotations

import time
from typing import Any, Dict


def _terminal_cfg() -> Dict[str, Any]:
    from hermes_cli.config import load_config

    cfg = load_config()
    terminal = cfg.get("terminal", {})
    return terminal if isinstance(terminal, dict) else {}


def _build_inputs() -> tuple[str, str, Dict[str, Any]]:
    terminal = _terminal_cfg()
    base_image = str(
        terminal.get("docker_image") or "nikolaik/python-nodejs:python3.11-nodejs20"
    )
    command = str(terminal.get("docker_build_command") or "").strip()
    return base_image, command, terminal


def cmd_sandbox_build(args) -> int:  # noqa: ANN001
    from tools.sandbox_builds import run_build

    base_image, command, terminal = _build_inputs()
    if not command:
        print("No build command configured.")
        print("Set one with:  hermes config set terminal.docker_build_command \"<command>\"")
        return 1
    print(f"Building sandbox image from {base_image}")
    print(f"  command: {command}")
    record = run_build(
        base_image, command, container_config=terminal, stream_output=True,
    )
    if record["status"] == "success":
        print(f"\n✓ Build succeeded: {record['image_tag']}")
        print("  New Docker sandbox sessions will boot from this image.")
        return 0
    print(f"\n✗ Build failed (exit={record.get('exit_code')}).")
    print(f"  Log: {record.get('log_path')}")
    print("  Sessions keep using the previous successful build (or the base image).")
    return 1


def cmd_sandbox_status(args) -> int:  # noqa: ANN001
    from tools.sandbox_builds import status_summary

    base_image, command, _terminal = _build_inputs()
    info = status_summary(base_image, command)
    print("Sandbox builds")
    print(f"  Base image:    {base_image}")
    if not info["configured"]:
        print("  Build command: (not configured)")
        print("  Enable with:   hermes config set terminal.docker_build_command \"<command>\"")
        return 0
    print(f"  Build command: {command}")
    active = info.get("active")
    if active:
        age_h = (time.time() - (active.get("finished_at") or 0)) / 3600
        print(f"  Active build:  {active.get('image_tag')} ({age_h:.1f}h old)")
    else:
        print("  Active build:  none — sessions boot from the base image")
        print("  Run:           hermes sandbox build")
    records = info.get("records") or []
    if records:
        print("  Recent builds:")
        for r in records[-5:]:
            when = time.strftime(
                "%Y-%m-%d %H:%M", time.localtime(r.get("finished_at") or r.get("started_at") or 0)
            )
            print(f"    {when}  {r.get('status'):8s}  {r.get('fingerprint')}  {r.get('log_path', '')}")
    return 0


def cmd_sandbox_clear(args) -> int:  # noqa: ANN001
    from tools.sandbox_builds import clear_builds

    removed = clear_builds()
    print(f"Removed {removed} sandbox build image(s) and cleared build metadata.")
    return 0


def cmd_sandbox(args) -> int:  # noqa: ANN001
    sub = getattr(args, "sandbox_command", None)
    if sub in ("build",):
        return cmd_sandbox_build(args)
    if sub in ("status", "st"):
        return cmd_sandbox_status(args)
    if sub in ("clear", "clean"):
        return cmd_sandbox_clear(args)
    # No subcommand: show status (cheap, informative default).
    return cmd_sandbox_status(args)


def build_sandbox_parser(subparsers) -> None:
    """Attach the ``sandbox`` subcommand to ``subparsers``."""
    sandbox_parser = subparsers.add_parser(
        "sandbox",
        help="Manage pre-built Docker sandbox images (sandbox builds)",
        description=(
            "Sandbox builds bake terminal.docker_build_command into a committed "
            "Docker image ahead of sessions, so Docker sandbox containers boot "
            "with dependencies already installed. A failed build never replaces "
            "the last successful one."
        ),
    )
    sandbox_subparsers = sandbox_parser.add_subparsers(dest="sandbox_command")
    sandbox_subparsers.add_parser(
        "build",
        help="Run the configured build command and commit the prepared image now",
    )
    sandbox_subparsers.add_parser(
        "status", aliases=["st"],
        help="Show the active build, configuration, and recent build history",
    )
    sandbox_subparsers.add_parser(
        "clear", aliases=["clean"],
        help="Remove all sandbox build images and metadata",
    )
    sandbox_parser.set_defaults(func=cmd_sandbox)
