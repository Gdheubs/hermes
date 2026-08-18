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
  bless a credential from a file readable by other accounts),
- F1/P3: SID-based identity on Windows (same leaf name on different domains
  is NOT the current user) and restrict-BEFORE-write ordering (no credential
  bytes are ever written when the destination cannot be restricted).
"""

import json
import os
import stat
from pathlib import Path

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


# ---------------------------------------------------------------------------
# F1/P3: Windows identity must be compared by SID, not display-name suffix
# ---------------------------------------------------------------------------


class TestWindowsSidIdentity:
    """The Windows verifier must compare ACL principals by SID. DOMAIN_A\alice
    and DOMAIN_B\alice are different accounts despite sharing a leaf name; a
    local and a domain account with the same name must never be conflated.
    icacls output is synthetic (subprocess.run patched) so the identity logic
    is deterministic on every host."""

    USER_SID = "S-1-5-21-AAAAAAAA-BBBBBBBB-CCCCCCCC-1001"
    OTHER_SID = "S-1-5-21-AAAAAAAA-BBBBBBBB-CCCCCCCC-2002"

    def _verify(self, monkeypatch, icacls_lines, resolver):
        import subprocess
        from types import SimpleNamespace

        monkeypatch.setattr(hc.os, "name", "nt")  # force the Windows branch
        fake = SimpleNamespace(
            returncode=0, stdout="\n".join(icacls_lines), stderr=""
        )
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: fake)
        monkeypatch.setattr(hc, "_current_windows_user", lambda: "alice")
        monkeypatch.setattr(hc, "_resolve_account_sid", resolver)
        return hc.credential_file_is_user_restricted(Path(r"C:\fake\path\creds.json"))

    def test_same_leaf_different_domain_rejected(self, monkeypatch):
        """DOMAIN_A\alice must NOT count as the current user when it resolves
        to a different SID (the pre-fix leaf-name match accepted it)."""
        def resolver(account):
            return {
                "alice": self.USER_SID,
                "domain_a\\alice": self.OTHER_SID,
            }.get(account.lower())

        ok = self._verify(monkeypatch, [r"C:\fake\path\creds.json DOMAIN_A\alice:(R,W)"], resolver)
        assert ok is False

    def test_same_sid_different_display_name_accepted(self, monkeypatch):
        """The current user under a different display name (e.g. the domain
        prefix) must still be accepted when the SID matches."""
        def resolver(account):
            return {
                "alice": self.USER_SID,
                "domain_b\\alice": self.USER_SID,
            }.get(account.lower())

        ok = self._verify(monkeypatch, [r"C:\fake\path\creds.json DOMAIN_B\alice:(R,W)"], resolver)
        assert ok is True

    def test_sid_form_current_user_accepted(self, monkeypatch):
        """icacls prints unresolvable accounts in *SID form — the current
        user's own SID is accepted, any other SID is not."""
        ok = self._verify(
            monkeypatch,
            [rf"C:\fake\path\creds.json *{self.USER_SID}:(R,W)"],
            lambda a: self.USER_SID if a == "alice" else None,
        )
        assert ok is True

    def test_other_sid_form_rejected(self, monkeypatch):
        ok = self._verify(
            monkeypatch,
            [rf"C:\fake\path\creds.json *{self.OTHER_SID}:(R,W)"],
            lambda a: self.USER_SID if a == "alice" else None,
        )
        assert ok is False

    def test_unresolvable_ace_fails_closed(self, monkeypatch):
        """An ACE whose account cannot be resolved to a SID must fail closed,
        not be trusted by name."""
        ok = self._verify(
            monkeypatch,
            [r"C:\fake\path\creds.json MYSTERY\guy:(R,W)"],
            lambda a: self.USER_SID if a == "alice" else None,
        )
        assert ok is False

    def test_benign_plus_current_user_only_accepted(self, monkeypatch):
        ok = self._verify(
            monkeypatch,
            [
                r"C:\fake\path\creds.json NT AUTHORITY\SYSTEM:(I)(F)",
                r"C:\fake\path\creds.json BUILTIN\Administrators:(I)(F)",
                r"C:\fake\path\creds.json OWNER RIGHTS:(I)(F)",
                rf"C:\fake\path\creds.json *{self.USER_SID}:(R,W)",
            ],
            lambda a: self.USER_SID if a == "alice" else None,
        )
        assert ok is True


