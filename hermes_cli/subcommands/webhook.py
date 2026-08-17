"""``hermes webhook`` subcommand parser.

Extracted verbatim from ``hermes_cli/main.py:main()`` (god-file Phase 2).
Handler injected to avoid importing ``main``.
"""

from __future__ import annotations

from typing import Callable


def _add_profile_flag(parser) -> None:
    parser.add_argument(
        "--profile",
        default="",
        help="Profile whose webhook subscriptions to manage (default: active profile)",
    )


def _add_json_flag(parser) -> None:
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON (secrets are masked on read)",
    )


def build_webhook_parser(subparsers, *, cmd_webhook: Callable) -> None:
    """Attach the ``webhook`` subcommand to ``subparsers``."""
    # =========================================================================
    # webhook command
    # =========================================================================
    webhook_parser = subparsers.add_parser(
        "webhook",
        help="Manage dynamic webhook subscriptions",
        description="Create, list, and manage webhook subscriptions for event-driven agent activation",
    )
    webhook_subparsers = webhook_parser.add_subparsers(dest="webhook_action")

    wh_sub = webhook_subparsers.add_parser(
        "subscribe", aliases=["add", "create"], help="Create a webhook subscription"
    )
    wh_sub.add_argument("name", help="Route name (used in URL: /webhooks/<name>)")
    wh_sub.add_argument(
        "--prompt", default="", help="Prompt template with {dot.notation} payload refs"
    )
    wh_sub.add_argument(
        "--events", default="", help="Comma-separated event types to accept"
    )
    wh_sub.add_argument("--description", default="", help="What this subscription does")
    wh_sub.add_argument(
        "--skills", default="", help="Comma-separated skill names to load"
    )
    wh_sub.add_argument(
        "--deliver",
        default="log",
        help="Delivery target: log, telegram, discord, slack, etc.",
    )
    wh_sub.add_argument(
        "--deliver-chat-id",
        default="",
        help="Target chat ID for cross-platform delivery",
    )
    wh_sub.add_argument(
        "--secret", default="", help="HMAC secret (auto-generated if omitted)"
    )
    wh_sub.add_argument(
        "--deliver-only",
        action="store_true",
        help="Skip the agent — deliver the rendered prompt directly as the "
        "message. Zero LLM cost. Requires --deliver to be a real target "
        "(not 'log').",
    )
    wh_sub.add_argument(
        "--script",
        default="",
        help="Filter/transform script under ~/.hermes/scripts/. The route "
        "payload is passed as JSON on stdin; empty stdout, [SILENT], or a "
        "nonzero exit code ignores the webhook.",
    )
    wh_sub.add_argument(
        "--replace",
        action="store_true",
        help="Overwrite an existing route of the same name (default: error)",
    )
    _add_profile_flag(wh_sub)

    wh_list = webhook_subparsers.add_parser(
        "list", aliases=["ls"], help="List all dynamic subscriptions"
    )
    _add_profile_flag(wh_list)
    _add_json_flag(wh_list)

    wh_show = webhook_subparsers.add_parser(
        "show", help="Show one subscription's details"
    )
    wh_show.add_argument("name", help="Subscription name to show")
    _add_profile_flag(wh_show)
    _add_json_flag(wh_show)

    wh_upd = webhook_subparsers.add_parser(
        "update", help="Patch fields on an existing subscription"
    )
    wh_upd.add_argument("name", help="Subscription name to update")
    wh_upd.add_argument("--prompt", default="", help="New prompt template")
    wh_upd.add_argument("--events", default="", help="New comma-separated events")
    wh_upd.add_argument("--description", default="", help="New description")
    wh_upd.add_argument("--skills", default="", help="New comma-separated skills")
    wh_upd.add_argument("--deliver", default="", help="New delivery target")
    wh_upd.add_argument("--deliver-chat-id", default="", help="New target chat ID")
    _add_profile_flag(wh_upd)

    wh_enable = webhook_subparsers.add_parser(
        "enable", help="Enable a disabled subscription"
    )
    wh_enable.add_argument("name", help="Subscription name to enable")
    _add_profile_flag(wh_enable)

    wh_disable = webhook_subparsers.add_parser(
        "disable", help="Disable a subscription without removing it"
    )
    wh_disable.add_argument("name", help="Subscription name to disable")
    _add_profile_flag(wh_disable)

    wh_rotate = webhook_subparsers.add_parser(
        "rotate-secret", help="Rotate a subscription's HMAC secret"
    )
    wh_rotate.add_argument("name", help="Subscription name to rotate")
    _add_profile_flag(wh_rotate)

    wh_rm = webhook_subparsers.add_parser(
        "remove", aliases=["rm"], help="Remove a subscription"
    )
    wh_rm.add_argument("name", help="Subscription name to remove")
    _add_profile_flag(wh_rm)

    wh_test = webhook_subparsers.add_parser(
        "test", help="Send a test POST to a webhook route"
    )
    wh_test.add_argument("name", help="Subscription name to test")
    wh_test.add_argument(
        "--payload", default="", help="JSON payload to send (default: test payload)"
    )
    _add_profile_flag(wh_test)

    webhook_parser.set_defaults(func=cmd_webhook)
