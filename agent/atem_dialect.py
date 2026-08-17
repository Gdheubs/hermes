"""Recovers OpenAI-style tool calls from Muse-Glimmer's native ATEM markup.

Muse-Glimmer-30B (``meta-models/Muse-Glimmer-30B``) doesn't speak the OpenAI
tool-calling wire format and its chat template emits no ``<tool_call>`` JSON.
It writes a call like this instead::

    <atem:function_calls>
      <atem:invoke name="terminal">
        <atem:parameter name="command">ls logs/</atem:parameter>
      </atem:invoke>
    </atem:function_calls>

Served behind a standard OpenAI-compatible ``/chat/completions`` endpoint
(vLLM or similar), the serving stack applies the model's own chat template
on the way in — so Hermes still sends ordinary OpenAI-shaped ``tools`` and
``messages`` — but nothing on the way out turns this markup into
``message.tool_calls``. It arrives as literal text in ``message.content``,
indistinguishable (to every existing code path) from a model that simply
chose not to call a tool.

This module is the parse side only. There is no render side: because a
template-aware server sits between Hermes and the model, replaying a prior
ATEM call back to the model is the server's job (it re-applies the chat
template to whatever ``tool_calls`` Hermes sends, OpenAI-shaped, in
conversation history) — not something Hermes constructs itself, the way
``agent/copilot_acp_client.py`` must when it owns the raw prompt.

## Argument types are not recovered here

One JSON object per call becomes one XML element per *parameter*, and a
parameter's value is text. The template writes booleans bare (``true``),
``None`` as ``null``, and structured values through ``tojson`` — with
nothing on the wire marking which happened, so a value reading ``true`` is
indistinguishable from the string ``"true"``. Recovering that requires the
tool's declared JSON Schema, which isn't available at the point
``normalize_response`` runs (it sees only the raw API response, not the
``tools`` list the request was built with). Every extracted argument is
therefore left as a JSON string value — a safe under-recovery: most tool
parameters (paths, commands, queries) are strings already, so this is a
no-op for them, and a caller that needs the richer types can add
schema-aware coercion as a follow-up without changing this module's
contract.

## Failure classes

There's no arguments object, so there's no JSON syntax error to make — a
whole class of malformed call cannot happen here. Two others can, and both
would otherwise resolve to silent "no calls found":

* an opened block, never closed — the likeliest break under a token limit,
  since an ATEM call is several times longer than the JSON equivalent it
  replaces.
* an unterminated ``<atem:parameter>``, whose value swallows the rest of
  the invoke. The parameters that *did* parse look like a complete call,
  which is exactly why staying silent about it would be misleading.

Both are reported in the returned ``malformed`` list rather than dropped.
"""

from __future__ import annotations

import json
import re

CALLS_OPEN = "<atem:function_calls>"
CALLS_CLOSE = "</atem:function_calls>"

_BLOCK_RE = re.compile(re.escape(CALLS_OPEN) + r"(.*?)" + re.escape(CALLS_CLOSE), re.DOTALL)
_INVOKE_RE = re.compile(r'<atem:invoke\s+name="([^"]*)"\s*>(.*?)</atem:invoke>', re.DOTALL)
_PARAM_RE = re.compile(r'<atem:parameter\s+name="([^"]*)"\s*>(.*?)</atem:parameter>', re.DOTALL)

# Tag-shaped and not the tag: a namespace typo, a missing close, an unquoted name. Looked for
# only after the real blocks are removed, so a near miss is reported rather than read as
# silence — a correct call itself contains ``<atem:invoke>``, which is loose-tag-shaped too.
_LOOSE_RE = re.compile(
    r"<\s*/?\s*atem\s*:\s*(?:function_calls|invoke|parameter)\b[^>]*>?|<\s*/?\s*function_calls\b[^>]*>?",
    re.IGNORECASE,
)

# An opened block with no matching close anywhere after it.
_UNCLOSED_RE = re.compile(re.escape(CALLS_OPEN) + r"(?!.*" + re.escape(CALLS_CLOSE) + r")", re.DOTALL)


