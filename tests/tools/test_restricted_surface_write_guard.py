"""Tests for the sensitive-path write guard on messaging platforms.

Run with: python -m pytest tests/tools/test_restricted_surface_write_guard.py -v

Coverage model (repo doctrine, see tests-os.yml + tests/conftest.py):
- The cross-platform logic tests are UNMARKED: they run in the default
  Linux lane and on any local host. Windows path conventions are exercised
  on every host through the real ``ntpath`` primitives via a
  case-normalization shim (the only OS-dependent primitive in the guard),
  and the POSIX default home is simulated by patching the platform-default
  resolver.
- Real-OS behaviour that cannot be reproduced on another host (the native
  Windows ``os.path``/filesystem semantics, the native macOS casefold
  branch) is covered by ``windows_only`` / ``macos_only`` marked tests
  that execute on the windows-latest / macos-latest lanes of tests-os.yml.
"""

import contextlib
import ntpath
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pytest

import hermes_constants
from tools.approval import reset_current_session_key, set_current_session_key
from tools.file_tools import (
    _check_sensitive_messaging_path,
    _execution_trusting_prefixes,
    _messaging_platform_from_key,
    _normalize_guard_path,
)

# Windows extended-length prefixes accepted by the OS (stripped by the
# guard's Windows normalization).
_WIN_EXTENDED_PREFIXES = ("\\\\?\\", "\\\\.\\", "\\??\\")


@contextlib.contextmanager
def _bind_key(key: str):
    token = set_current_session_key(key)
    try:
        yield
    finally:
        reset_current_session_key(token)


@contextlib.contextmanager
def _default_home():
    """Simulate the POSIX default home (~/.hermes) on every platform.

    The guard resolves execution-trusting roots from the LIVE active
    Hermes home (get_hermes_home()), so tests asserting on the default
    ~/.hermes layout must neutralize any HERMES_HOME the runner carries
    and pin the platform-default resolver to the POSIX convention.
    """
    with patch.dict(os.environ, {"HERMES_HOME": ""}), \
            patch.object(
                hermes_constants, "_get_platform_default_hermes_home",
                return_value=Path.home() / ".hermes",
            ):
        yield


def _win_case_normalize(path: str) -> str:
    """Windows case/separator semantics, using the real ntpath primitives."""
    for prefix in _WIN_EXTENDED_PREFIXES:
        if path.startswith(prefix):
            path = path[len(prefix):]
            break
    return ntpath.normcase(path).replace("\\", "/")


@contextlib.contextmanager
def _win_semantics():
    """Run the guard with Windows normalization semantics on any host."""
    with patch("tools.file_tools._guard_case_normalize",
               side_effect=_win_case_normalize):
        yield


class TestSessionPlatform(unittest.TestCase):
    def test_gateway_key_parses_platform(self):
        with _bind_key("agent:main:telegram:dm:123"):
            self.assertEqual(_messaging_platform_from_key(), "telegram")

    def test_named_profile_gateway_key(self):
        with _bind_key("agent:cissp-coach:discord:g:9"):
            self.assertEqual(_messaging_platform_from_key(), "discord")

    def test_unregistered_platform_token_is_none(self):
        with _bind_key("agent:main:not_a_real_platform:dm:1"):
            self.assertIsNone(_messaging_platform_from_key())

    def test_local_platform_is_none(self):
        with _bind_key("agent:main:local:dm:1"):
            self.assertIsNone(_messaging_platform_from_key())

    def test_malformed_key_is_none(self):
        with _bind_key("agent:main"):
            self.assertIsNone(_messaging_platform_from_key())

    def test_cli_key_has_no_platform(self):
        with _bind_key("default"):
            self.assertIsNone(_messaging_platform_from_key())
        with _bind_key("20260811_093018_2cb019f0"):
            self.assertIsNone(_messaging_platform_from_key())

    def test_no_key_binds_none(self):
        self.assertIsNone(_messaging_platform_from_key())


