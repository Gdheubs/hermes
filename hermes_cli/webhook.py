"""hermes webhook — manage dynamic webhook subscriptions from the CLI.

Usage:
    hermes webhook subscribe <name> [options]
    hermes webhook list
    hermes webhook remove <name>
    hermes webhook test <name> [--payload '{"key": "value"}']

Subscriptions persist to ~/.hermes/webhook_subscriptions.json and are
hot-reloaded by the webhook adapter without a gateway restart.
"""

import json
import os
import re
import secrets
import tempfile
import time
from pathlib import Path
from typing import Dict

from hermes_constants import display_hermes_home
from utils import atomic_replace
from hermes_cli.config import cfg_get


_SUBSCRIPTIONS_FILENAME = "webhook_subscriptions.json"
_SUBSCRIPTIONS_FILE_MODE = 0o600


def _hermes_home() -> Path:
    from hermes_constants import get_hermes_home
    return get_hermes_home()


def _active_profile_name() -> str:
    """Return the effective profile name for webhook subscription storage."""
    try:
        from hermes_cli.profiles import get_active_profile_name
        name = get_active_profile_name()
        if name and name != "custom":
            return name
    except Exception:
        pass
    return "default"


def _profile_root(profile: str | None) -> Path:
    """Return the profile-scoped Hermes root for subscription storage.

    The default profile lives directly in HERMES_HOME; named profiles live
    under ``HERMES_HOME/profiles/<name>``.
    """
    home = _hermes_home()
    name = (profile or "").strip() or _active_profile_name()
    if not name or name == "default":
        return home
    return home / "profiles" / name


def _subscriptions_path(profile: str | None = None) -> Path:
    return _profile_root(profile) / _SUBSCRIPTIONS_FILENAME


def _load_subscriptions(profile: str | None = None) -> Dict[str, dict]:
    path = _subscriptions_path(profile)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_subscriptions(subs: Dict[str, dict], profile: str | None = None) -> None:
    path = _subscriptions_path(profile)
    path.parent.mkdir(parents=True, exist_ok=True)
    # webhook_subscriptions.json contains per-route HMAC secrets — write
    # via tempfile + chmod 0o600 before the atomic rename so a permissive
    # umask cannot leave the secrets readable to other local users in the
    # window between create and rename.
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        text=True,
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(subs, fh, indent=2, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_path, _SUBSCRIPTIONS_FILE_MODE)
        atomic_replace(tmp_path, path)
        # Re-assert after rename in case the destination existed with a
        # broader mode and atomic_replace preserved it.
        os.chmod(path, _SUBSCRIPTIONS_FILE_MODE)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _get_webhook_config() -> dict:
    """Load webhook platform config. Returns {} if not configured."""
    try:
        from hermes_cli.config import load_config
        cfg = load_config()
        return cfg_get(cfg, "platforms", "webhook", default={})
    except Exception:
        return {}


def _is_webhook_enabled() -> bool:
    return bool(_get_webhook_config().get("enabled"))


def _get_webhook_base_url() -> str:
    wh = _get_webhook_config().get("extra", {})
    host = wh.get("host")
    port = wh.get("port", 8644)
    display_host = "localhost" if not host or host in {"0.0.0.0", "::"} else host
    if ":" in display_host and not display_host.startswith("["):
        display_host = f"[{display_host}]"
    return f"http://{display_host}:{port}"


def _setup_hint() -> str:
    _dhh = display_hermes_home()
    return f"""
  Webhook platform is not enabled. To set it up:

  1. Run the gateway setup wizard:
     hermes gateway setup

  2. Or manually add to {_dhh}/config.yaml:
     platforms:
       webhook:
         enabled: true
         extra:
           port: 8644
           secret: "your-global-hmac-secret"

  3. Or set environment variables in {_dhh}/.env:
     WEBHOOK_ENABLED=true
     WEBHOOK_PORT=8644
     WEBHOOK_SECRET=your-global-secret

  Then start the gateway: hermes gateway run
"""


def _require_webhook_enabled() -> bool:
    """Check webhook is enabled. Print setup guide and return False if not."""
    if _is_webhook_enabled():
        return True
    print(_setup_hint())
    return False


def _args_profile(args) -> str | None:
    profile = getattr(args, "profile", "") or ""
    return profile.strip() or None


def _redact_secret(value: str) -> str:
    if not isinstance(value, str) or not value:
        return value
    if len(value) <= 8:
        return "***"
    return value[:4] + "..." + value[-4:]


