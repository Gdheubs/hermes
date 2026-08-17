"""Behavior contracts for ``atomic_write_text`` newline handling."""

import os

from utils import atomic_write_text


def test_explicit_lf_disables_platform_newline_translation(tmp_path):
    target = tmp_path / "canonical.txt"

    atomic_write_text(target, "first\nsecond", newline="\n")

    assert target.read_bytes() == b"first\nsecond"


def test_default_preserves_platform_native_newline_behavior(tmp_path):
    target = tmp_path / "native.txt"

    atomic_write_text(target, "first\nsecond")

    assert target.read_bytes() == f"first{os.linesep}second".encode()
