"""Focused seam tests for the Discord command-payload extraction."""

import ast
import hashlib
import inspect
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.discord import adapter as adapter_module
from plugins.platforms.discord import command_sync
from plugins.platforms.discord.adapter import DiscordAdapter


@pytest.fixture
def adapter():
    instance = DiscordAdapter(PlatformConfig(enabled=True, token="fake-token"))
    instance._sleep_between_command_sync_mutations = AsyncMock()
    return instance


class _DesiredCommand:
    def __init__(self, payload):
        self.payload = dict(payload)

    def to_dict(self, _tree):
        return dict(self.payload)


class _ExistingCommand:
    def __init__(self, *, command_id="existing-id", payload=None, **attrs):
        self.id = command_id
        self.name = (payload or {}).get("name", "existing")
        self.type = (payload or {}).get("type", 1)
        self._payload = dict(payload or {"name": self.name, "type": self.type})
        for name, value in attrs.items():
            setattr(self, name, value)

    def to_dict(self):
        return dict(self._payload)


def _sync_client(desired, existing):
    tree = MagicMock()
    tree.get_commands.return_value = [_DesiredCommand(desired)]
    tree.fetch_commands = AsyncMock(return_value=existing)
    http = SimpleNamespace(
        delete_global_command=AsyncMock(),
        upsert_global_command=AsyncMock(),
        edit_global_command=AsyncMock(),
    )
    return SimpleNamespace(
        tree=tree,
        application_id="app-id",
        user=SimpleNamespace(id="user-id"),
        http=http,
    )


def test_module_helper_outputs_cover_defaults_scalars_options_and_recursion(adapter):
    helper = command_sync._CommandSyncHelpers
    nested = {
        "type": "4",
        "name": "nested",
        "description": "child",
        "required": True,
    }
    payload = {
        "type": None,
        "name": 0,
        "description": None,
        "default_member_permissions": 8,
        "dm_permission": 0,
        "nsfw": 1,
        "contexts": [4, "1"],
        "integration_types": ["3", 1],
        "options": [
            {
                "type": "3",
                "name": None,
                "description": 0,
                "required": 1,
                "autocomplete": "",
                "choices": [
                    {"name": None, "value": 7},
                    "malformed-choice",
                ],
                "channel_types": (5, 6),
                "min_value": 0,
                "max_value": "10",
                "min_length": 0,
                "max_length": "12",
                "options": [nested, "malformed-option"],
            },
            "malformed-option",
        ],
    }
    expected = {
        "type": 1,
        "name": "",
        "description": "",
        "default_member_permissions": "8",
        "dm_permission": False,
        "nsfw": True,
        "contexts": [1, 4],
        "integration_types": [1, 3],
        "options": [
            {
                "type": 3,
                "name": "",
                "description": "",
                "required": True,
                "autocomplete": False,
                "choices": [{"name": "", "value": 7}],
                "channel_types": [5, 6],
                "min_value": 0,
                "max_value": "10",
                "min_length": 0,
                "max_length": "12",
                "options": [
                    {
                        "type": 4,
                        "name": "nested",
                        "description": "child",
                        "required": True,
                        "autocomplete": False,
                        "choices": [],
                        "channel_types": [],
                        "min_value": None,
                        "max_value": None,
                        "min_length": None,
                        "max_length": None,
                        "options": [],
                    }
                ],
            }
        ],
    }

    assert helper._canonicalize_app_command_payload(adapter, payload) == expected
    assert adapter._canonicalize_app_command_payload(payload) == expected
    assert helper._canonicalize_app_command_payload(adapter, {}) == {
        "type": 1,
        "name": "",
        "description": "",
        "default_member_permissions": None,
        "dm_permission": True,
        "nsfw": False,
        "contexts": None,
        "integration_types": None,
        "options": [],
    }


