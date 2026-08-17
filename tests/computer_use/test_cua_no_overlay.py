"""Tests for the cua-driver --no-overlay policy.

cua-driver's cursor overlay rendering loop can consume CPU indefinitely when
idle (#28152, #47032). Hermes passes ``--no-overlay`` to suppress it when the
``computer_use.no_overlay`` config is enabled (or auto-detected on macOS and
headless Linux / WSL2).

These assert the behavior contract (auto-detect, explicit override, version
probe), not specific config snapshots.

Since #81220 the policy also applies to *user-configured*
``mcp_servers.cua-driver`` MCP entries: the same ``--no-overlay`` flag the
embedded cua_backend applies at ``_resolve_mcp_invocation`` is applied to
the user MCP launch path via ``normalize_user_cua_driver_args``.
"""

import os
from unittest.mock import MagicMock, patch

import pytest

from tools.computer_use import cua_backend


class TestNoOverlayFlag:





    def test_explicit_true_overrides(self):
        with patch("hermes_cli.config.load_config",
                   return_value={"computer_use": {"no_overlay": True}}):
            assert cua_backend._cua_no_overlay() is True


    @pytest.mark.macos_only
    def test_config_load_failure_falls_through_to_auto_detect_macos(self):
        """Unreadable config => auto-detect (macOS defaults to overlay off).

        macOS-only: the auto-detect verdict IS ``sys.platform == "darwin"``,
        so a patched platform would only re-assert the patch.
        """
        with patch("hermes_cli.config.load_config",
                   side_effect=RuntimeError("boom")):
            assert cua_backend._cua_no_overlay() is True

    @pytest.mark.linux_only
    def test_config_load_failure_falls_through_to_auto_detect_linux(self, monkeypatch):
        """Unreadable config must not raise; headless Linux auto-detects off.

        Linux-only: the auto-detect branch here keys off ``DISPLAY`` and
        ``/proc/version``, neither of which exists to be probed elsewhere.
        """
        monkeypatch.delenv("DISPLAY", raising=False)
        with patch("hermes_cli.config.load_config",
                   side_effect=RuntimeError("boom")):
            assert cua_backend._cua_no_overlay() is True




class TestDriverSupportsNoOverlay:
    def test_returns_true_when_help_shows_flag(self):
        fake_help = "Usage: cua-driver [OPTIONS] COMMAND\n  --no-overlay  Disable cursor overlay\n"
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.stdout = fake_help
            mock_run.return_value.stderr = ""
            assert cua_backend._cua_driver_supports_no_overlay("cua-driver") is True



    def test_help_probe_passes_sanitized_env(self):
        """The ``--help`` subprocess must not leak provider credentials
        via the inherited parent environment (third-party binary; same
        policy as the manifest probe and MCP spawn).
        """
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="--no-overlay in help", stderr="")
            cua_backend._cua_driver_supports_no_overlay.cache_clear()
            cua_backend._cua_driver_supports_no_overlay("cua-driver")
            kwargs = mock_run.call_args.kwargs
            assert "env" in kwargs, (
                "subprocess.run was called without env= — cua-driver is a "
                "third-party binary and must not receive inherited secrets"
            )
            # The sanitized env must come from the same helper the MCP
            # spawn uses, so the policy is consistent across every
            # cua-driver invocation in this file.
            assert kwargs["env"] is not None


class TestMcpInvocationUsesResolvedCommand:
    """Surface 8 (NousResearch/hermes-agent#47072) + sweeper feedback
    #4701565902: when the manifest surfaces a relocated executable for
    ``mcp_invocation.command``, the support probe must run against THAT
    binary, not the system-resolved ``_CUA_DRIVER_CMD``. Otherwise a
    wrapper/relocation with a different feature set either crashes on
    the unknown flag (when the probe falsely reports support) or
    silently keeps an unwanted overlay (when the probe falsely reports
    no support).
    """

    @staticmethod
    def _fake_run(stdout: str = "", returncode: int = 0):
        def _run(*args, **kwargs):
            proc = MagicMock()
            proc.stdout = stdout
            proc.returncode = returncode
            return proc
        return _run

    def test_manifest_command_drives_support_probe(self):
        """When the manifest returns a distinct command, the support
        probe runs against the manifest command, not the input
        ``driver_cmd`` parameter.
        """
        from tools.computer_use.cua_backend import _resolve_mcp_invocation

        manifest = (
            '{"mcp_invocation":'
            '{"command":"/opt/relocated/cua-driver","args":["mcp"]}}'
        )
        with patch("subprocess.run", new=self._fake_run(stdout=manifest)), \
             patch.object(cua_backend, "_cua_no_overlay", return_value=True), \
             patch.object(
                 cua_backend, "_cua_driver_supports_no_overlay",
                 return_value=True,
             ) as mock_probe:
            cua_backend._cua_driver_supports_no_overlay.cache_clear()
            cmd, args = _resolve_mcp_invocation("/usr/bin/cua-driver")
        assert cmd == "/opt/relocated/cua-driver"
        # The support probe must be called with the manifest-resolved
        # command, not the input driver_cmd argument.
        mock_probe.assert_called_with("/opt/relocated/cua-driver")


    def test_probe_distinguishes_support_between_binaries(self):
        """Different binaries must produce independent support verdicts.
        The cache is keyed on ``driver_cmd``; the same cached result
        must not leak between the system binary and a manifest-relocated
        one.
        """
        with patch.object(cua_backend, "_cua_no_overlay", return_value=True), \
             patch.object(
                 cua_backend, "_cua_driver_supports_no_overlay",
                 side_effect=lambda cmd: cmd == "/opt/relocated/cua-driver",
             ):
            # System binary does NOT support, manifest binary DOES.
            args = cua_backend._mcp_args_with_overlay_flag(
                ["mcp"], driver_cmd="/usr/bin/cua-driver",
            )
            assert "--no-overlay" not in args
            args = cua_backend._mcp_args_with_overlay_flag(
                ["mcp"], driver_cmd="/opt/relocated/cua-driver",
            )
            assert "--no-overlay" in args


