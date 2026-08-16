"""HTTP and authorization conformance for the execution read contract."""

from __future__ import annotations

import json
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from jsonschema import Draft202012Validator

from gateway.config import (
    GatewayConfig,
    Platform,
    PlatformConfig,
    _apply_env_overrides,
)
from gateway.platforms.api_server import APIServerAdapter, _api_request_profile
from hermes_cli.execution_contract import (
    CONTRACT_VERSION,
    ContractDataError,
    ContractRateLimitedError,
    ContractUnavailableError,
    canonical_digest,
    contract_schema,
)


FULL_KEY = "full-mutation-key-0123456789abcdef"
READ_KEY = "execution-read-key-0123456789abcdef"
NOW = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)


def _auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _assert_closed_error(payload: dict) -> None:
    Draft202012Validator(contract_schema()["$defs"]["errorDetail"]).validate(payload)


@pytest.fixture
def contract_adapter(monkeypatch):
    monkeypatch.delenv("API_SERVER_KEY", raising=False)
    monkeypatch.delenv("API_SERVER_READ_KEY", raising=False)
    adapter = APIServerAdapter(
        PlatformConfig(
            enabled=True,
            extra={"key": FULL_KEY, "read_key": READ_KEY},
        )
    )
    try:
        yield adapter
    finally:
        adapter._response_store.close()


def _contract_app(adapter: APIServerAdapter) -> web.Application:
    app = web.Application(
        middlewares=[
            adapter._make_profile_prefix_middleware(),
            adapter._make_execution_contract_router_error_middleware(),
        ]
    )
    wanted = {
        ("GET", "/v1/capabilities"),
        ("GET", "/v1/models"),
        ("GET", "/v1/execution-contract/capabilities"),
        ("GET", "/v1/execution-contract/schema"),
        ("GET", "/v1/execution-contract/executions"),
        ("GET", "/v1/execution-contract/executions/{execution_id}"),
        ("GET", "/v1/execution-contract/decisions"),
        ("GET", "/v1/execution-contract/decisions/{decision_id}"),
        ("GET", "/v1/execution-contract/events"),
        ("GET", "/v1/execution-contract/receipts"),
        ("GET", "/v1/execution-contract/receipts/{receipt_id}"),
        ("POST", "/v1/runs"),
        ("POST", "/v1/runs/{run_id}/approval"),
        ("POST", "/v1/runs/{run_id}/steer"),
        ("POST", "/v1/runs/{run_id}/stop"),
        ("POST", "/v1/responses"),
    }
    for method, path, handler in adapter._http_route_table():
        if (method, path) in wanted:
            app.router.add_route(method, path, handler)
            app.router.add_route(method, f"/p/{{profile}}{path}", handler)
    return app


@pytest.mark.asyncio
async def test_read_key_is_side_effect_free_and_returns_asserted_authority(
    contract_adapter,
):
    store = contract_adapter._execution_contract_store()
    assert not store.database_path.exists()
    store.initialize()
    ledger_mtime = store.database_path.stat().st_mtime_ns
    app = _contract_app(contract_adapter)

    async with TestClient(TestServer(app)) as client:
        capabilities_response = await client.get(
            "/v1/execution-contract/capabilities",
            headers=_auth(READ_KEY),
        )
        assert capabilities_response.status == 200
        capabilities = await capabilities_response.json()
        assert capabilities["contract_version"] == CONTRACT_VERSION
        assert capabilities["authority"] == store.authority.public()
        assert capabilities["authority"]["profile_id"].startswith(
            "hermes-profile-instance:"
        )
        assert not capabilities["authority"]["profile_id"].endswith(":default")
        assert capabilities["features"]["action_dispatch"] is False

        list_response = await client.get(
            "/v1/execution-contract/executions",
            headers=_auth(READ_KEY),
        )
        assert list_response.status == 200
        payload = await list_response.json()
        assert payload["executions"] == []
        assert payload["page"]["completeness"] == "complete"
        assert list_response.headers["Hermes-Execution-Contract-Version"] == (
            CONTRACT_VERSION
        )
        assert list_response.headers["Cache-Control"] == "no-store"

        schema_response = await client.get(
            "/v1/execution-contract/schema",
            headers=_auth(FULL_KEY),
        )
        assert schema_response.status == 200
        schema = await schema_response.json()
        assert schema["$defs"]["receipt"]["additionalProperties"] is False

    assert store.database_path.stat().st_mtime_ns == ledger_mtime


