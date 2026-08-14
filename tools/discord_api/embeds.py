"""Typed Discord embed builder, aligned to the Discord REST v10 API.

Feature M4 of the Discord Omniscience campaign (EPIC #79564): a safe, typed
outbound embed surface so an agent can attach rich embeds to Discord messages
without hand-rolling discord.py objects or trusting untrusted JSON.

Discord embed limits (REST v10, ``resources/message.mdx``) are enforced here so
the REST call can never 400 on an oversized or malformed embed:

    title            256 chars
    description     4096 chars
    field name       256 chars
    field value     1024 chars
    fields            25 max
    footer text     2048 chars
    author name      256 chars
    total content   6000 chars (title + description + field names/values
                    + footer + author)

This module never executes arbitrary marker/JSON payloads; it is a pure typed
builder that validates on construction and exposes a plain-text fallback for
clients where embeds do not render.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional, Sequence, Tuple

__all__ = [
    "EMBED_LIMITS",
    "EmbedField",
    "EmbedAuthor",
    "EmbedFooter",
    "Embed",
    "EmbedValidationError",
    "embed_to_plain_text",
    "MENTION_PATTERNS",
    "contains_mention",
    "validate_embeds",
]

# ── Discord-documented embed limits (REST v10) ───────────────────────────────
EMBED_LIMITS = {
    "title": 256,
    "description": 4096,
    "field_name": 256,
    "field_value": 1024,
    "fields": 25,
    "footer_text": 2048,
    "author_name": 256,
    "total": 6000,
    "per_message": 10,
}

_HTTP_URL_RE = re.compile(r"^https?://", re.IGNORECASE)
_ISO_TS_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:?\d{2})$"
)
# Control characters (C0 + DEL, excluding tab/newline/carriage-return) that have
# no place in a Discord payload URL.
_URL_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class EmbedValidationError(ValueError):
    """Raised when an embed violates a Discord-documented limit."""


def _check_len(value: Optional[str], limit: int, what: str) -> Optional[str]:
    if value is None:
        return None
    if len(value) > limit:
        raise EmbedValidationError(
            f"embed {what} is {len(value)} chars, exceeds Discord limit {limit}"
        )
    return value


def _check_url(value: Optional[str], what: str) -> Optional[str]:
    """Validate a URL: must use an http(s) scheme, have a non-empty hostname,
    and contain no control characters. Preserves None handling and
    EmbedValidationError behavior.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise EmbedValidationError(
            f"embed {what} must be an http(s) URL, got {value!r}"
        )
    if _URL_CONTROL_RE.search(value):
        raise EmbedValidationError(
            f"embed {what} contains control characters, got {value!r}"
        )
    # Parse the URL for structural validation rather than relying on a
    # prefix regex alone.
    from urllib.parse import urlparse

    parsed = urlparse(value)
    if parsed.scheme.lower() not in ("http", "https"):
        raise EmbedValidationError(
            f"embed {what} must be an http(s) URL, got {value!r}"
        )
    if not parsed.hostname:
        raise EmbedValidationError(
            f"embed {what} must have a non-empty hostname, got {value!r}"
        )
    return value


@dataclass(frozen=True)
class EmbedField:
    """A single embed field (name/value/inline)."""

    name: str
    value: str
    inline: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _check_len(self.name, EMBED_LIMITS["field_name"], "field name"))
        object.__setattr__(self, "value", _check_len(self.value, EMBED_LIMITS["field_value"], "field value"))


@dataclass(frozen=True)
class EmbedAuthor:
    """Embed author line (name ≤256, optional url/icon)."""

    name: str
    url: Optional[str] = None
    icon_url: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _check_len(self.name, EMBED_LIMITS["author_name"], "author name"))
        object.__setattr__(self, "url", _check_url(self.url, "author url"))
        object.__setattr__(self, "icon_url", _check_url(self.icon_url, "author icon_url"))


@dataclass(frozen=True)
class EmbedFooter:
    """Embed footer (text ≤2048, optional icon)."""

    text: str
    icon_url: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "text", _check_len(self.text, EMBED_LIMITS["footer_text"], "footer text"))
        object.__setattr__(self, "icon_url", _check_url(self.icon_url, "footer icon_url"))


