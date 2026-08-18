"""Shared scheduler-readiness checks for cron management surfaces."""

from typing import Optional


_GATEWAY_NOT_RUNNING_WARNING = (
    "Gateway is not running — jobs won't fire automatically. "
    "Start it with `hermes gateway install` "
    "(`sudo hermes gateway install --system` on Linux servers); "
    "check with `hermes cron status`."
)


def active_cron_provider_name() -> str:
    """Return the resolved cron provider name without network access."""
    try:
        from cron.scheduler_provider import resolve_cron_scheduler

        return resolve_cron_scheduler().name or "builtin"
    except Exception:
        return "builtin"


def scheduler_readiness_warning(provider_name: Optional[str] = None) -> Optional[str]:
    """Return a warning when the built-in scheduler has no process driving it.

    External providers such as Chronos do not depend on the local gateway ticker,
    so an absent gateway is not evidence that those jobs will fail to fire.
    Readiness detection is best-effort: indeterminate state stays silent rather
    than producing a false warning.

    The Hermes Desktop app runs its own cron ticker
    (``_start_desktop_cron_ticker`` in ``hermes_cli/web_server.py``) without a
    gateway process, so ``HERMES_DESKTOP=1`` is treated as a valid readiness
    path — the warning would be a false alarm for desktop users.
    """
    try:
        if (provider_name or active_cron_provider_name()) != "builtin":
            return None

        import os

        if os.getenv("HERMES_DESKTOP") == "1":
            return None

        from hermes_cli.gateway import find_gateway_pids

        if find_gateway_pids():
            return None
    except Exception:
        return None

    return _GATEWAY_NOT_RUNNING_WARNING