@pytest.mark.asyncio
async def test_read_scope_is_accepted_only_for_contract_gets(contract_adapter):
    app = _contract_app(contract_adapter)
    async with TestClient(TestServer(app)) as client:
        allowed = await client.get(
            "/v1/capabilities",
            headers=_auth(READ_KEY),
        )
        assert allowed.status == 200

        forbidden_get = await client.get("/v1/models", headers=_auth(READ_KEY))
        assert forbidden_get.status == 403

        writes = [
            ("/v1/runs", {"input": "synthetic"}),
            ("/v1/runs/run-synthetic/approval", {"choice": "deny"}),
            ("/v1/runs/run-synthetic/steer", {"message": "synthetic"}),
            ("/v1/runs/run-synthetic/stop", {}),
            ("/v1/responses", {"input": "synthetic"}),
        ]
        for path, body in writes:
            response = await client.post(path, json=body, headers=_auth(READ_KEY))
            assert response.status == 403, path
            payload = await response.json()
            assert payload["error"]["granted_scope"] == "execution:read"

        missing = await client.get("/v1/execution-contract/executions")
        invalid = await client.get(
            "/v1/execution-contract/executions",
            headers=_auth("wrong-token-0123456789abcdef"),
        )
        assert missing.status == invalid.status == 401
        _assert_closed_error(await missing.json())
        _assert_closed_error(await invalid.json())


@pytest.mark.asyncio
async def test_http_contract_validation_not_found_conflict_and_profile_denial(
    contract_adapter,
):
    store = contract_adapter._execution_contract_store()
    execution = store.create_execution(
        lifecycle="queued",
        source_run_id="run-http",
        work_ref="work:http",
        proposal_ref="proposal:http",
        now=NOW,
    )
    app = _contract_app(contract_adapter)
    async with TestClient(TestServer(app)) as client:
        detail = await client.get(
            f"/v1/execution-contract/executions/{execution['execution_id']}",
            headers=_auth(READ_KEY),
        )
        assert detail.status == 200
        assert (await detail.json())["execution"]["source_run_id"] == "run-http"

        malformed = await client.get(
            "/v1/execution-contract/executions?lifecycle=future_state",
            headers=_auth(READ_KEY),
        )
        assert malformed.status == 400
        _assert_closed_error(await malformed.json())

        unknown_version = await client.get(
            "/v1/execution-contract/executions",
            headers={
                **_auth(READ_KEY),
                "Hermes-Execution-Contract-Version": "hermes.execution.read.v999",
            },
        )
        assert unknown_version.status == 409
        _assert_closed_error(await unknown_version.json())

        unknown_id = (
            f"exe_{store.authority.profile_key}_" + "f" * 32
        )
        missing = await client.get(
            f"/v1/execution-contract/executions/{unknown_id}",
            headers=_auth(READ_KEY),
        )
        assert missing.status == 404
        _assert_closed_error(await missing.json())

        foreign_id = "exe_abcdefabcdef_" + "0" * 32
        forbidden = await client.get(
            f"/v1/execution-contract/executions/{foreign_id}",
            headers=_auth(READ_KEY),
        )
        assert forbidden.status == 403
        _assert_closed_error(await forbidden.json())


@pytest.mark.asyncio
async def test_cursor_gone_rate_limit_corruption_and_unavailable_statuses(
    contract_adapter,
    monkeypatch,
):
    store = contract_adapter._execution_contract_store()
    old = NOW - timedelta(days=40)
    execution = store.create_execution(lifecycle="running", now=old)
    store.transition_execution(
        execution["execution_id"],
        "terminal_failed",
        now=old + timedelta(seconds=1),
    )
    assert store.prune_events(now=NOW) == 2
    app = _contract_app(contract_adapter)

    async with TestClient(TestServer(app)) as client:
        gone = await client.get(
            "/v1/execution-contract/events?after=0",
            headers=_auth(READ_KEY),
        )
        assert gone.status == 410
        gone_payload = await gone.json()
        _assert_closed_error(gone_payload)
        assert gone_payload["error"]["minimum_available"] == 3

        class FailingStore:
            authority = store.authority

            def __init__(self, error):
                self.error = error

            def list_executions(self, **_kwargs):
                raise self.error

        for error, status in (
            (ContractRateLimitedError("synthetic busy"), 429),
            (ContractDataError("synthetic corrupt secret detail"), 500),
            (ContractUnavailableError("synthetic unavailable"), 503),
        ):
            monkeypatch.setattr(
                contract_adapter,
                "_execution_contract_store",
                lambda error=error: FailingStore(error),
            )
            response = await client.get(
                "/v1/execution-contract/executions",
                headers=_auth(READ_KEY),
            )
            assert response.status == status
            payload = await response.json()
            _assert_closed_error(payload)
            if status >= 500:
                assert payload["error"]["message"] == "Execution contract unavailable"
            if status == 429:
                assert response.headers["Retry-After"] == "1"


