"""Termux/Android cryptography overlay fix.

PyPI's cryptography>=50.0.0 links PyLong_Type / PyExc_Warning expecting them in
the main executable. Termux's CPython does not re-export those symbols from the
main binary — they live only in libpython3.x.so — so cryptography's abi3
extension fails to dlopen at runtime with::

    ImportError: dlopen failed: cannot locate symbol "PyLong_Type"

The Termux dpkg ``python-cryptography`` is patched (NEEDED libpython3.x.so +
RUNPATH -> $PREFIX/lib), so it loads. This module copies the distro build over
the broken pip overlay after any venv rebuild (installer or ``hermes update``),
which would otherwise re-introduce the overlay.

The smoke test imports a hazmat submodule. A shallow ``import cryptography``
does NOT load ``_rust`` and falsely passes.

See NousResearch/hermes-agent#83680 / #85972.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess

logger = logging.getLogger(__name__)


def _hazmat_ok(venv_py: str) -> bool:
    """Return True when the venv's cryptography can load the abi3 extension."""
    result = subprocess.run(
        [venv_py, "-c", "from cryptography.hazmat.primitives import hashes"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    return result.returncode == 0


def fix_termux_cryptography_overlay(venv_path: "str | os.PathLike[str]") -> bool:
    """Replace the pip cryptography overlay with the Termux distro build.

    Returns True when the working build is in place (or was already fine),
    False when the hazmat smoke test still fails after the copy. Callers may
    choose to abort setup/update on False.
    """
    venv_path = str(venv_path)
    venv_py = os.path.join(venv_path, "bin", "python")
    if not os.path.isfile(venv_py):
        return True

    # PREFIX is always set on Termux; fall back to the canonical path otherwise.
    prefix = os.environ.get("PREFIX") or "/data/data/com.termux/files/usr"
    try:
        pyminor = subprocess.run(
            [venv_py, "-c", 'import sys;print(f"{sys.version_info.major}.{sys.version_info.minor}")'],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        ).stdout.strip()
    except (subprocess.CalledProcessError, OSError):
        return True
    if not pyminor:
        return True

    sys_site = os.path.join(prefix, "lib", f"python{pyminor}", "site-packages")
    venv_site = os.path.join(venv_path, "lib", f"python{pyminor}", "site-packages")
    sys_crypto = os.path.join(sys_site, "cryptography")
    if not os.path.isdir(sys_crypto) or not os.path.isdir(venv_site):
        return True

    if _hazmat_ok(venv_py):
        return True

    logger.info("Termux: replacing pip cryptography with distro build (PyLong_Type fix)")
    try:
        subprocess.run(
            [venv_py, "-m", "pip", "uninstall", "-y", "cryptography"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        for entry in os.listdir(venv_site):
            if entry.startswith("cryptography"):
                path = os.path.join(venv_site, entry)
                if os.path.isdir(path):
                    shutil.rmtree(path)
                else:
                    os.remove(path)
        shutil.copytree(sys_crypto, os.path.join(venv_site, "cryptography"))
        for dist in os.listdir(sys_site):
            if dist.startswith("cryptography-") and dist.endswith(".dist-info"):
                shutil.copytree(
                    os.path.join(sys_site, dist),
                    os.path.join(venv_site, dist),
                    dirs_exist_ok=True,
                )
    except (subprocess.CalledProcessError, OSError) as exc:
        logger.warning("Termux cryptography overlay copy failed: %s", exc)
        return False

    if _hazmat_ok(venv_py):
        logger.info("cryptography Termux build in place (PyLong_Type resolved)")
        return True
    logger.error(
        "cryptography Termux build still failing — Hermes will crash on "
        "Bitwarden/secret sources at startup. Manual fix: cp -r %s/cryptography* %s/",
        sys_site,
        venv_site,
    )
    return False
