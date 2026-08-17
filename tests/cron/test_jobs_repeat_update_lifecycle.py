"""Regression tests for the cron repeat-lifecycle update guard.

Defect (D11.3 heartbeat E2E incident, 2026-08-09): ``cronjob update
repeat=N`` preserved ``repeat.completed``, so a recurring job with
completed=2 converted to a one-shot became ``{times: 1, completed: 2}`` —
born exhausted — and the scheduler GC'd it at the due tick with no execution
row and no output.

Fix contract (v2, amended after 2026-08-09 canary R1-R4/R9/R10):
``update_job`` rejects any update that would leave ``repeat.completed >=
repeat.times`` for finite times, that re-arms an already-exhausted job via a
schedule change or resume/trigger/enable, or that sets ``repeat.completed``
directly — unless ``reset_completed=true`` explicitly starts a fresh
repetition lifecycle. The control flag is never stored on the job.
"""

import json

import pytest

import cron.jobs

from cron.jobs import (
    create_job,
    get_due_jobs,
    get_job,
    mark_job_run,
    resume_job,
    trigger_job,
    update_job,
)

# NOTE: InvalidScheduleUpdate is NOT imported at module level. test_cron_no_agent's
# hermes_env fixture runs importlib.reload(cron.jobs), which re-executes the module
# and creates a NEW class object; a module-level import would bind the OLD class and
# pytest.raises(OldClass) would never match the reloaded module's raises. Always
# resolve the class via fresh attribute access at call time.