def is_muse_glimmer_model(model: str | None) -> bool:
    """True for any Muse-Glimmer model slug, bare or aggregator/path-prefixed.

    Matches bare names (``muse-glimmer-30b``) and prefixed slugs
    (``meta-models/Muse-Glimmer-30B``, ``openrouter/meta-models/muse-glimmer-30b``),
    the same way :func:`agent.moonshot_schema.is_moonshot_model` covers aggregator
    prefixes for Kimi.
    """
    if not model:
        return False
    tail = model.strip().lower().rsplit("/", 1)[-1]
    return tail.startswith("muse-glimmer") or tail.startswith("museglimmer")


def extract_atem_tool_calls(
    text: str, *, call_id_prefix: str = "atem_call"
) -> tuple[list[tuple[str, str, str]], str, list[str]]:
    """Pull ``<atem:function_calls>`` blocks out of raw completion text.

    Returns ``(calls, content, malformed)``:

    * ``calls`` — one ``(call_id, name, arguments_json)`` tuple per
      ``<atem:invoke>``, in document order. ``arguments_json`` is a JSON
      object string (``json.dumps`` of the parsed parameters), matching the
      shape ``ToolCall.arguments`` / ``Function.arguments`` already expect
      elsewhere in this codebase — every value inside it is a string (see
      module docstring).
    * ``content`` — ``text`` with every well-formed call block, and any
      malformed near-miss markup, removed. What's left is what the user
      would actually see.
    * ``malformed`` — human-readable diagnostics for call-shaped markup that
      didn't parse (see module docstring). Empty when nothing looked broken.
    """
    if not isinstance(text, str) or not text.strip():
        return [], (text or ""), []

    calls: list[tuple[str, str, str]] = []
    malformed: list[str] = []
    spans: list[tuple[int, int]] = []

    for block in _BLOCK_RE.finditer(text):
        spans.append(block.span())
        body = block.group(1)
        invokes = list(_INVOKE_RE.finditer(body))
        if not invokes:
            malformed.append(
                f"{CALLS_OPEN} block contains no <atem:invoke>; the model opened a call "
                "block and wrote nothing callable in it"
            )
            continue
        for invoke in invokes:
            name = invoke.group(1).strip()
            if not name:
                malformed.append("<atem:invoke> has an empty name attribute")
                continue
            raw_params = {p.group(1).strip(): p.group(2) for p in _PARAM_RE.finditer(invoke.group(2))}
            leftover = _PARAM_RE.sub("", invoke.group(2)).strip()
            if leftover and "<atem:parameter" in leftover:
                # An unterminated parameter: its value swallowed the rest of the invoke.
                malformed.append(
                    f"{name}: an <atem:parameter> is not closed, so its value ran to the end "
                    "of the call"
                )
                continue
            call_id = f"{call_id_prefix}_{len(calls) + 1}"
            calls.append((call_id, name, json.dumps(raw_params, ensure_ascii=False)))

    remainder = _strip_spans(text, spans)

    if _UNCLOSED_RE.search(remainder):
        malformed.append(
            f"{CALLS_OPEN} was opened and never closed, so the call is truncated. An ATEM "
            "call is several times longer than the JSON equivalent, which makes this the "
            "likeliest way a turn breaks under a token limit."
        )
    for loose in _LOOSE_RE.finditer(remainder):
        malformed.append(
            f"{loose.group(0)[:40]!r} is call-shaped but not a call. The namespace prefix is "
            "part of the tag: `<function_calls>` and `<atem :invoke>` are both unparseable."
        )

    cleaned = _LOOSE_RE.sub("", _UNCLOSED_RE.sub("", remainder)).strip()
    return calls, cleaned, malformed


def _strip_spans(text: str, spans: list[tuple[int, int]]) -> str:
    out: list[str] = []
    cursor = 0
    for start, end in spans:
        out.append(text[cursor:start])
        cursor = end
    out.append(text[cursor:])
    return "".join(out)


__all__ = ["CALLS_CLOSE", "CALLS_OPEN", "extract_atem_tool_calls", "is_muse_glimmer_model"]
