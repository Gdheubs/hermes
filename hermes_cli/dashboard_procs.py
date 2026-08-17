"""Dashboard process-hygiene helpers — extracted from ``hermes_cli/main.py``.

Mechanical move (main.py decomposition): the three leaf process-hygiene
helpers (``_scan_dashboard_processes``, ``_kill_stale_dashboard_processes``,
``_detect_concurrent_hermes_instances``) are lifted verbatim. References to
helpers that STAY in ``hermes_cli.main`` (``_find_stale_dashboard_pids``,
``_respawn_dashboard_processes``, ``_is_windows``, ...) are routed through a
lazy ``_m()`` main reference so existing test monkeypatches on
``hermes_cli.main.<name>`` keep reaching this code path, and imports stay
one-way at import time (main.py imports this module, never the reverse).
``main.py`` re-exports all three names (``# noqa: F401``) so callers and test
patches on ``hermes_cli.main`` resolve unchanged.
"""

import os
import subprocess
import sys
from pathlib import Path


def _m():
    """Lazy ``hermes_cli.main`` reference (call-time; keeps patches working)."""
    from hermes_cli import main

    return main


def _scan_dashboard_processes(
    *,
    exclude_pids: set[int] | None = None,
) -> list[tuple[int, str]]:
    """Return matching ``dashboard``/``serve`` processes with their cmdlines.

    ``hermes dashboard`` is a long-lived server process commonly started and
    forgotten.  When ``hermes update`` replaces files on disk, the running
    process keeps the old Python backend in memory while the JS bundle on
    disk is updated, causing a silent frontend/backend mismatch (e.g. new
    auth headers the old backend doesn't recognise → every API call 401s).

    The dashboard may be manually started or managed by the optional
    ``hermes-dashboard.service`` systemd unit.  Managed units are restarted
    through their owning systemd scope; only manually-started processes use
    the kill path because we can't know their original launch args.

    *exclude_pids* is an optional set of PIDs that must never be returned.
    This is used by the Hermes Desktop Electron app to protect its own
    backend child process: when the desktop spawns ``hermes serve`` as
    a backend and triggers an auto-update, the update must not kill the
    backend that the desktop itself manages.  The desktop sets the
    environment variable ``HERMES_DESKTOP_CHILD_PID`` on the spawned
    backend process; ``_kill_stale_dashboard_processes`` reads it and
    passes it here.  (#37532)

    Returns an empty list on any scan error (missing ps/wmic, timeout, etc.).
    """
    patterns = [
        "hermes dashboard",
        "hermes_cli.main dashboard",
        "hermes_cli/main.py dashboard",
        # The headless backend (`hermes serve`) is the same long-lived server
        # under a different command name — the desktop app spawns it. Reap it
        # on update for the same frontend/backend-mismatch reason.
        "hermes serve",
        "hermes_cli.main serve",
        "hermes_cli/main.py serve",
    ]
    self_pid = os.getpid()
    dashboard_processes: list[tuple[int, str]] = []

    try:
        if sys.platform == "win32":
            # wmic may emit text in the system code page (for example cp936
            # on zh-CN systems), not UTF-8. In text mode, subprocess output
            # decoding depends on Python's configuration (locale-dependent
            # by default, or UTF-8 in UTF-8 mode). The important protection
            # here is errors="ignore": it prevents a reader-thread
            # UnicodeDecodeError from leaving result.stdout=None and turning
            # the later .split() into an AttributeError (#17049).
            # bounded_probe_run (rather than subprocess.run with a timeout)
            # keeps a slow scan from wedging the caller forever: run()'s
            # post-timeout cleanup joins the pipe reader threads unbounded,
            # and a conhost.exe descendant holding duplicated pipe handles
            # blocks that join indefinitely (#87134). It also passes
            # CREATE_NO_WINDOW: this scan can run from the windowless
            # pythonw.exe desktop/gateway backend during an update, where a
            # bare wmic spawn would pop a console window.
            from hermes_cli._subprocess_compat import bounded_probe_run

            result = bounded_probe_run(
                ["wmic", "process", "get", "ProcessId,CommandLine", "/FORMAT:LIST"],
                timeout=10,
                errors="ignore",
            )
            if result is None or result.returncode != 0 or result.stdout is None:
                return []
            current_cmd = ""
            for line in result.stdout.split("\n"):
                line = line.strip()
                if line.startswith("CommandLine="):
                    current_cmd = line[len("CommandLine=") :]
                elif line.startswith("ProcessId="):
                    pid_str = line[len("ProcessId=") :]
                    if (
                        any(p in current_cmd for p in patterns)
                        and int(pid_str) != self_pid
                    ):
                        try:
                            dashboard_processes.append((int(pid_str), current_cmd))
                        except ValueError:
                            pass
        else:
            # Linux / macOS: scan the process table via ps and match against
            # the same explicit patterns list used on Windows.  Using ps
            # (rather than `pgrep -f "hermes.*dashboard"`) keeps us consistent
            # with `hermes_cli.gateway._scan_gateway_pids` and avoids the
            # greedy regex matching unrelated cmdlines that merely contain
            # both words (e.g. a chat session discussing "dashboard").
            result = subprocess.run(
                ["ps", "-A", "-o", "pid=,command="],
                capture_output=True,
                text=True, encoding="utf-8", errors="replace",
                timeout=10,
            )
            if result.returncode == 0:
                for line in getattr(result, "stdout", "").split("\n"):
                    stripped = line.strip()
                    if not stripped or "grep" in stripped:
                        continue
                    parts = stripped.split(None, 1)
                    if len(parts) != 2:
                        continue
                    try:
                        pid = int(parts[0])
                    except ValueError:
                        continue
                    command = parts[1]
                    if any(p in command for p in patterns) and pid != self_pid:
                        dashboard_processes.append((pid, command))
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return []

    if exclude_pids:
        dashboard_processes = [
            proc for proc in dashboard_processes if proc[0] not in exclude_pids
        ]
    return dashboard_processes


def _hermes_home_for_pid(pid: int) -> str | None:
    """Best-effort ``HERMES_HOME`` from *pid*'s environment."""
    try:
        import psutil

        home = psutil.Process(pid).environ().get("HERMES_HOME")
        if home:
            return home
    except Exception:
        pass
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes()
    except (OSError, PermissionError):
        return None
    for part in raw.split(b"\x00"):
        if part.startswith(b"HERMES_HOME="):
            return part.split(b"=", 1)[1].decode("utf-8", errors="replace") or None
    return None


def _is_ephemeral_port_zero_backend(argv: list[str]) -> bool:
    """True for Desktop-style ``serve|dashboard --port 0`` backends (#78821).

    Ephemeral-port backends are owned by Hermes Desktop (or become PPID-1
    orphans after a prior update respawn).  Replaying them after
    ``hermes update`` multiplies listening backends because ``--port 0``
    always binds a fresh free port.  Covers both ``serve`` and the legacy
    ``dashboard --no-open`` fallback older Desktop runtimes use.
    """
    if _dashboard_subcommand_index(argv) is None:
        return False
    for i, tok in enumerate(argv):
        if tok == "--port" and i + 1 < len(argv) and str(argv[i + 1]) == "0":
            return True
        if tok.startswith("--port=") and tok.split("=", 1)[1].strip() == "0":
            return True
    return False


def _dashboard_subcommand_index(argv: list[str]) -> int | None:
    for i, tok in enumerate(argv):
        if tok in ("serve", "dashboard"):
            return i
    return None


