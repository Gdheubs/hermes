"""Install and remove the Linux desktop entry (``hermes.desktop``).

``hermes desktop`` builds and launches the Electron app. On Linux, a
freshly-built app has no launcher presence: no menu item, no icon. This
module writes the XDG desktop entry that gives it one.
``hermes uninstall --gui`` removes the entry again.

Two values must be absolute for the entry to work:

  - ``Exec`` — the launcher runs without shell ``PATH`` customizations, so
    a bare ``hermes desktop`` fails when hermes lives in ``~/.local/bin``
    or a venv. Resolve the real binary and write its full path.
  - ``Icon`` — an unqualified icon name needs an indexed icon theme. The
    spec allows an absolute path instead, so point at the app icon in the
    checkout. Do not copy the icon: ``Exec`` already depends on that tree.

Cache refresh is best-effort and tool-gated: ``update-desktop-database``
for the freedesktop menu cache, and ``kbuildsycoca6``/``kbuildsycoca5``
for Plasma. Run each tool only when it exists. A missing tool is not an
error.

Import-light and side-effect-free at import time: the uninstaller and the
Electron main process both use this without loading the full CLI.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

DESKTOP_ENTRY_NAME = "hermes.desktop"


def is_supported() -> bool:
    """XDG desktop entries exist only on Linux and BSD."""
    return sys.platform.startswith(("linux", "freebsd", "openbsd", "netbsd"))


def _xdg_data_home() -> Path:
    raw = os.environ.get("XDG_DATA_HOME")
    if raw and raw.strip():
        return Path(raw).expanduser()
    return Path.home() / ".local" / "share"


def desktop_entry_path() -> Path:
    """Where the ``hermes.desktop`` entry lives."""
    return _xdg_data_home() / "applications" / DESKTOP_ENTRY_NAME


def icon_path(project_root: Path) -> Path:
    """The app icon shipped in the desktop workspace."""
    return project_root / "apps" / "desktop" / "assets" / "icon.png"


#: Shebang interpreters that may be overridden with ``sys.executable``.
#: The CPython family only: a substring test would also claim ``pypy3``,
#: ``python2``, or a wrapper whose *path* merely contains "python"
#: (e.g. ``/opt/python-wrapper``), and running those under
#: ``sys.executable`` would invoke the wrong interpreter.
_PYTHON_SHEBANG_RE = re.compile(rb"python(?:3(?:\.?\d+)*)?")


def _is_python_script(path: Path) -> bool:
    """True when ``path`` is a script whose shebang names a CPython interpreter.

    A desktop entry runs without shell ``PATH`` customizations, so a bare
    script with an ``env python`` shebang resolves to the *system*
    interpreter, which lacks the project's dependencies. Such a binary
    must be launched through the interpreter that is actually running
    Hermes instead.
    """
    try:
        with path.open("rb") as fh:
            first = fh.readline(128)
    except OSError:
        return False
    if not first.startswith(b"#!"):
        return False
    tokens = first[2:].strip().split()
    if tokens and tokens[0].rsplit(b"/", 1)[-1] == b"env":
        # ``env`` may take flags (-S) or VAR=value assignments first.
        tokens = tokens[1:]
        while tokens and (tokens[0].startswith(b"-") or b"=" in tokens[0]):
            tokens = tokens[1:]
    if not tokens:
        return False
    # Match the interpreter basename only, so a wrapper whose *path*
    # contains "python" is not claimed.
    basename = tokens[0].rsplit(b"/", 1)[-1]
    return _PYTHON_SHEBANG_RE.fullmatch(basename) is not None


def resolve_exec_command() -> str:
    """Build the absolute ``Exec=`` command line for ``hermes desktop``.

    Prefer the real ``hermes`` executable (argv[0] or PATH). When Hermes
    runs as a module with no launcher installed, use the current
    interpreter, also absolute. A Python-script binary is invoked through
    ``sys.executable``: the desktop entry runs without shell ``PATH``
    customizations, so a bare script with an ``env python3`` shebang
    resolves to the system interpreter and fails (missing deps).
    """
    from hermes_cli.relaunch import resolve_hermes_bin

    bin_path = resolve_hermes_bin()
    if bin_path:
        resolved = Path(bin_path).resolve()
        if _is_python_script(resolved):
            # sys.executable verbatim, never resolved: venv pythons are
            # symlinks, and realpath()ing one lands on the *base*
            # interpreter, which breaks venv site-packages discovery
            # (pyvenv.cfg is found via the symlink's directory).
            argv = [sys.executable, str(resolved), "desktop"]
        else:
            argv = [str(resolved), "desktop"]
    else:
        # sys.executable verbatim, never resolved — same venv-symlink rule
        # as the script branch above.
        argv = [sys.executable, "-m", "hermes_cli.main", "desktop"]
    return " ".join(_quote_exec_arg(a) for a in argv)


def _quote_exec_arg(arg: str) -> str:
    """Quote one ``Exec`` argument per the desktop entry spec.

    Reserved characters require double quotes. Inside the quotes, escape
    a backslash and a double quote with a backslash.
    """
    if not any(c in arg for c in ' \t\n"\'\\><~|&;$*?#()`'):
        return arg
    escaped = arg.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def render_desktop_entry(exec_command: str, icon: str) -> str:
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=Hermes\n"
        "GenericName=Hermes Desktop\n"
        "Comment=Launch Hermes Desktop\n"
        f"Exec={exec_command}\n"
        f"Icon={icon}\n"
        "Terminal=false\n"
        "Categories=Utility;\n"
        "StartupNotify=true\n"
        "StartupWMClass=Hermes\n"
    )


def refresh_desktop_databases(applications_dir: Path) -> "list[str]":
    """Reindex the menu caches. Run each tool only when it exists.

    Return the names of the tools that ran (for logging and tests).
    """
    ran: list[str] = []

    update_db = shutil.which("update-desktop-database")
    if update_db:
        if _run_quiet([update_db, str(applications_dir)]):
            ran.append("update-desktop-database")

    # Plasma 6 first, then Plasma 5. Only one of them is ever installed.
    for tool in ("kbuildsycoca6", "kbuildsycoca5"):
        resolved = shutil.which(tool)
        if not resolved:
            continue
        if _run_quiet([resolved, "--noincremental"]):
            ran.append(tool)
        break

    return ran


def _run_quiet(cmd: "list[str]") -> bool:
    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def install_desktop_entry(project_root: Path) -> Optional[Path]:
    """Write (or refresh) the Hermes desktop entry. Return its path.

    Return ``None`` on non-Linux platforms or when the write fails. This
    is a convenience, never a reason to fail a launch.
    """
    if not is_supported():
        return None

    entry_path = desktop_entry_path()
    icon = icon_path(project_root)
    # Use the themed name when the checkout has no icon (a lite or
    # packaged install). A broken absolute path renders as no icon.
    icon_value = str(icon) if icon.is_file() else "hermes"
    contents = render_desktop_entry(resolve_exec_command(), icon_value)

    try:
        entry_path.parent.mkdir(parents=True, exist_ok=True)
        # When nothing changed, skip the rewrite. Then a launch does not
        # churn the menu caches.
        if entry_path.is_file() and entry_path.read_text(encoding="utf-8") == contents:
            return entry_path
        entry_path.write_text(contents, encoding="utf-8")
        # Some launchers (and older Plasma) offer the entry only when it
        # is executable.
        entry_path.chmod(0o755)
    except OSError:
        return None

    refresh_desktop_databases(entry_path.parent)
    return entry_path
