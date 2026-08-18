"""Regression coverage for the Termux cryptography source-build OOM fix.

cryptography (and pydantic-core) ship no Android wheel on PyPI, so both
Termux install paths always source-build them from the sdist. Cargo's default
one-rustc-per-core parallelism exhausts RAM+swap on a phone, and the OOM
killer reaps rustc silently instead of cargo surfacing a build error, so pip
is left hanging on a dead child forever (#87663). The fix serializes the Rust
build via CARGO_BUILD_JOBS=1 instead of downgrading the CVE-fixed
cryptography pin.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_SH = REPO_ROOT / "scripts" / "install.sh"
SETUP_HERMES_SH = REPO_ROOT / "setup-hermes.sh"
PYPROJECT = REPO_ROOT / "pyproject.toml"


def test_install_sh_serializes_rust_builds_on_termux() -> None:
    text = INSTALL_SH.read_text(encoding="utf-8")
    assert 'if [ -z "${CARGO_BUILD_JOBS:-}" ]; then' in text
    assert "export CARGO_BUILD_JOBS=1" in text
    assert "#87663" in text
    # The guard must run before the pip install call it protects.
    guard_pos = text.index("export CARGO_BUILD_JOBS=1")
    pip_install_pos = text.index("pip install -e '.[termux-all]' -c constraints-termux.txt")
    assert guard_pos < pip_install_pos


def test_setup_hermes_sh_serializes_rust_builds_on_termux() -> None:
    text = SETUP_HERMES_SH.read_text(encoding="utf-8")
    assert 'if [ -z "${CARGO_BUILD_JOBS:-}" ]; then' in text
    assert "export CARGO_BUILD_JOBS=1" in text
    guard_pos = text.index("export CARGO_BUILD_JOBS=1")
    pip_install_pos = text.index('pip install -e ".[termux]" -c constraints-termux.txt')
    assert guard_pos < pip_install_pos


def test_cargo_build_jobs_guard_respects_caller_override() -> None:
    """The guard must not clobber a value the caller/environment already set."""
    for path in (INSTALL_SH, SETUP_HERMES_SH):
        lines = path.read_text(encoding="utf-8").splitlines()
        guard_idx = next(
            i for i, line in enumerate(lines) if 'if [ -z "${CARGO_BUILD_JOBS:-}" ]; then' in line
        )
        # Guarded assignment on the very next line, not an unconditional export.
        assert lines[guard_idx + 1].strip() == "export CARGO_BUILD_JOBS=1"


def test_cryptography_pin_not_downgraded_for_termux() -> None:
    """Guard against 'fixing' the Termux OOM by reintroducing the CVEs the
    cryptography==50.0.0 pin exists to close (no Android wheel exists at any
    version, so downgrading would not even avoid the source build)."""
    text = PYPROJECT.read_text(encoding="utf-8")
    assert '"cryptography==50.0.0"' in text
    assert '"cryptography==48.0.1"' not in text