def _normalize_dashboard_cmdline(argv: list[str]) -> tuple[str, ...]:
    """Collapse argv to profile flags + serve/dashboard tail for dedupe."""
    idx = _dashboard_subcommand_index(argv)
    if idx is None:
        return tuple(argv)
    prefix: list[str] = []
    i = 0
    while i < idx:
        tok = argv[i]
        if tok in ("--profile", "-p") and i + 1 < idx:
            prefix.extend([tok, argv[i + 1]])
            i += 2
            continue
        if tok.startswith("--profile="):
            prefix.append(tok)
        i += 1
    return tuple(prefix + list(argv[idx:]))


def _profile_key_for_respawn(
    argv: list[str], hermes_home: str | None = None
) -> str:
    """Stable owner key: ``HERMES_HOME`` when known, else ``--profile`` / ``-p``.

    ``HERMES_HOME`` ending in ``profiles/<name>`` is normalized to
    ``profile:<name>`` so it shares a cap with an explicit ``--profile``
    flag for the same profile (#78821).  Non-profile homes (including
    distinct ``…/.hermes`` roots) keep a resolved ``home:`` key so
    unrelated installs do not collapse together.
    """
    profile_name: str | None = None
    for i, tok in enumerate(argv):
        if tok in ("--profile", "-p") and i + 1 < len(argv):
            profile_name = argv[i + 1]
            break
        if tok.startswith("--profile="):
            profile_name = tok.split("=", 1)[1]
            break

    if hermes_home:
        try:
            home_path = Path(hermes_home).resolve()
        except (OSError, RuntimeError, ValueError):
            home_path = Path(hermes_home)
        parts = home_path.parts
        if len(parts) >= 2 and parts[-2] == "profiles" and parts[-1]:
            return f"profile:{parts[-1]}"
        try:
            return f"home:{os.path.normcase(str(home_path))}"
        except (OSError, RuntimeError, ValueError):
            return f"home:{os.path.normcase(hermes_home)}"

    if profile_name:
        return f"profile:{profile_name}"
    return "profile:default"


def _filter_dashboard_respawn_candidates(
    candidates: list[tuple[int, list[str], str | None]],
) -> list[list[str]]:
    """Select which killed manual backends to respawn after ``hermes update``.

    Each candidate is ``(pid, argv, hermes_home)``.

    Rules (#78821):
    1. Never resurrect Desktop ephemeral ``serve|dashboard --port 0``
       backends — Desktop (``HERMES_DESKTOP_CHILD_PID``) owns their
       lifecycle.  These are also the PPID-1 orphans that previously
       multiplied across updates because ``--port 0`` always binds a
       fresh free port.
    2. Dedupe by normalized cmdline (identical argv → one respawn).
    3. Cap at most one managed backend per profile / ``HERMES_HOME``.

    Intentionally does **not** blanket-skip every PPID-1 process: a prior
    ``hermes update`` respawn detaches with ``start_new_session=True``, so
    fixed-port manual backends are reparented to init and must still be
    eligible for the next update's #40449 restart.
    """
    selected: list[list[str]] = []
    seen_cmdlines: set[tuple[str, ...]] = set()
    seen_profiles: set[str] = set()

    for _pid, argv, hermes_home in candidates:
        if not argv:
            continue
        if _is_ephemeral_port_zero_backend(argv):
            continue
        norm = _normalize_dashboard_cmdline(argv)
        if norm in seen_cmdlines:
            continue
        profile_key = _profile_key_for_respawn(argv, hermes_home)
        if profile_key in seen_profiles:
            continue
        seen_cmdlines.add(norm)
        seen_profiles.add(profile_key)
        selected.append(list(argv))

    return selected