def test_existing_projection_restores_conditional_attributes_and_permission_value(adapter):
    command = _ExistingCommand(
        payload={"name": "deploy", "type": 1},
        nsfw=True,
        guild_only=True,
        default_member_permissions=SimpleNamespace(value=16),
    )

    assert adapter._existing_command_to_payload(command) == {
        "name": "deploy",
        "type": 1,
        "nsfw": True,
        "dm_permission": False,
        "default_member_permissions": 16,
    }

    no_overrides = _ExistingCommand(payload={"name": "plain"}, nsfw=None, guild_only=None)
    no_overrides.default_member_permissions = None
    assert adapter._existing_command_to_payload(no_overrides) == {"name": "plain"}


def test_patchable_projection_only_contains_supported_fields(adapter):
    payload = {
        "name": "deploy",
        "description": "Deploy",
        "default_member_permissions": "32",
        "dm_permission": False,
        "nsfw": True,
        "contexts": [1],
        "integration_types": [2],
        "options": [{"type": 3, "name": "env"}],
    }

    assert adapter._patchable_app_command_payload(payload) == {
        "name": "deploy",
        "description": "Deploy",
        "options": [
            {
                "type": 3,
                "name": "env",
                "description": "",
                "required": False,
                "autocomplete": False,
                "choices": [],
                "channel_types": [],
                "min_value": None,
                "max_value": None,
                "min_length": None,
                "max_length": None,
                "options": [],
            }
        ],
    }


def test_shims_keep_signatures_static_descriptor_and_module_ownership(adapter):
    expected_parameters = {
        "_canonicalize_app_command_payload": ["self", "payload"],
        "_normalize_permissions": ["value"],
        "_existing_command_to_payload": ["self", "command"],
        "_canonicalize_app_command_option": ["self", "payload"],
        "_patchable_app_command_payload": ["self", "payload"],
    }
    for name, parameters in expected_parameters.items():
        assert list(inspect.signature(getattr(DiscordAdapter, name)).parameters) == parameters

    descriptor = inspect.getattr_static(DiscordAdapter, "_normalize_permissions")
    assert isinstance(descriptor, staticmethod)
    assert DiscordAdapter._normalize_permissions(0) == "0"
    assert adapter._normalize_permissions(None) is None
    assert "discord" not in command_sync.__dict__


@pytest.mark.asyncio
async def test_instance_monkeypatches_route_through_safe_sync_async(adapter):
    adapter._client = _sync_client(
        {"name": "deploy", "type": 1, "marker": "desired"},
        [_ExistingCommand(payload={"name": "deploy", "type": 1})],
    )
    adapter._existing_command_to_payload = MagicMock(
        return_value={"name": "deploy", "marker": "existing"}
    )
    adapter._canonicalize_app_command_payload = MagicMock(
        side_effect=lambda payload: {"marker": payload["marker"]}
    )
    adapter._patchable_app_command_payload = MagicMock(
        side_effect=lambda payload: {"name": payload["name"]}
    )

    summary = await adapter._safe_sync_slash_commands()

    assert summary["recreated"] == 1
    adapter._existing_command_to_payload.assert_called_once()
    assert adapter._canonicalize_app_command_payload.call_count == 2
    assert adapter._patchable_app_command_payload.call_count == 2
    calls = [call.args[-1] for call in adapter._client.http.delete_global_command.await_args_list]
    assert calls == ["existing-id"]
    assert adapter._client.http.upsert_global_command.await_count == 1


@pytest.mark.asyncio
async def test_class_patch_and_subclass_override_win_at_adapter_seam(monkeypatch, adapter):
    class Subclass(DiscordAdapter):
        def __init__(self, config):
            super().__init__(config)
            self.override_calls = 0

        def _canonicalize_app_command_payload(self, payload):
            self.override_calls += 1
            return super()._canonicalize_app_command_payload(payload)

    subclass = Subclass(PlatformConfig(enabled=True, token="fake-token"))
    subclass._sleep_between_command_sync_mutations = AsyncMock()
    subclass._client = _sync_client(
        {"name": "same", "type": 1},
        [_ExistingCommand(payload={"name": "same", "type": 1})],
    )
    await subclass._safe_sync_slash_commands()
    assert subclass.override_calls >= 2

    class_patch_calls = []

    def patched(self, payload):
        class_patch_calls.append(payload)
        return {
            "type": int(payload.get("type", 1) or 1),
            "name": str(payload.get("name", "") or ""),
            "description": str(payload.get("description", "") or ""),
            "default_member_permissions": None,
            "dm_permission": True,
            "nsfw": False,
            "contexts": None,
            "integration_types": None,
            "options": [],
        }

    monkeypatch.setattr(DiscordAdapter, "_canonicalize_app_command_payload", patched)
    adapter._client = _sync_client(
        {"name": "same", "type": 1},
        [_ExistingCommand(payload={"name": "same", "type": 1})],
    )
    await adapter._safe_sync_slash_commands()
    assert len(class_patch_calls) >= 2


