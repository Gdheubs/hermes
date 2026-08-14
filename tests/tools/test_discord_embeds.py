"""Tests for the typed Discord embed builder (tools/discord_api/embeds.py).

Feature M4 of the Discord Omniscience campaign (EPIC #79564).
"""

import pytest

from tools.discord_api.embeds import (
    EMBED_LIMITS,
    Embed,
    EmbedAuthor,
    EmbedField,
    EmbedFooter,
    EmbedValidationError,
    contains_mention,
    embed_to_plain_text,
)


def test_minimal_embed_payload():
    e = Embed(title="Hi", description="body")
    assert e.to_payload() == {"title": "Hi", "description": "body"}


def test_full_embed_payload_roundtrip():
    e = Embed(
        title="T",
        description="D",
        url="https://example.com",
        color=0xFF0000,
        timestamp="2026-08-14T10:00:00Z",
        author=EmbedAuthor("A", url="https://example.com/a", icon_url="https://x/i.png"),
        footer=EmbedFooter("F", icon_url="https://x/f.png"),
        fields=[EmbedField("N", "V", inline=True)],
        image_url="https://x/img.png",
        thumbnail_url="https://x/th.png",
    )
    payload = e.to_payload()
    assert payload["title"] == "T"
    assert payload["color"] == 0xFF0000
    assert payload["author"] == {
        "name": "A", "url": "https://example.com/a", "icon_url": "https://x/i.png"
    }
    assert payload["footer"] == {"text": "F", "icon_url": "https://x/f.png"}
    assert payload["fields"] == [{"name": "N", "value": "V", "inline": True}]
    assert payload["image"] == {"url": "https://x/img.png"}
    assert payload["thumbnail"] == {"url": "https://x/th.png"}


# ── Limit enforcement ────────────────────────────────────────────────────────
def test_title_too_long_rejected():
    with pytest.raises(EmbedValidationError):
        Embed(title="x" * (EMBED_LIMITS["title"] + 1))


def test_description_too_long_rejected():
    with pytest.raises(EmbedValidationError):
        Embed(description="x" * (EMBED_LIMITS["description"] + 1))


def test_field_limits_enforced():
    with pytest.raises(EmbedValidationError):
        EmbedField("x" * (EMBED_LIMITS["field_name"] + 1), "v")
    with pytest.raises(EmbedValidationError):
        EmbedField("n", "x" * (EMBED_LIMITS["field_value"] + 1))


def test_field_count_capped_at_25():
    fields = [EmbedField(f"n{i}", "v") for i in range(EMBED_LIMITS["fields"] + 1)]
    with pytest.raises(EmbedValidationError):
        Embed(fields=fields)


def test_total_character_budget_enforced():
    # Title + description + a field that together exceed the 6000 total.
    title = "x" * 3000
    desc = "y" * 3000
    field = EmbedField("z" * 100, "w" * 100)
    with pytest.raises(EmbedValidationError):
        Embed(title=title, description=desc, fields=[field])


def test_budget_ok_under_limit():
    # title 200 (<256) + description 3000 (<4096) = 3200 total (<6000).
    Embed(title="x" * 200, description="y" * 3000)


# ── URL validation ───────────────────────────────────────────────────────────
def test_url_must_be_http():
    with pytest.raises(EmbedValidationError):
        Embed(url="ftp://example.com")
    with pytest.raises(EmbedValidationError):
        Embed(title="t", image_url="javascript:alert(1)")
    with pytest.raises(EmbedValidationError):
        EmbedAuthor("a", icon_url="not-a-url")


def test_url_http_ok():
    Embed(url="https://example.com", image_url="http://example.com/i.png")


# ── color / timestamp validation ─────────────────────────────────────────────
def test_color_must_be_24bit_int():
    with pytest.raises(EmbedValidationError):
        Embed(color=0x1000000)
    with pytest.raises(EmbedValidationError):
        Embed(color=-1)
    with pytest.raises(EmbedValidationError):
        Embed(color="red")
    Embed(color=0xFFFFFF)


def test_timestamp_must_be_iso8601():
    with pytest.raises(EmbedValidationError):
        Embed(timestamp="yesterday")
    Embed(timestamp="2026-08-14T10:00:00Z")
    Embed(timestamp="2026-08-14T10:00:00.123+00:00")


# ── mention policy ───────────────────────────────────────────────────────────
def test_mention_detection():
    assert contains_mention("ping @everyone")
    assert contains_mention("hi @here")
    assert contains_mention("<@123456>")
    assert contains_mention("<@!987654>")
    assert not contains_mention("just plain text")
    assert not contains_mention("email@example.com")


# ── plain-text fallback ──────────────────────────────────────────────────────
def test_plain_text_fallback_preserves_payload():
    e = Embed(
        author=EmbedAuthor("Author"),
        title="Title",
        description="Body line",
        fields=[EmbedField("Field", "Value")],
        footer=EmbedFooter("Foot"),
    )
    text = embed_to_plain_text(e)
    assert "**Author**" in text
    assert "# Title" in text
    assert "Body line" in text
    assert "**Field:** Value" in text
    assert "_Foot_" in text


def test_plain_text_empty_embed():
    assert embed_to_plain_text(Embed()) == ""