# ---------------------------------------------------------------------------
# F1/P3: restrict BEFORE write — no credential bytes when ACL cannot be set
# ---------------------------------------------------------------------------


class TestNoBytesBeforeAcl:
    def test_claude_credentials_acl_failure_writes_nothing(self, tmp_path, monkeypatch):
        """If the temp file cannot be restricted, NO credential bytes are
        written and an existing destination is left byte-identical."""
        import agent.anthropic_adapter as aa

        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        cred = tmp_path / ".claude" / ".credentials.json"
        cred.parent.mkdir(parents=True, exist_ok=True)
        cred.write_text('{"claudeAiOauth": {"accessToken": "old-token"}}', encoding="utf-8")
        original = cred.read_bytes()

        state = {"temp_size_at_restrict": None}

        def _fake_restrict(path):
            state["temp_size_at_restrict"] = Path(path).stat().st_size
            return False

        monkeypatch.setattr(aa, "restrict_credential_file", _fake_restrict)
        aa._write_claude_code_credentials("new-token", "new-refresh", 99999)

        assert cred.read_bytes() == original, "destination must be untouched"
        assert state["temp_size_at_restrict"] == 0, (
            "restriction must run BEFORE any credential bytes are written"
        )
        leftovers = list(cred.parent.glob("*.tmp.*"))
        assert leftovers == [], f"temp credential files left behind: {leftovers}"

    def test_claude_credentials_verify_failure_persists_nothing(self, tmp_path, monkeypatch):
        """If the bytes-bearing temp loses its restriction before replacement,
        the write fails closed and the destination is untouched."""
        import agent.anthropic_adapter as aa

        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        cred = tmp_path / ".claude" / ".credentials.json"
        cred.parent.mkdir(parents=True, exist_ok=True)
        cred.write_text('{"claudeAiOauth": {"accessToken": "old-token"}}', encoding="utf-8")
        original = cred.read_bytes()

        monkeypatch.setattr(aa, "restrict_credential_file", lambda p: True)
        monkeypatch.setattr(aa, "credential_file_is_user_restricted", lambda p: False)
        aa._write_claude_code_credentials("new-token", "new-refresh", 99999)

        assert cred.read_bytes() == original, "destination must be untouched"
        leftovers = list(cred.parent.glob("*.tmp.*"))
        assert leftovers == []


class TestSaveAuthStoreFailClosed:
    def test_save_rejected_when_temp_cannot_be_restricted(self, tmp_path, monkeypatch):
        """_save_auth_store must fail CLOSED when the temp cannot be
        restricted: no bytes written, destination untouched, no temp left."""
        target = tmp_path / "auth.json"
        target.write_text(
            json.dumps({"version": 1, "providers": {"nous": {"api_key": "old"}}}),
            encoding="utf-8",
        )
        original = target.read_bytes()

        state = {"temp_size_at_restrict": None}

        def _fake_restrict(path):
            state["temp_size_at_restrict"] = Path(path).stat().st_size
            return False

        monkeypatch.setattr(hc, "restrict_credential_file", _fake_restrict)
        with pytest.raises(OSError, match="F1"):
            auth._save_auth_store(
                {"version": 1, "providers": {"nous": {"api_key": "new"}}},
                target_path=target,
            )

        assert target.read_bytes() == original, "destination must be untouched"
        assert state["temp_size_at_restrict"] == 0, (
            "restriction must run BEFORE any credential bytes are written"
        )
        assert not list(tmp_path.glob("auth.json.tmp.*")), "temp store left behind"

    def test_save_rejected_when_destination_not_verified_restricted(self, tmp_path, monkeypatch):
        """After the atomic replace, the destination must verify as
        user-restricted; failure to verify raises (fail closed) instead of
        silently leaving the store exposed."""
        target = tmp_path / "auth.json"
        target.write_text(
            json.dumps({"version": 1, "providers": {}}), encoding="utf-8"
        )
        monkeypatch.setattr(hc, "restrict_credential_file", lambda p: True)
        monkeypatch.setattr(hc, "credential_file_is_user_restricted", lambda p: False)
        with pytest.raises(OSError, match="F1"):
            auth._save_auth_store(
                {"version": 1, "providers": {"nous": {"api_key": "new"}}},
                target_path=target,
            )