def _kill_stale_dashboard_processes(
    reason: str = "the running backend no longer matches the updated frontend",
    *,
    restart_managed: bool = False,
    already_restarted_units: "set[str] | None" = None,
) -> dict[str, list]:
    """Kill running ``hermes dashboard`` / ``hermes serve`` processes.

    Called at the end of ``hermes update`` (default ``reason``) and also
    from ``hermes dashboard --stop`` (which overrides ``reason``).  The
    dashboard has no service manager, so after a code update the running
    process is guaranteed to be serving stale Python against a
    freshly-updated JS bundle.  Leaving it alive produces silent
    frontend/backend mismatches (new auth headers the old backend doesn't
    recognise → every API call 401s).

    POSIX: SIGTERM, wait up to ~3s for graceful exit, SIGKILL any survivors.
    Windows: ``taskkill /PID <pid> /F`` since there's no clean SIGTERM
    equivalent for background console apps.

    Manually-started dashboards are not auto-restarted because we don't know
    the original launch args (--host, --port, --insecure, --tui, --no-open).
    When ``restart_managed`` is true (the ``hermes update`` path), a detected
    ``hermes-dashboard.service`` is restarted through systemd; any OTHER
    killed PID that was supervised by a systemd unit (custom unit names —
    e.g. a remote backend's ``hermes-serve.service``) has its owning unit
    restarted after the kill, because systemd treats our SIGTERM as a clean
    stop and ``Restart=on-failure`` would never fire (#68934).

    *already_restarted_units* names units (no ``.service`` suffix) the
    caller already restarted directly — e.g. ``hermes update``'s systemd
    fleet-restart loop, which restarts ``hermes-serve*`` units before this
    function runs. Without excluding them, a Serve-only install's freshly
    restarted process is found again here and restarted a second time for
    no benefit (review on #83595). PIDs owned by one of these units are
    left untouched.
    """
    if restart_managed and _m()._restart_managed_dashboard_service(reason):
        return {"matched": [], "killed": [], "failed": []}

    # When the Hermes Desktop Electron app spawns this dashboard as a
    # backend child, it sets HERMES_DESKTOP_CHILD_PID so that the update
    # path can skip killing the desktop-managed process.  (#37532)
    exclude: set[int] | None = None
    raw_pid = os.environ.get("HERMES_DESKTOP_CHILD_PID")
    if raw_pid:
        # The desktop may manage several backends (one per active profile) and
        # passes them comma-separated; a lone int still parses for back-compat.
        parsed: set[int] = set()
        for part in raw_pid.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                parsed.add(int(part))
            except (ValueError, TypeError):
                pass
        if parsed:
            exclude = parsed

    pids = _m()._find_stale_dashboard_pids(exclude_pids=exclude)
    if not pids:
        return {"matched": [], "killed": [], "failed": []}

    # Before killing, snapshot systemd cgroup info for each PID so we can
    # restart supervised services after the kill (the cgroup disappears
    # along with the process).  Only meaningful on Linux, and only when the
    # caller asked for restarts (the `hermes update` path) — `--stop` must
    # stay a stop, not a restart.
    pid_cgroup: dict[int, str | None] = {}
    pid_service: dict[int, str | None] = {}
    pid_cmdline: dict[int, list[str]] = {}
    pid_home: dict[int, str | None] = {}
    if restart_managed and sys.platform != "win32":
        for pid in pids:
            cg_path = _m()._get_pid_cgroup_path(pid)
            pid_cgroup[pid] = cg_path
            pid_service[pid] = _m()._get_systemd_service_for_pid(pid)
            if not pid_service[pid]:
                # Manually-started process: preserve its exact argv so we
                # can respawn it after the update (#40449, #68934).
                # Snapshot HERMES_HOME before the kill so per-profile caps
                # still work after the process is gone (#78821).
                cmdline = _m()._dashboard_cmdline_for_pid(pid)
                if cmdline:
                    pid_cmdline[pid] = cmdline
                    pid_home[pid] = _hermes_home_for_pid(pid)

        if already_restarted_units:
            # Already handled directly by the caller (e.g. hermes update's
            # systemd fleet-restart loop) — leave these alone instead of
            # killing and re-restarting a process that's already fresh.
            pids = [
                pid
                for pid in pids
                if (pid_service.get(pid) or "").removesuffix(".service")
                not in already_restarted_units
            ]
            if not pids:
                return {"matched": [], "killed": [], "failed": []}

    print()
    print(f"⟲ Stopping {len(pids)} dashboard process(es) ({reason})")

    killed: list[int] = []
    failed: list[tuple[int, str]] = []

    if sys.platform == "win32":
        for pid in pids:
            try:
                result = subprocess.run(
                    ["taskkill", "/PID", str(pid), "/F"],
                    capture_output=True,
                    text=True, encoding="utf-8", errors="replace",
                    timeout=10,
                )
                if result.returncode == 0:
                    killed.append(pid)
                else:
                    failed.append((pid, (result.stderr or result.stdout or "").strip()))
            except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as e:
                failed.append((pid, str(e)))
    else:
        import signal as _signal
        import time as _time

        # SIGTERM first — give each process a chance to shut down cleanly
        # (uvicorn closes its socket, flushes logs, etc.).
        for pid in pids:
            try:
                os.kill(pid, _signal.SIGTERM)
            except ProcessLookupError:
                # Already gone — count as killed.
                killed.append(pid)
            except (PermissionError, OSError) as e:
                failed.append((pid, str(e)))

        # Poll for exit up to ~3s total.
        deadline = _time.monotonic() + 3.0
        pending = [
            p for p in pids if p not in killed and p not in {f[0] for f in failed}
        ]
        while pending and _time.monotonic() < deadline:
            _time.sleep(0.1)
            still_pending = []
            # On Windows, os.kill(pid, 0) is NOT a no-op. Route through
            # the cross-platform existence check.
            from gateway.status import _pid_exists
            for pid in pending:
                if _pid_exists(pid):
                    still_pending.append(pid)
                else:
                    killed.append(pid)
            pending = still_pending

        # SIGKILL any survivors.
        for pid in pending:
            try:
                os.kill(pid, _signal.SIGKILL)
                killed.append(pid)
            except ProcessLookupError:
                killed.append(pid)
            except (PermissionError, OSError) as e:
                failed.append((pid, str(e)))

    for pid in killed:
        print(f"    ✓ stopped PID {pid}")
    for pid, err_msg in failed:
        print(f"    ✗ failed to stop PID {pid}: {err_msg}")

    # Restart what we just killed (update path only).  Two categories:
    #  - systemd-supervised PIDs: restart the owning unit.  Without this, a
    #    remote backend (hermes serve) under Restart=on-failure never comes
    #    back after our clean SIGTERM, and the Desktop can't reconnect (#68934).
    #  - manually-started PIDs: respawn the argv captured before the kill
    #    (#40449) — detached, headless, logged to logs/dashboard-restart.log.
    #    Filtered so Desktop ``serve|dashboard --port 0`` backends are not
    #    resurrected and duplicates collapse to one per profile (#78821).
    restarted_services: list[str] = []
    unrecovered: list[int] = []
    if killed and restart_managed:
        failed_restarts: list[tuple[str, str]] = []
        seen_services: set[str] = set()
        respawn_candidates: list[tuple[int, list[str], str | None]] = []
        for pid in killed:
            svc_name = pid_service.get(pid)
            if svc_name:
                if svc_name in seen_services:
                    continue
                seen_services.add(svc_name)
                if _m()._try_restart_systemd_service(svc_name, pid_cgroup.get(pid)):
                    restarted_services.append(svc_name)
                else:
                    failed_restarts.append((svc_name, "systemctl restart returned non-zero"))
                    unrecovered.append(pid)
            elif pid in pid_cmdline:
                respawn_candidates.append(
                    (pid, pid_cmdline[pid], pid_home.get(pid))
                )
            else:
                unrecovered.append(pid)

        for svc in restarted_services:
            print(f"    ✓ restarted systemd service {svc}")
        for svc, err in failed_restarts:
            print(f"    ⚠ {svc}: {err}")

        respawn_cmds = _filter_dashboard_respawn_candidates(respawn_candidates)
        if respawn_cmds:
            failed_cmds = _m()._respawn_dashboard_processes(respawn_cmds)
            if failed_cmds:
                unrecovered.extend(p for p in killed if pid_cmdline.get(p) in failed_cmds)

        if failed_restarts or unrecovered:
            print("  Restart anything not auto-restarted when you're ready:")
            print("    hermes dashboard --port <port>")
    elif killed:
        unrecovered = list(killed)
        print("  Restart the dashboard when you're ready:")
        print("    hermes dashboard --port <port>")

    return {
        "matched": list(pids),
        "killed": list(killed),
        "failed": list(failed),
        "unrecovered": list(unrecovered),
    }

def _detect_concurrent_hermes_instances(
    scripts_dir: Path, *, exclude_pid: int | None = None
) -> list[tuple[int, str]]:
    """Find other live processes whose .exe is one of our entry-point shims.

    Windows blocks DELETE/REPLACE on a running .exe — and even RENAME on the
    same .exe when another process opened it without ``FILE_SHARE_DELETE``.
    The Hermes Desktop Electron app spawns ``hermes.EXE`` as a backend child,
    so during ``hermes update`` the user-invoked process and the desktop's
    child both hold the same file. The quarantine rename then fails with
    ``[WinError 32]`` and uv inherits the lock.

    This helper enumerates processes whose ``exe`` matches one of the venv's
    shims (``hermes.exe`` / ``hermes-gateway.exe``) and returns ``(pid,
    process_name)`` pairs. The caller's own PID and its entire ancestor
    chain are excluded so the running ``hermes update`` invocation never
    reports itself — this matters on Windows where the setuptools .exe
    launcher (``hermes.exe``) is a separate process from the Python
    interpreter it loads (``python.exe``).

    Returns an empty list off-Windows, on missing psutil, or when no other
    instances exist. Never raises — process enumeration is best-effort.
    """
    if not _m()._is_windows():
        return []

    try:
        import psutil
    except Exception:
        return []

    # Resolve every shim path to its canonical form once for cheap comparison.
    shim_paths: set[str] = set()
    for shim in _m()._hermes_exe_shims(scripts_dir):
        try:
            shim_paths.add(str(shim.resolve()).lower())
        except OSError:
            shim_paths.add(str(shim).lower())
    if not shim_paths:
        return []

    # Build a set of PIDs to exclude: the Python process itself plus every
    # ancestor whose executable is one of our shims. On Windows the
    # setuptools-generated hermes.exe launcher is a separate native process
    # that spawns python.exe (the interpreter that runs our code).
    # os.getpid() returns the Python PID, but the launcher (which holds the
    # file lock) is the parent. Without excluding it, every ``hermes update``
    # reports its own launcher as a concurrent instance — a false positive
    # (issues #29341, #34795).
    #
    # Two robustness points learned from the field:
    #   1. Use ``proc.parents()`` — it returns the WHOLE ancestor list in one
    #      call. The earlier per-hop ``current.parent()`` loop bailed on the
    #      first psutil error (AccessDenied/NoSuchProcess is common on Windows
    #      across session/elevation boundaries), leaving the launcher shim in
    #      the candidate set and re-triggering the false positive.
    #   2. Only exclude ancestors whose exe is itself a shim. A genuine second
    #      hermes.exe sitting *under* a non-Hermes parent (e.g. a Hermes
    #      Desktop backend child) must still be flagged, so we don't blanket-
    #      exclude unrelated ancestors like the shell or terminal.
    # Broad ``except Exception`` guards against partially-stubbed psutil in
    # unit tests; this helper is documented as "never raises".
    if exclude_pid is not None:
        exclude_pids: set[int] = {int(exclude_pid)}
    else:
        exclude_pids = {os.getpid()}
    try:
        seed = next(iter(exclude_pids))
        try:
            ancestors = psutil.Process(seed).parents()
        except Exception:
            ancestors = []
        for ancestor in ancestors:
            try:
                anc_exe = ancestor.exe()
            except Exception:
                continue
            if not anc_exe:
                continue
            try:
                anc_norm = str(Path(anc_exe).resolve()).lower()
            except (OSError, ValueError):
                anc_norm = str(anc_exe).lower()
            if anc_norm in shim_paths:
                try:
                    exclude_pids.add(int(ancestor.pid))
                except Exception:
                    continue
    except Exception:
        pass

    matches: list[tuple[int, str]] = []
    try:
        proc_iter = psutil.process_iter(["pid", "exe", "name"])
    except Exception:
        return []

    for proc in proc_iter:
        try:
            info = proc.info
        except Exception:
            continue
        pid = info.get("pid")
        exe = info.get("exe")
        if not exe or pid is None or pid in exclude_pids:
            continue
        try:
            exe_norm = str(Path(exe).resolve()).lower()
        except (OSError, ValueError):
            exe_norm = str(exe).lower()
        if exe_norm in shim_paths:
            name = info.get("name") or Path(exe).name
            matches.append((int(pid), str(name)))

    return matches


