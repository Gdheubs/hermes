"""Signal single-recipient JSON-RPC params must be scalar strings, not lists.

signal-cli's single-recipient commands (``send``, ``sendTyping``,
``sendReaction``) resolve their ``recipient`` field via
``CommandUtil.getSingleRecipientIdentifiers``, which expects a scalar string
identifier. The adapter historically wrapped the recipient in a single-element
list (``[recipient]``). That shape is not part of signal-cli's single-recipient
contract and fails on some builds — observed in the wild as::

    ERROR SignalJsonRpcDispatcherHandler - Command execution failed
    java.lang.ClassCastException: java.util.LinkedHashMap cannot be cast to
        java.lang.String
        at org.asamk.signal.util.CommandUtil.getSingleRecipientIdentifiers(
            CommandUtil.java:94)
        at org.asamk.signal.commands.SendTypingCommand.handleCommand(
            SendTypingCommand.java:52)

These tests pin the invariant: for a direct (non-group) chat the adapter must
set ``params["recipient"]`` to a scalar ``str`` across every single-recipient
command path. They assert a behaviour contract (shape + resolution semantics),
not a snapshot of any particular value.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Ensure the repo root is importable when tests run in isolation.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from gateway.platforms.signal import SignalAdapter  # noqa: E402


def _make_adapter() -> SignalAdapter:
    """Construct a SignalAdapter without touching the network or config files.

    ``__init__`` only reads plain attributes off ``PlatformConfig`` and some
    env vars; nothing is dialled until ``connect``/``_rpc`` is called. We build
    a minimal duck-typed config so the constructor runs, then exercise only the
    pure ``_set_direct_recipient_param`` helper.
    """
    cfg = MagicMock()
    cfg.extra = {"http_url": "http://127.0.0.1:8080", "account": "+15550000000"}
    adapter = SignalAdapter(cfg)
    return adapter


@pytest.mark.asyncio
async def test_direct_recipient_is_scalar_string_for_send():
    """Send path: recipient must be a scalar str, never a list."""
    adapter = _make_adapter()
    # A raw service-id / non-e164 chat_id short-circuits _resolve_recipient,
    # so no RPC is issued and the value passes through unchanged.
    chat_id = "abcd1234-0000-0000-0000-000000000000"
    params: dict = {}

    recipient = await adapter._set_direct_recipient_param(params, chat_id)

    assert params["recipient"] == chat_id
    assert isinstance(params["recipient"], str)
    assert not isinstance(params["recipient"], list)
    assert recipient == chat_id


@pytest.mark.asyncio
async def test_direct_recipient_resolves_number_to_service_id():
    """When resolve=True, the helper runs the chat_id through _resolve_recipient."""
    adapter = _make_adapter()

    async def _fake_resolve(cid: str) -> str:
        return "resolved-service-id"

    adapter._resolve_recipient = _fake_resolve  # type: ignore[assignment]
    params: dict = {}

    recipient = await adapter._set_direct_recipient_param(params, "+15551234567")

    assert params["recipient"] == "resolved-service-id"
    assert isinstance(params["recipient"], str)
    assert recipient == "resolved-service-id"


@pytest.mark.asyncio
async def test_reaction_recipient_is_scalar_and_unresolved():
    """Reaction path passes the raw chat_id (resolve=False) as a scalar str."""
    adapter = _make_adapter()

    async def _boom(cid: str) -> str:  # pragma: no cover - must NOT be called
        raise AssertionError("_resolve_recipient must not run when resolve=False")

    adapter._resolve_recipient = _boom  # type: ignore[assignment]
    chat_id = "+15559876543"
    params: dict = {}

    recipient = await adapter._set_direct_recipient_param(
        params, chat_id, resolve=False
    )

    assert params["recipient"] == chat_id
    assert isinstance(params["recipient"], str)
    assert recipient == chat_id


def test_no_list_form_recipient_remains_in_source():
    """Guard against regressions: no single-recipient list form in the adapter.

    A behaviour contract on the source itself — every direct-recipient param
    assignment must go through the scalar helper, so the list literal patterns
    that produced the ClassCastException must not reappear.
    """
    src = (
        _REPO_ROOT / "gateway" / "platforms" / "signal.py"
    ).read_text(encoding="utf-8")
    assert 'params["recipient"] = [await self._resolve_recipient(chat_id)]' not in src
    assert 'base_params["recipient"] = [await self._resolve_recipient(chat_id)]' not in src
    assert 'params["recipient"] = [chat_id]' not in src
