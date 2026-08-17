"""Behavior tests for ``hermes migrate state-to-postgres``.

These tests cover:
- Argument parsing and defaults
- DSN resolution precedence (explicit > env > config)
- Error path when no DSN can be resolved anywhere
- Non-interactive refusal (not a TTY and no --yes)
- Count mismatch surface in output
- Successful migration reporting

``migrate_state_to_postgres.migrate`` is mocked throughout — no PostgreSQL or
Docker required.
"""
from __future__ import annotations

import io
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_GOOD_SUMMARY = {
    "sqlite_path": "/tmp/state.db",
    "source_sessions": 3,
    "source_messages": 12,
    "imported_sessions": 3,
    "target_sessions": 3,
    "target_messages": 12,
    "nul_rows": 0,
}

_MISMATCH_SUMMARY = {
    **_GOOD_SUMMARY,
    "target_sessions": 2,  # one short
    "target_messages": 9,
}


def _make_args(**kwargs: Any) -> SimpleNamespace:
    """Return a minimal argparse-like namespace for cmd_migrate_state_to_postgres."""
    defaults = {
        "dsn": None,
        "sqlite_path": None,
        "yes": False,
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _run(args: SimpleNamespace, *, tty: bool, env: dict | None = None) -> tuple[int, str, str]:
    """
    Invoke cmd_migrate_state_to_postgres and capture stdout/stderr.

    Returns (exit_code, stdout, stderr).
    """
    from hermes_cli.migrate import cmd_migrate_state_to_postgres

    buf_out = io.StringIO()
    buf_err = io.StringIO()

    env_patch = env or {}

    with (
        patch("sys.stdout", buf_out),
        patch("sys.stderr", buf_err),
        patch("sys.stdin", MagicMock(isatty=MagicMock(return_value=tty))),
        patch.dict("os.environ", env_patch, clear=False),
    ):
        rc = cmd_migrate_state_to_postgres(args)

    return rc, buf_out.getvalue(), buf_err.getvalue()


# ---------------------------------------------------------------------------
# DSN resolution
# ---------------------------------------------------------------------------


class TestDSNResolution:
    """explicit --dsn > env var > config; no-DSN-anywhere is a clean error."""

    def test_explicit_dsn_is_used(self, tmp_path: Path) -> None:
        """When --dsn is given it is passed straight through to migrate()."""
        sqlite_file = tmp_path / "state.db"
        sqlite_file.touch()
        explicit = "postgresql://user:pw@localhost/db"
        args = _make_args(dsn=explicit, sqlite_path=str(sqlite_file), yes=True)

        with patch(
            "migrate_state_to_postgres.migrate",
            return_value=_GOOD_SUMMARY,
        ) as mock_migrate, patch(
            "migrate_state_to_postgres._resolve_sqlite_path",
            return_value=sqlite_file,
        ):
            rc, out, _ = _run(args, tty=False)

        assert rc == 0
        mock_migrate.assert_called_once()
        _, call_dsn = mock_migrate.call_args.args
        assert call_dsn == explicit

    def test_env_var_beats_config(self, tmp_path: Path) -> None:
        """HERMES_STATE_DATABASE_URL takes priority over config resolution."""
        sqlite_file = tmp_path / "state.db"
        sqlite_file.touch()
        env_dsn = "postgresql://env-host/envdb"
        args = _make_args(sqlite_path=str(sqlite_file), yes=True)

        with patch(
            "migrate_state_to_postgres.migrate",
            return_value=_GOOD_SUMMARY,
        ) as mock_migrate, patch(
            "migrate_state_to_postgres._resolve_sqlite_path",
            return_value=sqlite_file,
        ), patch(
            "hermes_state_postgres.resolve_postgres_dsn",
            return_value="postgresql://config-host/configdb",
        ):
            rc, _, _ = _run(args, tty=False, env={"HERMES_STATE_DATABASE_URL": env_dsn})

        assert rc == 0
        _, call_dsn = mock_migrate.call_args.args
        assert call_dsn == env_dsn

    def test_explicit_dsn_beats_env_and_config(self, tmp_path: Path) -> None:
        """Explicit --dsn wins even when env vars and config both have values."""
        sqlite_file = tmp_path / "state.db"
        sqlite_file.touch()
        explicit = "postgresql://explicit-host/explicitdb"
        args = _make_args(dsn=explicit, sqlite_path=str(sqlite_file), yes=True)

        with patch(
            "migrate_state_to_postgres.migrate",
            return_value=_GOOD_SUMMARY,
        ) as mock_migrate, patch(
            "migrate_state_to_postgres._resolve_sqlite_path",
            return_value=sqlite_file,
        ):
            rc, _, _ = _run(
                args,
                tty=False,
                env={"HERMES_STATE_DATABASE_URL": "postgresql://env-host/envdb"},
            )

        assert rc == 0
        _, call_dsn = mock_migrate.call_args.args
        assert call_dsn == explicit

    def test_config_dsn_used_when_no_explicit_or_env(self, tmp_path: Path) -> None:
        """Falls back to resolve_postgres_dsn(config) when no explicit/env DSN."""
        sqlite_file = tmp_path / "state.db"
        sqlite_file.touch()
        config_dsn = "postgresql://config-host/configdb"
        args = _make_args(sqlite_path=str(sqlite_file), yes=True)

        import os

        saved = {k: os.environ.pop(k, None) for k in ("HERMES_STATE_DATABASE_URL", "HERMES_STATE_POSTGRES_DSN")}
        try:
            with patch(
                "migrate_state_to_postgres.migrate",
                return_value=_GOOD_SUMMARY,
            ) as mock_migrate, patch(
                "migrate_state_to_postgres._resolve_sqlite_path",
                return_value=sqlite_file,
            ), patch(
                "hermes_state_postgres.resolve_postgres_dsn",
                return_value=config_dsn,
            ):
                rc, _, _ = _run(args, tty=False)
        finally:
            for k, v in saved.items():
                if v is not None:
                    os.environ[k] = v

        assert rc == 0
        _, call_dsn = mock_migrate.call_args.args
        assert call_dsn == config_dsn

    def test_no_dsn_anywhere_exits_1_with_actionable_message(
        self, tmp_path: Path
    ) -> None:
        """When no DSN can be resolved, exit code is 1 and stderr names the fix."""
        sqlite_file = tmp_path / "state.db"
        sqlite_file.touch()
        args = _make_args(sqlite_path=str(sqlite_file), yes=True)

        import os

        saved = {k: os.environ.pop(k, None) for k in ("HERMES_STATE_DATABASE_URL", "HERMES_STATE_POSTGRES_DSN")}
        try:
            with patch(
                "migrate_state_to_postgres._resolve_sqlite_path",
                return_value=sqlite_file,
            ), patch(
                "hermes_state_postgres.resolve_postgres_dsn",
                return_value=None,
            ):
                rc, _, err = _run(args, tty=False)
        finally:
            for k, v in saved.items():
                if v is not None:
                    os.environ[k] = v

        assert rc == 1
        # Error message must name both the env var and the config key.
        assert "HERMES_STATE_DATABASE_URL" in err or "HERMES_STATE_POSTGRES_DSN" in err
        assert "sessions.postgres_dsn" in err or "config.yaml" in err


# ---------------------------------------------------------------------------
# Non-interactive safety
# ---------------------------------------------------------------------------


class TestNonInteractiveSafety:
    """stdin not a TTY and no --yes must refuse, not hang."""

    def test_non_tty_without_yes_exits_nonzero(self, tmp_path: Path) -> None:
        sqlite_file = tmp_path / "state.db"
        sqlite_file.touch()
        args = _make_args(dsn="postgresql://h/db", sqlite_path=str(sqlite_file), yes=False)

        with patch(
            "migrate_state_to_postgres._resolve_sqlite_path",
            return_value=sqlite_file,
        ):
            rc, _, err = _run(args, tty=False)

        assert rc != 0
        assert "TTY" in err or "--yes" in err or "-y" in err

    def test_non_tty_with_yes_proceeds(self, tmp_path: Path) -> None:
        sqlite_file = tmp_path / "state.db"
        sqlite_file.touch()
        args = _make_args(dsn="postgresql://h/db", sqlite_path=str(sqlite_file), yes=True)

        with patch(
            "migrate_state_to_postgres.migrate",
            return_value=_GOOD_SUMMARY,
        ), patch(
            "migrate_state_to_postgres._resolve_sqlite_path",
            return_value=sqlite_file,
        ):
            rc, _, _ = _run(args, tty=False)

        assert rc == 0

    def test_tty_without_yes_shows_prompt(self, tmp_path: Path) -> None:
        """With a TTY and no --yes a confirmation prompt is shown."""
        sqlite_file = tmp_path / "state.db"
        sqlite_file.touch()
        args = _make_args(dsn="postgresql://h/db", sqlite_path=str(sqlite_file), yes=False)

        with patch(
            "migrate_state_to_postgres.migrate",
            return_value=_GOOD_SUMMARY,
        ), patch(
            "migrate_state_to_postgres._resolve_sqlite_path",
            return_value=sqlite_file,
        ), patch(
            "builtins.input",
            return_value="y",
        ) as mock_input, patch(
            "sys.stdin",
            MagicMock(isatty=MagicMock(return_value=True)),
        ):
            rc, out, _ = _run(args, tty=True)

        # input() must have been called (the prompt appeared).
        mock_input.assert_called_once()
        assert rc == 0


# ---------------------------------------------------------------------------
# Count mismatch reporting
# ---------------------------------------------------------------------------


class TestMismatchReporting:
    """A count mismatch must be visually obvious and return non-zero."""

    def test_mismatch_exits_nonzero(self, tmp_path: Path) -> None:
        sqlite_file = tmp_path / "state.db"
        sqlite_file.touch()
        args = _make_args(dsn="postgresql://h/db", sqlite_path=str(sqlite_file), yes=True)

        with patch(
            "migrate_state_to_postgres.migrate",
            return_value=_MISMATCH_SUMMARY,
        ), patch(
            "migrate_state_to_postgres._resolve_sqlite_path",
            return_value=sqlite_file,
        ):
            rc, out, _ = _run(args, tty=False)

        assert rc != 0

    def test_mismatch_surfaces_in_output(self, tmp_path: Path) -> None:
        """Output must contain something indicating the mismatch, not just a count."""
        sqlite_file = tmp_path / "state.db"
        sqlite_file.touch()
        args = _make_args(dsn="postgresql://h/db", sqlite_path=str(sqlite_file), yes=True)

        with patch(
            "migrate_state_to_postgres.migrate",
            return_value=_MISMATCH_SUMMARY,
        ), patch(
            "migrate_state_to_postgres._resolve_sqlite_path",
            return_value=sqlite_file,
        ):
            rc, out, _ = _run(args, tty=False)

        combined = out + _  # out is stdout from _run; _ is stderr
        # The word MISMATCH or a visual indicator must appear.
        assert "MISMATCH" in combined.upper() or "⚠" in combined


# ---------------------------------------------------------------------------
# Success reporting
# ---------------------------------------------------------------------------


class TestSuccessReporting:
    """On a clean migration, exit 0 and source counts visible in output."""

    def test_success_exits_zero(self, tmp_path: Path) -> None:
        sqlite_file = tmp_path / "state.db"
        sqlite_file.touch()
        args = _make_args(dsn="postgresql://h/db", sqlite_path=str(sqlite_file), yes=True)

        with patch(
            "migrate_state_to_postgres.migrate",
            return_value=_GOOD_SUMMARY,
        ), patch(
            "migrate_state_to_postgres._resolve_sqlite_path",
            return_value=sqlite_file,
        ):
            rc, out, _ = _run(args, tty=False)

        assert rc == 0

    def test_success_shows_counts(self, tmp_path: Path) -> None:
        sqlite_file = tmp_path / "state.db"
        sqlite_file.touch()
        args = _make_args(dsn="postgresql://h/db", sqlite_path=str(sqlite_file), yes=True)

        with patch(
            "migrate_state_to_postgres.migrate",
            return_value=_GOOD_SUMMARY,
        ), patch(
            "migrate_state_to_postgres._resolve_sqlite_path",
            return_value=sqlite_file,
        ):
            rc, out, _ = _run(args, tty=False)

        assert str(_GOOD_SUMMARY["source_sessions"]) in out
        assert str(_GOOD_SUMMARY["source_messages"]) in out


# ---------------------------------------------------------------------------
# Connection / credential failure handling
# ---------------------------------------------------------------------------


class TestConnectionFailure:
    """psycopg errors must not spew a raw traceback; they must be caught."""

    def test_psycopg_error_exits_nonzero_no_traceback(self, tmp_path: Path) -> None:
        sqlite_file = tmp_path / "state.db"
        sqlite_file.touch()
        args = _make_args(dsn="postgresql://bad/db", sqlite_path=str(sqlite_file), yes=True)

        class FakePsycopgError(Exception):
            pass

        with patch(
            "migrate_state_to_postgres.migrate",
            side_effect=FakePsycopgError("connection refused"),
        ), patch(
            "migrate_state_to_postgres._resolve_sqlite_path",
            return_value=sqlite_file,
        ):
            rc, out, err = _run(args, tty=False)

        assert rc != 0
        combined = out + err
        # "Traceback" must not appear; the error should be caught and prettified.
        assert "Traceback" not in combined
        # The underlying message should be somewhere in the output.
        assert "connection refused" in combined


# ---------------------------------------------------------------------------
# Argument parsing: verify the subcommand is reachable via the main parser
# ---------------------------------------------------------------------------


class TestArgumentParsing:
    """Smoke-test that the CLI parser wires state-to-postgres correctly."""

    def test_subcommand_registered(self) -> None:
        """hermes migrate state-to-postgres is parsed without error."""
        # Import the build_parser function via the module, not the full CLI.
        # We only need to verify the parser accepts the subcommand name and
        # the expected flags; we do not need to invoke the handler.
        from hermes_cli.main import main as hermes_main  # noqa: F401

        # build_arg_parser is defined inside main() so we call parse_args
        # via the module's public surface with --help captured.
        import argparse

        captured = io.StringIO()
        # Build just the migrate subparser fragment in isolation is complex —
        # instead verify that help output for the full CLI contains our name.
        # This is a registration check, not a snapshot test.
        try:
            with patch("sys.argv", ["hermes", "migrate", "state-to-postgres", "--help"]), patch(
                "sys.stdout", captured
            ):
                hermes_main()
        except SystemExit:
            pass

        help_text = captured.getvalue()
        # At minimum the help should reference the known flags.
        assert "--dsn" in help_text
        assert "--sqlite-path" in help_text
        assert "--yes" in help_text or "-y" in help_text
