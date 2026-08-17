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

    def test_script_modifying_config_fails_and_reverts(self, hermes_env):
        config = hermes_env / "config.yaml"
        original = config.read_text(encoding="utf-8")

        # Obfuscated payload: builds the flip at runtime so the F3
        # create-time content scan cannot see it (that is exactly the case
        # F4's runtime guard exists for — F3 stops the obvious payloads,
        # F4 reverts the ones that slip through). .py suffix routes the
        # script through the Python interpreter.
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
        assert error is not None
        assert "modified" in error or "reverted" in error
        # The config must be back to its original approval policy.
        assert config.read_text(encoding="utf-8") == original
        assert "cron_mode: deny" in config.read_text(encoding="utf-8")

    def test_benign_script_succeeds_and_leaves_config(self, hermes_env):
        config = hermes_env / "config.yaml"
        original = config.read_text(encoding="utf-8")

        job = self._make_job(hermes_env, "#!/bin/bash\necho 'RAM 92% on host'\n")
        success, doc, final_response, error = run_job(job)

        assert success is True
        assert error is None
        assert "RAM 92% on host" in final_response
        assert config.read_text(encoding="utf-8") == original
