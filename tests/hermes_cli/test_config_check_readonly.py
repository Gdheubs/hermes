"""Regression tests for the read-only ``hermes config check`` contract."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from hermes_cli import config as config_mod


REPO_ROOT = Path(__file__).resolve().parents[2]


def _write_explicit_gate_config(tmp_path, *, write_approval=False):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "_config_version": config_mod.DEFAULT_CONFIG["_config_version"],
                "memory": {"write_approval": write_approval},
                "skills": {"write_approval": write_approval},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return config_path


def _fingerprint(path):
    stat = path.stat()
    return hashlib.sha256(path.read_bytes()).hexdigest(), stat.st_mtime_ns


def test_config_check_preserves_config_bytes_and_mtime(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    config_path = _write_explicit_gate_config(tmp_path)
    before = _fingerprint(config_path)

    config_mod.config_command(SimpleNamespace(config_command="check"))

    assert _fingerprint(config_path) == before
    persisted = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert persisted["memory"]["write_approval"] is False
    assert persisted["skills"]["write_approval"] is False
    capsys.readouterr()


def test_config_check_rejects_transitive_config_write(tmp_path, monkeypatch, capsys):
    """A diagnostic imported by ``config check`` must not persist config."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["pytest"])
    config_path = _write_explicit_gate_config(tmp_path)
    before = _fingerprint(config_path)

    def diagnostic_with_write_side_effect():
        config = config_mod.load_config()
        config["memory"]["write_approval"] = True
        config["skills"]["write_approval"] = True
        config_mod.save_config(config)
        return []

    monkeypatch.setattr(
        config_mod,
        "get_missing_config_fields",
        diagnostic_with_write_side_effect,
    )

    with pytest.raises(RuntimeError, match=r"config check.*read-only"):
        config_mod.config_command(SimpleNamespace(config_command="check"))

    assert _fingerprint(config_path) == before
    persisted = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert persisted["memory"]["write_approval"] is False
    assert persisted["skills"]["write_approval"] is False
    capsys.readouterr()


def test_config_check_guard_is_scoped_from_mutating_commands(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    config_path = _write_explicit_gate_config(tmp_path)

    config_mod.config_command(SimpleNamespace(config_command="check"))
    config_mod.set_config_value("memory.write_approval", "true")
    config_mod.unset_config_value("skills.write_approval")

    persisted = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert persisted["memory"]["write_approval"] is True
    assert "write_approval" not in persisted.get("skills", {})

    persisted["_config_version"] = config_mod.DEFAULT_CONFIG["_config_version"] - 1
    persisted["skills"] = {"write_approval": False}
    config_path.write_text(yaml.safe_dump(persisted, sort_keys=False), encoding="utf-8")
    config_mod._RAW_CONFIG_CACHE.clear()

    config_mod.migrate_config(interactive=False, quiet=True)

    migrated = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert migrated["_config_version"] == config_mod.DEFAULT_CONFIG["_config_version"]
    assert migrated["memory"]["write_approval"] is True
    assert migrated["skills"]["write_approval"] is False
    capsys.readouterr()


def _profile_cli_env(source_home: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["HERMES_HOME"] = str(source_home)
    env["HERMES_PROFILE"] = source_home.name
    env["PYTHONPATH"] = os.pathsep.join([
        str(REPO_ROOT),
        env.get("PYTHONPATH", ""),
    ]).rstrip(os.pathsep)
    return env


def _run_profile_config_check(
    source_home: Path,
    target_name: str,
    env,
    *,
    global_args=(),
):
    return subprocess.run(
        [
            sys.executable,
            "-c",
            "from hermes_cli.main import main; main()",
            "--profile",
            target_name,
            *global_args,
            "config",
            "check",
        ],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )


def test_profile_scoped_cli_config_check_is_byte_identical(tmp_path):
    source_home = tmp_path / "profiles" / "source"
    target_home = tmp_path / "profiles" / "target"
    source_home.mkdir(parents=True)
    target_home.mkdir(parents=True)
    source_config = _write_explicit_gate_config(source_home, write_approval=True)
    target_config = _write_explicit_gate_config(target_home)
    source_before = _fingerprint(source_config)
    before = _fingerprint(target_config)

    result = _run_profile_config_check(
        source_home,
        target_home.name,
        _profile_cli_env(source_home),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert _fingerprint(source_config) == source_before
    assert _fingerprint(target_config) == before


@pytest.mark.parametrize("global_args", [(), ("--yolo",), ("--cli",)])
def test_profile_scoped_cli_blocks_startup_hook_config_write(tmp_path, global_args):
    source_home = tmp_path / "profiles" / "source"
    target_home = tmp_path / "profiles" / "target"
    source_home.mkdir(parents=True)
    target_home.mkdir(parents=True)
    source_config = _write_explicit_gate_config(source_home, write_approval=True)
    target_config = _write_explicit_gate_config(target_home)
    source_before = _fingerprint(source_config)
    before = _fingerprint(target_config)

    bootstrap_dir = tmp_path / "bootstrap"
    bootstrap_dir.mkdir()
    (bootstrap_dir / "sitecustomize.py").write_text(
        """
import hermes_cli.env_loader as env_loader

def load_hermes_dotenv(*args, **kwargs):
    from hermes_cli.config import load_config, save_config
    config = load_config()
    config["memory"]["write_approval"] = True
    config["skills"]["write_approval"] = True
    save_config(config)
    return []

env_loader.load_hermes_dotenv = load_hermes_dotenv
""".lstrip(),
        encoding="utf-8",
    )
    env = _profile_cli_env(source_home)
    env["PYTHONPATH"] = os.pathsep.join([str(bootstrap_dir), env["PYTHONPATH"]])

    result = _run_profile_config_check(
        source_home,
        target_home.name,
        env,
        global_args=global_args,
    )

    assert result.returncode != 0
    assert "config check` is read-only" in result.stderr
    assert _fingerprint(source_config) == source_before
    assert _fingerprint(target_config) == before


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["--yolo", "config", "check"], True),
        (["config", "--cli", "check"], True),
        (["--model", "config", "check"], False),
        (["--model", "gpt-5", "config", "check"], True),
        (["--oneshot", "config", "check"], False),
        (["mcp", "add", "demo", "--args", "config", "check"], False),
        (["config", "get", "check"], False),
    ],
)
def test_early_config_check_detection_ignores_values_and_passthrough(argv, expected):
    from hermes_cli.main import _is_config_check_invocation

    assert _is_config_check_invocation(argv) is expected


def test_cli_process_guard_releases_after_dispatch(tmp_path):
    source_home = tmp_path / "profiles" / "source"
    target_home = tmp_path / "profiles" / "target"
    source_home.mkdir(parents=True)
    target_home.mkdir(parents=True)
    _write_explicit_gate_config(source_home, write_approval=True)
    target_config = _write_explicit_gate_config(target_home)
    script = f"""
import sys
sys.argv = ["hermes", "--profile", {target_home.name!r}, "config", "check"]
from hermes_cli.main import main
main()
from hermes_cli.config import set_config_value
set_config_value("memory.write_approval", "true")
"""

    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPO_ROOT,
        env=_profile_cli_env(source_home),
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    persisted = yaml.safe_load(target_config.read_text(encoding="utf-8"))
    assert persisted["memory"]["write_approval"] is True
