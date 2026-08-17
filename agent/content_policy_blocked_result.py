"""Content-policy blocked turn-result builder — R1 extraction.

Extracted byte-verbatim from agent/conversation_loop.py lines 1043-1065
(pin ee4bb75b532e932a1055d9a710802a7435163b6a) into its own module. The pure
dict-builder uses only typing names (Dict/Any/List).
agent.conversation_loop keeps a real module-level binding (import) to this
function, preserving the module-path contract for consumers (run_agent.py,
cli.py, turn_finalizer.py, context_compressor.py, acp_adapter/server.py,
gateway/run.py, tui_gateway/server.py) and module-object patch consumers.
"""

from typing import Any, Dict, List


def _content_policy_blocked_result(
    messages: List[Dict],
    api_call_count: int,
    *,
    final_response: str,
    error_detail: str,
) -> Dict[str, Any]:
    """Build the terminal turn result for a content-policy block.

    A content-policy refusal is deterministic for the unchanged prompt, so the
    turn ends here (no retry). Both the HTTP-200 refusal handler and the
    exception-path handler return the identical shape — a failed, non-completed
    turn carrying the user-facing message and a ``content_policy_blocked:``
    prefixed error — so they funnel through this one builder.
    """
    return {
        "final_response": final_response,
        "messages": messages,
        "api_calls": api_call_count,
        "completed": False,
        "failed": True,
        "error": f"content_policy_blocked: {error_detail}",
    }