@pytest.fixture()
def tmp_cron_dir(tmp_path, monkeypatch):
    """Redirect cron storage to a temp directory."""
    monkeypatch.setattr("cron.jobs.CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr("cron.jobs.JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr("cron.jobs.OUTPUT_DIR", tmp_path / "cron" / "output")
    return tmp_path


def _recurring(completed):
    """Recurring (forever) job with N completed runs."""
    job = create_job(prompt="Recurring", schedule="every 1h")
    for _ in range(completed):
        mark_job_run(job["id"], success=True)
    return get_job(job["id"])  # re-read: mark_job_run mutates the store


def _limited(times, completed):
    """Repeat-limited job with N completed runs."""
    job = create_job(prompt="Limited", schedule="every 1h", repeat=times)
    for _ in range(completed):
        mark_job_run(job["id"], success=True)
    return get_job(job["id"])  # re-read: mark_job_run mutates the store


def _spent_oneshot():
    """One-shot (times=1) that has already run: {times:1, completed:1}."""
    job = create_job(prompt="Once", schedule="30m", repeat=1)
    mark_job_run(job["id"], success=True)
    assert get_job(job["id"])["repeat"]["completed"] == 1
    return job["id"]


class TestRepeatLifecycleUpdateGuard:
    def test_forever_to_times_below_completed_rejected(self, tmp_cron_dir):
        """forever (times=null, completed=2) -> times=1 must be rejected —
        this is the exact D11.3 incident shape (2/1 born exhausted)."""
        job = _recurring(completed=2)
        assert job["repeat"]["completed"] == 2
        with pytest.raises(cron.jobs.InvalidScheduleUpdate, match="reset_completed=true"):
            update_job(job["id"], {"repeat": {"times": 1}})

    def test_tighten_above_completed_accepted(self, tmp_cron_dir):
        """times=5/completed=2 -> times=3 is a valid tightening; completed kept."""
        job = _limited(times=5, completed=2)
        updated = update_job(job["id"], {"repeat": {"times": 3}})
        assert updated["repeat"]["times"] == 3
        assert updated["repeat"]["completed"] == 2
        # R10: assert from disk, not just the return value.
        disk = get_job(job["id"])
        assert disk["repeat"]["times"] == 3
        assert disk["repeat"]["completed"] == 2

    def test_tighten_below_completed_without_reset_rejected(self, tmp_cron_dir):
        """times=5/completed=4 -> times=1 with reset=false must be rejected."""
        job = _limited(times=5, completed=4)
        with pytest.raises(cron.jobs.InvalidScheduleUpdate, match="reset_completed=true"):
            update_job(job["id"], {"repeat": {"times": 1}, "reset_completed": False})

    def test_tighten_below_completed_with_reset_ok(self, tmp_cron_dir):
        """Same tightening with reset=true starts a fresh lifecycle: completed=0."""
        job = _limited(times=5, completed=4)
        updated = update_job(
            job["id"], {"repeat": {"times": 1}, "reset_completed": True}
        )
        assert updated["repeat"]["times"] == 1
        assert updated["repeat"]["completed"] == 0
        disk = get_job(job["id"])
        assert disk["repeat"]["times"] == 1
        assert disk["repeat"]["completed"] == 0

    def test_reschedule_exhausted_oneshot_rejected(self, tmp_cron_dir):
        """Re-arming a spent one-shot (times=1/completed=1) via a schedule
        change must be rejected — otherwise the scheduler silently GC's the
        newly-updated job on inherited exhausted state."""
        job_id = _spent_oneshot()
        with pytest.raises(cron.jobs.InvalidScheduleUpdate, match="exhausted"):
            update_job(job_id, {"schedule": "2h"})

    def test_reschedule_exhausted_oneshot_with_reset_then_due(self, tmp_cron_dir):
        """The same reschedule with reset=true is accepted, the counter re-arms,
        and the job becomes due again — no silent GC."""
        job_id = _spent_oneshot()
        updated = update_job(
            job_id, {"schedule": "2h", "reset_completed": True}
        )
        assert updated["repeat"]["completed"] == 0
        assert updated["next_run_at"] is not None
        # Explicit re-enable is still the operator's step (completed jobs stay
        # disabled); once triggered, the job must actually be due — proving the
        # scheduler will fire it instead of GC'ing on inherited exhaustion.
        trigger_job(job_id)
        due = {j["id"] for j in get_due_jobs()}
        assert job_id in due

    def test_reset_completed_never_stored(self, tmp_cron_dir):
        """The control flag must not leak into the saved record."""
        job = _limited(times=5, completed=4)
        updated = update_job(
            job["id"], {"repeat": {"times": 1}, "reset_completed": True}
        )
        assert "reset_completed" not in updated
        assert "reset_completed" not in get_job(job["id"])

    def test_partial_repeat_dict_preserves_completed(self, tmp_cron_dir):
        """A caller passing only {"times": N} must not silently zero completed
        (the born-exhausted bug at the store layer)."""
        job = _recurring(completed=2)
        updated = update_job(job["id"], {"repeat": {"times": 4}})
        assert updated["repeat"]["times"] == 4
        assert updated["repeat"]["completed"] == 2
        disk = get_job(job["id"])
        assert disk["repeat"]["times"] == 4
        assert disk["repeat"]["completed"] == 2

    def test_forever_means_infinite_regardless_of_completed(self, tmp_cron_dir):
        """times=null (forever) with any completed count stays valid."""
        job = _recurring(completed=2)
        updated = update_job(job["id"], {"repeat": {"times": None}})
        assert updated["repeat"]["times"] is None
        assert updated["repeat"]["completed"] == 2
        disk = get_job(job["id"])
        assert disk["repeat"]["times"] is None
        assert disk["repeat"]["completed"] == 2


class TestR1ExactEqualityPredicate:
    """R1: guard predicate must match the scheduler's `completed >= times`
    exhaustion check — `times == completed` is born exhausted too."""

    def test_tighten_to_exact_completed_rejected(self, tmp_cron_dir):
        """times=5/completed=4 -> times=4 is `completed >= new_times` (4/4 =
        exhausted per the scheduler); v1's strict `>` accepted this and the job
        was silently GC'd on the repair path."""
        job = _limited(times=5, completed=4)
        with pytest.raises(cron.jobs.InvalidScheduleUpdate, match="reset_completed=true"):
            update_job(job["id"], {"repeat": {"times": 4}})

    def test_noop_tighten_to_exact_completed_rejected(self, tmp_cron_dir):
        """times=4/completed=4 -> times=4 (no-op) is still exhausted; must not
        silently pass and leave the job on the GC path."""
        job = _limited(times=4, completed=4)
        with pytest.raises(cron.jobs.InvalidScheduleUpdate, match="reset_completed=true"):
            update_job(job["id"], {"repeat": {"times": 4}})

    def test_exact_with_reset_ok(self, tmp_cron_dir):
        """Exact-equality with reset=true re-arms: completed=0, due."""
        job = _limited(times=4, completed=4)
        updated = update_job(
            job["id"], {"repeat": {"times": 4}, "reset_completed": True}
        )
        assert updated["repeat"]["times"] == 4
        assert updated["repeat"]["completed"] == 0
        assert get_job(job["id"])["repeat"]["completed"] == 0


class TestR3RearmSiblings:
    """R3: resume/trigger/enable on an exhausted finite job must be rejected
    (sibling re-arm paths v1 left unguarded — silent GC class)."""

    def test_trigger_exhausted_oneshot_rejected(self, tmp_cron_dir):
        job_id = _spent_oneshot()
        with pytest.raises(cron.jobs.InvalidScheduleUpdate, match="re-arm"):
            trigger_job(job_id)

    def test_resume_exhausted_oneshot_rejected(self, tmp_cron_dir):
        job_id = _spent_oneshot()
        with pytest.raises(cron.jobs.InvalidScheduleUpdate, match="re-arm"):
            resume_job(job_id)

    def test_enable_exhausted_oneshot_rejected(self, tmp_cron_dir):
        job_id = _spent_oneshot()
        with pytest.raises(cron.jobs.InvalidScheduleUpdate, match="re-arm"):
            update_job(job_id, {"enabled": True})

    def test_trigger_healthy_finite_allowed(self, tmp_cron_dir):
        """A finite job with headroom (times=5/completed=2) can still be
        triggered — the guard must not break the normal re-arm path."""
        job = _limited(times=5, completed=2)
        trigger_job(job["id"])
        due = {j["id"] for j in get_due_jobs()}
        assert job["id"] in due

    def test_trigger_recurring_allowed(self, tmp_cron_dir):
        """Recurring (times=null) jobs are never exhausted — trigger stays open."""
        job = _recurring(completed=3)
        trigger_job(job["id"])
        due = {j["id"] for j in get_due_jobs()}
        assert job["id"] in due


class TestR4DirectCompletedGuard:
    """R4: `repeat.completed` may only be changed via reset_completed=true —
    an explicit completed in the incoming dict bypasses the flag contract."""

    def test_explicit_completed_without_flag_rejected(self, tmp_cron_dir):
        job = _recurring(completed=2)
        with pytest.raises(cron.jobs.InvalidScheduleUpdate, match="reset_completed=true"):
            update_job(job["id"], {"repeat": {"times": 1, "completed": 0}})

    def test_explicit_completed_with_flag_ok(self, tmp_cron_dir):
        """With the flag, explicit completed is allowed (reset wins, stored as 0)."""
        job = _recurring(completed=2)
        updated = update_job(
            job["id"],
            {"repeat": {"times": 1, "completed": 0}, "reset_completed": True},
        )
        assert updated["repeat"]["times"] == 1
        assert updated["repeat"]["completed"] == 0


class TestR9NonDictRepeat:
    """R9: non-dict `repeat` must be a loud, typed error — not AttributeError."""

    def test_int_repeat_rejected(self, tmp_cron_dir):
        job = _recurring(completed=2)
        with pytest.raises(cron.jobs.InvalidScheduleUpdate, match="dict"):
            update_job(job["id"], {"repeat": 1})

    def test_string_repeat_rejected(self, tmp_cron_dir):
        job = _recurring(completed=2)
        with pytest.raises(cron.jobs.InvalidScheduleUpdate, match="dict"):
            update_job(job["id"], {"repeat": "every 1h"})

    def test_null_repeat_allowed(self, tmp_cron_dir):
        """repeat=null means infinite — a legit explicit value, not an error.
        (Stored shape is a falsy repeat; the scheduler reads falsy as no limit.)"""
        job = _recurring(completed=2)
        updated = update_job(job["id"], {"repeat": None})
        assert not (updated.get("repeat") or {})  # falsy = infinite
        assert get_job(job["id"])["repeat"] in (None, {})


class TestCronjobToolSchemaSurfacesReset:
    def test_reset_completed_in_tool_schema(self):
        """The cronjob tool schema must expose reset_completed for updates."""
        from tools.cronjob_tools import CRONJOB_SCHEMA

        props = CRONJOB_SCHEMA["parameters"]["properties"]
        assert "reset_completed" in props
        assert props["reset_completed"]["type"] == "boolean"


class TestToolLevelE2E:
    """R10: the guard and the flag must be wired through the REAL tool surface
    (not just update_job directly) and asserted via disk re-read."""

    def test_tool_repeat_lowering_without_flag_rejected(self, tmp_cron_dir):
        from tools.cronjob_tools import cronjob

        job = _recurring(completed=2)
        out = json.loads(
            cronjob(action="update", job_id=job["id"], repeat=1)
        )
        assert out["success"] is False
        assert "reset_completed=true" in out.get("error", "")
        # R10: job survives byte-for-byte on disk — no silent mutation.
        disk = get_job(job["id"])
        assert disk["repeat"]["times"] is None
        assert disk["repeat"]["completed"] == 2

    def test_tool_repeat_lowering_with_flag_ok(self, tmp_cron_dir):
        from tools.cronjob_tools import cronjob

        job = _recurring(completed=2)
        out = json.loads(
            cronjob(action="update", job_id=job["id"], repeat=1, reset_completed=True)
        )
        assert out["success"] is True, out
        disk = get_job(job["id"])
        assert disk["repeat"]["times"] == 1
        assert disk["repeat"]["completed"] == 0
        assert "reset_completed" not in disk

    def test_tool_schedule_only_reset_ok(self, tmp_cron_dir):
        """R2/FM2 regression: a schedule-only reset (no `repeat` in the same
        update) must forward the flag — v1 silently dropped it here."""
        from tools.cronjob_tools import cronjob

        job_id = _spent_oneshot()
        out = json.loads(
            cronjob(action="update", job_id=job_id, schedule="2h", reset_completed=True)
        )
        assert out["success"] is True, out
        disk = get_job(job_id)
        assert disk["repeat"]["completed"] == 0
        assert "reset_completed" not in disk

    def test_tool_reschedule_exhausted_without_flag_rejected(self, tmp_cron_dir):
        from tools.cronjob_tools import cronjob

        job_id = _spent_oneshot()
        out = json.loads(
            cronjob(action="update", job_id=job_id, schedule="2h")
        )
        assert out["success"] is False
        assert "exhausted" in out.get("error", "")
        disk = get_job(job_id)
        assert disk["repeat"]["completed"] == 1  # untouched on disk


class TestR3TruthyEnabled:
    """FM-v2-3: sloppy truthy `enabled` (1, "true") must not bypass the
    re-arm guard — truthiness, not `is True`."""

    def test_enabled_int_rejected(self, tmp_cron_dir):
        job_id = _spent_oneshot()
        with pytest.raises(cron.jobs.InvalidScheduleUpdate, match="re-arm"):
            update_job(job_id, {"enabled": 1})

    def test_enabled_str_rejected(self, tmp_cron_dir):
        job_id = _spent_oneshot()
        with pytest.raises(cron.jobs.InvalidScheduleUpdate, match="re-arm"):
            update_job(job_id, {"enabled": "true"})

    def test_healthy_job_enabled_int_allowed(self, tmp_cron_dir):
        """Truthiness must not over-reject: a healthy finite job with headroom
        can be enabled with a sloppy truthy value (non-canonical but harmless)."""
        job = _limited(times=5, completed=2)
        updated = update_job(job["id"], {"enabled": 1})
        assert updated["enabled"] is True or updated["enabled"] == 1


class TestStrictResetCoercion:
    """FM-v2-5: only literal True triggers a reset. `bool("false")` is True,
    so string/truthy non-bool values must NOT silently re-arm a job."""

    def test_string_false_does_not_reset(self, tmp_cron_dir):
        job = _limited(times=5, completed=4)
        with pytest.raises(cron.jobs.InvalidScheduleUpdate, match="reset_completed=true"):
            update_job(job["id"], {"repeat": {"times": 1}, "reset_completed": "false"})
        # Job untouched on disk.
        assert get_job(job["id"])["repeat"]["completed"] == 4

    def test_int_one_does_not_reset(self, tmp_cron_dir):
        job = _limited(times=5, completed=4)
        with pytest.raises(cron.jobs.InvalidScheduleUpdate, match="reset_completed=true"):
            update_job(job["id"], {"repeat": {"times": 1}, "reset_completed": 1})
        assert get_job(job["id"])["repeat"]["completed"] == 4

    def test_literal_true_resets(self, tmp_cron_dir):
        job = _limited(times=5, completed=4)
        updated = update_job(
            job["id"], {"repeat": {"times": 1}, "reset_completed": True}
        )
        assert updated["repeat"]["completed"] == 0


class TestConsoleSurfaceSurfacesGuard:
    """FM-v2-2: console `cron run` / `cron resume` on an exhausted job must
    raise ConsoleCommandError (clean message), not an unhandled traceback."""

    def test_console_resume_exhausted_is_command_error(self, tmp_cron_dir):
        from hermes_cli.console_engine import ConsoleCommandError, _cron_resume

        job_id = _spent_oneshot()
        with pytest.raises(ConsoleCommandError, match="re-arm"):
            _cron_resume(None, [job_id])

    def test_console_run_exhausted_is_command_error(self, tmp_cron_dir):
        from hermes_cli.console_engine import ConsoleCommandError, _cron_run

        job_id = _spent_oneshot()
        with pytest.raises(ConsoleCommandError, match="re-arm"):
            _cron_run(None, [job_id])

    def test_console_resume_healthy_ok(self, tmp_cron_dir):
        from hermes_cli.console_engine import _cron_resume

        job = _limited(times=5, completed=2)
        r = _cron_resume(None, [job["id"]])
        assert "Resumed" in r
