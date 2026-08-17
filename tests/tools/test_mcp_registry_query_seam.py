"""Seam tests for the mcp_tool R5 registry-query extraction.

``has_registered_mcp_tools`` and ``get_registered_mcp_server_names`` were
moved byte-verbatim from ``tools/mcp_tool.py`` into
``tools/mcp_registry_query.py``; ``tools.mcp_tool`` lazily re-exports the
same function objects through a PEP 562 module ``__getattr__``. These
tests pin the seam contract:

* object identity: the names exposed by ``tools.mcp_tool`` ARE the
  objects defined in ``tools.mcp_registry_query`` (a re-export, not
  wrappers), so existing callers in ``agent/turn_context.py`` /
  ``gateway/session.py`` keep resolving the same callables;
* shared state: the moved functions read the SAME ``_lock`` /
  ``_mcp_tool_server_names`` objects owned by ``tools.mcp_tool`` — the
  module-scope import in ``tools.mcp_registry_query`` binds the original
  module's objects (identity, not copies), so writes made through the
  original module are visible through the new module and vice versa;
* behavior: empty-registry and populated-registry results match the
  pre-extraction semantics.

PEP 562 note: a module ``__getattr__`` does NOT make the resolved names
``dir()``-visible. No consumer enumerates ``tools.mcp_tool`` via ``dir()``
or ``__all__`` (the module defines neither for these names); resolution is
always via attribute access / ``getattr`` / ``hasattr`` / from-import,
all of which route through the hook.
"""

import threading

import pytest

SEAM_NAMES = ("has_registered_mcp_tools", "get_registered_mcp_server_names")


@pytest.fixture()
def mcp_state():
    """Snapshot and restore the shared registry map around each test."""
    import tools.mcp_tool as mcp_tool

    with mcp_tool._lock:
        saved_names = dict(mcp_tool._mcp_tool_server_names)
        mcp_tool._mcp_tool_server_names.clear()
    try:
        yield mcp_tool
    finally:
        with mcp_tool._lock:
            mcp_tool._mcp_tool_server_names.clear()
            mcp_tool._mcp_tool_server_names.update(saved_names)


def _populate(mcp_tool, mapping):
    with mcp_tool._lock:
        mcp_tool._mcp_tool_server_names.update(mapping)


def _clear(mcp_tool):
    with mcp_tool._lock:
        mcp_tool._mcp_tool_server_names.clear()


class TestReexportIdentity:
    def test_reexported_names_are_identical_objects(self, mcp_state):
        import tools.mcp_tool as mcp_tool
        import tools.mcp_registry_query as mcp_registry_query

        for name in SEAM_NAMES:
            assert getattr(mcp_tool, name) is getattr(mcp_registry_query, name)
            # Canonical implementation lives in the new module.
            assert getattr(mcp_registry_query, name).__module__ == (
                "tools.mcp_registry_query"
            )

    def test_mcp_tool_has_no_duplicate_definition(self, mcp_state):
        """mcp_tool.py must not define the moved names itself anymore."""
        import tools.mcp_tool as mcp_tool
        import tools.mcp_registry_query as mcp_registry_query

        for name in SEAM_NAMES:
            mcp_impl = getattr(mcp_tool, name)
            query_impl = getattr(mcp_registry_query, name)
            assert mcp_impl is query_impl
            assert mcp_impl.__code__.co_filename.endswith("mcp_registry_query.py")

    def test_module_scope_seam_binds_original_state_objects(self, mcp_state, monkeypatch):
        """The aggressive identity proof for the module-scope seam: the
        moved functions' module globals ARE the owner module's state
        objects (bound at import time by the module-scope ``from
        tools.mcp_tool import ...``). A stale import-time copy would fail
        the identity assertions below, and the sentinel swap proves the
        byte-identical bodies read through those module globals."""
        import tools.mcp_tool as mcp_tool
        import tools.mcp_registry_query as mcp_registry_query

        # Module-scope import binds the owner module's exact objects.
        assert mcp_registry_query._lock is mcp_tool._lock
        assert mcp_registry_query._mcp_tool_server_names is mcp_tool._mcp_tool_server_names

        sentinel_lock = threading.Lock()
        sentinel_map = {"mcp__sentinel__x": "sentinel"}
        monkeypatch.setattr(mcp_registry_query, "_lock", sentinel_lock)
        monkeypatch.setattr(mcp_registry_query, "_mcp_tool_server_names", sentinel_map)

        assert mcp_registry_query.has_registered_mcp_tools() is True
        assert mcp_registry_query.get_registered_mcp_server_names() == {"sentinel"}


