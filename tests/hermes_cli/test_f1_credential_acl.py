"""F1 regression tests: credential files must be restricted to the current user.

Cross-sandbox credential exposure (F1): Hermes writes fresh Anthropic OAuth
tokens to ~/.claude/.credentials.json and imports OpenAI tokens from
~/.codex/auth.json. POSIX ``0o600`` mode bits are meaningless on Windows —
the file inherits the parent directory's ACL (commonly granting ``Users`` /
sandbox groups read access). These tests pin:

- the cross-platform ``restrict_credential_file`` / ``credential_file_is_user_restricted``
  helpers (POSIX mode-bit path; the Windows icacls path is exercised live on
  Windows hosts),
- the fail-closed import gate in ``_import_codex_cli_tokens`` (refuses to
  bless a credential from a file readable by other accounts).
"""

import json
import os
import stat

import pytest

import hermes_constants as hc
from hermes_cli import auth


# ---------------------------------------------------------------------------
# Cross-platform helpers (hermes_constants)
# ---------------------------------------------------------------------------


class TestCredentialFileRestrictionHelpers:
    def test_posix_mode_bits_0644_not_restricted(self, tmp_path):
        f = tmp_path / "creds.json"
        f.write_text("{}", encoding="utf-8")
        if os.name != "nt":
            os.chmod(f, 0o644)
            assert hc.credential_file_is_user_restricted(f) is False
        else:
            # On Windows, write_text lands with inherited ACLs (SYSTEM +
            # Administrators + OWNER RIGHTS are benign) — the helper must
            # still call the file "restricted" (no other-account ACEs).
            assert hc.credential_file_is_user_restricted(f) is True

    def test_posix_mode_bits_0600_restricted(self, tmp_path):
        f = tmp_path / "creds.json"
        f.write_text("{}", encoding="utf-8")
        if os.name != "nt":
            os.chmod(f, 0o600)
            assert hc.credential_file_is_user_restricted(f) is True

    def test_restrict_credential_file_posix(self, tmp_path):
        f = tmp_path / "creds.json"
        f.write_text("{}", encoding="utf-8")
        if os.name != "nt":
            os.chmod(f, 0o644)
            assert hc.restrict_credential_file(f) is True
            assert stat.S_IMODE(os.stat(f).st_mode) == 0o600
            assert hc.credential_file_is_user_restricted(f) is True

    def test_missing_file_fails_closed(self, tmp_path):
        # A path that cannot be verified must report NOT restricted so
        # callers fail closed rather than trusting an unknown ACL.
        assert hc.credential_file_is_user_restricted(tmp_path / "nope.json") is False


# ---------------------------------------------------------------------------
# _import_codex_cli_tokens fail-closed gate
# ---------------------------------------------------------------------------


class TestImportCodexCliTokensAclGate:
    def _write_codex_auth(self, codex_home, *, chmod_0600=True):
        codex_home.mkdir(parents=True, exist_ok=True)
        f = codex_home / "auth.json"
        f.write_text(
            json.dumps({
                "tokens": {
                    "access_token": "at",
                    "refresh_token": "rt",
                }
            }),
            encoding="utf-8",
        )
        if chmod_0600 and os.name != "nt":
            os.chmod(f, 0o600)
        return f

    def test_imports_from_user_restricted_file(self, tmp_path, monkeypatch):
        codex_home = tmp_path / "codex"
        self._write_codex_auth(codex_home)
        monkeypatch.setenv("CODEX_HOME", str(codex_home))
        monkeypatch.setattr(auth, "_codex_access_token_is_expiring", lambda *a, **k: False)

        tokens = auth._import_codex_cli_tokens()
        assert tokens is not None
        assert tokens["access_token"] == "at"

    def test_refuses_group_readable_file(self, tmp_path, monkeypatch, caplog):
        codex_home = tmp_path / "codex"
        f = self._write_codex_auth(codex_home, chmod_0600=False)
        monkeypatch.setenv("CODEX_HOME", str(codex_home))
        if os.name != "nt":
            os.chmod(f, 0o644)
        else:
            # On Windows a freshly written file inherits only benign ACEs
            # (SYSTEM/Administrators/Owner Rights). Grant the built-in
            # Users group read access to reproduce the F1 exposure shape
            # (an inherited/group ACE readable by other local accounts).
            import subprocess
            res = subprocess.run(
                ["icacls", str(f), "/grant", "Users:(R)"],
                capture_output=True, text=True,
            )
            assert res.returncode == 0, res.stdout + res.stderr

        with caplog.at_level("WARNING", logger="hermes_cli.auth"):
            assert auth._import_codex_cli_tokens() is None
        assert "Refusing to import tokens" in caplog.text

    def test_refuses_when_acl_check_fails(self, tmp_path, monkeypatch, caplog):
        codex_home = tmp_path / "codex"
        self._write_codex_auth(codex_home)
        monkeypatch.setenv("CODEX_HOME", str(codex_home))

        def _boom(path):
            raise RuntimeError("icacls unavailable")
        monkeypatch.setattr(hc, "credential_file_is_user_restricted", _boom)

        with caplog.at_level("WARNING", logger="hermes_cli.auth"):
            assert auth._import_codex_cli_tokens() is None
        assert "could not verify the file" in caplog.text
