"""Tests for the cronjob tool schema shape.

Guards the description text that flags ``schedule`` as always REQUIRED for
``action=create`` and states when a prompt may be replaced by skills or a
script. This is load-bearing for description-driven models (e.g. Grok) that
omit schedule when the schema only lists ``action`` in ``required[]``. See
issue #32427 / PR #32448.
"""

from __future__ import annotations


def test_cronjob_schema_action_description_flags_create_requirements():
    """`action` must keep schedule mandatory and document prompt alternatives."""
    from tools.cronjob_tools import CRONJOB_SCHEMA

    action_desc = CRONJOB_SCHEMA["parameters"]["properties"]["action"]["description"]
    assert "action=create" in action_desc
    assert "schedule is REQUIRED" in action_desc
    assert "prompt is REQUIRED unless skills define the task" in action_desc
    assert "no_agent=True requires script and ignores prompt" in action_desc


def test_cronjob_schema_requires_fresh_session_contract_and_readback():
    """The model-facing contract must define safe creation and proof of save."""
    from tools.cronjob_tools import CRONJOB_SCHEMA

    description = CRONJOB_SCHEMA["description"].lower()
    for required_phrase in (
        "fresh-session contract",
        "exact input",
        "expected output",
        "tool boundary",
        "working directory",
        "delivery",
        "model",
        "skills",
        "next_run_at",
        "read back",
    ):
        assert required_phrase in description
    assert "stop rather than schedule a guess" in description
    assert "tasks that consume files or data" in description
    assert "prompt, skills, or a no_agent script" in description
    assert "for no_agent=true" in description
    assert "script path and stdout" in description
    assert "do not require or invent a prompt" in description


