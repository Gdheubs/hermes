"""Tests for warn_deprecated_cwd_env_vars() migration warning."""

import pytest


@pytest.fixture
def env_file(tmp_path, monkeypatch):
    """Point HERMES_HOME at a temp dir and return a writer for its .env."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import hermes_cli.config as hc_config

    monkeypatch.setattr(hc_config, "_env_cache", None, raising=False)

    def write(text: str) -> None:
        (tmp_path / ".env").write_text(text, encoding="utf-8")
        monkeypatch.setattr(hc_config, "_env_cache", None, raising=False)

    return write


class TestDeprecatedCwdWarning:
    """Warn when MESSAGING_CWD or TERMINAL_CWD is set in the on-disk .env."""

    def test_messaging_cwd_triggers_warning(self, env_file, capsys):
        env_file("MESSAGING_CWD=/some/path\n")

        from hermes_cli.config import warn_deprecated_cwd_env_vars
        warn_deprecated_cwd_env_vars(config={})

        captured = capsys.readouterr()
        assert "MESSAGING_CWD" in captured.err
        assert "deprecated" in captured.err.lower()
        assert "config.yaml" in captured.err

    def test_both_deprecated_vars_warn(self, env_file, capsys):
        env_file("MESSAGING_CWD=/msg/path\nTERMINAL_CWD=/term/path\n")

        from hermes_cli.config import warn_deprecated_cwd_env_vars
        warn_deprecated_cwd_env_vars(config={})

        captured = capsys.readouterr()
        assert "MESSAGING_CWD" in captured.err
        assert "TERMINAL_CWD" in captured.err

    def test_runtime_terminal_cwd_does_not_warn(self, env_file, monkeypatch, capsys):
        """TERMINAL_CWD bridged into the process env is not a .env entry.

        The config.yaml bridge, session-cwd restore, worktree switch and kanban
        workspace pinning all set os.environ["TERMINAL_CWD"]; none of them mean
        the user has a stale .env line to delete.
        """
        env_file("SOME_API_KEY=abc\n")
        monkeypatch.setenv("TERMINAL_CWD", "/runtime/path")
        monkeypatch.delenv("MESSAGING_CWD", raising=False)

        from hermes_cli.config import warn_deprecated_cwd_env_vars
        warn_deprecated_cwd_env_vars(config={})

        assert capsys.readouterr().err == ""