class TestMcpArgsOverlayFlag:
    def test_appended_when_enabled_and_supported(self):
        with patch.object(cua_backend, "_cua_no_overlay", return_value=True), \
             patch.object(cua_backend, "_cua_driver_supports_no_overlay", return_value=True):
            result = cua_backend._mcp_args_with_overlay_flag(["mcp"])
            assert result == ["mcp", "--no-overlay"]

    def test_not_appended_when_disabled(self):
        with patch.object(cua_backend, "_cua_no_overlay", return_value=False), \
             patch.object(cua_backend, "_cua_driver_supports_no_overlay", return_value=True):
            result = cua_backend._mcp_args_with_overlay_flag(["mcp"])
            assert result == ["mcp"]


    def test_does_not_mutate_original_list(self):
        original = ["mcp"]
        with patch.object(cua_backend, "_cua_no_overlay", return_value=True), \
             patch.object(cua_backend, "_cua_driver_supports_no_overlay", return_value=True):
            result = cua_backend._mcp_args_with_overlay_flag(original)
            assert "--no-overlay" in result
            assert "--no-overlay" not in original


# ---------------------------------------------------------------------------
# looks_like_cua_driver_command
# ---------------------------------------------------------------------------


class TestLooksLikeCuaDriverCommand:
    @pytest.mark.parametrize(
        "command",
        [
            "cua-driver",
            "/usr/local/bin/cua-driver",
            "/opt/cua/cua-driver",
            "/home/u/.local/bin/cua-driver",
            "C:\\Program Files\\cua\\cua-driver.exe",
            "cua-driver.exe",
            "./cua-driver",
        ],
    )
    def test_recognises_known_binaries(self, command):
        assert cua_backend.looks_like_cua_driver_command(command) is True

    @pytest.mark.parametrize(
        "command",
        [
            "",
            None,
            "node",
            "npx",
            "python",
            "/usr/local/bin/some-other-mcp",
            "mcp-remote",
        ],
    )
    def test_rejects_unrelated_commands(self, command):
        assert cua_backend.looks_like_cua_driver_command(command) is False


# ---------------------------------------------------------------------------
# normalize_user_cua_driver_args
# ---------------------------------------------------------------------------


class TestNormalizeUserCuaDriverArgs:
    def test_appends_no_overlay_for_cua_driver_when_enabled(self):
        """User-configured cua-driver MCP receives ``--no-overlay`` when
        the policy resolves True and the installed driver supports it.
        """
        with patch.object(cua_backend, "_cua_no_overlay", return_value=True), \
             patch.object(cua_backend, "_cua_driver_supports_no_overlay", return_value=True):
            args = cua_backend.normalize_user_cua_driver_args(
                "/usr/local/bin/cua-driver", ["mcp"],
            )
        assert args == ["mcp", "--no-overlay"]

    def test_does_not_mutate_caller_list(self):
        original = ["mcp"]
        with patch.object(cua_backend, "_cua_no_overlay", return_value=True), \
             patch.object(cua_backend, "_cua_driver_supports_no_overlay", return_value=True):
            args = cua_backend.normalize_user_cua_driver_args(
                "cua-driver", original,
            )
        assert "--no-overlay" in args
        assert "--no-overlay" not in original

    def test_passthrough_for_non_cua_driver(self):
        """The helper must not touch args for unrelated MCP servers —
        this is what guarantees the change is class-level instead of
        touching every MCP spawn.
        """
        with patch.object(cua_backend, "_cua_no_overlay", return_value=True), \
             patch.object(cua_backend, "_cua_driver_supports_no_overlay", return_value=True):
            args = cua_backend.normalize_user_cua_driver_args(
                "npx", ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
            )
        assert args == ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]

    def test_no_overlay_omitted_when_policy_disabled(self):
        with patch.object(cua_backend, "_cua_no_overlay", return_value=False), \
             patch.object(cua_backend, "_cua_driver_supports_no_overlay", return_value=True):
            args = cua_backend.normalize_user_cua_driver_args("cua-driver", ["mcp"])
        assert "--no-overlay" not in args

    def test_no_overlay_omitted_when_driver_does_not_support(self):
        """Older drivers reject unknown flags; the helper must not append
        ``--no-overlay`` when the installed binary doesn't recognise it.
        """
        with patch.object(cua_backend, "_cua_no_overlay", return_value=True), \
             patch.object(cua_backend, "_cua_driver_supports_no_overlay", return_value=False):
            args = cua_backend.normalize_user_cua_driver_args("cua-driver", ["mcp"])
        assert "--no-overlay" not in args

    def test_supports_flag_probed_against_user_command(self):
        """The support probe must run against the user's resolved
        binary path (not the embedded default), so a wrapper or
        relocated driver with a different feature set is treated
        correctly — mirrors the embedded-backend invariant.
        """
        with patch.object(cua_backend, "_cua_no_overlay", return_value=True), \
             patch.object(
                 cua_backend, "_cua_driver_supports_no_overlay",
                 return_value=True,
             ) as mock_probe:
            cua_backend._cua_driver_supports_no_overlay.cache_clear()
            cua_backend.normalize_user_cua_driver_args(
                "/opt/relocated/cua-driver", ["mcp"],
            )
        mock_probe.assert_called_with("/opt/relocated/cua-driver")