def test_process_local_and_projection_state_cannot_publish_receipts(contract_adapter):
    context = {
        "work_ref": "work:synthetic",
        "proposal_ref": "proposal:synthetic",
        "effect_id": "effect:synthetic",
    }
    execution = contract_adapter._create_execution_contract_run(
        "run-unproven",
        context,
    )
    contract_adapter._run_statuses["run-unproven"] = {
        "status": "completed",
        "final_response": "chat output is not executor evidence",
        "session_history": ["synthetic transcript"],
        "kanban_summary": "synthetic operator projection",
    }
    terminal = contract_adapter._terminalize_execution_contract_run(
        "run-unproven",
        "completed",
        reason_code="process_local_completed",
    )
    assert terminal is not None
    assert terminal["execution_id"] == execution["execution_id"]
    assert terminal["lifecycle"] == "terminal_ambiguous"
    assert terminal["receipt_state"] == "unproven"
    assert contract_adapter._execution_contract_store().list_receipts()["items"] == []

    proven = contract_adapter._create_execution_contract_run("run-proven", context)
    contract_adapter._transition_execution_contract_run("run-proven", "running")
    evidence = contract_adapter.record_execution_effect_evidence(
        run_id="run-proven",
        effect_id="effect:synthetic",
        outcome="succeeded",
        subject_digest=canonical_digest({"subject": "synthetic"}),
        evidence_digest=canonical_digest({"executor_evidence": "synthetic"}),
        result_digest=canonical_digest({"result": "synthetic"}),
    )
    assert evidence["execution_id"] == proven["execution_id"]
    published = contract_adapter._terminalize_execution_contract_run(
        "run-proven",
        "completed",
    )
    assert published is not None
    assert published["receipt_state"] == "published"
    receipt = contract_adapter._execution_contract_store().get_receipt(
        published["receipt_id"]
    )
    assert receipt["effect_id"] == "effect:synthetic"


def test_execution_context_is_closed_and_requires_exact_effect_bindings():
    assert APIServerAdapter._parse_execution_contract_context({}) == {
        "work_ref": None,
        "proposal_ref": None,
        "effect_id": None,
    }
    with pytest.raises(ValueError, match="unsupported fields"):
        APIServerAdapter._parse_execution_contract_context(
            {"execution_context": {"provider": "synthetic"}}
        )
    with pytest.raises(ValueError, match="exact work_ref"):
        APIServerAdapter._parse_execution_contract_context(
            {"execution_context": {"effect_id": "effect:synthetic"}}
        )


def test_read_key_configuration_and_profile_scope_are_explicit(monkeypatch):
    monkeypatch.setenv("API_SERVER_KEY", FULL_KEY)
    monkeypatch.setenv("API_SERVER_READ_KEY", READ_KEY)
    config = GatewayConfig()
    _apply_env_overrides(config)
    api_config = config.platforms[Platform.API_SERVER]
    assert api_config.enabled is True
    assert api_config.extra["key"] == FULL_KEY
    assert api_config.extra["read_key"] == READ_KEY

    adapter = APIServerAdapter(api_config)
    try:
        assert adapter._read_api_key_passes_startup_guard() is True
        adapter._read_api_key = "weak"
        assert adapter._read_api_key_passes_startup_guard() is False

        profile_key = "worker-read-key-0123456789abcdef"
        monkeypatch.setattr(
            "agent.secret_scope.get_secret",
            lambda name, default="": profile_key
            if name == "API_SERVER_READ_KEY"
            else default,
        )
        token = _api_request_profile.set("worker")
        try:
            assert adapter._expected_read_api_key() == profile_key
        finally:
            _api_request_profile.reset(token)
    finally:
        adapter._response_store.close()