def _route_for_json(name: str, route: dict, base_url: str) -> dict:
    return {
        "name": name,
        "description": route.get("description", ""),
        "enabled": route.get("enabled", True),
        "events": route.get("events", []),
        "deliver": route.get("deliver", "log"),
        "deliver_only": bool(route.get("deliver_only")),
        "script": route.get("script"),
        "url": f"{base_url}/webhooks/{name}",
        # Secret is never emitted verbatim on read; masked for safety.
        "secret_masked": _redact_secret(str(route.get("secret", ""))),
    }


def webhook_command(args):
    """Entry point for 'hermes webhook' subcommand."""
    sub = getattr(args, "webhook_action", None)

    if not sub:
        print("Usage: hermes webhook {subscribe|list|show|update|enable|disable|rotate-secret|remove|test}")
        print("Run 'hermes webhook --help' for details.")
        return

    if not _require_webhook_enabled():
        return

    if sub in {"subscribe", "add", "create"}:
        _cmd_subscribe(args)
    elif sub in {"list", "ls"}:
        _cmd_list(args)
    elif sub == "show":
        _cmd_show(args)
    elif sub == "update":
        _cmd_update(args)
    elif sub == "enable":
        _cmd_set_enabled(args, True)
    elif sub == "disable":
        _cmd_set_enabled(args, False)
    elif sub == "rotate-secret":
        _cmd_rotate_secret(args)
    elif sub in {"remove", "rm"}:
        _cmd_remove(args)
    elif sub == "test":
        _cmd_test(args)


