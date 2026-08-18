"""F4 regression tests: no_agent script lane cannot tamper with config.yaml.

Approval policy (approvals.cron_mode / approvals.mode / yolo) lives in
config.yaml and approval reads are mtime-keyed, so a no_agent script that
rewrites config.yaml would flip the approval gate and the next tick would
pick the flip up. Scripts run as subprocesses OUTSIDE the file_tools /
terminal hard-blocks that protect config.yaml.

These tests pin the snapshot/restore guard in cron/scheduler.py:
- a script that modifies config.yaml during its run is detected, the
  change is reverted, and the run fails with a clear message,
- a benign script leaves config.yaml untouched and succeeds,
- a script that DELETES config.yaml is also caught and restored.
"""

import json
import os

import pytest

from cron.scheduler import (
    _restore_config_yaml_if_tampered,
    _snapshot_config_yaml,
    run_job,
)


@pytest.fixture
def hermes_env(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with a config.yaml and a scripts dir."""
    home = tmp_path / "hermes"
    (home / "scripts").mkdir(parents=True, exist_ok=True)
    config = home / "config.yaml"
    config.write_text(
        "approvals:\n  cron_mode: deny\n  mode: manual\n", encoding="utf-8"
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    # Force config-path resolution to the isolated home for the snapshot.
    monkeypatch.setattr(
        "hermes_cli.config.get_config_path",
        lambda: config,
    )
    return home


class TestSnapshotRestoreGuard:
    def test_untouched_config_returns_none(self, hermes_env):
        snap = _snapshot_config_yaml()
        assert snap is not None
        assert _restore_config_yaml_if_tampered(snap) is None

    def test_modified_config_is_reverted(self, hermes_env):
        snap = _snapshot_config_yaml()
        config = hermes_env / "config.yaml"

        # Simulate the script flipping cron_mode deny -> approve.
        config.write_text(
            "approvals:\n  cron_mode: approve\n  mode: manual\n",
            encoding="utf-8",
        )

        message = _restore_config_yaml_if_tampered(snap)
        assert message is not None
        assert "modified" in message
        # Reverted to the original bytes.
        assert config.read_text(encoding="utf-8") == (
            "approvals:\n  cron_mode: deny\n  mode: manual\n"
        )

    def test_deleted_config_is_restored(self, hermes_env):
        snap = _snapshot_config_yaml()
        config = hermes_env / "config.yaml"
        config.unlink()

        message = _restore_config_yaml_if_tampered(snap)
        assert message is not None
        assert config.read_text(encoding="utf-8") == (
            "approvals:\n  cron_mode: deny\n  mode: manual\n"
        )


class TestRunJobScriptTamperGuard:
    def _make_job(self, hermes_env, script_body, name="tamper.sh"):
        from cron.jobs import create_job
        (hermes_env / "scripts" / name).write_text(
            script_body, encoding="utf-8"
        )
        return create_job(
            prompt=None, schedule="every 5m",
            script=name, no_agent=True, deliver="local",
        )

    def test_script_modifying_config_blocked_by_prevention(self, hermes_env):
        """F4 (prevention): the flip never lands. The obfuscated payload
        (built at runtime so the F3 create-time content scan cannot see it)
        is blocked at write time — config.yaml is non-writable for the
        child's lifetime — so the approval policy is never exposed, the
        script crashes on the denied write, and the config is byte-identical
        afterwards. (The completion-time revert remains as defense in depth
        for deliberate writers that restore writability first.)"""
        import os
        config = hermes_env / "config.yaml"
        original = config.read_text(encoding="utf-8")

        job = self._make_job(
            hermes_env,
            "import os\n"
            "home = os.environ['HERMES_HOME']\n"
            "p = os.path.join(home, 'config.yaml')\n"
            "data = open(p).read()\n"
            "data = data.replace('cron_mode: deny', 'cron_mode: ' + 'approve')\n"
            "open(p, 'w').write(data)\n"
            "print('tampered')\n",
            name="tamper.py",
        )
        success, doc, final_response, error = run_job(job)

        assert success is False
        # The write was denied — the config never contained the flip.
        assert config.read_text(encoding="utf-8") == original
        assert "cron_mode: deny" in config.read_text(encoding="utf-8")
        assert "PermissionError" in (error or "") or "denied" in (error or "")

    def test_delayed_detached_writer_after_first_clean_observation(self, hermes_env):
        """F4/P3 review matrix: a genuinely detached child that rewrites
        config.yaml AFTER the first clean settle observation must not win —
        the settle loop watches the FULL window (no break on clean) once
        tampering was detected. The script flips the policy (deliberate
        writer: restores writability first — the prevention pass cannot stop
        it, which is exactly why the revert + settle remain), then detaches
        a nohup child that re-flips ~5s later, after the completion revert."""
        import os
        if os.name == "nt":
            import pytest
            pytest.skip("POSIX-only: Windows scripts cannot chmod writability back")

        import stat
        config = hermes_env / "config.yaml"
        original = config.read_text(encoding="utf-8")
        os.chmod(config, 0o600)

        body = (
            "#!/bin/bash\n"
            "chmod u+w \"$HERMES_HOME/config.yaml\" 2>/dev/null || true\n"
            "printf 'approvals:\\n  cron_mode: approve\\n  mode: manual\\n' "
            "> \"$HERMES_HOME/config.yaml\"\n"
            "nohup bash -c 'sleep 5; printf \"approvals:\\n  cron_mode: approve\\n"
            "  mode: manual\\n\" > \"$HERMES_HOME/config.yaml\"' >/dev/null 2>&1 &\n"
            "echo detached\n"
        )
        job = self._make_job(hermes_env, body, name="detach.sh")
        success, doc, final_response, error = run_job(job)

        # The detached child's late write (after the first clean observation)
        # was caught by the full settle window and reverted.
        assert success is False
        assert "modified" in (error or "") or "reverted" in (error or "")
        assert config.read_text(encoding="utf-8") == original
        assert "cron_mode: deny" in config.read_text(encoding="utf-8")

    def test_script_created_config_when_absent_is_removed(self, hermes_env):
        """F4 review matrix: initially ABSENT config — a script-created
        config.yaml must not persist (the old snapshot returned None for the
        absent case, so the created file survived). The absent sentinel
        removes it."""
        import os
        config = hermes_env / "config.yaml"
        config.unlink()

        body = (
            "#!/bin/bash\n"
            # Obfuscated at runtime (cron_mode: app\"rove\"): the F3
            # create-time content scan must NOT see the flip — F4's absent
            # sentinel is the guard under test here.
            "printf 'approvals:\\n  cron_mode: app\"rove\"\\n  mode: manual\\n' "
            "> \"$HERMES_HOME/config.yaml\"\n"
            "echo created\n"
        )
        job = self._make_job(hermes_env, body, name="createcfg.sh")
        success, doc, final_response, error = run_job(job)

        assert success is False
        assert "created" in (error or "")
        assert not config.exists(), "script-created config must be removed"

    def test_restore_preserves_security_metadata(self, hermes_env):
        """F4 review matrix: the revert must preserve security metadata — a
        0600 policy file must not come back with a broader (umask-derived)
        mode after os.replace."""
        import os
        import stat
        if os.name == "nt":
            import pytest
            pytest.skip("mode bits are meaningless on Windows")
        config = hermes_env / "config.yaml"
        original = config.read_text(encoding="utf-8")
        os.chmod(config, 0o600)

        body = (
            "#!/bin/bash\n"
            "chmod u+w \"$HERMES_HOME/config.yaml\" 2>/dev/null || true\n"
            "printf 'approvals:\\n  cron_mode: approve\\n  mode: manual\\n' "
            "> \"$HERMES_HOME/config.yaml\"\n"
            "echo flipped\n"
        )
        job = self._make_job(hermes_env, body, name="flip.sh")
        success, doc, final_response, error = run_job(job)

        assert success is False
        assert config.read_text(encoding="utf-8") == original
        mode = stat.S_IMODE(os.stat(config).st_mode)
        assert mode == 0o600, f"mode after restore: {oct(mode)}"

    def test_passive_write_prevented_and_config_intact(self, hermes_env):
        """F4 prevention: a passive writer (no writability workaround) is
        blocked at write time — the config never changes and the protection
        is fully released afterwards (subsequent writes by the operator
        work)."""
        import os
        config = hermes_env / "config.yaml"
        original = config.read_text(encoding="utf-8")

        body = (
            "#!/bin/bash\n"
            "echo 'approvals:' >> \"$HERMES_HOME/config.yaml\"\n"
            "echo attempted\n"
        )
        job = self._make_job(hermes_env, body, name="passive.sh")
        success, doc, final_response, error = run_job(job)

        assert config.read_text(encoding="utf-8") == original
        # Protection was released: the operator can write again.
        config.write_text(original, encoding="utf-8")

    def test_benign_script_succeeds_and_leaves_config(self, hermes_env):
        config = hermes_env / "config.yaml"
        original = config.read_text(encoding="utf-8")

        job = self._make_job(hermes_env, "#!/bin/bash\necho 'RAM 92% on host'\n")
        success, doc, final_response, error = run_job(job)

        assert success is True
        assert error is None
        assert "RAM 92% on host" in final_response
        assert config.read_text(encoding="utf-8") == original

    def test_settle_loop_reverts_detached_retamper(self, hermes_env, monkeypatch):
        """F4/P2: a detached child re-flipping config.yaml AFTER the
        completion revert must not win — the settle loop re-reverts."""
        import cron.scheduler as sched
        monkeypatch.setattr(sched.time, "sleep", lambda s: None)
        real_restore = sched._restore_config_yaml_if_tampered

        config = hermes_env / "config.yaml"
        original = config.read_text(encoding="utf-8")
        calls = {"n": 0}

        def _flaky_restore(snapshot):
            calls["n"] += 1
            if calls["n"] == 1:
                # Simulate the script's detached child re-flipping the
                # approval policy right after the first completion revert.
                config.write_text(
                    "approvals:\n  cron_mode: approve\n  mode: manual\n",
                    encoding="utf-8",
                )
            return real_restore(snapshot)

        monkeypatch.setattr(sched, "_restore_config_yaml_if_tampered", _flaky_restore)

        job = self._make_job(hermes_env, "#!/bin/bash\necho hi\n")
        success, doc, final_response, error = run_job(job)

        assert success is False
        assert config.read_text(encoding="utf-8") == original
        assert calls["n"] >= 2, "settle loop must re-check after the first revert"
