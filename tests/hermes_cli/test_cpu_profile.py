from __future__ import annotations

import importlib
import os
import pstats
import subprocess
import sys
from pathlib import Path


def test_parser_accepts_cpu_profile_before_chat(tmp_path: Path):
    from hermes_cli._parser import build_top_level_parser

    parser, _subparsers, _chat = build_top_level_parser()
    target = tmp_path / "hermes.prof"
    args = parser.parse_args(["--cpu-profile", str(target), "chat"])

    assert args.cpu_profile == str(target)
    assert args.command == "chat"


def test_enable_cpu_profile_writes_stats_and_text_summary(tmp_path: Path):
    import hermes_cli.cpu_profile as cpu_profile

    cpu_profile = importlib.reload(cpu_profile)
    target = tmp_path / "nested" / "hermes.prof"

    enabled = cpu_profile.enable_cpu_profile(target)
    assert enabled == target.resolve()
    sum(i * i for i in range(1000))
    cpu_profile._write_cpu_profile()

    assert target.exists()
    assert target.with_suffix(target.suffix + ".txt").exists()
    stats = pstats.Stats(str(target))
    assert stats.total_calls > 0


def test_cli_cpu_profile_smoke_writes_profile(tmp_path: Path):
    target = tmp_path / "cli.prof"
    hermes_home = tmp_path / "home"
    hermes_home.mkdir()

    env = os.environ.copy()
    env.update({"HERMES_HOME": str(hermes_home), "PYTHONPATH": str(Path(__file__).resolve().parents[2])})

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "hermes_cli.main",
            "--cpu-profile",
            str(target),
            "completion",
            "bash",
        ],
        cwd=Path(__file__).resolve().parents[2],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, result.stderr[:1000]
    assert "CPU profiling enabled:" in result.stderr
    assert target.exists()
    assert target.with_suffix(target.suffix + ".txt").exists()
    assert pstats.Stats(str(target)).total_calls > 0