class TestSensitiveMessagingPathWrite(unittest.TestCase):
    def test_telegram_cron_write_blocked(self):
        with _bind_key("agent:main:telegram:dm:1"), _default_home():
            err = _check_sensitive_messaging_path("~/.hermes/cron/jobs.json")
            self.assertIsNotNone(err)
            self.assertIn("BLOCKED", err)
            self.assertIn("telegram", err)

    def test_telegram_scripts_write_blocked(self):
        with _bind_key("agent:main:telegram:dm:1"), _default_home():
            self.assertIsNotNone(
                _check_sensitive_messaging_path("~/.hermes/scripts/evil.sh")
            )

    def test_telegram_ssh_write_blocked(self):
        with _bind_key("agent:main:telegram:dm:1"), _default_home():
            self.assertIsNotNone(
                _check_sensitive_messaging_path("~/.ssh/id_ed25519")
            )

    def test_telegram_unrelated_paths_allowed(self):
        with _bind_key("agent:main:telegram:dm:1"), _default_home():
            self.assertIsNone(_check_sensitive_messaging_path("/tmp/note.md"))
            # Non-execution hermes paths (logs, etc.) remain writable
            self.assertIsNone(
                _check_sensitive_messaging_path("~/.hermes/logs/app.log")
            )

    def test_cli_never_blocked(self):
        with _bind_key("default"), _default_home():
            self.assertIsNone(
                _check_sensitive_messaging_path("~/.hermes/cron/jobs.json")
            )
        with _bind_key("20260811_093018_2cb019f0"), _default_home():
            self.assertIsNone(
                _check_sensitive_messaging_path("~/.hermes/scripts/x.sh")
            )

    def test_unregistered_platform_not_blocked(self):
        with _bind_key("agent:main:not_a_real_platform:dm:1"), _default_home():
            self.assertIsNone(
                _check_sensitive_messaging_path("~/.hermes/cron/jobs.json")
            )

    def test_local_platform_not_blocked(self):
        with _bind_key("agent:main:local:dm:1"), _default_home():
            self.assertIsNone(
                _check_sensitive_messaging_path("~/.hermes/cron/jobs.json")
            )

    def test_other_gateway_platforms_blocked(self):
        with _bind_key("agent:main:slack:dm:9"), _default_home():
            self.assertIsNotNone(
                _check_sensitive_messaging_path("~/.hermes/cron/jobs.json")
            )
        with _bind_key("agent:main:whatsapp:dm:7"), _default_home():
            self.assertIsNotNone(
                _check_sensitive_messaging_path("~/.ssh/authorized_keys")
            )