def _is_desktop_local_serve_cmdline(command: str) -> bool:
    """True for the Desktop-local serve spawn shape (loopback + ephemeral port).

    Desktop primary/pool backends launch as::

        hermes serve --host 127.0.0.1 --port 0
        hermes serve --isolated --host 127.0.0.1 --port 0 ...

    Intentional long-lived headless serves (e.g. ``--host <tailscale-ip>
    --port 9119``) must never match — those are operator-managed remote
    backends and may legitimately run with ppid 1 under launchd/nohup.
    """
    cmd = command.lower()
    if "serve" not in cmd:
        return False
    if "hermes" not in cmd and "hermes_cli" not in cmd:
        return False
    # Ephemeral desktop bind: host loopback + port 0 (exact tokens).
    has_loopback = (
        "--host 127.0.0.1" in cmd
        or "--host=127.0.0.1" in cmd
        or "--host localhost" in cmd
        or "--host=localhost" in cmd
    )
    has_ephemeral = "--port 0" in cmd or "--port=0" in cmd
    if not (has_loopback and has_ephemeral):
        return False
    # Spare anything with a concrete non-zero port flag first (defensive).
    # (port 0 already required above.)
    return True


# --- Orphaned TUI node reaper ------------------------------------------------
#
# ``hermes desktop`` / ``hermes --tui`` spawn a ``node`` TUI process
# (``node ui-tui/dist/entry.js`` via _make_tui_argv). When the parent
# ``hermes``/``python`` process exits uncleanly, the node child survives and
# keeps emitting prompt_toolkit status chrome to a shared terminal, producing
# the stacked/repeated status frames seen on Windows.
#
# Safety is the whole point: this MUST NOT kill anything except a Hermes TUI
# node that is a true orphan. Two independent gates enforce that:
#   (1) the executable must be verified Node (node / node.exe) — a process
#       whose cmdline merely *mentions* ``ui-tui/dist/entry`` (a python
#       shell, a notepad.exe, a grep) is never selected;
#   (2) the node must be a true orphan — its launching parent has exited.

# The launcher (_launch_tui in main.py) only ever launches these concrete
# entry scripts (each via `node --expose-gc <entry>`):
#   1. <ui-tui>/dist/entry.js             (development checkout / build)
#   2. <HERMES_TUI_DIR>/dist/entry.js     (external prebuilt, via env)
#   3. <hermes_cli>/tui_dist/entry.js     (wheel-bundled prebuilt)
# We match ONLY those normalized, approved paths (not generic fragments), so an
# unrelated `node --expose-gc /tmp/unrelated/dist/entry.js` is never reaped
# (Greptile P1: generic entry paths match unrelated nodes).
def _hermes_tui_entry_paths() -> set[str]:
    """Return the normalized absolute entry.js paths Hermes can launch as TUI.

    These are exactly the paths _launch_tui may pass to `node --expose-gc`.
    The reaper matches a candidate cmdline's entry path against this set.
    """
    paths: set[str] = set()
    # (3) wheel-bundled prebuilt: <hermes_cli>/tui_dist/entry.js
    try:
        bundled = Path(__file__).resolve().parent / "tui_dist" / "entry.js"
        paths.add(str(bundled))
    except Exception:
        pass
    # (2) external prebuilt via HERMES_TUI_DIR
    ext = os.environ.get("HERMES_TUI_DIR")
    if ext:
        try:
            paths.add(str(Path(ext).resolve() / "dist" / "entry.js"))
        except Exception:
            pass
    # (1) development checkout: <HERMES_HOME>/ui-tui/dist/entry.js
    home = os.environ.get("HERMES_HOME")
    if home:
        try:
            paths.add(str(Path(home).resolve() / "ui-tui" / "dist" / "entry.js"))
        except Exception:
            pass
    # Also accept a ui-tui dir next to the hermes_cli package (checkout layout).
    try:
        checkout = Path(__file__).resolve().parent.parent / "ui-tui" / "dist" / "entry.js"
        paths.add(str(checkout))
    except Exception:
        pass
    return {p.lower() for p in paths}


# Precomputed once at import; cheap and stable for a given home/env.
_APPROVED_TUI_ENTRY_PATHS = _hermes_tui_entry_paths()


def _is_tui_node_cmdline(cmd: str) -> bool:
    """True iff *cmd* is a Hermes TUI node launch (one of the approved layouts).

    Requires the Hermes launcher flag ``--expose-gc`` followed by an entry path
    that normalizes to one of the concrete TUI entry scripts Hermes launches
    (see ``_hermes_tui_entry_paths``). An unrelated node whose cmdline merely
    contains ``dist/entry.js`` is NOT selected.
    """
    if "--expose-gc" not in cmd:
        return False
    # Find the path token that follows --expose-gc.
    try:
        tokens = cmd.split()
        idx = tokens.index("--expose-gc")
        entry = tokens[idx + 1] if idx + 1 < len(tokens) else ""
    except (ValueError, IndexError):
        return False
    if not entry:
        return False
    try:
        normalized = str(Path(entry).resolve()).lower()
    except (OSError, ValueError):
        normalized = entry.lower()
    return normalized in _APPROVED_TUI_ENTRY_PATHS


def _is_node_exe(command: str) -> bool:
    """True iff the command's executable is Node (node / node.exe).

    The first whitespace-delimited token of a process command line is normally
    the executable. We resolve its basename case-insensitively so both POSIX
    ``node`` and Windows ``node.exe`` match, and common shim forms
    (``node.cmd``, ``node.bat``) are rejected as non-Node. Paths are stripped
    of surrounding quotes (Windows wmic may quote the exe).

    When the first token is not Node, a short multi-token prefix is tried: a
    Node binary launched by absolute path whose path contains spaces (e.g.
    ``C:\\Program Files\\nodejs\\node.exe``) is split by the cmdline join, so
    the executable only reassembles across several tokens. Only a
    path-qualified (separator-bearing) token may extend the head — a bare
    ``node`` argument after a non-Node executable must not match. The
    scanners prefer psutil's ``exe`` attribute (ground truth) over this
    heuristic whenever it is available.
    """
    if not command:
        return False
    tokens = command.split()
    if not tokens:
        return False

    def _basename_is_node(token: str) -> bool:
        name = Path(token.strip('"').strip("'")).name.lower()
        return name.strip('"').strip("'") in ("node", "node.exe")

    if _basename_is_node(tokens[0]):
        return True
    # Reassemble a spaced absolute path: accept once a path-qualified token
    # whose basename is node(.exe) completes the executable portion.
    for token in tokens[1:5]:
        token = token.strip('"').strip("'")
        if ("/" in token or "\\" in token) and _basename_is_node(token):
            return True
    return False


