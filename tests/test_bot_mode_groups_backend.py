from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

from fastapi import FastAPI
from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_API = ROOT / "plugins" / "hermes-bots" / "dashboard" / "plugin_api.py"


def _load_api():
    spec = importlib.util.spec_from_file_location("hermes_bots_groups_plugin_api_test", PLUGIN_API)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _client(tmp_path: Path):
    api = _load_api()
    service = api.GroupService(api.GroupStore(tmp_path / "groups.sqlite3"))
    app = FastAPI()
    app.include_router(api.create_router(lambda: service))
    return TestClient(app), service


def test_durable_groups_api_full_lifecycle(tmp_path: Path) -> None:
    client, _service = _client(tmp_path)

    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"

    reconciled = client.post(
        "/bots/reconcile",
        json={
            "bots": [
                {"profile_name": "alpha", "instance_id": None},
                {"profile_name": "beta", "instance_id": None},
                {"profile_name": "gamma", "instance_id": None},
            ]
        },
    )
    assert reconciled.status_code == 200
    identities = {row["profile_name"]: row["instance_id"] for row in reconciled.json()["bots"]}
    assert set(identities) == {"alpha", "beta", "gamma"}
    assert len(set(identities.values())) == 3

    create_payload = {
        "name": "Build Crew",
        "color": "purple",
        "icon_kind": "emoji",
        "icon_value": "🛠️",
        "members": [
            {"profile_name": "alpha", "bot_instance_id": identities["alpha"]},
            {"profile_name": "beta", "bot_instance_id": identities["beta"]},
        ],
        "leader_bot_instance_id": identities["alpha"],
        "idempotency_key": "create-build-crew",
    }
    created = client.post("/groups", json=create_payload)
    assert created.status_code == 201
    group = created.json()
    group_id = group["id"]
    assert group["revision"] == 1
    assert group["leader_bot_instance_id"] == identities["alpha"]
    assert [member["profile_name"] for member in group["members"]] == ["alpha", "beta"]

    replay = client.post("/groups", json=create_payload)
    assert replay.status_code == 201
    assert replay.json()["id"] == group_id
    assert len(client.get("/groups").json()) == 1

    renamed = client.patch(
        f"/groups/{group_id}",
        json={
            "expected_revision": 1,
            "name": "Ship Crew",
            "color": "green",
            "icon_kind": "emoji",
            "icon_value": "🚀",
            "idempotency_key": "rename-build-crew",
        },
    )
    assert renamed.status_code == 200
    group = renamed.json()
    assert group["name"] == "Ship Crew"
    assert group["revision"] == 2

    stale = client.patch(
        f"/groups/{group_id}",
        json={
            "expected_revision": 1,
            "name": "Should Fail",
            "color": "green",
            "icon_kind": "emoji",
            "icon_value": "🚀",
        },
    )
    assert stale.status_code == 409

    membership = client.put(
        f"/groups/{group_id}/membership",
        json={
            "expected_revision": 2,
            "members": [
                {"profile_name": "beta", "bot_instance_id": identities["beta"]},
                {"profile_name": "gamma", "bot_instance_id": identities["gamma"]},
            ],
            "leader_bot_instance_id": identities["beta"],
            "idempotency_key": "replace-members",
        },
    )
    assert membership.status_code == 200
    group = membership.json()
    assert group["revision"] == 3
    assert group["leader_bot_instance_id"] == identities["beta"]
    assert [member["profile_name"] for member in group["members"]] == ["beta", "gamma"]

    user_message = client.post(
        f"/groups/{group_id}/messages",
        json={"content": "Ship it.", "idempotency_key": "user-message"},
    )
    assert user_message.status_code == 201
    assert user_message.json()["sender_profile_name"] is None

    bot_message = client.post(
        f"/groups/{group_id}/messages",
        json={
            "content": "On it.",
            "sender_bot_instance_id": identities["beta"],
            "idempotency_key": "bot-message",
        },
    )
    assert bot_message.status_code == 201
    assert bot_message.json()["sender_profile_name"] == "beta"

    messages = client.get(f"/groups/{group_id}/messages")
    assert messages.status_code == 200
    assert [message["content"] for message in messages.json()] == ["Ship it.", "On it."]

    deleted = client.request(
        "DELETE",
        f"/groups/{group_id}",
        json={"expected_revision": 3, "idempotency_key": "delete-group"},
    )
    assert deleted.status_code == 204
    assert client.get("/groups").json() == []
    assert client.get(f"/groups/{group_id}").status_code == 410


def test_bot_identity_reconciliation_is_stable_and_clone_safe(tmp_path: Path) -> None:
    client, _service = _client(tmp_path)

    first = client.post(
        "/bots/reconcile",
        json={"bots": [{"profile_name": "alpha", "instance_id": None}]},
    ).json()["bots"][0]
    same = client.post(
        "/bots/reconcile",
        json={"bots": [{"profile_name": "alpha", "instance_id": first["instance_id"]}]},
    ).json()["bots"][0]
    assert same["instance_id"] == first["instance_id"]

    # Simulate profile metadata copied while cloning alpha -> alpha-copy. The
    # backend must split identity rather than let two durable members alias.
    clone_rows = client.post(
        "/bots/reconcile",
        json={
            "bots": [
                {"profile_name": "alpha", "instance_id": first["instance_id"]},
                {"profile_name": "alpha-copy", "instance_id": first["instance_id"]},
            ]
        },
    ).json()["bots"]
    by_name = {row["profile_name"]: row["instance_id"] for row in clone_rows}
    assert by_name["alpha"] == first["instance_id"]
    assert by_name["alpha-copy"] != first["instance_id"]