@pytest.mark.asyncio
async def test_safe_sync_deletes_obsolete_before_creating_new_command(adapter):
    adapter._client = _sync_client(
        {"name": "new", "type": 1},
        [_ExistingCommand(command_id="old-id", payload={"name": "old", "type": 1})],
    )
    mutation_log = []

    async def delete(*args):
        mutation_log.append(("delete", args[-1]))

    async def upsert(*args):
        mutation_log.append(("create", args[-1]["name"]))

    adapter._client.http.delete_global_command = delete
    adapter._client.http.upsert_global_command = upsert
    summary = await adapter._safe_sync_slash_commands()

    assert summary["deleted"] == 1
    assert summary["created"] == 1
    assert mutation_log == [("delete", "old-id"), ("create", "new")]


def test_desired_fingerprint_uses_adapter_canonicalizer(adapter):
    tree = MagicMock()
    tree.get_commands.return_value = [_DesiredCommand({"name": "sync", "type": 1})]
    adapter._client = SimpleNamespace(tree=tree)
    adapter._canonicalize_app_command_payload = MagicMock(
        side_effect=lambda payload: {"name": payload["name"], "type": payload["type"]}
    )

    fingerprint = adapter._desired_command_sync_fingerprint()

    assert len(fingerprint) == 64
    adapter._canonicalize_app_command_payload.assert_called_once_with(
        {"name": "sync", "type": 1}
    )


@pytest.mark.asyncio
async def test_post_connect_rate_limit_records_retry_for_next_fingerprint_attempt(adapter):
    tree = MagicMock()
    tree.get_commands.return_value = [_DesiredCommand({"name": "sync", "type": 1})]
    http = SimpleNamespace(max_ratelimit_timeout=11.0)
    adapter._client = SimpleNamespace(tree=tree, application_id="app-id", http=http)

    class RateLimitError(Exception):
        retry_after = 2.5

    adapter._safe_sync_slash_commands = AsyncMock(side_effect=RateLimitError("429"))
    adapter._read_command_sync_state = MagicMock(return_value={})
    adapter._write_command_sync_state = MagicMock()

    await adapter._run_post_connect_initialization()

    adapter._safe_sync_slash_commands.assert_awaited_once()
    assert http.max_ratelimit_timeout == 11.0
    recorded_states = [call.args[0] for call in adapter._write_command_sync_state.call_args_list]
    retry_state = recorded_states[-1]["app-id"]
    assert retry_state["retry_after"] == 2.5
    assert retry_state["fingerprint"]


def test_package_registration_and_adapter_entry_point_remain_unchanged():
    from plugins.platforms.discord import register

    assert register is adapter_module.register
    assert adapter_module.__name__ == "plugins.platforms.discord.adapter"


_EXTRACTED_HELPER_NAMES = (
    "_canonicalize_app_command_payload",
    "_normalize_permissions",
    "_existing_command_to_payload",
    "_canonicalize_app_command_option",
    "_patchable_app_command_payload",
)


def _class_method(tree, class_name, method_name):
    classes = [
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == class_name
    ]
    assert len(classes) == 1, f"expected one {class_name} class"
    methods = [
        node
        for node in classes[0].body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == method_name
    ]
    assert len(methods) == 1, f"expected one {method_name} method"
    return methods[0]


