"""MCP servers read the Desktop connection mode from per-call ``_meta`` (#82140).

An MCP server can't read the gateway's contextvars, and its stdio env is fixed
at spawn time while the mode is per-session — one gateway can serve a local
Desktop client and a remote one at once. Per-call ``_meta`` is the only vehicle
that is both live and session-correct.
"""

import pytest

import gateway.session_context as sc
from gateway.session_context import set_desktop_connection_mode
from tools.mcp_tool import (
    MCP_DESKTOP_CONNECTION_MODE_META_KEY as META_KEY,
    _call_tool_meta,
    _call_tool_supports_meta,
)


@pytest.fixture(autouse=True)
def _reset_mode():
    saved = sc._DESKTOP_CONNECTION_MODE.get()
    sc._DESKTOP_CONNECTION_MODE.set(sc._UNSET)
    try:
        yield
    finally:
        sc._DESKTOP_CONNECTION_MODE.set(saved)


@pytest.mark.parametrize("mode", ["local", "remote"])
def test_bound_mode_becomes_call_meta(mode):
    set_desktop_connection_mode(mode)
    assert _call_tool_meta() == {META_KEY: mode}


def test_remote_like_saved_mode_is_normalized_before_it_leaves():
    set_desktop_connection_mode("cloud")
    assert _call_tool_meta() == {META_KEY: "remote"}


def test_non_desktop_session_sends_no_meta():
    """CLI/TUI/messaging requests keep exactly today's shape."""
    assert _call_tool_meta() is None


def test_meta_carries_the_mode_and_nothing_else():
    """No base URL, host, token, SSH key, or auth mode may ride along."""
    set_desktop_connection_mode("remote")
    meta = _call_tool_meta()
    assert list(meta) == [META_KEY]
    assert meta[META_KEY] in {"local", "remote"}


def test_meta_key_does_not_squat_the_reserved_spec_prefix():
    """MCP reserves `modelcontextprotocol.io/` for the spec itself."""
    assert not META_KEY.startswith("modelcontextprotocol.io/")
    assert "/" in META_KEY


class TestSdkCapabilityProbe:
    def test_probe_is_boolean_and_never_raises_without_the_sdk(self):
        _call_tool_supports_meta.cache_clear()
        try:
            assert isinstance(_call_tool_supports_meta(), bool)
        finally:
            _call_tool_supports_meta.cache_clear()

    def test_probe_reports_false_when_the_sdk_lacks_meta(self, monkeypatch):
        """An older SDK degrades to today's request shape instead of raising."""
        import sys
        import types

        class _Session:
            async def call_tool(self, name, arguments=None):  # no `meta` param
                ...

        module = types.ModuleType("mcp")
        module.ClientSession = _Session
        monkeypatch.setitem(sys.modules, "mcp", module)
        _call_tool_supports_meta.cache_clear()
        try:
            assert _call_tool_supports_meta() is False
        finally:
            _call_tool_supports_meta.cache_clear()

    def test_probe_reports_true_when_the_sdk_accepts_meta(self, monkeypatch):
        import sys
        import types

        class _Session:
            async def call_tool(self, name, arguments=None, meta=None):
                ...

        module = types.ModuleType("mcp")
        module.ClientSession = _Session
        monkeypatch.setitem(sys.modules, "mcp", module)
        _call_tool_supports_meta.cache_clear()
        try:
            assert _call_tool_supports_meta() is True
        finally:
            _call_tool_supports_meta.cache_clear()


class TestDocsExample:
    """The published FastMCP example must be executable against the pinned SDK.

    The original example showed ``@server.call_tool()`` (not a decorator in
    this tree's ``mcp`` SDK) and ``context.meta`` (absent on ``Context``);
    the supported shape is a ``@server.tool()`` handler reading
    ``ctx.request_context.meta`` (#82187 follow-up review, item 6). This pins
    the docs snippet to the real SDK so a future SDK bump or docs edit that
    breaks the pairing fails here instead of on a reader's machine.
    """

    def _docs_fastmcp_snippet(self) -> str:
        import pathlib
        import re

        doc = (
            pathlib.Path(__file__).resolve().parents[2]
            / "website"
            / "docs"
            / "developer-guide"
            / "desktop-connection-mode.md"
        )
        blocks = re.findall(r"```python\n(.*?)```", doc.read_text(encoding="utf-8"), re.DOTALL)
        sdk_blocks = [block for block in blocks if "FastMCP" in block]
        assert len(sdk_blocks) == 1, "expected exactly one FastMCP example in the docs page"
        return sdk_blocks[0]

    def test_example_executes_against_the_pinned_sdk(self):
        pytest.importorskip("mcp.server.fastmcp")
        snippet = self._docs_fastmcp_snippet()
        namespace: dict = {}
        # Executing (not just compiling) registers the tool: FastMCP inspects
        # the handler signature at decoration time, so an unsupported decorator
        # or Context parameter shape fails right here.
        exec(compile(snippet, "desktop-connection-mode.md", "exec"), namespace)
        assert namespace["MODE_KEY"] == META_KEY

    def test_documented_meta_access_reads_the_namespaced_key(self):
        mcp_types = pytest.importorskip("mcp.types")
        meta = mcp_types.RequestParams.Meta(**{META_KEY: "remote"})
        # The exact expression the docs show, on the real metadata model.
        assert (meta.model_extra or {}).get(META_KEY) == "remote"

    def test_pinned_sdk_still_lacks_the_shapes_the_old_example_used(self):
        """If the SDK grows Context.meta or a call_tool decorator, revisit the
        docs example rather than silently drifting."""
        fastmcp = pytest.importorskip("mcp.server.fastmcp")
        import inspect

        assert "meta" not in dir(fastmcp.Context)
        assert "request_context" in dir(fastmcp.Context)
        # call_tool is the dispatch method (self, name, arguments), not a
        # decorator factory like tool().
        params = list(inspect.signature(fastmcp.FastMCP.call_tool).parameters)
        assert params[:3] == ["self", "name", "arguments"]
