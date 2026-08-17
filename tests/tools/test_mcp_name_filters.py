"""Seam and behavior tests for extracted MCP name filters."""

import logging

import tools.mcp_name_filters as mcp_name_filters
import tools.mcp_tool as mcp_tool


def test_mcp_tool_reexports_name_filters_by_identity():
    assert getattr(mcp_tool, "_normalize_name_filter") is getattr(
        mcp_name_filters, "_normalize_name_filter"
    )
    assert getattr(mcp_tool, "matches_name_filter") is getattr(
        mcp_name_filters, "matches_name_filter"
    )


def test_normalize_name_filter_accepts_supported_shapes_and_warns_on_invalid(caplog):
    assert mcp_name_filters._normalize_name_filter(None, "include") == set()
    assert mcp_name_filters._normalize_name_filter("server_tool", "include") == {"server_tool"}
    assert mcp_name_filters._normalize_name_filter(
        ["one", 2, "one"], "include"
    ) == {"one", "2"}
    assert mcp_name_filters._normalize_name_filter(("tuple",), "include") == {"tuple"}
    assert mcp_name_filters._normalize_name_filter({"set"}, "include") == {"set"}

    with caplog.at_level(logging.WARNING, logger="tools.mcp_name_filters"):
        assert mcp_name_filters._normalize_name_filter(42, "exclude") == set()

    assert "MCP config exclude must be a string or list of strings" in caplog.text


def test_matches_name_filter_covers_exact_glob_and_case_sensitive_paths():
    assert mcp_name_filters.matches_name_filter("literal", set()) is False
    assert mcp_name_filters.matches_name_filter("literal", {"literal"}) is True
    assert mcp_name_filters.matches_name_filter("get_zones_alpha", {"get_zones_*"}) is True
    assert mcp_name_filters.matches_name_filter("radar_7", {"*_radar_*", "radar_?"}) is True
    assert mcp_name_filters.matches_name_filter("radar_77", {"radar_?"}) is False
    assert mcp_name_filters.matches_name_filter("ab", {"[ab]?"}) is True
    assert mcp_name_filters.matches_name_filter("Get_Zones_alpha", {"get_zones_*"}) is False
    assert mcp_name_filters.matches_name_filter("other", {"literal", "get_*"}) is False