class TestBehavioral:
    def test_empty_registry(self, mcp_state):
        import tools.mcp_tool as mcp_tool
        import tools.mcp_registry_query as mcp_registry_query

        assert mcp_registry_query.has_registered_mcp_tools() is False
        assert mcp_registry_query.get_registered_mcp_server_names() == set()
        assert mcp_tool.has_registered_mcp_tools() is False
        assert mcp_tool.get_registered_mcp_server_names() == set()

    def test_writes_through_mcp_tool_visible_via_new_module(self, mcp_state):
        import tools.mcp_tool as mcp_tool
        import tools.mcp_registry_query as mcp_registry_query

        _populate(
            mcp_tool,
            {
                "mcp__filesystem__read": "filesystem",
                "mcp__github__list_issues": "github",
            },
        )
        try:
            assert mcp_registry_query.has_registered_mcp_tools() is True
            assert mcp_registry_query.get_registered_mcp_server_names() == {
                "filesystem",
                "github",
            }
        finally:
            _clear(mcp_tool)

    def test_shared_map_readable_in_both_directions(self, mcp_state):
        """The moved functions read the SAME map object owned by tools.mcp_tool:
        writes to the shared registry are visible through both modules."""
        import tools.mcp_tool as mcp_tool
        import tools.mcp_registry_query as mcp_registry_query

        with mcp_tool._lock:
            mcp_tool._mcp_tool_server_names["mcp__slack__post"] = "slack"
        try:
            # Reads through the new module reflect the write above...
            assert mcp_registry_query.get_registered_mcp_server_names() == {"slack"}
            # ...and the shared map is the one owned by tools.mcp_tool.
            with mcp_tool._lock:
                assert mcp_tool._mcp_tool_server_names["mcp__slack__post"] == "slack"
        finally:
            _clear(mcp_tool)

    def test_both_modules_report_identical_results(self, mcp_state):
        import tools.mcp_tool as mcp_tool
        import tools.mcp_registry_query as mcp_registry_query

        _populate(
            mcp_tool,
            {
                "mcp__a__x": "alpha",
                "mcp__b__y": "beta",
                "mcp__b__z": "beta",
            },
        )
        try:
            assert (
                mcp_tool.has_registered_mcp_tools()
                == mcp_registry_query.has_registered_mcp_tools()
                is True
            )
            assert (
                mcp_tool.get_registered_mcp_server_names()
                == mcp_registry_query.get_registered_mcp_server_names()
                == {"alpha", "beta"}
            )
        finally:
            _clear(mcp_tool)

    def test_returned_set_is_a_fresh_copy(self, mcp_state):
        """Mutation of the returned set must not leak into the registry."""
        import tools.mcp_tool as mcp_tool
        import tools.mcp_registry_query as mcp_registry_query

        _populate(mcp_tool, {"mcp__a__x": "alpha"})
        try:
            names = mcp_registry_query.get_registered_mcp_server_names()
            names.add("beta")
            assert mcp_registry_query.get_registered_mcp_server_names() == {"alpha"}
            with mcp_tool._lock:
                assert "beta" not in mcp_tool._mcp_tool_server_names.values()
        finally:
            _clear(mcp_tool)

    def test_registry_truthiness_tracks_nonempty_map(self, mcp_state):
        import tools.mcp_tool as mcp_tool
        import tools.mcp_registry_query as mcp_registry_query

        # Re-additions after an empty state flip the cheap check back on.
        _populate(mcp_tool, {"mcp__a__x": "alpha"})
        try:
            assert mcp_registry_query.has_registered_mcp_tools() is True
            _clear(mcp_tool)
            assert mcp_registry_query.has_registered_mcp_tools() is False
            _populate(mcp_tool, {"mcp__c__q": "charlie"})
            assert mcp_registry_query.has_registered_mcp_tools() is True
        finally:
            _clear(mcp_tool)

    def test_patch_on_mcp_tool_still_intercepts_callers(self, mcp_state):
        """Existing callers patch ``tools.mcp_tool.has_registered_mcp_tools``;
        the re-export must keep that patch surface working."""
        from unittest.mock import patch

        import tools.mcp_tool as mcp_tool
        import tools.mcp_registry_query as mcp_registry_query

        with patch("tools.mcp_tool.has_registered_mcp_tools", return_value=True):
            # The patch replaces the mcp_tool attribute; the re-export seam is
            # the attribute itself, so the new module's function object is no
            # longer what mcp_tool exposes while patched.
            assert mcp_tool.has_registered_mcp_tools() is True
            # And the canonical object is untouched by the patch.
            assert mcp_registry_query.has_registered_mcp_tools() is False
