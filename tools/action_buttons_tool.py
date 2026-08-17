#!/usr/bin/env python3
"""Telegram-style fixed action buttons tool.

This is a sibling of ``clarify`` for the ClaudeCLAW-style UX:

- the message body contains the full numbered options;
- the buttons themselves are only short numeric selectors (``1``, ``2``, ``3``);
- there is no automatic "Other" free-text branch.

The actual rendering/interaction is delegated to a platform-provided callback,
same as the clarify tool.
"""

from __future__ import annotations

import json
from typing import Callable, List, Optional

from tools.clarify_tool import _flatten_choice
from tools.registry import registry, tool_error

MAX_CHOICES = 4


def action_buttons_tool(
    question: str,
    choices: List[str],
    callback: Optional[Callable] = None,
) -> str:
    if not question or not question.strip():
        return tool_error("Question text is required.")
    if not isinstance(choices, list):
        return tool_error("choices must be a list of strings.")

    normalized = [s for s in (_flatten_choice(c) for c in choices) if s]
    if len(normalized) < 1:
        return tool_error("At least one choice is required.")
    if len(normalized) > MAX_CHOICES:
        normalized = normalized[:MAX_CHOICES]

    if callback is None:
        return json.dumps(
            {"error": "Action buttons are not available in this execution context."},
            ensure_ascii=False,
        )

    try:
        user_response = callback(question.strip(), normalized)
    except Exception as exc:
        return json.dumps(
            {"error": f"Failed to get user input: {exc}"},
            ensure_ascii=False,
        )

    return json.dumps(
        {
            "question": question.strip(),
            "choices_offered": normalized,
            "user_response": str(user_response).strip(),
        },
        ensure_ascii=False,
    )


ACTION_BUTTONS_SCHEMA = {
    "name": "action_buttons",
    "description": (
        "Present 1-4 fixed answer choices as compact action buttons. Use this "
        "when the message body already explains the options and the visible "
        "buttons should be only short numeric selectors like 1, 2, 3. Unlike "
        "clarify multiple-choice, this tool does NOT add an automatic 'Other' "
        "free-text option."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "The prompt question only. Do not embed the options in this field.",
            },
            "choices": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "maxItems": MAX_CHOICES,
                "description": (
                    "The full semantic options. The UI may render only numeric "
                    "button labels, but the resolved answer returns the full "
                    "selected choice text."
                ),
            },
        },
        "required": ["question", "choices"],
    },
}


registry.register(
    name="action_buttons",
    toolset="clarify",
    schema=ACTION_BUTTONS_SCHEMA,
    handler=lambda args, **kw: action_buttons_tool(
        question=args.get("question", ""),
        choices=args.get("choices") or [],
        callback=kw.get("callback") or kw.get("action_buttons_callback"),
    ),
    check_fn=lambda: True,
    emoji="🔘",
)
