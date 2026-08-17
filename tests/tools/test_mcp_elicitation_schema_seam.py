"""Seam tests for the R2-1 extraction: tools/mcp_tool.py -> tools/mcp_elicitation_schema.py.

`_format_elicitation_schema_summary` moved byte-verbatim into
tools/mcp_elicitation_schema.py; tools/mcp_tool.py re-exports the name so the
original namespace (direct imports, in-file call sites, monkeypatch authority)
is unchanged.

Runtime-only assertions (no source reading): object identity both directions,
canonical ownership, old-owner monkeypatch authority, and behavioral
equivalence through both access surfaces.

SDK-independent: importing tools.mcp_tool is a no-op module when the optional
`mcp` package is absent, and this function is pure.
"""

import pytest

import tools.mcp_elicitation_schema as mcp_elicitation_schema
import tools.mcp_tool as mcp_tool

MOVED_NAME = "_format_elicitation_schema_summary"


def _old():
    return getattr(mcp_tool, MOVED_NAME)


def _new():
    return getattr(mcp_elicitation_schema, MOVED_NAME)


# --- identity ---------------------------------------------------------------


def test_reexport_identity_forward():
    # getattr(mcp_tool, name) is getattr(mcp_elicitation_schema, name)
    assert _old() is _new()


def test_reexport_identity_backward():
    assert _new() is _old()


def test_canonical_owner_module():
    assert _new().__module__ == "tools.mcp_elicitation_schema"


def test_old_owner_is_not_a_stale_duplicate():
    # mcp_tool must NOT carry its own definition anymore — the name must
    # resolve to the canonical object in the new module.
    assert _old() is _new()
    assert mcp_elicitation_schema.__dict__[MOVED_NAME] is _old()


# --- monkeypatch authority --------------------------------------------------


def test_old_owner_patch_shadows_reexport(monkeypatch):
    # Patching the name on the ORIGINAL module must shadow the re-export for
    # old-namespace consumers (in-file call sites resolve the module global),
    # while the canonical new module stays untouched.
    sentinel = object()
    monkeypatch.setattr(mcp_tool, MOVED_NAME, sentinel)
    assert _old() is sentinel
    assert _new() is not sentinel
    assert _new() is mcp_elicitation_schema.__dict__[MOVED_NAME]


def test_new_owner_patch_does_not_leak_into_old_owner(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(mcp_elicitation_schema, MOVED_NAME, sentinel)
    assert _new() is sentinel
    assert _old() is not sentinel


# --- behavioral equivalence (both access surfaces) ---------------------------


@pytest.mark.parametrize(
    "schema,server_name",
    [
        ({}, "pay"),
        ({"properties": {}}, "pay"),
        ({"type": "object", "properties": {}}, "pay"),
        (None, "pay"),
        ("not-a-dict", "pay"),
        (42, "pay"),
        (
            {"type": "object",
             "properties": {
                 "amount": {"type": "string", "description": "USD amount"},
                 "recipient": {"type": "string"},
             }},
            "pay",
        ),
        (
            {"properties": {"a": {"type": "number", "description": "desc a"},
                            "b": {"type": "boolean"}}},
            "srv-1",
        ),
        # non-dict field specs
        ({"properties": {"a": "string", "b": None, "c": 3}}, "x"),
        # empty-string type/description normalization
        ({"properties": {"a": {"type": "", "description": ""}}}, "x"),
        # unicode content
        ({"properties": {"é": {"type": "string", "description": "café"}}}, "täst"),
    ],
)
def test_behavioral_equivalence_both_surfaces(schema, server_name):
    old_out = _old()(schema, server_name)
    new_out = _new()(schema, server_name)
    assert old_out == new_out
    assert isinstance(old_out, str)