def test_equal_full_and_read_keys_fail_startup_default_and_named(monkeypatch):
    adapter = APIServerAdapter(
        PlatformConfig(
            enabled=True,
            extra={"key": FULL_KEY, "read_key": FULL_KEY},
        )
    )
    try:
        assert adapter._read_api_key_passes_startup_guard() is False

        adapter._read_api_key = READ_KEY
        monkeypatch.setattr(
            adapter,
            "_execution_contract_profiles",
            lambda: [None, "worker"],
        )
        monkeypatch.setattr(adapter, "_profile_scope", lambda _profile: nullcontext())
        monkeypatch.setattr(
            "agent.secret_scope.get_secret",
            lambda name, default="": FULL_KEY
            if name in {"API_SERVER_KEY", "API_SERVER_READ_KEY"}
            else default,
        )
        assert adapter._read_api_key_passes_startup_guard() is False
    finally:
        adapter._response_store.close()


def test_profile_authority_comes_from_active_home_not_request_name(
    contract_adapter,
    tmp_path,
):
    from gateway.run import _profile_runtime_scope

    named_home = tmp_path / "named-single-profile"
    alpha_home = tmp_path / "multiplex-alpha"
    beta_home = tmp_path / "multiplex-beta"
    for home in (named_home, alpha_home, beta_home):
        home.mkdir()

    with _profile_runtime_scope(named_home):
        first = contract_adapter._execution_contract_store()
        first.initialize()
        token = _api_request_profile.set("url-name-must-not-control-authority")
        try:
            same_home = contract_adapter._execution_contract_store()
        finally:
            _api_request_profile.reset(token)
    assert first.authority == same_home.authority
    assert first.authority.profile_name == named_home.name

    with _profile_runtime_scope(alpha_home):
        alpha = contract_adapter._execution_contract_store()
        alpha.initialize()
    with _profile_runtime_scope(beta_home):
        beta = contract_adapter._execution_contract_store()
        beta.initialize()
    assert alpha.authority.profile_id != beta.authority.profile_id
    assert alpha.authority.profile_key != beta.authority.profile_key
    assert alpha.profile_anchor_path.exists()
    assert beta.profile_anchor_path.exists()
    assert alpha.profile_anchor_path.read_bytes() != beta.profile_anchor_path.read_bytes()


@pytest.mark.asyncio
async def test_unknown_profile_route_uses_closed_contract_error(
    contract_adapter,
    monkeypatch,
):
    from gateway.platforms.api_server import _PROFILE_REJECTED

    monkeypatch.setattr(
        contract_adapter,
        "_resolve_request_profile",
        lambda _request: _PROFILE_REJECTED,
    )
    app = web.Application(
        middlewares=[
            contract_adapter._make_profile_prefix_middleware(),
            contract_adapter._make_execution_contract_router_error_middleware(),
        ]
    )
    app.router.add_get(
        "/p/{profile}/v1/execution-contract/executions",
        contract_adapter._handle_execution_contract_executions,
    )
    async with TestClient(TestServer(app)) as client:
        unauthenticated = await client.get(
            "/p/unknown/v1/execution-contract/executions"
        )
        assert unauthenticated.status == 401
        _assert_closed_error(await unauthenticated.json())
        response = await client.get(
            "/p/unknown/v1/execution-contract/executions",
            headers=_auth(READ_KEY),
        )
        assert response.status == 404
        _assert_closed_error(await response.json())