def _adapter_shim_calls(tree):
    calls = {}
    for name in _EXTRACTED_HELPER_NAMES:
        method = _class_method(tree, "DiscordAdapter", name)
        returns = [node.value for node in method.body if isinstance(node, ast.Return)]
        assert len(returns) == 1, f"{name} must have one forwarding return"
        call = returns[0]
        assert isinstance(call, ast.Call), f"{name} must forward by call"
        assert isinstance(call.func, ast.Attribute)
        assert isinstance(call.func.value, ast.Name)
        assert call.func.value.id == "_CommandSyncHelpers"
        assert call.func.attr == name
        if name == "_normalize_permissions":
            assert call.args and isinstance(call.args[0], ast.Name)
            assert call.args[0].id == "value"
        else:
            assert call.args and isinstance(call.args[0], ast.Name)
            assert call.args[0].id == "self", f"{name} must preserve dynamic self dispatch"
        calls[name] = call
    return calls


def test_extracted_helpers_match_pinned_semantic_hashes():
    pinned_hashes = {
        "_canonicalize_app_command_payload": (
            "45bdde78c43693a12bdf73da7eea85f459c2f69b452160cd5917c590599d4c5d"
        ),
        "_normalize_permissions": (
            "f5b113aa2aa7c8595b90097d0da81b739484d0194fc8fdbaa54249eb2c41cc86"
        ),
        "_existing_command_to_payload": (
            "59093d528f0fc9814f6465a04c033b02783700f81e462e237a3feea2685c517e"
        ),
        "_canonicalize_app_command_option": (
            "9d0e9329d0f9eebdfcf21a17f2110c8938d2b61583a775fcc7afe4bd46e45d83"
        ),
        "_patchable_app_command_payload": (
            "dcf146b8e8ecf16bb46967e238b89852a97f58b15732aa2a5aba0073e56cca1d"
        ),
    }
    current_source = Path(command_sync.__file__).read_text(encoding="utf-8")
    current_tree = ast.parse(current_source)
    for helper_name in _EXTRACTED_HELPER_NAMES:
        current = _class_method(current_tree, "_CommandSyncHelpers", helper_name)
        semantic_source = ast.unparse(current).encode("utf-8")
        assert hashlib.sha256(semantic_source).hexdigest() == pinned_hashes[helper_name], (
            f"{helper_name} implementation diverged from pinned semantic golden"
        )

    adapter_tree = ast.parse(
        Path(adapter_module.__file__).read_text(encoding="utf-8")
    )
    _adapter_shim_calls(adapter_tree)
    for name in _EXTRACTED_HELPER_NAMES:
        expected = (
            ["value"]
            if name == "_normalize_permissions"
            else ["self", "command"]
            if name == "_existing_command_to_payload"
            else ["self", "payload"]
        )
        assert list(inspect.signature(getattr(DiscordAdapter, name)).parameters) == expected
    assert isinstance(inspect.getattr_static(DiscordAdapter, "_normalize_permissions"), staticmethod)


def test_fresh_import_boundary_works_without_discord_available():
    probe = r'''
import importlib.abc
import sys


class DiscordBlocker(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "discord" or fullname.startswith("discord."):
            raise ImportError("discord intentionally blocked")


sys.meta_path.insert(0, DiscordBlocker())
for module_name in list(sys.modules):
    if module_name == "discord" or module_name.startswith("discord."):
        del sys.modules[module_name]

from plugins.platforms.discord import adapter, command_sync

assert command_sync.__spec__ is not None
assert "discord" not in command_sync.__dict__
assert not any(
    name == "discord" or name.startswith("discord.") for name in sys.modules
)
assert adapter._CommandSyncHelpers is command_sync._CommandSyncHelpers
assert all(
    hasattr(adapter.DiscordAdapter, name)
    for name in (
        "_canonicalize_app_command_payload",
        "_normalize_permissions",
        "_existing_command_to_payload",
        "_canonicalize_app_command_option",
        "_patchable_app_command_payload",
    )
)
'''
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
