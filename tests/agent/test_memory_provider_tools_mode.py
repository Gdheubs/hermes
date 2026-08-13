"""Regression coverage for tools-only external memory providers."""

import json

from agent.memory_manager import MemoryManager
from agent.memory_provider import MemoryProvider


class _Provider(MemoryProvider):
    def __init__(self):
        self.init_kwargs = {}
        self.prefetch_queries = []
        self.queued_prefetches = []
        self.synced_turns = []
        self.turn_starts = []
        self.session_end_called = False
        self.pre_compress_called = False
        self.memory_writes = []

    @property
    def name(self):
        return "external"

    def is_available(self):
        return True

    def initialize(self, session_id, **kwargs):
        self.init_kwargs = {"session_id": session_id, **kwargs}

    def system_prompt_block(self):
        return "must not enter cron prompt"

    def prefetch(self, query, *, session_id=""):
        self.prefetch_queries.append(query)
        return "must not be recalled automatically"

    def queue_prefetch(self, query, *, session_id=""):
        self.queued_prefetches.append(query)

    def sync_turn(self, user_content, assistant_content, *, session_id="", messages=None):
        self.synced_turns.append((user_content, assistant_content))

    def get_tool_schemas(self):
        return [{"name": "external_memory", "description": "x", "parameters": {}}]

    def handle_tool_call(self, tool_name, args, **kwargs):
        return json.dumps({"handled": tool_name})

    def on_turn_start(self, turn_number, message, **kwargs):
        self.turn_starts.append((turn_number, message))

    def on_session_end(self, messages):
        self.session_end_called = True

    def on_pre_compress(self, messages):
        self.pre_compress_called = True
        return "should not be returned"

    def on_memory_write(self, action, target, content, metadata=None):
        self.memory_writes.append((action, target, content))


def test_tools_mode_keeps_explicit_tool_dispatch_but_blocks_lifecycle():
    provider = _Provider()
    manager = MemoryManager(mode="tools")
    manager.add_provider(provider)
    manager.initialize_all("cron-test")

    assert provider.init_kwargs["memory_provider_mode"] == "tools"
    assert manager.build_system_prompt() == ""
    assert manager.prefetch_all("query") == ""
    manager.queue_prefetch_all("query")
    manager.sync_all("user", "assistant")
    manager.on_turn_start(1, "user")
    manager.on_session_end([])
    assert manager.on_pre_compress([]) == ""
    manager.on_memory_write("add", "memory", "not mirrored")
    manager.on_delegation("task", "result")
    assert provider.prefetch_queries == []
    assert provider.queued_prefetches == []
    assert provider.synced_turns == []
    assert provider.turn_starts == []
    assert provider.session_end_called is False
    assert provider.pre_compress_called is False
    assert provider.memory_writes == []

    assert json.loads(manager.handle_tool_call("external_memory", {"query": "x"})) == {
        "handled": "external_memory"
    }
