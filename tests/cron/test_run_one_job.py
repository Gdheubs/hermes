"""Characterization + unit tests for the `run_one_job` shared helper (Phase 4A).

`tick`'s per-job body (`_process_job`) is the execute → save → deliver → mark
sequence that fires ONE due job. Phase 4A extracts it into a module-level
`run_one_job(job, *, adapters=None, loop=None, verbose=False)` so the external
Chronos provider's `fire_due` can reuse the IDENTICAL body — no duplicated
correctness.

The first test characterizes the sequence as driven through `tick()` (proving
the extraction didn't change `tick`'s behavior); the rest unit-test the
extracted helper directly.
"""
import cron.scheduler as s


def _patch_pipeline(monkeypatch, *, success=True, output="out", final="final response",
                    error=None, silent_marker_in=None):
    """Patch the job pipeline primitives and record the call order."""
    calls = []

    def fake_run_job(job, *, defer_agent_teardown=None, **kw):
        calls.append(("run_job", job["id"]))
        fr = final if silent_marker_in is None else silent_marker_in
        return (success, output, fr, error)

    def fake_save(jid, out):
        calls.append(("save", jid))
        return f"/tmp/{jid}.txt"

    def fake_deliver(job, content, adapters=None, loop=None, profile_adapters=None):
        calls.append(("deliver", job["id"]))
        return None

    def fake_mark(jid, ok, err=None, delivery_error=None, **_kw):
        calls.append(("mark", jid, ok))

    monkeypatch.setattr(s, "run_job", fake_run_job)
    monkeypatch.setattr(s, "save_job_output", fake_save)
    monkeypatch.setattr(s, "_deliver_result", fake_deliver)
    monkeypatch.setattr(s, "mark_job_run", fake_mark)
    return calls


def test_tick_process_job_sequence(monkeypatch):
    """Characterization: a single due job driven through tick() runs the
    sequence run_job → save → deliver → mark, in that order."""
    calls = _patch_pipeline(monkeypatch)
    monkeypatch.setattr(s, "get_due_jobs", lambda: [{"id": "j1", "name": "t"}])
    monkeypatch.setattr(s, "advance_next_runs", lambda ids: 1)

    s.tick(verbose=False, sync=True)

    assert [c[0] for c in calls] == ["run_job", "save", "deliver", "mark"]
    assert calls[-1] == ("mark", "j1", True)


def test_run_one_job_success_sequence(monkeypatch):
    """The extracted helper runs the same execute→save→deliver→mark sequence
    for a successful job."""
    calls = _patch_pipeline(monkeypatch)

    ok = s.run_one_job({"id": "j2", "name": "t"})

    assert ok is True
    assert [c[0] for c in calls] == ["run_job", "save", "deliver", "mark"]
    assert calls[-1] == ("mark", "j2", True)


def test_run_one_job_installs_secret_scope_under_multiplex(monkeypatch, tmp_path):
    """Regression: under profile isolation (multiplex active), run_one_job must
    execute run_job inside a profile secret scope so credential reads
    (resolve_runtime_provider -> get_secret) don't fail-close with
    UnscopedSecretError, and must tear the scope down afterward.

    Behavior contract: a scope is present during run_job and absent after,
    regardless of the concrete secret values.
    """
    from agent import secret_scope as ss

    # Point cron's home resolution at a profile whose .env carries a secret.
    (tmp_path / ".env").write_text("OPENROUTER_BASE_URL=https://openrouter.ai/api/v1\n")
    monkeypatch.setattr(s, "_get_hermes_home", lambda: tmp_path)

    scope_during_run = {}

    def fake_run_job(job, *, defer_agent_teardown=None, **kw):
        # This is where resolve_runtime_provider() would read a secret. Prove a
        # scope is installed and the profile's secret resolves without raising.
        scope_during_run["scope"] = ss.current_secret_scope()
        scope_during_run["base_url"] = ss.get_secret("OPENROUTER_BASE_URL")
        return (True, "out", "final", None)

    monkeypatch.setattr(s, "run_job", fake_run_job)
    monkeypatch.setattr(s, "save_job_output", lambda jid, out: f"/tmp/{jid}.txt")
    monkeypatch.setattr(s, "_deliver_result", lambda *a, **k: None)
    monkeypatch.setattr(s, "mark_job_run", lambda *a, **k: None)

    ss.set_multiplex_active(True)
    try:
        ok = s.run_one_job({"id": "j7", "name": "t"})
    finally:
        ss.set_multiplex_active(False)

    assert ok is True
    # Scope was installed during run_job and the profile secret resolved.
    assert scope_during_run["scope"] is not None
    assert scope_during_run["base_url"] == "https://openrouter.ai/api/v1"
    # And it was torn down after run_one_job returned (no leak).
    assert ss.current_secret_scope() is None


