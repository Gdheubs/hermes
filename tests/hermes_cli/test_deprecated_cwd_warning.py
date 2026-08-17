"""Tests for warn_deprecated_cwd_env_vars() migration warning."""


class TestDeprecatedCwdWarning:
    """Warn when MESSAGING_CWD or TERMINAL_CWD is set in .env."""

    def test_messaging_cwd_triggers_warning(self, monkeypatch, capsys):
        monkeypatch.setenv("MESSAGING_CWD", "/some/path")
        monkeypatch.delenv("TERMINAL_CWD", raising=False)

        from hermes_cli.config import warn_deprecated_cwd_env_vars
        warn_deprecated_cwd_env_vars(config={})

        captured = capsys.readouterr()
        assert "MESSAGING_CWD" in captured.err
        assert "deprecated" in captured.err.lower()
        assert "config.yaml" in captured.err


    def test_both_deprecated_vars_warn(self, monkeypatch, capsys):
        # Guard against a launch sentinel leaked from a real bridge in the same
        # process: this test asserts a stale TERMINAL_CWD still warns, which
        # requires no matching _HERMES_TERMINAL_CWD_LAUNCHED to be present.
        monkeypatch.delenv("_HERMES_TERMINAL_CWD_LAUNCHED", raising=False)
        monkeypatch.setenv("MESSAGING_CWD", "/msg/path")
        monkeypatch.setenv("TERMINAL_CWD", "/term/path")

        from hermes_cli.config import warn_deprecated_cwd_env_vars
        warn_deprecated_cwd_env_vars(config={})

        captured = capsys.readouterr()
        assert "MESSAGING_CWD" in captured.err
        assert "TERMINAL_CWD" in captured.err

    def test_fresh_bridge_cwd_no_warning(self, monkeypatch, capsys):
        # cli.py's bridge writes TERMINAL_CWD AND the matching _HERMES_...LAUNCHED
        # sentinel at set-time; the warn must treat this fresh value as bridge-written
        # (not a stale .env export) and stay silent.
        monkeypatch.setenv("TERMINAL_CWD", "/some/project")
        monkeypatch.setenv("_HERMES_TERMINAL_CWD_LAUNCHED", "/some/project")
        monkeypatch.delenv("MESSAGING_CWD", raising=False)

        from hermes_cli.config import warn_deprecated_cwd_env_vars
        warn_deprecated_cwd_env_vars(config={})

        captured = capsys.readouterr()
        assert "TERMINAL_CWD" not in captured.err
        assert "deprecated" not in captured.err.lower()

    def test_stale_terminal_cwd_no_sentinel_still_warns(self, monkeypatch, capsys):
        # A genuine stale .env export has no matching bridge sentinel, so it must
        # still warn — this is the "real .env, no bridge" case.
        monkeypatch.setenv("TERMINAL_CWD", "/some/project")
        monkeypatch.delenv("_HERMES_TERMINAL_CWD_LAUNCHED", raising=False)
        monkeypatch.delenv("MESSAGING_CWD", raising=False)

        from hermes_cli.config import warn_deprecated_cwd_env_vars
        warn_deprecated_cwd_env_vars(config={})

        captured = capsys.readouterr()
        assert "TERMINAL_CWD" in captured.err
        assert "deprecated" in captured.err.lower()

    def test_stale_mismatched_sentinel_still_warns(self, monkeypatch, capsys):
        # A sentinel pointing at a different cwd is not a fresh bridge value, so a
        # stale TERMINAL_CWD must still warn.
        monkeypatch.setenv("TERMINAL_CWD", "/a/b")
        monkeypatch.setenv("_HERMES_TERMINAL_CWD_LAUNCHED", "/c/d")
        monkeypatch.delenv("MESSAGING_CWD", raising=False)

        from hermes_cli.config import warn_deprecated_cwd_env_vars
        warn_deprecated_cwd_env_vars(config={})

        captured = capsys.readouterr()
        assert "TERMINAL_CWD" in captured.err
        assert "deprecated" in captured.err.lower()

    def test_explicit_terminal_cwd_suppresses_even_with_env_set(self, monkeypatch, capsys):
        # An explicit terminal.cwd in config.yaml suppresses the warning even when a
        # matching sentinel is present — the config_has_explicit_cwd path, independent
        # of the fresh-bridge fix.
        monkeypatch.setenv("TERMINAL_CWD", "/some/project")
        monkeypatch.setenv("_HERMES_TERMINAL_CWD_LAUNCHED", "/some/project")
        monkeypatch.delenv("MESSAGING_CWD", raising=False)

        from hermes_cli.config import warn_deprecated_cwd_env_vars
        warn_deprecated_cwd_env_vars(config={"terminal": {"cwd": "/explicit/path"}})

        captured = capsys.readouterr()
        assert "TERMINAL_CWD" not in captured.err
        assert "deprecated" not in captured.err.lower()

    def test_chdir_between_bridge_and_warn_no_false_positive(self, monkeypatch, capsys):
        # The fix compares TERMINAL_CWD against the set-time sentinel, never a live
        # os.getcwd(). A chdir between the bridge and the warn must not matter: even
        # with getcwd() returning a different path, a fresh sentinel still suppresses.
        from hermes_cli.config import warn_deprecated_cwd_env_vars
        monkeypatch.setattr("os.getcwd", lambda: "/some/other/cwd")
        monkeypatch.setenv("TERMINAL_CWD", "/some/project")
        monkeypatch.setenv("_HERMES_TERMINAL_CWD_LAUNCHED", "/some/project")
        monkeypatch.delenv("MESSAGING_CWD", raising=False)

        warn_deprecated_cwd_env_vars(config={})

        captured = capsys.readouterr()
        assert "TERMINAL_CWD" not in captured.err
        assert "deprecated" not in captured.err.lower()

    def test_messaging_cwd_warning_unaffected_by_sentinel(self, monkeypatch, capsys):
        # MESSAGING_CWD and the TERMINAL_CWD suppression are independent: a fresh
        # bridge sentinel silences TERMINAL_CWD but must NOT silence MESSAGING_CWD.
        monkeypatch.setenv("MESSAGING_CWD", "/msg/path")
        monkeypatch.setenv("TERMINAL_CWD", "/some/project")
        monkeypatch.setenv("_HERMES_TERMINAL_CWD_LAUNCHED", "/some/project")

        from hermes_cli.config import warn_deprecated_cwd_env_vars
        warn_deprecated_cwd_env_vars(config={})

        captured = capsys.readouterr()
        assert "MESSAGING_CWD" in captured.err
        assert "deprecated" in captured.err.lower()
        # Independence: the terminal line must be absent (suppressed) while the
        # messaging line is still present.
        assert "TERMINAL_CWD" not in captured.err