def _cmd_subscribe(args):
    name = args.name.strip().lower().replace(" ", "-")
    if not re.match(r'^[a-z0-9][a-z0-9_-]*$', name):
        print(f"Error: Invalid name '{name}'. Use lowercase alphanumeric with hyphens/underscores.")
        return

    profile = _args_profile(args)
    subs = _load_subscriptions(profile)
    is_update = name in subs
    if is_update and not getattr(args, "replace", False):
        print(
            f"Error: A subscription named '{name}' already exists. "
            f"Use 'hermes webhook update {name}' to patch fields, or pass "
            f"--replace to overwrite."
        )
        return

    secret = args.secret or secrets.token_urlsafe(32)
    events = [e.strip() for e in args.events.split(",")] if args.events else []

    route = {
        "description": args.description or f"Agent-created subscription: {name}",
        "events": events,
        "secret": secret,
        "prompt": args.prompt or "",
        "skills": [s.strip() for s in args.skills.split(",")] if args.skills else [],
        "deliver": args.deliver or "log",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    if getattr(args, "deliver_only", False):
        if route["deliver"] == "log":
            print(
                "Error: --deliver-only requires --deliver to be a real target "
                "(telegram, discord, slack, github_comment, etc.) — not 'log'."
            )
            return
        route["deliver_only"] = True

    script = getattr(args, "script", "") or ""
    if script.strip():
        route["script"] = script.strip()

    if args.deliver_chat_id:
        route["deliver_extra"] = {"chat_id": args.deliver_chat_id}

    subs[name] = route
    _save_subscriptions(subs, profile)

    base_url = _get_webhook_base_url()
    status = "Updated" if is_update else "Created"

    print(f"\n  {status} webhook subscription: {name}")
    print(f"  URL:    {base_url}/webhooks/{name}")
    print(f"  Secret: {secret}")
    if events:
        print(f"  Events: {', '.join(events)}")
    else:
        print("  Events: (all)")
    print(f"  Deliver: {route['deliver']}")
    if route.get("deliver_only"):
        print("  Mode: direct delivery (no agent, zero LLM cost)")
    if route.get("prompt"):
        prompt_preview = route["prompt"][:80] + ("..." if len(route["prompt"]) > 80 else "")
        label = "Message" if route.get("deliver_only") else "Prompt"
        print(f"  {label}: {prompt_preview}")
    if route.get("script"):
        print(f"  Script: {route['script']}")
    print("\n  Configure your service to POST to the URL above.")
    print("  Use the secret for HMAC-SHA256 signature validation.")
    print("  The gateway must be running to receive events (hermes gateway run).\n")


def _cmd_show(args):
    name = args.name.strip().lower()
    subs = _load_subscriptions(_args_profile(args))
    if name not in subs:
        print(f"  No subscription named '{name}'.")
        return
    route = subs[name]
    base_url = _get_webhook_base_url()
    if getattr(args, "json", False):
        print(json.dumps(_route_for_json(name, route, base_url), indent=2))
        return
    events = ", ".join(route.get("events", [])) or "(all)"
    deliver = route.get("deliver", "log")
    if route.get("deliver_only"):
        deliver = f"{deliver} (direct — no agent)"
    enabled = "yes" if route.get("enabled", True) else "no"
    print(f"\n  ◆ {name}")
    if route.get("description"):
        print(f"    {route['description']}")
    print(f"    URL:       {base_url}/webhooks/{name}")
    print(f"    Enabled:   {enabled}")
    print(f"    Events:    {events}")
    print(f"    Deliver:   {deliver}")
    print(f"    Secret:    {_redact_secret(str(route.get('secret', ''))) or '(none)'}")
    if route.get("script"):
        print(f"    Script:    {route['script']}")
    if route.get("skills"):
        print(f"    Skills:    {', '.join(route['skills'])}")
    if route.get("prompt"):
        print(f"    Prompt:    {route['prompt'][:80]}")
    print()


def _cmd_update(args):
    name = args.name.strip().lower()
    subs = _load_subscriptions(_args_profile(args))
    if name not in subs:
        print(f"  No subscription named '{name}'.")
        return
    route = subs[name]
    if args.prompt:
        route["prompt"] = args.prompt
    if args.events:
        route["events"] = [e.strip() for e in args.events.split(",") if e.strip()]
    if args.description:
        route["description"] = args.description
    if args.skills:
        route["skills"] = [s.strip() for s in args.skills.split(",") if s.strip()]
    if args.deliver:
        route["deliver"] = args.deliver
    if args.deliver_chat_id:
        route["deliver_extra"] = {"chat_id": args.deliver_chat_id}
    _save_subscriptions(subs, _args_profile(args))
    print(f"  Updated webhook subscription: {name}")


def _cmd_set_enabled(args, enabled: bool):
    name = args.name.strip().lower()
    subs = _load_subscriptions(_args_profile(args))
    if name not in subs:
        print(f"  No subscription named '{name}'.")
        return
    subs[name]["enabled"] = enabled
    _save_subscriptions(subs, _args_profile(args))
    action = "Enabled" if enabled else "Disabled"
    print(f"  {action} webhook subscription: {name}")


def _cmd_rotate_secret(args):
    name = args.name.strip().lower()
    subs = _load_subscriptions(_args_profile(args))
    if name not in subs:
        print(f"  No subscription named '{name}'.")
        return
    new_secret = secrets.token_urlsafe(32)
    subs[name]["secret"] = new_secret
    _save_subscriptions(subs, _args_profile(args))
    print(f"  Rotated secret for {name}.")
    print(f"  New secret (shown once): {new_secret}")
    print("  Store this in your provider's webhook configuration.")


def _cmd_list(args):
    profile = _args_profile(args)
    subs = _load_subscriptions(profile)
    if not subs:
        print("  No dynamic webhook subscriptions.")
        print("  Create one with: hermes webhook subscribe <name>")
        return

    base_url = _get_webhook_base_url()
    if getattr(args, "json", False):
        payload = [_route_for_json(n, r, base_url) for n, r in subs.items()]
        print(json.dumps(payload, indent=2))
        return
    print(f"\n  {len(subs)} webhook subscription(s):\n")
    for name, route in subs.items():
        events = ", ".join(route.get("events", [])) or "(all)"
        deliver = route.get("deliver", "log")
        if route.get("deliver_only"):
            deliver = f"{deliver} (direct — no agent)"
        desc = route.get("description", "")
        print(f"  ◆ {name}")
        if desc:
            print(f"    {desc}")
        print(f"    URL:     {base_url}/webhooks/{name}")
        print(f"    Events:  {events}")
        print(f"    Deliver: {deliver}")
        if route.get("script"):
            print(f"    Script:  {route['script']}")
        print()


def _cmd_remove(args):
    name = args.name.strip().lower()
    profile = _args_profile(args)
    subs = _load_subscriptions(profile)

    if name not in subs:
        print(f"  No subscription named '{name}'.")
        print("  Note: Static routes from config.yaml cannot be removed here.")
        return

    del subs[name]
    _save_subscriptions(subs, profile)
    print(f"  Removed webhook subscription: {name}")


def _cmd_test(args):
    """Send a test POST to a webhook route."""
    name = args.name.strip().lower()
    subs = _load_subscriptions(_args_profile(args))

    if name not in subs:
        print(f"  No subscription named '{name}'.")
        return

    route = subs[name]
    secret = route.get("secret", "")
    base_url = _get_webhook_base_url()
    url = f"{base_url}/webhooks/{name}"

    payload = args.payload or '{"test": true, "event_type": "test", "message": "Hello from hermes webhook test"}'

    import hmac
    import hashlib
    sig = "sha256=" + hmac.new(
        secret.encode(), payload.encode(), hashlib.sha256
    ).hexdigest()

    print(f"  Sending test POST to {url}")
    try:
        import urllib.request
        req = urllib.request.Request(
            url,
            data=payload.encode(),
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": sig,
                "X-GitHub-Event": "test",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode()
            print(f"  Response ({resp.status}): {body}")
    except Exception as e:
        print(f"  Error: {e}")
        print("  Is the gateway running? (hermes gateway run)")
