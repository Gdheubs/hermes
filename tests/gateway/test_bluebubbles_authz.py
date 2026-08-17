"""BLUEBUBBLES_ALLOWED_USERS must match phone-number handles across formats.

BlueBubbles (iMessage) handles are phone numbers or Apple ID email addresses.
Operators write phone numbers in human formats (``+1 (555) 123-0001``) while
the wire form differs only in punctuation (``+15551230001``), and
``_is_user_authorized`` compared them with raw string equality — so the
allowlist silently never matched and every allowlisted sender was denied.
That pushes operators toward ``BLUEBUBBLES_ALLOW_ALL_USERS``, the opposite of
SECURITY.md §2.6 ("An allowlist is required for every enabled network-exposed
adapter").

The fix normalizes BlueBubbles handles on both sides of the comparison:
emails lowercased as-is, phone numbers reduced to digits only. Original
(un-normalized) forms keep matching too, and no other platform's comparison
changes.
"""

from gateway.config import Platform
from gateway.session import SessionSource

FORMATTED = "+1 (555) 123-0001"
WIRE = "+15551230001"
DIGITS = "15551230001"
OTHER_WIRE = "+15559870002"
EMAIL = "contact@example.com"
EMAIL_MIXED_CASE = "Contact@Example.COM"

AUTH_ENV_VARS = (
    "BLUEBUBBLES_ALLOWED_USERS",
    "BLUEBUBBLES_ALLOW_ALL_USERS",
    "TELEGRAM_ALLOWED_USERS",
    "TELEGRAM_ALLOW_ALL_USERS",
    "GATEWAY_ALLOWED_USERS",
    "GATEWAY_ALLOW_ALL_USERS",
)


def _clear_auth_env(monkeypatch) -> None:
    for key in AUTH_ENV_VARS:
        monkeypatch.delenv(key, raising=False)


def _make_runner():
    """Bare GatewayRunner: no adapters, no pairing store — env allowlists only."""
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.adapters = {}
    runner.pairing_store = None
    return runner


def _source(user_id: str, platform: Platform = Platform.BLUEBUBBLES) -> SessionSource:
    return SessionSource(
        platform=platform,
        chat_id=f"iMessage;-;{user_id}" if platform == Platform.BLUEBUBBLES else "42",
        chat_type="dm",
        user_id=user_id,
    )


# ------------------------------------------------------------- phone matching


def test_formatted_env_entry_matches_wire_handle(monkeypatch):
    """Human-formatted allowlist entry matches the punctuation-free wire form."""
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("BLUEBUBBLES_ALLOWED_USERS", FORMATTED)

    assert _make_runner()._is_user_authorized(_source(WIRE)) is True


def test_wire_env_entry_matches_formatted_handle(monkeypatch):
    """Symmetric case: wire-form allowlist entry matches a formatted handle."""
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("BLUEBUBBLES_ALLOWED_USERS", WIRE)

    assert _make_runner()._is_user_authorized(_source(FORMATTED)) is True


def test_digits_only_env_entry_matches_plus_prefixed_wire_handle(monkeypatch):
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("BLUEBUBBLES_ALLOWED_USERS", DIGITS)

    assert _make_runner()._is_user_authorized(_source(WIRE)) is True


def test_exact_raw_match_still_works(monkeypatch):
    """Regression guard: an already-exact entry keeps matching."""
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("BLUEBUBBLES_ALLOWED_USERS", WIRE)

    assert _make_runner()._is_user_authorized(_source(WIRE)) is True


def test_global_allowlist_entry_is_normalized_for_bluebubbles(monkeypatch):
    """GATEWAY_ALLOWED_USERS entries get the same treatment for BlueBubbles."""
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("GATEWAY_ALLOWED_USERS", FORMATTED)

    assert _make_runner()._is_user_authorized(_source(WIRE)) is True


# ------------------------------------------------------------- email matching


def test_email_matches_case_insensitively(monkeypatch):
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("BLUEBUBBLES_ALLOWED_USERS", EMAIL_MIXED_CASE)

    assert _make_runner()._is_user_authorized(_source(EMAIL)) is True


def test_email_matches_case_insensitively_reversed(monkeypatch):
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("BLUEBUBBLES_ALLOWED_USERS", EMAIL)

    assert _make_runner()._is_user_authorized(_source(EMAIL_MIXED_CASE)) is True


# ---------------------------------------------------------------- still denies


def test_unlisted_sender_still_denied(monkeypatch):
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("BLUEBUBBLES_ALLOWED_USERS", FORMATTED)

    assert _make_runner()._is_user_authorized(_source(OTHER_WIRE)) is False


def test_unlisted_email_still_denied(monkeypatch):
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("BLUEBUBBLES_ALLOWED_USERS", EMAIL)

    assert _make_runner()._is_user_authorized(_source("other@example.com")) is False


def test_no_allowlist_still_default_denies(monkeypatch):
    """SECURITY.md §2.6: no allowlist configured means deny, not allow."""
    _clear_auth_env(monkeypatch)

    assert _make_runner()._is_user_authorized(_source(WIRE)) is False


def test_empty_normalized_forms_do_not_cross_match(monkeypatch):
    """A digit-free entry must not normalize to '' and match a digit-free sender."""
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("BLUEBUBBLES_ALLOWED_USERS", "---")

    assert _make_runner()._is_user_authorized(_source("()")) is False


# ------------------------------------------------------------ allow-all paths


def test_wildcard_entry_still_allows_everyone(monkeypatch):
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("BLUEBUBBLES_ALLOWED_USERS", "*")

    assert _make_runner()._is_user_authorized(_source(OTHER_WIRE)) is True


def test_allow_all_flag_still_works(monkeypatch):
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("BLUEBUBBLES_ALLOW_ALL_USERS", "true")

    assert _make_runner()._is_user_authorized(_source(OTHER_WIRE)) is True


# ----------------------------------------------------- other platforms intact


def test_other_platforms_are_not_phone_normalized(monkeypatch):
    """A formatted Telegram entry must NOT digit-match a bare numeric user id."""
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", FORMATTED)

    runner = _make_runner()
    assert runner._is_user_authorized(_source(DIGITS, platform=Platform.TELEGRAM)) is False
    assert runner._is_user_authorized(_source(WIRE, platform=Platform.TELEGRAM)) is False


def test_telegram_exact_numeric_id_still_matches(monkeypatch):
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "111222333")

    runner = _make_runner()
    assert runner._is_user_authorized(_source("111222333", platform=Platform.TELEGRAM)) is True
    assert runner._is_user_authorized(_source("999888777", platform=Platform.TELEGRAM)) is False
