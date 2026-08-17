"""Focused adversarial coverage for the Discord standalone error sanitizer."""

import pytest

from plugins.platforms.discord.standalone_send import _standalone_sanitize_error


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "request failed: Authorization: Bot abc.def.ghi, retrying",
            "request failed: Authorization: Bot ***, retrying",
        ),
        (
            "HTTP 401 {Authorization: Bot token-with-punctuation}"
            " response",
            "HTTP 401 {Authorization: Bot ***} response",
        ),
        (
            "AUTHORIZATION:   Bot secret-token\nnext line",
            "AUTHORIZATION:   Bot ***\nnext line",
        ),
        (
            "prefix Authorization: Bot opaque-secret, suffix",
            "prefix Authorization: Bot ***, suffix",
        ),
    ],
)
def test_sanitizer_masks_bot_token_and_preserves_delimiters(text, expected):
    assert _standalone_sanitize_error(text) == expected


def test_sanitizer_does_not_consume_following_error_metadata():
    text = "Authorization: Bot top-secret, status=401, body={\"ok\":false}"

    sanitized = _standalone_sanitize_error(text)

    assert sanitized == "Authorization: Bot ***, status=401, body={\"ok\":false}"
    assert "top-secret" not in sanitized
    assert "status=401" in sanitized


def test_sanitizer_is_idempotent_after_redaction():
    redacted = "Authorization: Bot ***, status=401"

    assert _standalone_sanitize_error(_standalone_sanitize_error(redacted)) == redacted
