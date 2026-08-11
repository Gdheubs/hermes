"""Regression: background tasks respect profile secret scope when multiplexing.

Issue #60726: /background spawns _run_background_task as a fire-and-forget
asyncio task with no profile scope, so _resolve_session_agent_runtime()'s
credential reads raise UnscopedSecretError when multiplex_profiles is on.
The fix wraps the task body in _profile_runtime_scope, mirroring _run_agent.
"""
import asyncio
from pathlib import Path
from unittest import mock

from gateway.config import GatewayConfig
from gateway.run import GatewayRunner


def _make_runner(multiplex: bool) -> GatewayRunner:
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig(multiplex_profiles=multiplex)
    return runner


class TestBackgroundTaskProfileScope:
    """_run_background_task installs _profile_runtime_scope when multiplexing is active."""

    def test_wraps_in_profile_scope_when_multiplex_active(self):
        runner = _make_runner(multiplex=True)
        inner = mock.AsyncMock(return_value=None)
        runner._run_background_task_inner = inner

        source = mock.MagicMock()
        source.profile = "test_profile"
        decision_after_old_limit = "APPROVE_THE_REPORT_AFTER_CHARACTER_500"
        complete_reply = "q" * 700 + decision_after_old_limit

        with mock.patch.object(
            GatewayRunner,
            "_resolve_profile_home_for_source",
            return_value=Path("/fake/profile"),
        ), mock.patch("gateway.run._profile_runtime_scope") as scope:
            scope.return_value.__enter__ = mock.MagicMock()
            scope.return_value.__exit__ = mock.MagicMock(return_value=False)
            asyncio.run(
                runner._run_background_task(
                    prompt="test",
                    source=source,
                    task_id="bg_test",
                    parent_session_id="parent",
                    parent_session_key="telegram:chat",
                    reply_to_text=complete_reply,
                    reply_to_is_own_message=True,
                )
            )

        scope.assert_called_once_with(Path("/fake/profile"))
        inner.assert_awaited_once()
        forwarded = inner.await_args.kwargs
        assert forwarded["parent_session_id"] == "parent"
        assert forwarded["parent_session_key"] == "telegram:chat"
        assert forwarded["reply_to_text"] == complete_reply
        assert decision_after_old_limit in forwarded["reply_to_text"]
        assert forwarded["reply_to_is_own_message"] is True


