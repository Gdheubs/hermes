from agent.verification_stop import build_verify_on_stop_nudge, verify_on_stop_enabled


class TestVerifyOnStopDefaults:
    def test_default_path_disabled(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
        monkeypatch.delenv("HERMES_VERIFY_ON_STOP", raising=False)
        monkeypatch.setenv("HERMES_SESSION_SOURCE", "cli")
        assert verify_on_stop_enabled() is False

    def test_env_can_disable(self, monkeypatch):
        monkeypatch.setenv("HERMES_VERIFY_ON_STOP", "0")
        assert verify_on_stop_enabled({}) is False


class TestVerifyOnStopNudge:
    def test_stale_attempts_stop_loop(self):
        # At max_attempts, returning None tells the conversation loop to
        # accept the final answer and stop nudging.
        nudge = build_verify_on_stop_nudge(
            session_id="s1",
            changed_paths=["src/app.ts"],
            attempts=999,
            max_attempts=2,
        )
        assert nudge is None