def _process_ppid(pid: int) -> int | None:
    """Best-effort parent pid lookup, cross-platform via psutil.

    Returns None on any failure. Uses psutil (a core dependency) rather than a
    POSIX-only ``ps`` shell-out so Windows TUI nodes can also be evaluated for
    orphanhood — a shell-out to ``ps`` returns nothing on Windows and would make
    the entire Windows reaper unreachable.
    """
    try:
        import psutil  # type: ignore

        return psutil.Process(pid).ppid()
    except Exception:
        return None


def _is_alive_parent(ppid: int, child_create_time: float | None = None) -> bool:
    """True iff *ppid* is a live process (psutil-free check).

    When *child_create_time* is known, a PID-reuse check is applied: a real
    parent is always older than its child, so a "parent" whose create time is
    newer than the child's is an unrelated process that reused the dead
    parent's PID — treat the parent as dead. Without the child's create time
    (or when the parent's create time cannot be read), the check stays
    fail-closed: spare the process.
    """
    if ppid <= 1:
        return False  # reparented to init / already a true orphan
    try:
        import psutil  # type: ignore

        if not psutil.pid_exists(ppid):
            return False
        if child_create_time is not None:
            try:
                parent_create_time = psutil.Process(ppid).create_time()
            except psutil.Error:
                return True  # cannot prove reuse; spare
            if parent_create_time > child_create_time:
                return False  # PID reused by a newer process: parent is dead
        return True
    except Exception:
        # If we cannot determine liveness, err toward sparing the process.
        return True


def _current_process_identity(pid: int) -> tuple[float, str] | None:
    """Live ``(create_time, exe)`` identity of *pid*, or None if gone/unreadable."""
    try:
        import psutil  # type: ignore

        proc = psutil.Process(pid)
        return (proc.create_time(), (proc.exe() or "").lower())
    except Exception:
        return None


def _tui_node_exclude_pids() -> set[int]:
    """PIDs that must never be reaped by the TUI node reaper.

    Mirrors the safety exclusions of the dashboard reaper:
    - the current Python process and every ancestor whose exe is a Hermes venv
      shim (so we never reap the live launcher that owns this launch);
    - HERMES_DESKTOP_CHILD_PID entries (Desktop-managed backends).
    Never raises.
    """
    exclude: set[int] = set()
    try:
        import psutil  # type: ignore
    except Exception:
        return exclude

    seed = os.getpid()
    exclude.add(seed)
    try:
        exclude.add(os.getppid())  # direct parent (the launcher / shell)
    except Exception:
        pass
    # Desktop-managed live backend children must be spared.
    raw_pid = os.environ.get("HERMES_DESKTOP_CHILD_PID")
    if raw_pid:
        for part in raw_pid.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                exclude.add(int(part))
            except (ValueError, TypeError):
                pass
    # Exclude the whole ancestor chain whose exe is a Hermes venv shim.
    try:
        shim_paths: set[str] = set()
        scripts_dir = _m()._venv_scripts_dir()
        if scripts_dir is not None:
            for shim in _m()._hermes_exe_shims(scripts_dir):
                try:
                    shim_paths.add(str(shim.resolve()).lower())
                except OSError:
                    shim_paths.add(str(shim).lower())
    except Exception:
        shim_paths = set()
    if shim_paths:
        try:
            proc = psutil.Process(seed)
            for ancestor in proc.parents():
                try:
                    anc_exe = ancestor.exe()
                except Exception:
                    continue
                if not anc_exe:
                    continue
                try:
                    anc_norm = str(Path(anc_exe).resolve()).lower()
                except (OSError, ValueError):
                    anc_norm = str(anc_exe).lower()
                if anc_norm in shim_paths:
                    try:
                        exclude.add(int(ancestor.pid))
                    except Exception:
                        continue
        except Exception:
            pass
    return exclude


def _scan_posix_node_processes(
    exclude: set[int]
) -> list[tuple[int, str, tuple[float, str] | None]]:
    """Scan POSIX for orphanable TUI node processes (psutil process_iter).

    Returns ``(pid, cmdline, identity)`` tuples. A process is only returned
    when BOTH:
    - its executable is verified Node (gate 1), and
    - its cmdline matches a TUI pattern.
    ``identity`` is a NON-NONE stable (create_time, exe) snapshot taken from the
    SAME process_iter observation as the cmdline (no separate lookup), so a PID
    reused after this snapshot can never masquerade as the scanned process
    (Greptile P1: scan identity reused). Excludes *exclude* PIDs and self.
    Empty list on any error.
    """
    self_pid = os.getpid()
    found: list[tuple[int, str, tuple[float, str] | None]] = []
    try:
        import psutil  # type: ignore
    except Exception:
        return []
    try:
        for proc in psutil.process_iter(["pid", "cmdline", "create_time", "exe"]):
            try:
                info = proc.info
                pid = int(info.get("pid"))
                cmd_parts = info.get("cmdline") or []
                cmd = " ".join(str(c) for c in cmd_parts)
            except (ValueError, TypeError, psutil.Error):
                continue
            if pid == self_pid or pid in exclude:
                continue
            if not cmd:
                continue
            # Prefer psutil's exe (ground truth) over the cmdline heuristic:
            # a Node binary at a spaced path ("C:\Program Files\...") splits
            # across cmdline tokens and defeats first-token matching.
            exe_path = (proc.info.get("exe") or "").strip()
            if exe_path:
                if Path(exe_path).name.lower() not in ("node", "node.exe"):
                    continue
            elif not _is_node_exe(cmd):
                continue
            if not _is_tui_node_cmdline(cmd):
                continue
            try:
                identity = (float(proc.info["create_time"]), (proc.info.get("exe") or "").lower())
            except (TypeError, ValueError, psutil.Error):
                identity = None
            found.append((pid, cmd, identity))
    except Exception:
        return found
    return found


def _scan_windows_node_processes(
    exclude: set[int]
) -> list[tuple[int, str, tuple[float, str] | None]]:
    """Scan Windows for orphanable TUI node processes (psutil process_iter).

    Uses the SAME cross-platform process_iter snapshot as the POSIX scanner, so
    identity (create_time, exe) is bound to the exact process whose cmdline was
    read — no separate ``psutil.Process(pid)`` lookup that a PID reuse could fool
    (Greptile P1: scan identity reused). This also removes the prior wmic
    dependency entirely, so discovery still works on builds where wmic was
    removed (Greptile P1: no WMIC fallback). Returns ``(pid, cmdline, identity)``
    tuples. Empty list on any error.
    """
    self_pid = os.getpid()
    found: list[tuple[int, str, tuple[float, str] | None]] = []
    try:
        import psutil  # type: ignore
    except Exception:
        return []
    try:
        for proc in psutil.process_iter(["pid", "cmdline", "create_time", "exe"]):
            try:
                info = proc.info
                pid = int(info.get("pid"))
                cmd_parts = info.get("cmdline") or []
                cmd = " ".join(str(c) for c in cmd_parts)
            except (ValueError, TypeError, psutil.Error):
                continue
            if pid == self_pid or pid in exclude:
                continue
            if not cmd:
                continue
            # Same exe-first rule as the POSIX scanner: psutil's exe is ground
            # truth; the cmdline heuristic is only a fallback.
            exe_path = (info.get("exe") or "").strip()
            if exe_path:
                if Path(exe_path).name.lower() not in ("node", "node.exe"):
                    continue
            elif not _is_node_exe(cmd):
                continue
            if not _is_tui_node_cmdline(cmd):
                continue
            try:
                identity = (float(proc.info["create_time"]), (proc.info.get("exe") or "").lower())
            except (TypeError, ValueError, psutil.Error):
                identity = None
            found.append((pid, cmd, identity))
    except Exception:
        return found
    return found


