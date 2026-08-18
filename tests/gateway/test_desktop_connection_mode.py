"""The resolved Desktop connection mode exposed to skills, MCP, and plugins.

See NousResearch/hermes-agent#82140. The value answers one question — is the
gateway's filesystem the same machine the user is looking at? — and must answer
it authoritatively, so the tests below pin three properties:

1. Only ``'local'``, ``'remote'``, or ``None`` ever come out.
2. A user-set ``HERMES_DESKTOP_CONNECTION_MODE`` in the environment is NOT a
   source of truth (issue acceptance criterion: no user-configurable env var).
3. The value is task-local, so a concurrent local-Desktop turn can't convince a
   remote-Desktop turn (or the CLI) that gateway files are already local.
"""

import asyncio

import pytest

from gateway.session_context import (
    _DESKTOP_CONNECTION_MODE,
    _UNSET,
    _VAR_MAP,
    DESKTOP_CONNECTION_MODE_ENV,
    clear_session_vars,
    desktop_connection_mode,
    get_session_env,
    normalize_desktop_connection_mode,
    reset_session_vars,
    set_desktop_connection_mode,
    set_session_vars,
)


@pytest.fixture(autouse=True)
def _reset_contextvars():
    """Tests share one thread context; restore the "never bound" sentinel."""
    yield
    for var in _VAR_MAP.values():
        var.set(_UNSET)
    _DESKTOP_CONNECTION_MODE.set(_UNSET)


class TestNormalize:
    @pytest.mark.parametrize("value", ["local", "LOCAL", "  Local  "])
    def test_local_variants_resolve_local(self, value):
        assert normalize_desktop_connection_mode(value) == "local"

    @pytest.mark.parametrize("value", ["remote", "cloud", "ssh", "url", "SSH"])
    def test_remote_like_saved_modes_resolve_remote(self, value):
        """A client forwarding its raw saved mode still gets a usable answer."""
        assert normalize_desktop_connection_mode(value) == "remote"

    @pytest.mark.parametrize("value", ["", None, "  ", "lokal", "true", 0, [], {"mode": "local"}])
    def test_unknown_values_resolve_none_not_a_guess(self, value):
        """Unknown must be None: a wrong 'local' sends the user to a missing file."""
        assert normalize_desktop_connection_mode(value) is None


class TestAccessor:
    def test_unbound_session_reports_none(self):
        assert desktop_connection_mode() is None

    def test_bound_mode_is_readable(self):
        set_desktop_connection_mode("remote")
        assert desktop_connection_mode() == "remote"

    def test_bound_garbage_reports_none(self):
        set_desktop_connection_mode("something-else")
        assert desktop_connection_mode() is None


class TestNotUserConfigurable:
    """The acceptance criterion: no user-configurable HERMES_* env var."""

    def test_env_var_is_not_a_source_of_truth(self, monkeypatch):
        monkeypatch.setenv(DESKTOP_CONNECTION_MODE_ENV, "local")
        assert desktop_connection_mode() is None

    def test_env_var_cannot_override_a_bound_remote_session(self, monkeypatch):
        monkeypatch.setenv(DESKTOP_CONNECTION_MODE_ENV, "local")
        set_desktop_connection_mode("remote")
        assert desktop_connection_mode() == "remote"

    def test_not_reachable_through_get_session_env(self, monkeypatch):
        """Mapped vars fall back to os.environ; this one must not be mapped."""
        assert DESKTOP_CONNECTION_MODE_ENV not in _VAR_MAP
        monkeypatch.setenv(DESKTOP_CONNECTION_MODE_ENV, "local")
        assert get_session_env(DESKTOP_CONNECTION_MODE_ENV, "") == "local"  # raw env read
        assert desktop_connection_mode() is None  # the supported API is unmoved


class TestSessionLifecycle:
    def test_set_session_vars_binds_the_mode(self):
        set_session_vars(source="desktop", desktop_connection_mode="remote")
        assert desktop_connection_mode() == "remote"

    def test_set_session_vars_defaults_to_none_for_non_desktop_surfaces(self):
        set_session_vars(platform="telegram", chat_id="-100")
        assert desktop_connection_mode() is None

    def test_clear_session_vars_drops_the_mode(self):
        tokens = set_session_vars(source="desktop", desktop_connection_mode="local")
        clear_session_vars(tokens)
        assert desktop_connection_mode() is None

    def test_reset_session_vars_drops_an_inherited_mode(self):
        """A freshly-spawned task must not inherit a sibling turn's mode."""
        set_desktop_connection_mode("local")
        reset_session_vars()
        assert desktop_connection_mode() is None

    def test_rebinding_reflects_a_connection_switch(self):
        """Switching the active Desktop connection re-announces; last write wins."""
        set_session_vars(source="desktop", desktop_connection_mode="local")
        assert desktop_connection_mode() == "local"
        set_session_vars(source="desktop", desktop_connection_mode="remote")
        assert desktop_connection_mode() == "remote"


def test_mode_is_task_local_across_concurrent_sessions():
    """Two concurrent Desktop clients on one gateway keep their own answers."""

    async def scenario():
        seen: dict[str, str | None] = {}
        started = asyncio.Event()

        async def turn(name: str, mode: str, wait_for_sibling: bool) -> None:
            set_session_vars(source="desktop", desktop_connection_mode=mode)
            if wait_for_sibling:
                started.set()
            else:
                await started.wait()
            # Yield so the sibling task definitely interleaves before we read.
            await asyncio.sleep(0)
            seen[name] = desktop_connection_mode()

        await asyncio.gather(
            turn("local-client", "local", wait_for_sibling=True),
            turn("remote-client", "remote", wait_for_sibling=False),
        )
        return seen

    seen = asyncio.run(scenario())
    assert seen == {"local-client": "local", "remote-client": "remote"}
