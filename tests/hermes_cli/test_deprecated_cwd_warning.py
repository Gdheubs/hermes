"""Tests for warn_deprecated_cwd_env_vars() migration warning."""

import pytest


class TestDeprecatedCwdWarning:
    """Warn when MESSAGING_CWD or TERMINAL_CWD is set in .env."""

    def test_messaging_cwd_triggers_warning(self, monkeypatch, capsys):
        monkeypatch.setenv("MESSAGING_CWD", "/some/path")
        monkeypatch.setenv("TERMINAL_CWD", "/term/path")
        # Simulate the .env file content (the warning must read the FILE,
        # not os.environ — the env vars above are ambient bridge noise).
        monkeypatch.setattr(
            "hermes_cli.config.load_env",
            lambda: {"MESSAGING_CWD": "/some/path"},
        )

        from hermes_cli.config import warn_deprecated_cwd_env_vars
        warn_deprecated_cwd_env_vars(config={})

        captured = capsys.readouterr()
        assert "MESSAGING_CWD" in captured.err
        assert "deprecated" in captured.err.lower()
        assert "config.yaml" in captured.err
        # TERMINAL_CWD is NOT in .env -> must not warn about it
        assert "TERMINAL_CWD" not in captured.err

    def test_both_deprecated_vars_warn(self, monkeypatch, capsys):
        monkeypatch.setattr(
            "hermes_cli.config.load_env",
            lambda: {"MESSAGING_CWD": "/msg/path", "TERMINAL_CWD": "/term/path"},
        )

        from hermes_cli.config import warn_deprecated_cwd_env_vars
        warn_deprecated_cwd_env_vars(config={})

        captured = capsys.readouterr()
        assert "MESSAGING_CWD" in captured.err
        assert "TERMINAL_CWD" in captured.err

    def test_terminal_cwd_ignored_when_config_has_explicit_cwd(self, monkeypatch, capsys):
        monkeypatch.setattr(
            "hermes_cli.config.load_env",
            lambda: {"TERMINAL_CWD": "/term/path"},
        )
        config = {"terminal": {"cwd": "/explicit/path"}}

        from hermes_cli.config import warn_deprecated_cwd_env_vars
        warn_deprecated_cwd_env_vars(config=config)

        captured = capsys.readouterr()
        assert "TERMINAL_CWD" not in captured.err

    def test_env_var_without_dotenv_entry_does_not_warn(self, monkeypatch, capsys):
        """Regression: TERMINAL_CWD present in os.environ (e.g. injected by
        Hermes' own config bridge) but absent from the .env file must NOT
        trigger the deprecated warning."""
        monkeypatch.setenv("TERMINAL_CWD", "/Users/wen")
        monkeypatch.setenv("MESSAGING_CWD", "/msg/path")
        monkeypatch.setattr("hermes_cli.config.load_env", lambda: {})

        from hermes_cli.config import warn_deprecated_cwd_env_vars
        warn_deprecated_cwd_env_vars(config={})

        captured = capsys.readouterr()
        assert captured.err == ""