@dataclass(frozen=True)
class Embed:
    """A validated Discord embed.

    Enforces every documented limit at construction. ``fields`` are bounded to
    25 and the total character budget across title/description/fields/footer/
    author is capped at 6000.
    """

    title: Optional[str] = None
    description: Optional[str] = None
    url: Optional[str] = None
    color: Optional[int] = None
    timestamp: Optional[str] = None
    author: Optional[EmbedAuthor] = None
    footer: Optional[EmbedFooter] = None
    fields: List[EmbedField] = field(default_factory=list)
    image_url: Optional[str] = None
    thumbnail_url: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "title", _check_len(self.title, EMBED_LIMITS["title"], "title"))
        object.__setattr__(
            self, "description",
            _check_len(self.description, EMBED_LIMITS["description"], "description"),
        )
        object.__setattr__(self, "url", _check_url(self.url, "url"))
        object.__setattr__(self, "image_url", _check_url(self.image_url, "image url"))
        object.__setattr__(self, "thumbnail_url", _check_url(self.thumbnail_url, "thumbnail url"))
        if self.timestamp is not None:
            if not isinstance(self.timestamp, str) or not _ISO_TS_RE.match(self.timestamp):
                raise EmbedValidationError(
                    f"embed timestamp must be ISO-8601, got {self.timestamp!r}"
                )
            # Parse the validated timestamp to reject invalid calendar dates
            # (e.g. 2026-02-30T00:00:00Z). Normalize trailing ``Z`` to ``+00:00``
            # for fromisoformat compatibility on Python < 3.11.
            normalized = self.timestamp
            if normalized.endswith("Z"):
                normalized = normalized[:-1] + "+00:00"
            try:
                datetime.fromisoformat(normalized)
            except ValueError:
                raise EmbedValidationError(
                    f"embed timestamp is not a valid date/time, got {self.timestamp!r}"
                )
        if self.color is not None:
            if not isinstance(self.color, int) or not (0 <= self.color <= 0xFFFFFF):
                raise EmbedValidationError(
                    f"embed color must be a 24-bit int, got {self.color!r}"
                )
        # Convert the mutable list to an immutable tuple before validation so
        # the frozen dataclass never exposes a mutable sequence externally.
        object.__setattr__(self, "fields", tuple(self.fields))
        if len(self.fields) > EMBED_LIMITS["fields"]:
            raise EmbedValidationError(
                f"embed has {len(self.fields)} fields, exceeds Discord limit "
                f"{EMBED_LIMITS['fields']}"
            )
        total = self._total_chars()
        if total > EMBED_LIMITS["total"]:
            raise EmbedValidationError(
                f"embed is {total} total chars, exceeds Discord limit "
                f"{EMBED_LIMITS['total']}"
            )

    def _total_chars(self) -> int:
        total = len(self.title or "") + len(self.description or "")
        total += len(self.author.name) if self.author else 0
        total += len(self.footer.text) if self.footer else 0
        total += sum(len(f.name) + len(f.value) for f in self.fields)
        return total

    def to_payload(self) -> dict:
        """Render to the Discord REST embed object (safe, no execution)."""
        payload: dict = {}
        for key in ("title", "description", "url", "color", "timestamp"):
            val = getattr(self, key)
            if val is not None:
                payload[key] = val
        if self.author is not None:
            a: dict = {"name": self.author.name}
            if self.author.url:
                a["url"] = self.author.url
            if self.author.icon_url:
                a["icon_url"] = self.author.icon_url
            payload["author"] = a
        if self.footer is not None:
            f: dict = {"text": self.footer.text}
            if self.footer.icon_url:
                f["icon_url"] = self.footer.icon_url
            payload["footer"] = f
        if self.image_url:
            payload["image"] = {"url": self.image_url}
        if self.thumbnail_url:
            payload["thumbnail"] = {"url": self.thumbnail_url}
        if self.fields:
            payload["fields"] = [
                {"name": f.name, "value": f.value, "inline": f.inline}
                for f in self.fields
            ]
        return payload


# ── Message-level batch validation ───────────────────────────────────────────
def validate_embeds(embeds: Sequence[Embed]) -> None:
    """Enforce Discord's per-message embed limits.

    A single message may carry at most EMBED_LIMITS["per_message"] (10) embeds,
    and the combined character budget across all embeds is capped at
    EMBED_LIMITS["total"] (6000) per embed — the aggregate limit applies per
    individual embed, not across the whole message. This function validates
    the count constraint and delegates per-embed validation (already enforced
    at construction) so callers get one entry point for the full message.

    Raises EmbedValidationError when the collection violates the count limit.
    """
    if len(embeds) > EMBED_LIMITS["per_message"]:
        raise EmbedValidationError(
            f"message has {len(embeds)} embeds, exceeds Discord limit "
            f"{EMBED_LIMITS['per_message']}"
        )


# ── Mention policy ───────────────────────────────────────────────────────────
# Discord mention forms: @everyone, @here, and <@id> / <@!id> user mentions.
MENTION_PATTERNS = (
    re.compile(r"@everyone"),
    re.compile(r"@here"),
    re.compile(r"<@!?[0-9]+>"),
)


def contains_mention(text: str) -> bool:
    """Return True when ``text`` carries a Discord mention that can ping."""
    return any(p.search(text or "") for p in MENTION_PATTERNS)


# ── Plain-text fallback ──────────────────────────────────────────────────────
def embed_to_plain_text(embed: Embed) -> str:
    """Render an embed as plain markdown for clients where embeds are invisible.

    Preserves the full payload next to any buttons/typing so an interactive
    prompt never loses its content on clients where embeds render separately.
    """
    lines: List[str] = []
    if embed.author:
        lines.append(f"**{embed.author.name}**")
    if embed.title:
        lines.append(f"# {embed.title}")
    if embed.description:
        lines.append(embed.description)
    for f in embed.fields:
        lines.append(f"**{f.name}:** {f.value}")
    if embed.footer:
        lines.append(f"_{embed.footer.text}_")
    return "\n".join(lines)