class TestExecutionTrustingRootsFollowActiveHome(unittest.TestCase):
    """Regression coverage for the live-home derivation (reviewer P1).

    The execution-trusting roots must follow the ACTIVE Hermes home
    (get_hermes_home(): context override → HERMES_HOME env → platform
    default), never literal ~/.hermes frozen at import: cron jobs and
    scripts are stored and executed under the active profile home
    (cron/jobs.py, cron/scheduler.py, per-profile isolation #4707), the
    Windows default home is %LOCALAPPDATA%\\hermes, and custom/profile
    deployments set HERMES_HOME elsewhere.
    """

    def test_custom_hermes_home_roots_blocked(self):
        with _bind_key("agent:main:telegram:dm:1"), \
                tempfile.TemporaryDirectory() as tmp, \
                patch.dict(os.environ, {"HERMES_HOME": tmp}):
            self.assertIsNotNone(
                _check_sensitive_messaging_path(os.path.join(tmp, "cron", "jobs.json"))
            )
            self.assertIsNotNone(
                _check_sensitive_messaging_path(os.path.join(tmp, "scripts", "x.sh"))
            )
            # Unrelated paths under the same home remain writable
            self.assertIsNone(
                _check_sensitive_messaging_path(os.path.join(tmp, "notes.md"))
            )

    def test_profile_style_home_roots_blocked(self):
        # A profile-scoped gateway runs jobs under HERMES_HOME=<root>/profiles/<name>
        with _bind_key("agent:mypro:telegram:dm:1"), \
                tempfile.TemporaryDirectory() as tmp:
            profile_home = os.path.join(tmp, "profiles", "mypro")
            with patch.dict(os.environ, {"HERMES_HOME": profile_home}):
                self.assertIsNotNone(
                    _check_sensitive_messaging_path(
                        os.path.join(profile_home, "scripts", "x.sh")
                    )
                )
                self.assertIsNotNone(
                    _check_sensitive_messaging_path(
                        os.path.join(profile_home, "cron", "jobs.json")
                    )
                )

    def test_custom_home_local_session_not_blocked(self):
        with _bind_key("default"), \
                tempfile.TemporaryDirectory() as tmp, \
                patch.dict(os.environ, {"HERMES_HOME": tmp}):
            self.assertIsNone(
                _check_sensitive_messaging_path(os.path.join(tmp, "scripts", "x.sh"))
            )

    def test_prefixes_follow_active_home(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.dict(os.environ, {"HERMES_HOME": tmp}):
            prefixes = _execution_trusting_prefixes()
            self.assertIn(_normalize_guard_path(os.path.join(tmp, "cron")), prefixes)
            self.assertIn(_normalize_guard_path(os.path.join(tmp, "scripts")), prefixes)
            # .ssh stays anchored to the OS-user home, NOT HERMES_HOME
            self.assertNotIn(_normalize_guard_path(os.path.join(tmp, ".ssh")), prefixes)
            # identical env/live home dedupes to a single root pair
            self.assertEqual(
                prefixes.count(_normalize_guard_path(os.path.join(tmp, "cron"))), 1
            )

    def test_override_scoped_session_blocks_both_homes(self):
        # A session context-scoped to another profile (set_hermes_home_override)
        # must not bypass the roots of the env home whose cron THIS process's
        # ticker executes — both homes are protected.
        with _bind_key("agent:main:telegram:dm:1"), \
                tempfile.TemporaryDirectory() as env_home, \
                tempfile.TemporaryDirectory() as override_home, \
                patch.dict(os.environ, {"HERMES_HOME": env_home}):
            token = hermes_constants.set_hermes_home_override(override_home)
            try:
                self.assertIsNotNone(
                    _check_sensitive_messaging_path(
                        os.path.join(env_home, "scripts", "x.sh")
                    )
                )
                self.assertIsNotNone(
                    _check_sensitive_messaging_path(
                        os.path.join(override_home, "scripts", "x.sh")
                    )
                )
                self.assertIsNotNone(
                    _check_sensitive_messaging_path(
                        os.path.join(override_home, "cron", "jobs.json")
                    )
                )
            finally:
                hermes_constants.reset_hermes_home_override(token)

    def test_sibling_profile_roots_blocked(self):
        # A messaging session in one profile must not plant payloads in
        # another profile's execution-trusting roots (scripts is not even
        # covered by the cross-profile soft guard, and cron is bypassable
        # via cross_profile=True), nor in the default profile's roots at
        # the shared root level.
        with _bind_key("agent:A:telegram:dm:1"), \
                tempfile.TemporaryDirectory() as root:
            profile_a = os.path.join(root, "profiles", "A")
            profile_b = os.path.join(root, "profiles", "B")
            os.makedirs(profile_a)
            os.makedirs(profile_b)
            with patch.object(
                    hermes_constants, "get_default_hermes_root",
                    return_value=Path(root)), \
                    patch.dict(os.environ, {"HERMES_HOME": profile_a}):
                self.assertIsNotNone(
                    _check_sensitive_messaging_path(
                        os.path.join(profile_b, "scripts", "x.sh")
                    )
                )
                self.assertIsNotNone(
                    _check_sensitive_messaging_path(
                        os.path.join(profile_b, "cron", "jobs.json")
                    )
                )
                # Default profile's execution roots at the shared root
                self.assertIsNotNone(
                    _check_sensitive_messaging_path(
                        os.path.join(root, "cron", "jobs.json")
                    )
                )
                self.assertIsNotNone(
                    _check_sensitive_messaging_path(
                        os.path.join(root, "scripts", "x.sh")
                    )
                )
                # Own profile still blocked (regression)
                self.assertIsNotNone(
                    _check_sensitive_messaging_path(
                        os.path.join(profile_a, "scripts", "x.sh")
                    )
                )
                # Unrelated root paths stay writable
                self.assertIsNone(
                    _check_sensitive_messaging_path(
                        os.path.join(root, "notes.md")
                    )
                )

    def test_ssh_anchored_to_os_user_home_not_hermes_home(self):
        with _bind_key("agent:main:telegram:dm:1"), \
                tempfile.TemporaryDirectory() as tmp, \
                patch.dict(os.environ, {"HERMES_HOME": tmp}):
            # Real user-home .ssh stays blocked even with HERMES_HOME elsewhere
            self.assertIsNotNone(
                _check_sensitive_messaging_path("~/.ssh/id_ed25519")
            )
            # HERMES_HOME/.ssh is not an execution-trusting root
            self.assertIsNone(
                _check_sensitive_messaging_path(
                    os.path.join(tmp, ".ssh", "id_ed25519")
                )
            )

    def test_windows_conventions_roots_blocked(self):
        # Windows default home (%LOCALAPPDATA%\hermes) and Windows path
        # conventions — forward/backslash separators, case variants,
        # extended-length prefixes — exercised with the real ntpath
        # primitives on every platform (the native-OS variant runs in the
        # windows_only lane of tests-os.yml).
        with _bind_key("agent:main:telegram:dm:1"), \
                _win_semantics(), \
                patch.dict(os.environ, {"HERMES_HOME": ""}), \
                patch.object(
                    hermes_constants, "_get_platform_default_hermes_home",
                    return_value=Path("C:/Users/t/AppData/Local/hermes"),
                ):
            self.assertIsNotNone(
                _check_sensitive_messaging_path(
                    "C:/Users/t/AppData/Local/hermes/scripts/x.ps1"
                )
            )
            self.assertIsNotNone(
                _check_sensitive_messaging_path(
                    "C:/Users/t/AppData/Local/hermes/cron/jobs.json"
                )
            )
            # Backslash separators
            self.assertIsNotNone(
                _check_sensitive_messaging_path(
                    r"C:\Users\t\AppData\Local\hermes\scripts\x.ps1"
                )
            )
            # Case variants (NTFS is case-insensitive)
            self.assertIsNotNone(
                _check_sensitive_messaging_path(
                    "C:/Users/t/AppData/Local/HERMES/Scripts/x.ps1"
                )
            )
            self.assertIsNotNone(
                _check_sensitive_messaging_path(
                    "C:/Users/t/AppData/Local/Hermes/CRON/jobs.json"
                )
            )
            # Extended-length prefix (\\?\C:\...) accepted by the OS
            self.assertIsNotNone(
                _check_sensitive_messaging_path(
                    r"\\?\C:\Users\t\AppData\Local\hermes\scripts\x.ps1"
                )
            )
            # Unrelated Windows paths remain writable
            self.assertIsNone(
                _check_sensitive_messaging_path(
                    "C:/Users/t/AppData/Local/hermes/logs/app.log"
                )
            )

    def test_case_sensitivity_follows_platform(self):
        # The guard must mirror the filesystem: on case-insensitive default
        # filesystems (macOS APFS) a case variant of the default home is the
        # same directory and is blocked; on case-sensitive filesystems
        # (Linux) it is a genuinely different directory and stays writable.
        # (The native Windows variant runs in the windows_only lane.)
        with _bind_key("agent:main:telegram:dm:1"), _default_home():
            variant = _check_sensitive_messaging_path("~/.HERMES/SCRIPTS/evil.sh")
            if sys.platform == "darwin":
                self.assertIsNotNone(variant)
            else:
                self.assertIsNone(variant)

    def test_symlink_alias_into_scripts_dir_blocked(self):
        # A path reaching the execution-trusting root through a symlinked
        # directory is canonicalized via the existing parent and blocked.
        with _bind_key("agent:main:telegram:dm:1"), \
                tempfile.TemporaryDirectory() as tmp:
            real = os.path.join(tmp, "scripts")
            os.makedirs(real)
            alias = os.path.join(tmp, "alias")
            try:
                os.symlink(real, alias)
            except OSError as exc:
                self.skipTest(f"symlinks unavailable on this host: {exc}")
            with patch.dict(os.environ, {"HERMES_HOME": tmp}):
                self.assertIsNotNone(
                    _check_sensitive_messaging_path(os.path.join(alias, "x.sh"))
                )
                # Unrelated symlinked directories are not affected
                other = os.path.join(tmp, "other")
                os.makedirs(other)
                other_alias = os.path.join(tmp, "other_alias")
                os.symlink(other, other_alias)
                self.assertIsNone(
                    _check_sensitive_messaging_path(os.path.join(other_alias, "x.sh"))
                )


@pytest.mark.windows_only
class TestWindowsNativeGuard(unittest.TestCase):
    """Native-Windows behaviour, run on windows-latest (tests-os.yml).

    Unlike the cross-platform conventions test above (which exercises the
    guard's Windows branch with the real ntpath primitives on any host),
    this class runs with the REAL ``os.name == "nt"`` branch, real
    ``os.path`` semantics and the real Windows filesystem behind it.
    """

    def test_native_windows_roots_blocked(self):
        with _bind_key("agent:main:telegram:dm:1"), \
                patch.dict(os.environ, {"HERMES_HOME": ""}), \
                patch.object(
                    hermes_constants, "_get_platform_default_hermes_home",
                    return_value=Path("C:/Users/t/AppData/Local/hermes"),
                ):
            self.assertIsNotNone(
                _check_sensitive_messaging_path(
                    "C:/Users/t/AppData/Local/hermes/scripts/x.ps1"
                )
            )
            self.assertIsNotNone(
                _check_sensitive_messaging_path(
                    r"C:\Users\t\AppData\Local\hermes\scripts\x.ps1"
                )
            )
            # Case variants under the native normcase
            self.assertIsNotNone(
                _check_sensitive_messaging_path(
                    "C:/Users/t/AppData/Local/HERMES/Scripts/x.ps1"
                )
            )
            # Extended-length prefix under the native OS path handling
            self.assertIsNotNone(
                _check_sensitive_messaging_path(
                    r"\\?\C:\Users\t\AppData\Local\hermes\scripts\x.ps1"
                )
            )

    def test_native_windows_custom_home_and_ssh(self):
        with _bind_key("agent:main:telegram:dm:1"), \
                tempfile.TemporaryDirectory() as tmp, \
                patch.dict(os.environ, {"HERMES_HOME": tmp}):
            self.assertIsNotNone(
                _check_sensitive_messaging_path(
                    os.path.join(tmp, "scripts", "x.ps1")
                )
            )
            # Real user-home .ssh blocked; HERMES_HOME/.ssh is not a root
            self.assertIsNotNone(
                _check_sensitive_messaging_path("~/.ssh/id_ed25519")
            )
            self.assertIsNone(
                _check_sensitive_messaging_path(
                    os.path.join(tmp, ".ssh", "id_ed25519")
                )
            )


@pytest.mark.macos_only
class TestMacOSNativeGuard(unittest.TestCase):
    """Native-macOS behaviour, run on macos-latest (tests-os.yml)."""

    def test_native_macos_case_variant_blocked(self):
        with _bind_key("agent:main:telegram:dm:1"), _default_home():
            self.assertIsNotNone(
                _check_sensitive_messaging_path("~/.HERMES/SCRIPTS/evil.sh")
            )
            self.assertIsNotNone(
                _check_sensitive_messaging_path("~/.hermes/Cron/jobs.json")
            )


if __name__ == "__main__":
    unittest.main()
