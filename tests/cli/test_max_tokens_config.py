"""Focused tests for CLI output-token cap parsing."""

import pytest


@pytest.mark.parametrize("value", [True, False, 0, -1, 12000.5, "12.5", "-12"])
def test_cli_rejects_invalid_output_token_caps(value):
    from utils import positive_output_token_cap

    assert positive_output_token_cap(value) is None


@pytest.mark.parametrize("value", [9000, "9000", " 9000 "])
def test_cli_accepts_positive_integer_output_token_caps(value):
    from utils import positive_output_token_cap

    assert positive_output_token_cap(value) == 9000


def test_oversized_decimal_output_token_cap_is_rejected():
    from utils import positive_output_token_cap

    assert positive_output_token_cap("9" * 5000) is None
