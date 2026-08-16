"""Tests for ``utils.parse_integer_list``.

``parse_integer_list`` parses a comma-separated string of integers, rejecting
empty or non-integer fields with clear position-bearing errors, and returns a
de-duplicated list that preserves first-occurrence order.
"""

from __future__ import annotations

import pytest

from utils import parse_integer_list


def test_parse_integer_list_splits_and_strips_fields():
    assert parse_integer_list("1, 2, 3") == [1, 2, 3]


def test_parse_integer_list_deduplicates_preserving_order():
    assert parse_integer_list("3,1,3,2,1") == [3, 1, 2]


def test_parse_integer_list_single_token():
    assert parse_integer_list("42") == [42]


def test_parse_integer_list_allows_negative_integers():
    assert parse_integer_list("1,-2,3") == [1, -2, 3]


def test_parse_integer_list_rejects_empty_string():
    with pytest.raises(ValueError, match="empty"):
        parse_integer_list("")


def test_parse_integer_list_rejects_whitespace_only_string():
    with pytest.raises(ValueError, match="empty"):
        parse_integer_list("   ")


def test_parse_integer_list_rejects_leading_empty_field():
    with pytest.raises(ValueError, match="position 1"):
        parse_integer_list(",1,2")


def test_parse_integer_list_rejects_trailing_empty_field():
    with pytest.raises(ValueError, match="position 3"):
        parse_integer_list("1,2,")


def test_parse_integer_list_rejects_interior_empty_field():
    with pytest.raises(ValueError, match="position 2"):
        parse_integer_list("1,,3")


def test_parse_integer_list_rejects_whitespace_only_field():
    with pytest.raises(ValueError, match="position 2"):
        parse_integer_list("1,   ,3")


def test_parse_integer_list_rejects_non_integer():
    with pytest.raises(ValueError, match="position 2.*abc"):
        parse_integer_list("1,abc,3")


def test_parse_integer_list_rejects_float_token():
    with pytest.raises(ValueError, match="position 1.*2.5"):
        parse_integer_list("2.5,3")


def test_parse_integer_list_rejects_non_string_input():
    with pytest.raises(TypeError):
        parse_integer_list(None)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        parse_integer_list(["1", "2"])  # type: ignore[arg-type]
