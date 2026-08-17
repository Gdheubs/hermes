"""Telegram configuration, mention routing, and group observation mixin.

Methods are mechanically extracted from the pinned Telegram adapter.  Imports
that must remain patchable through the original adapter namespace are local by
design; this keeps the public monkeypatch seam and avoids an import cycle.
"""

from typing import Any, List, Optional

import re

from gateway.platforms.base import MessageEvent, MessageType

try:
    from telegram import Message
except ImportError:
    Message = Any


class TelegramConfigMentionMixin:
    """Configuration, mention detection, and passive group-observation methods."""

    def format_message(self, content: str) -> str:
        """
        Convert standard markdown to Telegram MarkdownV2 format.

        Protected regions (code blocks, inline code) are extracted first so
        their contents are never modified.  Standard markdown constructs
        (headers, bold, italic, links) are translated to MarkdownV2 syntax,
        and all remaining special characters are escaped.
        """
        from plugins.platforms.telegram.adapter import _escape_mdv2, _wrap_markdown_tables, re
        if not content:
            return content

        placeholders: dict = {}
        counter = [0]

        def _ph(value: str) -> str:
            """Stash *value* behind a placeholder token that survives escaping."""
            key = f"\x00PH{counter[0]}\x00"
            counter[0] += 1
            placeholders[key] = value
            return key

        text = content

        # 0) Rewrite GFM-style pipe tables into Telegram-friendly row groups
        #    before the normal MarkdownV2 conversions run.
        text = _wrap_markdown_tables(text)

        # 1) Protect fenced code blocks (``` ... ```)
        #    Per MarkdownV2 spec, \ and ` inside pre/code must be escaped.
        def _protect_fenced(m):
            raw = m.group(0)
            # Split off opening ``` (with optional language) and closing ```
            open_end = raw.index('\n') + 1 if '\n' in raw[3:] else 3
            opening = raw[:open_end]
            body_and_close = raw[open_end:]
            body = body_and_close[:-3]
            body = body.replace('\\', '\\\\').replace('`', '\\`')
            return _ph(opening + body + '```')

        text = re.sub(
            r'(```(?:[^\n]*\n)?[\s\S]*?```)',
            _protect_fenced,
            text,
        )

        # 2) Protect inline code (`...`)
        #    Escape \ inside inline code per MarkdownV2 spec.
        text = re.sub(
            r'(`[^`]+`)',
            lambda m: _ph(m.group(0).replace('\\', '\\\\')),
            text,
        )

        # 3) Convert markdown links – escape the display text; inside the URL
        #    only ')' and '\' need escaping per the MarkdownV2 spec.
        def _convert_link(m):
            display = _escape_mdv2(m.group(1))
            url = m.group(2).replace('\\', '\\\\').replace(')', '\\)')
            return _ph(f'[{display}]({url})')

        text = re.sub(r'\[([^\]]+)\]\(([^()]*(?:\([^()]*\)[^()]*)*)\)', _convert_link, text)

        # 4) Convert markdown headers (## Title) → bold *Title*
        def _convert_header(m):
            inner = m.group(1).strip()
            # Strip redundant bold markers that may appear inside a header
            inner = re.sub(r'\*\*(.+?)\*\*', r'\1', inner)
            return _ph(f'*{_escape_mdv2(inner)}*')

        text = re.sub(
            r'^#{1,6}\s+(.+)$', _convert_header, text, flags=re.MULTILINE
        )

        # 5) Convert bold: **text** → *text* (MarkdownV2 bold)
        text = re.sub(
            r'\*\*(.+?)\*\*',
            lambda m: _ph(f'*{_escape_mdv2(m.group(1))}*'),
            text,
        )

        # 6) Convert italic: *text* (single asterisk) → _text_ (MarkdownV2 italic)
        #    [^*\n]+ prevents matching across newlines (which would corrupt
        #    bullet lists using * markers and multi-line content).
        text = re.sub(
            r'\*([^*\n]+)\*',
            lambda m: _ph(f'_{_escape_mdv2(m.group(1))}_'),
            text,
        )

        # 7) Convert strikethrough: ~~text~~ → ~text~ (MarkdownV2)
        text = re.sub(
            r'~~(.+?)~~',
            lambda m: _ph(f'~{_escape_mdv2(m.group(1))}~'),
            text,
        )

        # 8) Convert spoiler: ||text|| → ||text|| (protect from | escaping)
        text = re.sub(
            r'\|\|(.+?)\|\|',
            lambda m: _ph(f'||{_escape_mdv2(m.group(1))}||'),
            text,
        )

        # 9) Convert blockquotes: > at line start → protect > from escaping
        #    Handle both regular blockquotes (> text) and expandable blockquotes
        #    (Telegram MarkdownV2: **> for expandable start, || to end the quote)
        def _convert_blockquote(m):
            prefix = m.group(1)  # >, >>, >>>, **>, or **>> etc.
            content = m.group(2)
            # Check if content ends with || (expandable blockquote end marker)
            # In this case, preserve the trailing || unescaped for Telegram
            if prefix.startswith('**') and content.endswith('||'):
                return _ph(f'{prefix} {_escape_mdv2(content[:-2])}||')
            return _ph(f'{prefix} {_escape_mdv2(content)}')

        text = re.sub(
            r'^((?:\*\*)?>{1,3}) (.+)$',
            _convert_blockquote,
            text,
            flags=re.MULTILINE,
        )

        # 10) Escape remaining special characters in plain text
        text = _escape_mdv2(text)

        # 11) Restore placeholders in reverse insertion order so that
        #    nested references (a placeholder inside another) resolve correctly.
        for key in reversed(list(placeholders.keys())):
            text = text.replace(key, placeholders[key])

        # 12) Safety net: escape unescaped ( ) { } that slipped through
        #     placeholder processing.  Split the text into code/non-code
        #     segments so we never touch content inside ``` or ` spans.
        _code_split = re.split(r'(```[\s\S]*?```|`[^`]+`)', text)
        _safe_parts = []
        for _idx, _seg in enumerate(_code_split):
            if _idx % 2 == 1:
                # Inside code span/block — leave untouched
                _safe_parts.append(_seg)
            else:
                # Outside code — escape bare ( ) { }
                def _esc_bare(m, _seg=_seg):
                    s = m.start()
                    ch = m.group(0)
                    # Already escaped
                    if s > 0 and _seg[s - 1] == '\\':
                        return ch
                    # ( that opens a MarkdownV2 link [text](url)
                    if ch == '(' and s > 0 and _seg[s - 1] == ']':
                        return ch
                    # ) that closes a link URL
                    if ch == ')':
                        before = _seg[:s]
                        if '](http' in before or '](' in before:
                            # Check depth
                            depth = 0
                            for j in range(s - 1, max(s - 2000, -1), -1):
                                if _seg[j] == '(':
                                    depth -= 1
                                    if depth < 0:
                                        if j > 0 and _seg[j - 1] == ']':
                                            return ch
                                        break
                                elif _seg[j] == ')':
                                    depth += 1
                    return '\\' + ch
                _safe_parts.append(re.sub(r'[(){}]', _esc_bare, _seg))
        text = ''.join(_safe_parts)

        return text

    def _telegram_require_mention(self) -> bool:
        """Return whether group chats should require an explicit bot trigger."""
        from plugins.platforms.telegram.adapter import os
        configured = self.config.extra.get("require_mention")
        if configured is not None:
            if isinstance(configured, str):
                return configured.lower() in {"true", "1", "yes", "on"}
            return bool(configured)
        return os.getenv("TELEGRAM_REQUIRE_MENTION", "false").lower() in {"true", "1", "yes", "on"}

    def _telegram_observe_unmentioned_group_messages(self) -> bool:
        """Return whether skipped unmentioned group messages are stored as context.

        When enabled with ``require_mention``, Telegram matches the Yuanbao /
        OpenClaw-style group UX: observe ordinary group chatter in the session
        transcript, but only dispatch the agent when the bot is explicitly
        addressed.
        """
        from plugins.platforms.telegram.adapter import os
        configured = self.config.extra.get("observe_unmentioned_group_messages")
        if configured is None:
            configured = self.config.extra.get("ingest_unmentioned_group_messages")
        if configured is not None:
            if isinstance(configured, str):
                return configured.lower() in {"true", "1", "yes", "on"}
            return bool(configured)
        return os.getenv("TELEGRAM_OBSERVE_UNMENTIONED_GROUP_MESSAGES", "false").lower() in {"true", "1", "yes", "on"}

    def _telegram_guest_mode(self) -> bool:
        """Return whether non-allowlisted groups may trigger via direct @mention."""
        from plugins.platforms.telegram.adapter import os
        configured = self.config.extra.get("guest_mode")
        if configured is not None:
            if isinstance(configured, str):
                return configured.lower() in {"true", "1", "yes", "on"}
            return bool(configured)
        return os.getenv("TELEGRAM_GUEST_MODE", "false").lower() in {"true", "1", "yes", "on"}

    def _telegram_exclusive_bot_mentions(self) -> bool:
        """Return whether explicit @...bot mentions exclusively route group messages."""
        from plugins.platforms.telegram.adapter import os
        configured = self.config.extra.get("exclusive_bot_mentions")
        if configured is not None:
            if isinstance(configured, str):
                return configured.lower() in {"true", "1", "yes", "on"}
            return bool(configured)
        return os.getenv("TELEGRAM_EXCLUSIVE_BOT_MENTIONS", "true").lower() in {"true", "1", "yes", "on"}

    def _telegram_free_response_chats(self) -> set[str]:
        from plugins.platforms.telegram.adapter import _scoped_gate_env
        raw = self.config.extra.get("free_response_chats")
        if raw is None:
            raw = _scoped_gate_env("TELEGRAM_FREE_RESPONSE_CHATS")
        if isinstance(raw, list):
            return {str(part).strip() for part in raw if str(part).strip()}
        return {part.strip() for part in str(raw).split(",") if part.strip()}

    def _telegram_free_response_topics(self) -> set[str]:
        """Return topic-level free-response allowlist entries as ``<chat_id>:<thread_id>``.

        Unlike ``free_response_chats`` (whole-chat), each entry opens a single
        forum topic for free-response. A missing/omitted thread id on incoming
        messages is normalized to the General topic (``1``).
        """
        from plugins.platforms.telegram.adapter import _scoped_gate_env
        raw = self.config.extra.get("free_response_topics")
        if raw is None:
            raw = _scoped_gate_env("TELEGRAM_FREE_RESPONSE_TOPICS")
        if isinstance(raw, list):
            return {str(part).strip() for part in raw if str(part).strip()}
        return {part.strip() for part in str(raw).split(",") if part.strip()}

    def _telegram_is_free_response_topic(self, message: Message) -> bool:
        """True when the message's chat/topic pair is in ``free_response_topics``."""
        topics = self._telegram_free_response_topics()
        if not topics:
            return False
        chat_id = str(getattr(getattr(message, "chat", None), "id", ""))
        if not chat_id:
            return False
        thread_id = self._effective_message_thread_id(message)
        topic_id = str(thread_id) if thread_id is not None else self._GENERAL_TOPIC_THREAD_ID
        return f"{chat_id}:{topic_id}" in topics

    def _telegram_allowed_chats(self) -> set[str]:
        """Return the whitelist of group/supergroup chat IDs the bot will respond in.

        When non-empty, group messages from chats NOT in this set are
        silently ignored unless ``guest_mode`` is enabled and the bot is
        explicitly @mentioned.  DMs are never filtered.
        Empty set means no restriction (fully backward compatible).
        """
        from plugins.platforms.telegram.adapter import _scoped_gate_env
        raw = self.config.extra.get("allowed_chats")
        if raw is None:
            raw = _scoped_gate_env("TELEGRAM_ALLOWED_CHATS")
        if isinstance(raw, list):
            return {str(part).strip() for part in raw if str(part).strip()}
        return {part.strip() for part in str(raw).split(",") if part.strip()}

    def _telegram_group_allowed_chats(self) -> set[str]:
        """Return Telegram chats authorized at group scope."""
        from plugins.platforms.telegram.adapter import _scoped_gate_env
        raw = self.config.extra.get("group_allowed_chats")
        if raw is None:
            raw = _scoped_gate_env("TELEGRAM_GROUP_ALLOWED_CHATS")
        if isinstance(raw, list):
            return {str(part).strip() for part in raw if str(part).strip()}
        return {part.strip() for part in str(raw).split(",") if part.strip()}

    def _telegram_observe_allowed_chats(self) -> set[str]:
        """Chats where observed group context may use a shared source.

        ``group_allowed_chats`` is the gateway authorization allowlist for
        user-less group sources.  ``allowed_chats`` remains an optional response
        gate; when set, observed context must satisfy both lists.
        """
        group_allowed = self._telegram_group_allowed_chats()
        if not group_allowed:
            return set()
        response_allowed = self._telegram_allowed_chats()
        if response_allowed:
            return group_allowed & response_allowed
        return group_allowed

    def _telegram_allowed_topics(self) -> set[str]:
        """Return the whitelist of Telegram forum topic IDs this bot handles.

        When non-empty, group/supergroup messages from other topics are
        silently ignored. DMs are never filtered by topic. Telegram may omit
        ``message_thread_id`` for the forum General topic, so ``None`` is
        treated as topic ``1`` for matching purposes.
        """
        from plugins.platforms.telegram.adapter import _scoped_gate_env
        raw = self.config.extra.get("allowed_topics")
        if raw is None:
            raw = _scoped_gate_env("TELEGRAM_ALLOWED_TOPICS")
        if isinstance(raw, list):
            return {str(part).strip() for part in raw if str(part).strip()}
        return {part.strip() for part in str(raw).split(",") if part.strip()}

    def _telegram_ignored_threads(self) -> set[int]:
        from plugins.platforms.telegram.adapter import _scoped_gate_env, logger
        raw = self.config.extra.get("ignored_threads")
        if raw is None:
            raw = _scoped_gate_env("TELEGRAM_IGNORED_THREADS")

        if isinstance(raw, list):
            values = raw
        else:
            values = str(raw).split(",")

        ignored: set[int] = set()
        for value in values:
            text = str(value).strip()
            if not text:
                continue
            try:
                ignored.add(int(text))
            except (TypeError, ValueError):
                logger.warning("[%s] Ignoring invalid Telegram thread id: %r", self.name, value)
        return ignored

    def _compile_mention_patterns(self) -> List[re.Pattern]:
        """Compile optional regex wake-word patterns for group triggers."""
        from plugins.platforms.telegram.adapter import compile_mention_patterns, json, logger, os, re
        patterns = self.config.extra.get("mention_patterns")
        if patterns is None:
            raw = os.getenv("TELEGRAM_MENTION_PATTERNS", "").strip()
            if raw:
                try:
                    loaded = json.loads(raw)
                except Exception:
                    loaded = [part.strip() for part in raw.splitlines() if part.strip()]
                    if not loaded:
                        loaded = [part.strip() for part in raw.split(",") if part.strip()]
                patterns = loaded

        if patterns is None:
            # Parity with the historical inline implementation: return before
            # evaluating ``self.name`` (tests construct bare adapters via
            # object.__new__ that lack the attributes ``name`` reads).
            return []

        return compile_mention_patterns(
            patterns,
            log_prefix=self.name,
            platform_label="telegram",
            display_label="Telegram",
            logger_=logger,
        )

    def _is_group_chat(self, message: Message) -> bool:
        chat = getattr(message, "chat", None)
        if not chat:
            return False
        chat_type = str(getattr(chat, "type", "")).split(".")[-1].lower()
        return chat_type in {"group", "supergroup"}

    @classmethod
    def _effective_message_thread_id(cls, message: Message) -> Optional[str]:
        """Return the routable thread id for a Telegram message.

        Forum supergroup messages posted in the General topic arrive with
        ``message_thread_id=None`` while Telegram itself addresses that topic
        as thread id ``1``.  Ordinary replies are the opposite footgun:
        Telegram populates ``message_thread_id`` with a reply-UI anchor id on
        plain group/DM replies, but those ids are not topic/session routing
        ids and must not be treated as such.  Gating, skill binding, and
        outbound routing must all agree on the same normalized value.
        """
        chat = getattr(message, "chat", None)
        chat_type = str(getattr(chat, "type", "")).split(".")[-1].lower() if chat else ""
        raw = getattr(message, "message_thread_id", None)
        is_topic_message = bool(getattr(message, "is_topic_message", False))
        is_forum_group = chat_type in ("group", "supergroup") and getattr(chat, "is_forum", False) is True
        if raw is not None:
            if is_forum_group or (chat_type in ("group", "supergroup") and is_topic_message):
                return str(raw)
            if chat_type == "private" and is_topic_message:
                return str(raw)
            return None
        if is_forum_group:
            return cls._GENERAL_TOPIC_THREAD_ID
        return None

    def _current_bot_username(self) -> str:
        """Return this bot's live @username (lowercased, no leading ``@``).

        Prefers the most recently observed handle over PTB's ``get_me()``
        cache. ``Bot.username`` reads ``Bot._bot_user``, which is written only
        by ``get_me()`` — after a BotFather rename it keeps returning the old
        handle, so every mention comparison silently stops matching and the
        exclusive-mention gate concludes the message is addressed to a
        different bot. Observing the handle from inbound updates closes that
        window without an extra Bot API round-trip.
        """
        observed = getattr(self, "_bot_username_observed", None)
        if observed:
            return observed
        return (getattr(self._bot, "username", None) or "").lstrip("@").lower()

    def _note_bot_username(self, username: Optional[str]) -> None:
        """Record the bot's current @username, logging real renames."""
        from plugins.platforms.telegram.adapter import logger, time
        handle = (username or "").lstrip("@").lower()
        if not handle:
            return
        previous = getattr(self, "_bot_username_observed", None)
        if previous == handle:
            return
        self._bot_username_observed = handle
        self._bot_identity_checked_at = time.monotonic()
        if previous:
            logger.info(
                "[%s] Telegram bot username changed: @%s -> @%s "
                "(mention routing now follows the new handle)",
                self.name, previous, handle,
            )

    def _observe_bot_identity_from_message(self, message: Message) -> None:
        """Learn our own handle from a message Telegram says we authored.

        Telegram stamps the *current* username on the bot's own outgoing
        messages and on ``reply_to_message`` when a user replies to us, so a
        rename is observable from the update stream itself — no getMe needed.
        Only trusted when the user id matches this bot, so another account's
        handle can never be adopted as our own.
        """
        bot_id = getattr(self._bot, "id", None)
        if bot_id is None:
            return
        for candidate in (
            getattr(message, "from_user", None),
            getattr(getattr(message, "reply_to_message", None), "from_user", None),
        ):
            if candidate is None:
                continue
            if getattr(candidate, "id", None) != bot_id:
                continue
            self._note_bot_username(getattr(candidate, "username", None))

    def _bot_identity_is_fresh(self) -> bool:
        """True when identity was re-read within the TTL.

        ``None`` means never checked, which is always stale. Do not fold the
        sentinel into ``0.0``: monotonic clocks have an arbitrary epoch that
        can legitimately be smaller than the TTL on a freshly-booted host,
        which would make "never" look like "just now".
        """
        from plugins.platforms.telegram.adapter import time
        checked_at = getattr(self, "_bot_identity_checked_at", None)
        if checked_at is None:
            return False
        return (time.monotonic() - checked_at) < self._BOT_IDENTITY_TTL_SECONDS

    async def _refresh_bot_identity(self, *, force: bool = False) -> None:
        """Re-read the bot's identity from Telegram when the cache may be stale.

        ``get_me()`` rewrites PTB's ``Bot._bot_user`` in place, so this also
        repairs every other consumer of ``self._bot.username``. Best-effort:
        a failed probe leaves the last known handle in place.
        """
        from plugins.platforms.telegram.adapter import asyncio, logger, time
        bot = self._bot
        if bot is None or not callable(getattr(bot, "get_me", None)):
            return
        if not force and self._bot_identity_is_fresh():
            return
        try:
            me = await asyncio.wait_for(bot.get_me(), self._BOT_IDENTITY_PROBE_TIMEOUT)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug(
                "[%s] Telegram identity refresh failed (keeping @%s): %s",
                self.name, self._current_bot_username() or "unknown", exc,
            )
            return
        self._bot_identity_checked_at = time.monotonic()
        self._note_bot_username(getattr(me, "username", None))

    def _is_reply_to_bot(self, message: Message) -> bool:
        if not self._bot or not getattr(message, "reply_to_message", None):
            return False
        reply_user = getattr(message.reply_to_message, "from_user", None)
        return bool(reply_user and getattr(reply_user, "id", None) == getattr(self._bot, "id", None))

    @classmethod
    def _extract_bot_mention_usernames(cls, message: Message, self_username: str = "") -> set[str]:
        """Extract explicit Telegram bot usernames mentioned in text/captions.

        Foreign handles are only treated as bot mentions when they look
        bot-shaped (``...bot``), which keeps human ``@handles`` from acting as
        routing hints. ``self_username`` opts our OWN handle into the same set
        regardless of shape: collectible (Fragment) usernames can be assigned
        to bots and need not end in "bot" (@jarvis, @pic), and a bot addressed
        by such a handle must still recognise itself.

        Entity mentions are authoritative. The raw-text fallback is intentionally narrow so
        entity-less mobile/client variants still work without treating email
        addresses or arbitrary substrings as bot mentions.
        """
        from plugins.platforms.telegram.adapter import TelegramAdapter, re
        mentioned_bot_usernames: set[str] = set()
        own = (self_username or "").lstrip("@").lower()

        def _is_bot_handle(handle: str) -> bool:
            if not handle:
                return False
            if own and handle == own:
                return True
            return bool(cls._FOREIGN_BOT_HANDLE_RE.fullmatch(handle))

        def _iter_sources():
            yield getattr(message, "text", None) or "", getattr(message, "entities", None) or []
            yield getattr(message, "caption", None) or "", getattr(message, "caption_entities", None) or []

        for source_text, entities in _iter_sources():
            for entity in entities:
                entity_type = str(getattr(entity, "type", "")).split(".")[-1].lower()
                if entity_type not in {"mention", "bot_command"}:
                    continue
                offset = int(getattr(entity, "offset", -1))
                length = int(getattr(entity, "length", 0))
                if offset < 0 or length <= 0:
                    continue

                entity_text = TelegramAdapter._telegram_entity_text(source_text, offset, length).strip()
                if entity_type == "mention":
                    handle = entity_text.lstrip("@").lower()
                    if _is_bot_handle(handle):
                        mentioned_bot_usernames.add(handle)
                    continue

                # Telegram emits /cmd@botname as one bot_command entity, not as
                # a separate mention entity. Treat that suffix as an explicit
                # bot address for exclusive multi-bot routing even when the
                # group has require_mention/free-response disabled.
                at_index = entity_text.find("@")
                if at_index < 0:
                    continue
                command_target = entity_text[at_index + 1:].strip().lower()
                if _is_bot_handle(command_target):
                    mentioned_bot_usernames.add(command_target)

        # Entity-less fallback for older/client-specific updates. If Telegram
        # supplied entities for a source, trust them and do not regex-rescue
        # malformed/URL/code spans that the server did not mark as mentions.
        for raw_text, entities in _iter_sources():
            if not raw_text or entities:
                continue
            for match in re.finditer(r"(?i)(?<![A-Za-z0-9_`/])@([A-Za-z0-9_]{2,31})\b", raw_text):
                handle = match.group(1).lower()
                if _is_bot_handle(handle):
                    mentioned_bot_usernames.add(handle)

        return mentioned_bot_usernames

    def _message_mentions_bot(self, message: Message) -> bool:
        if not self._bot:
            return False

        bot_username = self._current_bot_username()
        bot_id = getattr(self._bot, "id", None)
        expected = f"@{bot_username}" if bot_username else None

        def _iter_sources():
            yield getattr(message, "text", None) or "", getattr(message, "entities", None) or []
            yield getattr(message, "caption", None) or "", getattr(message, "caption_entities", None) or []

        # Telegram parses mentions server-side and emits MessageEntity objects
        # (type=mention for @username, type=text_mention for @FirstName targeting
        # a user without a public username). Those entities are authoritative:
        # raw substring matches like "foo@hermes_bot.example" are not mentions
        # (bug #12545). Entities also correctly handle @handles inside URLs, code
        # blocks, and quoted text, where a regex scan would over-match.
        for source_text, entities in _iter_sources():
            for entity in entities:
                entity_type = str(getattr(entity, "type", "")).split(".")[-1].lower()
                if entity_type == "mention" and expected:
                    offset = int(getattr(entity, "offset", -1))
                    length = int(getattr(entity, "length", 0))
                    if offset < 0 or length <= 0:
                        continue
                    if self._telegram_entity_text(source_text, offset, length).strip().lower() == expected:
                        return True
                elif entity_type == "text_mention":
                    user = getattr(entity, "user", None)
                    if user and getattr(user, "id", None) == bot_id:
                        return True
                elif entity_type == "bot_command" and expected:
                    # Telegram's official group-disambiguation form for slash
                    # commands (``/cmd@botname``) is emitted as a single
                    # ``bot_command`` entity covering the whole span — there
                    # is no accompanying ``mention`` entity. Treat it as a
                    # direct address to this bot when the ``@botname`` suffix
                    # matches. This is the form Telegram's own command menu
                    # autocomplete produces in groups, so dropping it at the
                    # mention gate would break /new, /reset, /help, ... for
                    # every group that has ``require_mention`` enabled (#15415).
                    offset = int(getattr(entity, "offset", -1))
                    length = int(getattr(entity, "length", 0))
                    if offset < 0 or length <= 0:
                        continue
                    command_text = self._telegram_entity_text(source_text, offset, length)
                    at_index = command_text.find("@")
                    if at_index < 0:
                        continue
                    if command_text[at_index:].strip().lower() == expected:
                        return True
        if bot_username:
            return bot_username in self._extract_bot_mention_usernames(message, bot_username)
        return False

    def _schedule_bot_identity_recheck(self) -> None:
        """Fire a TTL-guarded identity refresh in the background.

        Called when routing is about to discard a message because the bot
        handles it names don't include ours — the exact symptom of a stale
        username after a BotFather rename. The TTL in
        ``_refresh_bot_identity`` bounds this to one getMe per
        ``_BOT_IDENTITY_TTL_SECONDS``, so a busy group that legitimately
        addresses other bots cannot turn this into per-message API traffic.
        Fire-and-forget: the current message still routes on what we know now.
        """
        from plugins.platforms.telegram.adapter import asyncio
        existing = getattr(self, "_bot_identity_refresh_task", None)
        if existing is not None and not existing.done():
            return
        if self._bot_identity_is_fresh():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(self._refresh_bot_identity())
        self._bot_identity_refresh_task = task
        tracked = getattr(self, "_background_tasks", None)
        if isinstance(tracked, set):
            tracked.add(task)
            task.add_done_callback(tracked.discard)

    def _explicit_bot_mentions_exclude_self(self, message: Message) -> bool:
        """Return True when explicit bot handles target other bots, not this one.

        Telegram groups can contain several Hermes bot profiles. A message like
        ``@bot3 hi @bot4`` must not wake ``@bot1`` through reply/wake-word
        fallbacks. Treat explicit bot-handle mentions as an exclusive routing
        hint: if at least one @...bot username is present and none matches this
        adapter's own bot username, this adapter should ignore the message.

        MessageEntity values are preferred, but some Telegram clients expose
        selected bot handles as plain text in group messages. Foreign handles
        are limited to the ``...bot`` shape so human @handles never suppress
        this bot; our own handle is matched by identity, so a collectible
        username without that suffix still counts as addressing us.
        """
        if not self._bot:
            return False

        bot_username = self._current_bot_username()
        if not bot_username:
            return False

        mentioned_bot_usernames = self._extract_bot_mention_usernames(message, bot_username)
        excludes_self = bool(mentioned_bot_usernames) and bot_username not in mentioned_bot_usernames
        if excludes_self:
            # Either the message really is for another bot, or our cached
            # handle is stale after a rename and we are about to ignore a
            # message addressed to us. Re-check identity out of band (TTL
            # bounded) so the mistake self-corrects instead of persisting.
            self._schedule_bot_identity_recheck()
        return excludes_self

    def _message_matches_mention_patterns(self, message: Message) -> bool:
        if not self._mention_patterns:
            return False
        for candidate in (getattr(message, "text", None), getattr(message, "caption", None)):
            if not candidate:
                continue
            for pattern in self._mention_patterns:
                if pattern.search(candidate):
                    return True
        return False

    def _is_guest_mention(self, message: Message) -> bool:
        """Return True for the narrow guest-mode bypass: explicit bot mention.

        The caller (:meth:`_should_process_message`) has already verified
        the message is a group chat, so that check is not repeated here.
        """
        return self._telegram_guest_mode() and self._message_mentions_bot(message)

    def _clean_bot_trigger_text(self, text: Optional[str]) -> Optional[str]:
        from plugins.platforms.telegram.adapter import re
        bot_username = self._current_bot_username()
        if not text or not bot_username:
            return text
        username = re.escape(bot_username)
        cleaned = re.sub(rf"(?i)@{username}\b[,:\-]*\s*", "", text).strip()
        return cleaned or text

    def _should_observe_unmentioned_group_message(self, message: Message) -> bool:
        """Return True when a group message should be stored but not dispatched."""
        if self._is_own_message(message):
            return False
        if not self._telegram_observe_unmentioned_group_messages():
            return False
        if not self._is_group_chat(message):
            return False

        thread_id = getattr(message, "message_thread_id", None)
        allowed_topics = self._telegram_allowed_topics()
        if allowed_topics:
            topic_id = str(thread_id) if thread_id is not None else self._GENERAL_TOPIC_THREAD_ID
            if topic_id not in allowed_topics:
                return False

        if thread_id is not None:
            try:
                if int(thread_id) in self._telegram_ignored_threads():
                    return False
            except (TypeError, ValueError):
                return False

        chat_id_str = str(getattr(getattr(message, "chat", None), "id", ""))
        if self._telegram_exclusive_bot_mentions() and self._explicit_bot_mentions_exclude_self(message):
            return False

        allowed = self._telegram_observe_allowed_chats()
        # Observed context is shared at chat/topic scope so a later trigger from
        # another user can see it.  Require an explicit chat allowlist; that
        # keeps shared observed history limited to operator-approved groups and
        # lets gateway authorization pass even after the shared session source
        # drops the per-sender user_id.
        if not allowed or chat_id_str not in allowed:
            return False

        # Only observe messages skipped by the require_mention gate.  If the
        # message would be processed normally, let the dispatcher handle it;
        # if require_mention is disabled, every group message is a request.
        if chat_id_str in self._telegram_free_response_chats():
            return False
        if self._telegram_is_free_response_topic(message):
            return False
        if not self._telegram_require_mention():
            return False
        if self._is_reply_to_bot(message):
            return False
        if self._message_mentions_bot(message):
            return False
        if self._message_matches_mention_patterns(message):
            return False
        return True

    def _telegram_group_observe_shared_source(self, source):
        """Return a chat/topic-scoped source for observed Telegram group context."""
        from plugins.platforms.telegram.adapter import dataclasses
        return dataclasses.replace(source, user_id=None, user_name=None, user_id_alt=None)

    def _telegram_group_observe_attributed_text(self, event: MessageEvent) -> str:
        from plugins.platforms.telegram.adapter import MessageEvent
        user_id = event.source.user_id or "unknown"
        sender = event.source.user_name or user_id
        return f"[{sender}|{user_id}]\n{event.text or ''}"

    def _telegram_group_observe_channel_prompt(self) -> str:
        username = self._current_bot_username() or "unknown"
        bot_id = getattr(getattr(self, "_bot", None), "id", None) or "unknown"
        return (
            "You are handling a Telegram group chat message.\n"
            f"- Your identity: user_id={bot_id}, @-mention name in this group=@{username}\n"
            "- observed Telegram group context may be provided in a separate context-only block "
            "before the current message; it is not necessarily addressed to you.\n"
            "- Treat only the current new message as a request explicitly directed at you, "
            "and use observed context only when the current message asks for it."
        )

    def _apply_telegram_group_observe_attribution(self, event: MessageEvent) -> MessageEvent:
        """Align triggered group turns with observed-history attribution."""
        from plugins.platforms.telegram.adapter import MessageEvent, MessageType, dataclasses
        if not self._telegram_observe_unmentioned_group_messages():
            return event
        raw_message = getattr(event, "raw_message", None)
        if not raw_message or not self._is_group_chat(raw_message):
            return event
        chat_id_str = str(getattr(getattr(raw_message, "chat", None), "id", ""))
        allowed = self._telegram_observe_allowed_chats()
        if not allowed or chat_id_str not in allowed:
            return event
        shared_source = self._telegram_group_observe_shared_source(event.source)
        observe_prompt = self._telegram_group_observe_channel_prompt()
        channel_prompt = f"{event.channel_prompt}\n\n{observe_prompt}" if event.channel_prompt else observe_prompt
        if event.message_type == MessageType.COMMAND:
            # Commands must retain the original source (with user_id) so
            # slash-access control (_check_slash_access) can identify the
            # sender.  Replacing the source with an anonymised shared source
            # (user_id=None) causes admin-only commands like /new to be
            # denied even when the sender is an admin, because
            # SlashAccessPolicy.is_admin(None) is always False.
            # Still inject channel_prompt for group context.
            return dataclasses.replace(
                event,
                channel_prompt=channel_prompt,
            )
        return dataclasses.replace(
            event,
            text=self._telegram_group_observe_attributed_text(event),
            source=shared_source,
            channel_prompt=channel_prompt,
        )

    @staticmethod
    def _append_observed_note(existing: Optional[str], note: str) -> str:
        if not note:
            return existing or ""
        if not existing:
            return note
        return f"{existing}\n\n{note}"