# ---------------------------------------------------------------------------
# Embedded default is unchanged (multi-monitor detection is out-of-scope here)
# ---------------------------------------------------------------------------

# The X11 multi-monitor auto-detect (virtual-root width > single 5K panel)
# is intentionally NOT part of the #81220 MCP fix: it changes embedded-backend
# auto-select behavior, so it is tracked as a separate follow-up. This guard
# locks the embedded contract: desktop Linux with a display keeps the overlay
# on by default, and only the user-MCP normalization helper (below) or an
# explicit ``computer_use.no_overlay`` flips it off.


@pytest.mark.linux_only
class TestEmbeddedDesktopDefaultUnchanged:
    def test_desktop_linux_with_display_keeps_overlay_by_default(self):
        """Embedded backend must NOT force ``--no-overlay`` on desktop Linux
        with a display. A wide virtual root (multi-monitor) used to flip this
        True; that detection is deferred out of this PR, so the default stays
        off-the-overlay=false for embedded spawns and session start.
        """
        with patch("hermes_cli.config.load_config", return_value={}), \
             patch.dict(os.environ, {"DISPLAY": ":0"}, clear=False):
            assert cua_backend._cua_no_overlay() is False

    def test_mcp_normalization_still_applies_without_multi_monitor_detect(self):
        """The #81220 fix must survive without the (deferred) multi-monitor
        heuristic: a user cua-driver MCP entry still gets ``--no-overlay``.
        """
        with patch.object(cua_backend, "_cua_no_overlay", return_value=True), \
             patch.object(cua_backend, "_cua_driver_supports_no_overlay", return_value=True):
            result = cua_backend.normalize_user_cua_driver_args(
                "/usr/local/bin/cua-driver", ["mcp"],
            )
        assert result == ["mcp", "--no-overlay"]


# ---------------------------------------------------------------------------
# tools/mcp_tool._run_stdio integration
# ---------------------------------------------------------------------------


class TestMcpToolAppliesCuaOverlayPolicy:
    """The fix lands in ``tools/mcp_tool.py::_run_stdio`` so a
    user-configured ``mcp_servers.cua-driver`` entry cannot silently
    bypass the embedded-backend normalization. These tests assert the
    integration without going through a live subprocess.
    """

    def _run_stdio_coro(self):
        # ``_run_stdio`` is an async method; importing the module triggers
        # the heavy ``mcp`` SDK import. Guard so a missing SDK surfaces as
        # ``pytest.skip`` rather than an ImportError during collection.
        try:
            import tools.mcp_tool as mcp_tool_mod
        except ImportError as exc:  # pragma: no cover - depends on env
            pytest.skip(f"mcp_tool import unavailable: {exc}")
        return mcp_tool_mod

    def test_user_cua_driver_args_receive_no_overlay(self):
        """End-to-end normalisation: a user MCP server named ``cua-driver``
        with ``args: [mcp]`` is augmented with ``--no-overlay`` before
        the OSV preflight + watchdog wrap.

        We exercise the helper directly (rather than calling ``_run_stdio``
        with a full mock) to keep the test focused on the policy hook.
        """
        with patch.object(cua_backend, "_cua_no_overlay", return_value=True), \
             patch.object(cua_backend, "_cua_driver_supports_no_overlay", return_value=True):
            result = cua_backend.normalize_user_cua_driver_args(
                "/usr/local/bin/cua-driver", ["mcp"],
            )
        assert result == ["mcp", "--no-overlay"], (
            "user-configured cua-driver MCP must receive --no-overlay "
            "so #81220 cannot reproduce via mcp_servers"
        )


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_probe_cache():
    cua_backend._cua_driver_supports_no_overlay.cache_clear()
    yield
    cua_backend._cua_driver_supports_no_overlay.cache_clear()