def _reap_orphaned_tui_nodes(
    *,
    tui_dir: Path | None = None,
    signal_term: int | None = None,
    signal_kill: int | None = None,
    sleep_fn=None,
    lock_owned_pids_fn=None,
) -> dict[str, list]:
    """Kill leftover ``node`` TUI processes (``ui-tui/dist/entry``) from prior launches.

    ``hermes desktop`` and ``hermes --tui`` launch a ``node`` TUI process via
    :func:`hermes_cli.main._launch_tui` (``node ui-tui/dist/entry.js``). That
    call previously never reaped the previous launch's node tree: when the
    parent ``hermes``/``python`` process died uncleanly, the node child
    survived and kept emitting prompt_toolkit status chrome to a shared
    terminal surface, producing the stacked/repeated status frames observed on
    Windows (multiple independent TUI UIs sharing one TTY).

    This is the TUI analogue of :func:`_reap_orphaned_desktop_local_serves`,
    applied at TUI-launch time. It reuses the same safety model and the same
    Windows ``taskkill /T /F`` tree-kill the rest of the codebase uses for
    Windows process teardown; the POSIX path mirrors the SIGTERM-then-SIGKILL
    grace of the desktop reaper.

    Safety (never relaxes below what the dashboard reaper enforces):
    - the process executable MUST be verified Node (``node``/``node.exe``):
      a ``python``/``notepad.exe``/``grep`` cmdline that merely mentions
      ``ui-tui/dist/entry`` is never selected (Greptile P1, #verified-node);
    - only nodes that are true orphans — their parent ``hermes``/launcher has
      exited (reparented to init on POSIX). A concurrently-running,
      legitimately-owned TUI launched by another still-alive ``hermes`` process
      keeps its parent and is never reaped, so a fresh launch cannot murder an
      unrelated in-use session;
    - never self / never the ancestor chain / never ``HERMES_DESKTOP_CHILD_PID``;
    - never a PID a valid ``backend.lock.json`` claims (SSH remote backends
      started by other clients/machines are legitimate, even at ppid 1);
    - on Windows, the tree-kill (``/T /F``) reaps the node child's own
      descendants along with it, matching Desktop ``forceKillProcessTree``;
    - best-effort; failures are swallowed and never propagate to launch.

    Returns the same ``{"matched", "killed", "failed"}`` shape as the dashboard
    reaper for testability and consistency.
    """
    import signal as _signal
    import time as _time

    if signal_term is None:
        signal_term = _signal.SIGTERM
    if signal_kill is None:
        signal_kill = getattr(_signal, "SIGKILL", _signal.SIGTERM)
    if sleep_fn is None:
        sleep_fn = _time.sleep
    if lock_owned_pids_fn is None:
        lock_owned_pids_fn = lambda: set()  # no external ownership claims by default

    exclude = _tui_node_exclude_pids()
    exclude.add(os.getpid())

    if sys.platform == "win32":
        scanned = _scan_windows_node_processes(exclude)
    else:
        scanned = _scan_posix_node_processes(exclude)

    if not scanned:
        return {"matched": [], "killed": [], "failed": []}

    targets: list[tuple[int, str, tuple[float, str]]] = []
    for pid, cmd, scan_identity in scanned:
        if pid in exclude:
            continue
        # Re-check lock ownership defensively (lock files may be written
        # between the scan and now). Defense in depth — never kill a freshly
        # claimed owner.
        try:
            owned_now = set(lock_owned_pids_fn())
        except Exception:
            owned_now = set()
        if pid in owned_now:
            continue
        # Only reap *orphaned* TUI nodes — those whose launcher parent has
        # already exited. A live sibling TUI launched from another still-alive
        # hermes process keeps its parent and must survive.
        #
        # Fail closed on an *unknown* parent on BOTH platforms: if we cannot
        # establish that the launcher is dead, we must not reap (a concurrent
        # live TUI would otherwise be killed). POSIX and Windows branches must
        # agree here.
        ppid = _process_ppid(pid)
        if ppid is None:
            # Parent lookup failed: cannot prove orphanhood, skip safely.
            continue
        # Pass the child's scan-time create time so _is_alive_parent can detect
        # a reused parent PID (a real parent is always older than its child).
        if _is_alive_parent(ppid, scan_identity[0] if scan_identity else None):
            continue
        # Require the NON-NONE identity captured by the scanner at scan time
        # (bound to the exact process that passed the Node/TUI gates). If the
        # scanner could not snapshot it, or it is empty, skip — we must not reap
        # an unidentifiable PID, and we must not let a later PID reuse replace
        # the baseline (Greptile P1: scan identity not preserved).
        if not scan_identity or scan_identity[0] == 0.0 and scan_identity[1] == "":
            continue
        targets.append((pid, cmd, scan_identity))

    if not targets:
        return {"matched": [], "killed": [], "failed": []}

    matched = [pid for pid, _cmd, _ident in targets]
    killed: list[int] = []
    failed: list[int] = []

    if sys.platform == "win32":
        from hermes_cli._subprocess_compat import windows_hide_flags

        for pid, _cmd, baseline in targets:
            # Require a non-None identity that still matches the scanned process
            # immediately before the forced tree-kill. A None, or a changed
            # identity (PID reused by an unrelated process), means skip.
            current = _current_process_identity(pid)
            if current is None or current != baseline:
                continue
            try:
                result = subprocess.run(
                    ["taskkill", "/T", "/F", "/PID", str(pid)],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=10,
                    creationflags=windows_hide_flags(),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                if result.returncode == 0:
                    killed.append(pid)
                else:
                    failed.append(pid)
            except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
                failed.append(pid)
    else:
        for pid, _cmd, baseline in targets:
            # Re-validate right before SIGTERM: skip if the PID is now gone or
            # belongs to a different (reused) process.
            current = _current_process_identity(pid)
            if current is None or current != baseline:
                continue
            try:
                os.kill(pid, signal_term)
            except ProcessLookupError:
                killed.append(pid)
                continue
            except (PermissionError, OSError):
                failed.append(pid)
                continue
        sleep_fn(1.5)

        for pid, _cmd, baseline in targets:
            if pid in failed:
                continue
            # Re-validate before escalating to SIGKILL: only the SAME process
            # instance we SIGTERM'd may be killed. A None or changed identity
            # (PID reused) means skip — never kill an unrelated replacement.
            current = _current_process_identity(pid)
            if current is None or current != baseline:
                continue
            try:
                os.kill(pid, signal_kill)
                killed.append(pid)
            except ProcessLookupError:
                killed.append(pid)
            except OSError:
                failed.append(pid)

    if matched:
        try:
            print(
                f"⟲ Reaped {len(killed)} orphaned TUI node process(es): "
                f"{killed or matched}"
            )
        except Exception:
            pass

    return {"matched": matched, "killed": killed, "failed": failed}



def _exclude_pids_from_env() -> set[int]:
    """PIDs Desktop marks as live backends (HERMES_DESKTOP_CHILD_PID)."""
    raw = os.environ.get("HERMES_DESKTOP_CHILD_PID", "")
    out: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.add(int(part))
        except ValueError:
            continue
    return out


# --- SSH remote-backend lock ownership -------------------------------------
#
# ``backend.lock.json`` is the ownership record the Desktop SSH runtime writes
# on the *remote* host for every ``hermes serve`` backend it spawns over SSH
# (see apps/desktop/electron/remote-lifecycle.ts). A backend started from
# another client/machine — e.g. a MacBook driving a ``hermes serve`` on a Mac
# Mini over SSH — is a *legitimate, lock-owned* backend even though it has no
# parent on this host (sshd has long since exited, reparenting it to pid 1).
#
# The orphan reap must NEVER kill a PID that a valid ``backend.lock.json``
# claims as its owner. Doing so murdered a real production SSH remote backend
# on a Mac Mini the first time the local Desktop app rebooted. The lock file is
# the source of truth for "is this serve legitimately owned by some client",
# regardless of which machine started it.

# Mirror the schema constants in remote-lifecycle.ts (the writer). Bumping one
# side without the other makes the lock unreadable on purpose, which is the
# safe failure mode for reuse — but for the reap we only ever *spare*, so a
# mismatched-schema record is simply ignored (never used to kill).
_LOCKFILE_SCHEMA_VERSION = 2
_PROTOCOL_VERSION = 1
_REMOTE_LOCK_SUBDIR = "desktop-ssh"
_HEX32 = set("0123456789abcdef")
_HEX16 = _HEX32


def _hermes_home_dir() -> Path:
    """Resolved Hermes home (HERMES_HOME override or ~/.hermes)."""
    override = os.environ.get("HERMES_HOME", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".hermes"


def _valid_lockfile_payload(parsed: object, ownership_id: str) -> bool:
    """Validate a parsed ``backend.lock.json`` body, mirroring readLockfile().

    Returns True only when every structural field the SSH runtime writes is
    present and well-formed. A lock that fails validation is ignored (treated
    as "no ownership claim"), which never causes a kill — the reap only ever
    *adds* lock-owned PIDs to its spare-set.
    """
    if not isinstance(parsed, dict):
        return False
    if parsed.get("schemaVersion") != _LOCKFILE_SCHEMA_VERSION:
        return False
    if parsed.get("protocolVersion") != _PROTOCOL_VERSION:
        return False
    if parsed.get("ownershipId") != ownership_id:
        return False
    spawn_nonce = parsed.get("spawnNonce")
    if not isinstance(spawn_nonce, str) or len(spawn_nonce) != 16:
        return False
    if set(spawn_nonce) - _HEX16:
        return False
    token_fp = parsed.get("tokenFingerprint")
    if not isinstance(token_fp, str) or len(token_fp) != 32 or set(token_fp) - _HEX32:
        return False
    pid = parsed.get("pid")
    if not isinstance(pid, int) or pid <= 0 or pid > 4194304:
        return False
    port = parsed.get("port")
    if not isinstance(port, int) or port < 0 or port > 65535:
        return False
    # String fields must be present and bounded (the writer enforces <=1024).
    for field in ("profile", "hermesPath", "hermesHome", "logPath", "startedAt"):
        value = parsed.get(field)
        if not isinstance(value, str) or len(value) > 1024:
            return False
    # logPath is written as ``{lock_root}/{ownershipId}/{spawnNonce}.log``. We
    # only check the suffix so a relocated HERMES_HOME (different leading path)
    # doesn't falsely reject a legitimate remote-owned backend — a false reject
    # here would re-introduce the exact kill we're fixing.
    log_path = parsed["logPath"]
    if not log_path.endswith(f"/{ownership_id}/{spawn_nonce}.log"):
        return False
    return True


def _lock_owned_serve_pids(base_dir: Path | None = None) -> set[int]:
    """PIDs claimed as owners by valid ``backend.lock.json`` records on this host.

    Scans ``{hermes_home}/desktop-ssh/<ownershipId>/backend.lock.json`` (the
    same directory the Desktop SSH runtime writes to). Any PID a valid lock
    names is a legitimately-owned backend — including backends another client
    or machine started over SSH — and must be spared by the orphan reap.

    Best-effort: any read/parse/IO error for a single record is swallowed and
    that record contributes no PID. Never raises.
    """
    import json

    root = base_dir if base_dir is not None else (
        _hermes_home_dir() / _REMOTE_LOCK_SUBDIR
    )
    owned: set[int] = set()
    if not root.is_dir():
        return owned
    try:
        entries = list(root.iterdir())
    except OSError:
        return owned
    for entry in entries:
        try:
            if not entry.is_dir():
                continue
        except OSError:
            continue
        ownership_id = entry.name
        # Mirror validateOwnershipId(): exactly 32 lowercase hex chars.
        if len(ownership_id) != 32 or set(ownership_id) - _HEX32:
            continue
        lock_path = entry / "backend.lock.json"
        try:
            if not lock_path.is_file():
                continue
            with open(lock_path, "rb") as handle:
                data = handle.read()
        except OSError:
            continue
        if len(data) > 65536:
            continue
        try:
            parsed = json.loads(data)
        except (UnicodeDecodeError, ValueError):
            continue
        if _valid_lockfile_payload(parsed, ownership_id):
            try:
                owned.add(int(parsed["pid"]))
            except (TypeError, ValueError):
                continue
    return owned


def _reap_orphaned_desktop_local_serves(
    *,
    reason: str = "orphaned desktop-local hermes serve",
    signal_term=None,
    signal_kill=None,
    sleep_fn=None,
    lock_owned_pids_fn=None,
) -> dict[str, list]:
    """Kill leftover Desktop-local ``hermes serve`` backends with no parent.

    When Electron dies uncleanly (crash / SIGKILL / update handoff), local
    ``serve --host 127.0.0.1 --port 0`` children can be reparented to pid 1 and
    keep their full MCP trees alive. The next Desktop boot then stacks a fresh
    backend on top of the corpses until the machine hits EMFILE and the UI
    loses tabs/sidebar.

    The parent-death watchdog prevents *future* orphans once a backend is
    running under HERMES_PARENT_PID; this helper clears *already* orphaned
    corpses at the start of a new Desktop backend.

    Safety:
    - only the Desktop-local spawn shape (loopback + ``--port 0``)
    - only processes whose current ppid is 1 (or 0 on some supervisors)
    - never self / never HERMES_DESKTOP_CHILD_PID entries
    - never a PID a valid ``backend.lock.json`` claims as its owner — that is
      a legitimately lock-owned backend, *including SSH remote backends started
      by another client/machine* which legitimately sit at ppid 1 after sshd
      exits. Killing those is a production incident, not cleanup.
    - never fixed-port remote serves (e.g. ``--port 9119``)
    - best-effort; failures never raise to the caller
    """
    import signal as _signal
    import time as _time

    if signal_term is None:
        signal_term = _signal.SIGTERM
    if signal_kill is None:
        signal_kill = getattr(_signal, "SIGKILL", _signal.SIGTERM)
    if sleep_fn is None:
        sleep_fn = _time.sleep
    if lock_owned_pids_fn is None:
        lock_owned_pids_fn = _lock_owned_serve_pids

    if sys.platform == "win32":
        # Windows desktop uses taskkill tree teardown; orphan scan here is POSIX.
        return {"matched": [], "killed": [], "failed": []}

    exclude = _exclude_pids_from_env()
    exclude.add(os.getpid())
    # Also spare our direct parent (the desktop / sshd wrapper).
    try:
        exclude.add(os.getppid())
    except Exception:
        pass
    # Spare every PID a valid backend.lock.json owns — SSH remote backends
    # started by other clients/machines are legitimate, lock-owned owners even
    # though they are orphaned (ppid 1) on this host. (#78872 regression)
    try:
        exclude |= set(lock_owned_pids_fn())
    except Exception:
        # Best-effort: never let lock scanning block or widen the reap.
        pass

    try:
        scanned = _scan_dashboard_processes(exclude_pids=exclude)
    except Exception:
        return {"matched": [], "killed": [], "failed": []}

    # Re-read lock ownership defensively: the scan above already filtered
    # exclude PIDs, but a lock file may have been written between the scan and
    # now. Defense in depth — never kill a freshly-claimed owner.
    try:
        owned_now = set(lock_owned_pids_fn())
    except Exception:
        owned_now = set()

    targets: list[tuple[int, str]] = []
    for pid, cmd in scanned:
        if not _is_desktop_local_serve_cmdline(cmd):
            continue
        if pid in owned_now:
            continue
        ppid = _process_ppid(pid)
        if ppid is None:
            continue
        # Orphaned under init/launchd.
        if ppid not in (0, 1):
            continue
        targets.append((pid, cmd))

    if not targets:
        return {"matched": [], "killed": [], "failed": []}

    matched = [pid for pid, _ in targets]
    killed: list[int] = []
    failed: list[int] = []

    for pid, _cmd in targets:
        try:
            os.kill(pid, signal_term)
        except ProcessLookupError:
            continue
        except PermissionError:
            failed.append(pid)
            continue
        except OSError:
            failed.append(pid)
            continue

    # Brief grace, then SIGKILL survivors.
    sleep_fn(1.5)
    # psutil.pid_exists for the liveness probe: os.kill(pid, 0) is a
    # Windows footgun (sends CTRL_C_EVENT, bpo-14484). This path is
    # POSIX-only (win32 early-returns above), but the linter blocks the
    # pattern everywhere and psutil is a core dependency anyway.
    import psutil

    for pid, _cmd in targets:
        if pid in failed:
            continue
        if not psutil.pid_exists(pid):
            killed.append(pid)
            continue
        try:
            os.kill(pid, signal_kill)
            killed.append(pid)
        except ProcessLookupError:
            killed.append(pid)
        except OSError:
            failed.append(pid)

    if matched:
        try:
            print(
                f"⟲ Reaped {len(killed)} orphaned desktop-local serve "
                f"backend(s) ({reason}): {killed or matched}"
            )
        except Exception:
            pass

    return {"matched": matched, "killed": killed, "failed": failed}


def _scan_windows_gateway_processes(
    *,
    exclude_pids: set[int] | None = None,
) -> list[tuple[int, str, tuple[float, str] | None]]:
    """Return candidate ``python.exe`` gateway processes on Windows.

    Scans for ``python.exe`` processes whose command line contains
    ``hermes_cli.main gateway run --replace``. Returns
    ``(pid, cmdline, identity)`` triples where ``identity`` is a scan-time
    ``(create_time, exe)`` snapshot bound to the exact process_iter
    observation (None if unavailable — the reaper skips those). Orphan
    verification (dead parent, PID-reuse-aware) is done by the caller via
    :func:`_is_alive_parent` with the scanned child's create time. Excludes
    any PIDs in ``exclude_pids``. Returns an empty list on any scan error.
    """
    if sys.platform != "win32":
        return []
    exclude_pids = exclude_pids or set()
    try:
        import psutil  # type: ignore

        matches: list[tuple[int, str, tuple[float, str] | None]] = []
        for proc in psutil.process_iter(
            ["pid", "name", "cmdline", "create_time", "exe"]
        ):
            try:
                info = proc.info
                pid = info.get("pid")
                if not pid or pid in exclude_pids:
                    continue
                name = (info.get("name") or "").lower()
                if name not in ("python.exe", "pythonw.exe"):
                    continue
                cmdline = " ".join(info.get("cmdline") or [])
                if "hermes_cli.main gateway run --replace" not in cmdline:
                    continue
                try:
                    identity = (
                        float(info["create_time"]),
                        (info.get("exe") or "").lower(),
                    )
                except (TypeError, ValueError):
                    identity = None
                matches.append((int(pid), cmdline, identity))
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
            except Exception:
                continue
        return matches
    except Exception:
        return []


def _reap_orphaned_windows_gateway_processes(
    *,
    reason: str = "orphaned windows gateway python",
) -> dict[str, list]:
    """Kill orphaned ``python.exe`` gateway backends on Windows.

    When a ``hermes gateway run --replace`` parent exits uncleanly, the
    child ``python.exe`` can survive with a dead parent PID. These orphans
    keep gateway state, port bindings, and MCP trees alive, causing
    silent frontend/backend mismatches on the next launch (similar to the
    desktop-local serve reaper, but for the gateway/python spawn shape on
    Windows, which the POSIX-only ``_reap_orphaned_desktop_local_serves``
    does not cover).

    Safety:
    - only processes whose cmdline matches ``hermes_cli.main gateway run --replace``
    - only processes whose current ppid is dead (not merely reparented to
      a live supervisor), checked PID-reuse-aware against the scanned
      child's create time (a reused parent PID cannot shield an orphan)
    - only processes with a scan-time ``(create_time, exe)`` identity that
      still matches immediately before the tree-kill (a PID reused between
      scan and kill can never be killed)
    - never self / never ``HERMES_DESKTOP_CHILD_PID`` entries
    - never a PID a valid ``backend.lock.json`` claims as its owner
    - uses ``taskkill /T /F /PID`` tree-kill, same as the rest of the
      Windows codebase
    - best-effort; failures never raise to the caller
    """
    if sys.platform != "win32":
        return {"matched": [], "killed": [], "failed": []}

    exclude = _exclude_pids_from_env()
    exclude.add(os.getpid())
    try:
        exclude.add(os.getppid())
    except Exception:
        pass
    try:
        exclude |= set(_lock_owned_serve_pids())
    except Exception:
        pass

    try:
        scanned = _scan_windows_gateway_processes(exclude_pids=exclude)
    except Exception:
        return {"matched": [], "killed": [], "failed": []}

    targets: list[tuple[int, str, tuple[float, str]]] = []
    try:
        owned_now = set(_lock_owned_serve_pids())
    except Exception:
        owned_now = set()
    for pid, cmd, scan_identity in scanned:
        if pid in owned_now:
            continue
        # Skip unidentifiable processes — same fail-closed rule as the TUI
        # node reaper: never kill a PID we cannot bind to the scanned process.
        if not scan_identity:
            continue
        ppid = _process_ppid(pid)
        if ppid is None:
            continue
        # Pass the child's scan-time create time so _is_alive_parent can
        # detect a reused parent PID (a real parent is always older).
        if _is_alive_parent(ppid, scan_identity[0]):
            continue
        targets.append((pid, cmd, scan_identity))

    if not targets:
        return {"matched": [], "killed": [], "failed": []}

    matched = [pid for pid, _, _ in targets]
    killed: list[int] = []
    failed: list[int] = []

    from hermes_cli._subprocess_compat import windows_hide_flags

    for pid, _cmd, baseline in targets:
        # Identity must still match the scanned process right before the
        # forced tree-kill; a gone or replaced (PID-reused) process is skipped.
        current = _current_process_identity(pid)
        if current is None or current != baseline:
            continue
        try:
            result = subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(pid)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                creationflags=windows_hide_flags(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if result.returncode == 0:
                killed.append(pid)
            else:
                failed.append(pid)
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            failed.append(pid)

    if matched:
        try:
            print(
                f"⟲ Reaped {len(killed)} orphaned windows gateway "
                f"process(es) ({reason}): {killed or matched}"
            )
        except Exception:
            pass

    return {"matched": matched, "killed": killed, "failed": failed}