def test_run_one_job_keeps_secret_scope_through_delivery(monkeypatch, tmp_path):
    """Regression (BUG: delivery drops the profile secret scope): the profile
    secret scope must STILL be installed when _deliver_result runs, so the
    owning profile's credentials (e.g. TELEGRAM_BOT_TOKEN, read via
    load_gateway_config -> _getenv) resolve at delivery time. Before the fix,
    reset_secret_scope ran in the inner finally BEFORE _deliver_result, leaving
    current_secret_scope() == None at delivery — multiplexed jobs delivered
    with no profile scope (wrong/empty token, wrong bot/chat, lost thread).

    Unlike test_run_one_job_installs_secret_scope_under_multiplex (which mocks
    _deliver_result away and therefore cannot catch this), this test injects a
    fake _deliver_result that asserts the scope is present at delivery time and
    that the profile secret resolves.
    """
    from agent import secret_scope as ss

    (tmp_path / ".env").write_text("TELEGRAM_BOT_TOKEN=profile_bot_token_123\n")
    monkeypatch.setattr(s, "_get_hermes_home", lambda: tmp_path)

    delivery_state = {}

    def fake_run_job(job, *, defer_agent_teardown=None, extra_prompt=None):
        return (True, "out", "final response", None)

    def fake_deliver(job, content, adapters=None, loop=None, profile_adapters=None):
        # This is the delivery-time assertion: the profile secret scope must
        # still be installed and the profile secret must resolve.
        delivery_state["scope_at_delivery"] = ss.current_secret_scope()
        delivery_state["token_at_delivery"] = ss.get_secret("TELEGRAM_BOT_TOKEN")
        return None

    monkeypatch.setattr(s, "run_job", fake_run_job)
    monkeypatch.setattr(s, "save_job_output", lambda jid, out: f"/tmp/{jid}.txt")
    monkeypatch.setattr(s, "_deliver_result", fake_deliver)
    monkeypatch.setattr(s, "mark_job_run", lambda *a, **k: None)

    ss.set_multiplex_active(True)
    try:
        ok = s.run_one_job({"id": "j8", "name": "t"})
    finally:
        ss.set_multiplex_active(False)

    assert ok is True
    # The scope was still installed at delivery time (the bug dropped it).
    assert delivery_state["scope_at_delivery"] is not None
    # And the profile's secret resolved at delivery — the symptom that was broken.
    assert delivery_state["token_at_delivery"] == "profile_bot_token_123"
    # Torn down after run_one_job returned (no leak).
    assert ss.current_secret_scope() is None


def test_run_one_job_delivery_uses_profile_adapters(monkeypatch, tmp_path):
    """BUG 1 PART 2: ``_deliver_adapters_for_job`` selects the owning profile's
    live adapter map (Gateway._profile_adapters[profile]) when multiplex is
    active and the job belongs to a secondary profile — NOT the shared
    default-profile ``adapters`` dict."""
    from agent import secret_scope as ss

    (tmp_path / ".env").write_text("TELEGRAM_BOT_TOKEN=profile_bot_token_123\n")
    monkeypatch.setattr(s, "_get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(
        "hermes_cli.profiles.get_active_profile_name", lambda: "home-ops"
    )

    profile_adapters = {"home-ops": {"telegram": "home-ops-telegram-adapter"}}
    shared = {"telegram": "default-telegram-adapter"}

    selected = s._deliver_adapters_for_job(
        {"id": "j9"}, adapters=shared, profile_adapters=profile_adapters
    )
    assert selected == {"telegram": "home-ops-telegram-adapter"}


def test_run_one_job_delivery_falls_back_to_shared_adapters_for_default(monkeypatch, tmp_path):
    """BUG 1 PART 2: for the default profile / non-multiplex path, ``_deliver_adapters_for_job``
    keeps the shared ``adapters`` dict (existing behavior unchanged)."""
    from agent import secret_scope as ss

    (tmp_path / ".env").write_text("TELEGRAM_BOT_TOKEN=x\n")
    monkeypatch.setattr(s, "_get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(
        "hermes_cli.profiles.get_active_profile_name", lambda: "default"
    )

    shared = {"telegram": "default-telegram-adapter"}
    selected = s._deliver_adapters_for_job(
        {"id": "j10"}, adapters=shared,
        profile_adapters={"home-ops": {"telegram": "home-ops-adapter"}},
    )
    assert selected == {"telegram": "default-telegram-adapter"}


def test_run_one_job_threads_profile_adapters_to_delivery(monkeypatch, tmp_path):
    """BUG 1 PART 2 wiring: run_one_job forwards the gateway's per-profile
    adapter map to _deliver_result so the real delivery path can select the
    job profile's adapter. The profile map must reach _deliver_result."""
    from agent import secret_scope as ss

    (tmp_path / ".env").write_text("TELEGRAM_BOT_TOKEN=x\n")
    monkeypatch.setattr(s, "_get_hermes_home", lambda: tmp_path)

    received = {}

    def fake_run_job(job, *, defer_agent_teardown=None, extra_prompt=None):
        return (True, "out", "final response", None)

    def fake_deliver(job, content, adapters=None, loop=None, profile_adapters=None):
        received["profile_adapters"] = profile_adapters
        received["shared_adapters"] = adapters
        return None

    monkeypatch.setattr(s, "run_job", fake_run_job)
    monkeypatch.setattr(s, "save_job_output", lambda jid, out: f"/tmp/{jid}.txt")
    monkeypatch.setattr(s, "_deliver_result", fake_deliver)
    monkeypatch.setattr(s, "mark_job_run", lambda *a, **k: None)

    profile_adapters = {"home-ops": {"telegram": "home-ops-telegram-adapter"}}

    ss.set_multiplex_active(True)
    try:
        ok = s.run_one_job(
            {"id": "j11", "name": "t"},
            adapters={"telegram": "default-telegram-adapter"},
            profile_adapters=profile_adapters,
        )
    finally:
        ss.set_multiplex_active(False)

    assert ok is True
    # The per-profile map reached _deliver_result (the real path uses it to
    # select the job profile's adapter), and the shared dict is still passed.
    assert received["profile_adapters"] == profile_adapters
    assert received["shared_adapters"] == {"telegram": "default-telegram-adapter"}


