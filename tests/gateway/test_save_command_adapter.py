"""#88713 — /save must resolve its adapter the way every other handler does.

``_handle_save_command`` called ``self.get_adapter(...)`` — a method that
does not exist on GatewayRunner — so every export died as
``Error exporting session: 'GatewayRunner' object has no attribute
'get_adapter'`` before the document was ever sent.
"""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

_repo = str(Path(__file__).resolve().parents[2])
if _repo not in sys.path:
    sys.path.insert(0, _repo)

from gateway.slash_commands import GatewaySlashCommandsMixin


def _runner(tmp_path):
    """Minimal runner exercising _handle_save_command's document branch."""

    class _Runner(GatewaySlashCommandsMixin):
        def __init__(self):
            entry = SimpleNamespace(session_id="20260817_sess1")
            self.async_session_store = MagicMock()
            self.async_session_store.get_or_create_session = AsyncMock(
                return_value=entry
            )
            self._session_db = MagicMock()
            self._session_db.export_session = AsyncMock(
                return_value={
                    "id": "20260817_sess1",
                    "title": "S",
                    "model": "m",
                    "started_at": 1755100000,
                    "messages": [
                        {"role": "user", "content": "hello"},
                        {"role": "assistant", "content": "hi"},
                    ],
                }
            )
            self.adapters = MagicMock()

    return _Runner()


def _event(platform="telegram"):
    event = MagicMock()
    event.get_command_args.return_value = "json"
    event.source = SimpleNamespace(
        platform=platform, chat_id="12345", user_id="u1"
    )
    return event


@pytest.mark.asyncio
async def test_save_sends_document_via_adapters_dict(tmp_path):
    runner = _runner(tmp_path)
    adapter = MagicMock()
    adapter.send_document = AsyncMock(return_value=True)
    runner.adapters.get = MagicMock(return_value=adapter)

    result = await runner._handle_save_command(_event())

    assert result == "Export complete."
    runner.adapters.get.assert_called_once_with("telegram")
    adapter.send_document.assert_awaited_once()
    kwargs = adapter.send_document.await_args.kwargs
    assert kwargs["chat_id"] == "12345"
    assert kwargs["file_name"].endswith(".json")


@pytest.mark.asyncio
async def test_save_reports_missing_adapter_without_raising(tmp_path):
    runner = _runner(tmp_path)
    runner.adapters.get = MagicMock(return_value=None)

    result = await runner._handle_save_command(_event("signal"))

    assert result == "Platform adapter not found to send the document."
    runner.adapters.get.assert_called_once_with("signal")


@pytest.mark.asyncio
async def test_save_never_touches_nonexistent_get_adapter(tmp_path):
    """The pre-fix crash: the mixin must not reference get_adapter at all."""
    import inspect

    src = inspect.getsource(
        GatewaySlashCommandsMixin._handle_save_command
    )
    assert "self.get_adapter" not in src
