def _dispatch_all_via_service_manager_if_s6(action: str) -> bool:
    """Inside a container with s6, dispatch ``--all`` lifecycle to every
    registered profile gateway.

    Returns True iff dispatched (caller should ``return``); False
    otherwise — caller continues with the host-side code path.

    Without this, ``hermes gateway stop --all`` and ``... restart --all``
    fall through to ``kill_gateway_processes(all_profiles=True)``, which
    just ``pkill``s every gateway process. s6-supervise observes the
    crash and restarts each one ~1s later — so ``--all`` ends up
    *kicking* every gateway instead of *stopping* it. By iterating
    ``list_profile_gateways()`` and sending the lifecycle command
    through the service manager we get the intended semantics (s6's
    ``want up``/``want down`` flips correctly so supervise stays down
    after a stop).

    ``action`` is one of ``stop`` / ``restart`` (``start --all`` isn't
    a supported CLI surface).
    """
    from hermes_cli.service_manager import (
        detect_service_manager,
        get_service_manager,
    )

    if detect_service_manager() != "s6":
        return False
    if action not in ("stop", "restart"):
        return False
    mgr = get_service_manager()
    profiles = mgr.list_profile_gateways()
    if not profiles:
        print("✗ No profile gateways registered under s6")
        return True
    fn = mgr.stop if action == "stop" else mgr.restart
    errors: list[tuple[str, Exception]] = []
    for profile in profiles:
        service_name = f"gateway-{profile}"
        try:
            fn(service_name)
        except Exception as exc:  # noqa: BLE001 — report and continue
            errors.append((profile, exc))
    succeeded = len(profiles) - len(errors)
    verb = "stopped" if action == "stop" else "restarted"
    if succeeded:
        print(f"✓ {verb.capitalize()} {succeeded} profile gateway(s) under s6")
    for profile, exc in errors:
        print(f"✗ Could not {action} gateway-{profile}: {exc}")
    return True
