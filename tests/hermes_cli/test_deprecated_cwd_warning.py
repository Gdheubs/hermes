"""Tests for warn_deprecated_cwd_env_vars() migration warning."""

import pytest


@pytest.fixture
def emitted_blocks(monkeypatch):
    """Capture startup warning blocks routed through banner.cprint.

    #87919: the warnings must go through the prompt_toolkit renderer
    (banner.cprint) instead of raw ``sys.stderr.write()`` — patch_stdout's
    StdoutProxy strips the ESC byte from raw stderr ANSI and leaves
    ``?[33m...?[0m`` artifacts in the interactive CLI.
    """
    emitted = []
    monkeypatch.setattr("hermes_cli.banner.cprint", lambda text: emitted.append(text))
    return emitted


class TestDeprecatedCwdWarning:
    """Warn when MESSAGING_CWD or TERMINAL_CWD is set in .env."""

    def test_messaging_cwd_triggers_warning(self, monkeypatch, emitted_blocks):
        monkeypatch.setenv("MESSAGING_CWD", "/some/path")
        monkeypatch.delenv("TERMINAL_CWD", raising=False)

        from hermes_cli.config import warn_deprecated_cwd_env_vars
        warn_deprecated_cwd_env_vars(config={})

        text = "\n".join(emitted_blocks)
        assert "MESSAGING_CWD" in text
        assert "deprecated" in text.lower()
        assert "config.yaml" in text

    def test_both_deprecated_vars_warn(self, monkeypatch, emitted_blocks):
        monkeypatch.setenv("MESSAGING_CWD", "/msg/path")
        monkeypatch.setenv("TERMINAL_CWD", "/term/path")

        from hermes_cli.config import warn_deprecated_cwd_env_vars
        warn_deprecated_cwd_env_vars(config={})

        text = "\n".join(emitted_blocks)
        assert "MESSAGING_CWD" in text
        assert "TERMINAL_CWD" in text

    def test_warning_does_not_write_raw_ansi_to_stderr(self, monkeypatch, capsys):
        """#87919 regression: no raw stderr write may carry ANSI escapes."""
        monkeypatch.setenv("MESSAGING_CWD", "/some/path")
        monkeypatch.delenv("TERMINAL_CWD", raising=False)

        captured_stderr = []
        real_stderr_write = captured_stderr.append

        import sys as _sys

        class _RecordingStderr:
            def write(self, data):
                real_stderr_write(data)
                return len(data)

            def __getattr__(self, name):
                return getattr(_sys.stderr, name)

        monkeypatch.setattr(_sys, "stderr", _RecordingStderr())

        from hermes_cli.config import warn_deprecated_cwd_env_vars
        warn_deprecated_cwd_env_vars(config={})

        assert not any("\033[" in chunk for chunk in captured_stderr), (
            "startup warnings must not write raw ANSI to sys.stderr "
            "(#87919): " + repr(captured_stderr)
        )