@pytest.mark.asyncio
async def test_router_404_and_405_use_closed_contract_errors_with_auth_precedence(
    contract_adapter,
):
    contract_adapter._execution_contract_store().initialize()
    app = _contract_app(contract_adapter)
    async with TestClient(TestServer(app)) as client:
        unknown_without_auth = await client.get(
            "/v1/execution-contract/not-a-route"
        )
        assert unknown_without_auth.status == 401
        _assert_closed_error(await unknown_without_auth.json())

        unknown = await client.get(
            "/v1/execution-contract/not-a-route",
            headers=_auth(READ_KEY),
        )
        assert unknown.status == 404
        unknown_payload = await unknown.json()
        _assert_closed_error(unknown_payload)
        assert unknown_payload["error"]["code"] == "execution_contract_not_found"

        wrong_method_without_auth = await client.post(
            "/v1/execution-contract/executions"
        )
        assert wrong_method_without_auth.status == 401
        _assert_closed_error(await wrong_method_without_auth.json())

        read_scope_denial = await client.post(
            "/v1/execution-contract/executions",
            headers=_auth(READ_KEY),
        )
        assert read_scope_denial.status == 403
        _assert_closed_error(await read_scope_denial.json())

        wrong_method = await client.post(
            "/v1/execution-contract/executions",
            headers=_auth(FULL_KEY),
        )
        assert wrong_method.status == 405
        wrong_method_payload = await wrong_method.json()
        _assert_closed_error(wrong_method_payload)
        assert wrong_method_payload["error"]["code"] == (
            "execution_contract_method_not_allowed"
        )

        prefixed_wrong_method = await client.post(
            "/p/default/v1/execution-contract/executions",
            headers=_auth(FULL_KEY),
        )
        assert prefixed_wrong_method.status == 405
        _assert_closed_error(await prefixed_wrong_method.json())

        malformed_without_auth = await client.get(
            "/p//v1/execution-contract/executions"
        )
        assert malformed_without_auth.status == 401
        _assert_closed_error(await malformed_without_auth.json())
        malformed = await client.get(
            "/p//v1/execution-contract/executions",
            headers=_auth(READ_KEY),
        )
        assert malformed.status == 404
        _assert_closed_error(await malformed.json())

        non_contract = await client.get(
            "/not-an-api-route",
            headers=_auth(FULL_KEY),
        )
        assert non_contract.status == 404
        assert non_contract.content_type != "application/json"


@pytest.mark.asyncio
async def test_named_multiplex_router_errors_use_named_profile_auth(
    contract_adapter,
    monkeypatch,
    tmp_path,
):
    from agent import secret_scope
    from gateway.run import _profile_runtime_scope

    worker_home = tmp_path / "profiles" / "worker"
    worker_home.mkdir(parents=True)
    worker_full_key = "worker-full-key-0123456789abcdef"
    worker_read_key = "worker-read-key-0123456789abcdef"
    (worker_home / ".env").write_text(
        f"API_SERVER_KEY={worker_full_key}\nAPI_SERVER_READ_KEY={worker_read_key}\n",
        encoding="utf-8",
    )
    contract_adapter.gateway_runner = type(
        "_Runner",
        (),
        {"config": GatewayConfig(multiplex_profiles=True)},
    )()
    monkeypatch.setattr(
        "hermes_cli.profiles.profiles_to_serve",
        lambda multiplex, profile_allowlist=None: [
            ("default", tmp_path),
            ("worker", worker_home),
        ],
    )
    monkeypatch.setattr(
        "hermes_cli.profiles.get_profile_dir",
        lambda name: tmp_path if name == "default" else worker_home,
    )
    with _profile_runtime_scope(worker_home):
        contract_adapter._execution_contract_store().initialize()
    secret_scope.set_multiplex_active(True)
    try:
        app = _contract_app(contract_adapter)
        async with TestClient(TestServer(app)) as client:
            rejected = await client.get(
                "/p/worker/v1/execution-contract/not-a-route",
                headers=_auth(READ_KEY),
            )
            assert rejected.status == 401
            _assert_closed_error(await rejected.json())

            unknown = await client.get(
                "/p/worker/v1/execution-contract/not-a-route",
                headers=_auth(worker_read_key),
            )
            assert unknown.status == 404
            _assert_closed_error(await unknown.json())

            wrong_method = await client.post(
                "/p/worker/v1/execution-contract/executions",
                headers=_auth(worker_full_key),
            )
            assert wrong_method.status == 405
            _assert_closed_error(await wrong_method.json())
    finally:
        secret_scope.set_multiplex_active(False)


def test_fixture_payloads_contain_no_runtime_or_secret_material():
    root = (
        __import__("pathlib").Path(__file__).parents[1]
        / "fixtures"
        / "execution_contract"
    )
    forbidden = ("api_key", "bearer", "credential", "session_history", "home/")
    for path in sorted(root.glob("*.json")):
        text = path.read_text(encoding="utf-8").lower()
        json.loads(text)
        assert not any(token in text for token in forbidden), path
