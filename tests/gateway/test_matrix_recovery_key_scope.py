"""Regression test for #69090: MATRIX_RECOVERY_KEY must honor the active
profile's secret scope under ``gateway.multiplex_profiles`` so that a
secondary profile resolves its own recovery key (not the default profile's),
otherwise E2EE cross-signing verification fails with "Key MAC does not match".

The fix routes the recovery-key read through ``_scoped_recovery_key()``,
which uses :func:`agent.secret_scope.get_secret` (scope-aware) and only falls
back to ``os.getenv`` for an *unscoped* read under multiplex — mirroring the
established Slack app-token pattern (#59739).
"""
import pytest

from agent import secret_scope as ss
from plugins.platforms.matrix.adapter import _scoped_recovery_key, _startup_env_secret


@pytest.fixture(autouse=True)
def _reset_multiplex():
    """Ensure each test starts and ends with multiplexing off (it's a global)."""
    ss.set_multiplex_active(False)
    yield
    ss.set_multiplex_active(False)


class TestScopedRecoveryKey:
    def test_multiplex_inactive_reads_environ(self, monkeypatch):
        """Default deployment: get_secret transparently reads os.environ."""
        monkeypatch.setenv("MATRIX_RECOVERY_KEY", "default-profile-key")
        assert _scoped_recovery_key() == "default-profile-key"

    def test_multiplex_active_scoped_uses_scope_not_environ(self, monkeypatch):
        """Secondary profile under multiplex must resolve its own key.

        This is the core regression: ``os.getenv`` would have returned the
        default profile's key (from os.environ), failing verification.
        """
        monkeypatch.setenv("MATRIX_RECOVERY_KEY", "default-profile-key")
        ss.set_multiplex_active(True)
        token = ss.set_secret_scope({"MATRIX_RECOVERY_KEY": "secondary-profile-key"})
        try:
            assert _scoped_recovery_key() == "secondary-profile-key"
        finally:
            ss.reset_secret_scope(token)

    def test_multiplex_active_unscoped_falls_back_to_environ(self, monkeypatch):
        """Default-profile startup loop under multiplex: unscoped read is fine.

        An unscoped read raises ``UnscopedSecretError``; in that context
        os.environ holds that profile's own value, so we fall back to it rather
        than crashing startup. This matches the Slack adapter's behavior.
        """
        monkeypatch.setenv("MATRIX_RECOVERY_KEY", "default-profile-key")
        ss.set_multiplex_active(True)
        # No secret scope installed -> get_secret raises UnscopedSecretError.
        assert _scoped_recovery_key() == "default-profile-key"

    def test_multiplex_active_scoped_missing_key_is_empty(self, monkeypatch):
        """A scope without the key must NOT fall through to another profile's env.

        If the secondary profile hasn't configured a recovery key, the scope is
        authoritative: we return empty rather than silently borrowing the
        default profile's key (which would fail verification with a confusing
        "Key MAC does not match").
        """
        monkeypatch.setenv("MATRIX_RECOVERY_KEY", "default-profile-key")
        ss.set_multiplex_active(True)
        token = ss.set_secret_scope({"SOME_OTHER_KEY": "x"})
        try:
            assert _scoped_recovery_key() == ""
        finally:
            ss.reset_secret_scope(token)

    def test_strips_whitespace(self, monkeypatch):
        monkeypatch.setenv("MATRIX_RECOVERY_KEY", "  padded-key  \n")
        assert _scoped_recovery_key() == "padded-key"

    def test_unset_returns_empty(self, monkeypatch):
        monkeypatch.delenv("MATRIX_RECOVERY_KEY", raising=False)
        assert _scoped_recovery_key() == ""


class TestMatrixStartupEnvSecret:
    def test_single_profile_uses_external_secret_snapshot_when_env_empty(
        self, monkeypatch, tmp_path
    ):
        """Bitwarden profile aliases must reach Matrix startup credential reads.

        A single-profile gateway can log that the external source applied
        ``MATRIX_ACCESS_TOKEN_ASHER as MATRIX_ACCESS_TOKEN``. If a later Matrix
        startup check sees only an empty env value, it must still consult the
        per-HERMES_HOME external-secret snapshot before failing credentials.
        """
        home = tmp_path / ".hermes" / "profiles" / "asher"
        home.mkdir(parents=True)
        monkeypatch.setenv("HERMES_HOME", str(home))
        monkeypatch.setenv("MATRIX_ACCESS_TOKEN", "")
        ss.set_multiplex_active(False)

        from hermes_cli import config as hermes_config
        from hermes_cli import env_loader

        monkeypatch.setattr(hermes_config, "get_hermes_home", lambda: home)
        monkeypatch.setitem(
            env_loader._SECRET_SOURCE_VALUES_BY_HOME,
            str(home.resolve()),
            {"MATRIX_ACCESS_TOKEN": "token-from-bitwarden-alias"},
        )

        assert _startup_env_secret("MATRIX_ACCESS_TOKEN") == "token-from-bitwarden-alias"

    def test_multiplex_scoped_miss_does_not_use_external_snapshot(
        self, monkeypatch, tmp_path
    ):
        """Multiplex mode remains fail-closed on scoped misses."""
        home = tmp_path / ".hermes" / "profiles" / "asher"
        home.mkdir(parents=True)
        monkeypatch.setenv("HERMES_HOME", str(home))
        monkeypatch.setenv("MATRIX_ACCESS_TOKEN", "default-profile-token")
        ss.set_multiplex_active(True)

        from hermes_cli import config as hermes_config
        from hermes_cli import env_loader

        monkeypatch.setattr(hermes_config, "get_hermes_home", lambda: home)
        monkeypatch.setitem(
            env_loader._SECRET_SOURCE_VALUES_BY_HOME,
            str(home.resolve()),
            {"MATRIX_ACCESS_TOKEN": "token-from-bitwarden-alias"},
        )

        token = ss.set_secret_scope({})
        try:
            assert _startup_env_secret("MATRIX_ACCESS_TOKEN") == ""
        finally:
            ss.reset_secret_scope(token)
