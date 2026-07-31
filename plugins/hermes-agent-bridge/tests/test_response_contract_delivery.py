import asyncio
import json
import urllib.error
from types import SimpleNamespace

import pytest

from clawchat_bridge.main import ActiveRun, ClawChatHermesBridge, HermesRunManager


def test_html_native_response_contract_is_model_visible():
    manager = HermesRunManager.__new__(HermesRunManager)
    prompt, metadata = manager._response_contract_prompt(
        {
            "responsePresentation": "html_native",
            "expectedContentFormat": "text/html",
            "responseFormatContract": "Return a styled <section> only.",
            "runtimeInstruction": "Use HTML and CSS directly.",
            "systemInstruction": "You are rendering a ClawChat message.",
        }
    )

    assert metadata["responsePresentation"] == "html_native"
    assert metadata["responseContractInjected"] is True
    assert metadata["responseFormatContractPresent"] is True
    assert metadata["runtimeInstructionPresent"] is True
    assert "## ClawChat Response Format Contract" in prompt
    assert "Return a styled <section> only." in prompt
    assert "## HTML/CSS Native Response Mode" in prompt
    assert "Do not wrap the answer in Markdown fences" in prompt


def test_response_contract_prompt_combines_with_default_skills_prompt():
    manager = HermesRunManager.__new__(HermesRunManager)

    combined = manager._compose_system_message(
        "preloaded skill instructions",
        "html native response instructions",
    )

    assert combined == "preloaded skill instructions\n\nhtml native response instructions"


def _manager():
    bridge = SimpleNamespace(config=SimpleNamespace(api_url="https://example.test"), access_token="token")
    return HermesRunManager(bridge)


def test_todo_tool_progress_emits_bounded_full_snapshots():
    manager = _manager()
    run = ActiveRun("dispatch-1", "runtime-1", external_agent_id="relay-agent")

    manager._emit_tool_callback(
        run,
        "tool.started",
        "todo",
        "Creating plan",
        {
            "todos": [
                {"id": "one", "content": "Inspect the issue", "status": "in_progress"},
                {"id": "two", "content": "Implement the fix", "status": "pending"},
            ]
        },
    )
    first = run.event_queue.get_nowait()
    assert first["tasks"] == [
        {"id": "one", "content": "Inspect the issue", "status": "in_progress"},
        {"id": "two", "content": "Implement the fix", "status": "pending"},
    ]

    manager._emit_tool_callback(
        run,
        "tool.started",
        "todo",
        "Updating plan",
        {
            "todos": [
                {"id": "one", "content": "Inspect the issue", "status": "completed"},
            ],
            "merge": True,
        },
    )
    second = run.event_queue.get_nowait()
    assert second["tasks"] == [
        {"id": "one", "content": "Inspect the issue", "status": "completed"},
        {"id": "two", "content": "Implement the fix", "status": "pending"},
    ]

    manager._emit_tool_callback(
        run,
        "tool.started",
        "terminal",
        "ls",
        {"command": "ls"},
    )
    ordinary_tool = run.event_queue.get_nowait()
    assert "tasks" not in ordinary_tool


class _FakeHttpResponse:
    def __init__(self, status=201, body=None):
        self.status = status
        self._body = body or {"ok": True}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self._body).encode("utf-8")


class _FakeErrorBody:
    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def close(self):
        return None


def test_dangerous_autonomy_policy_is_high_priority_and_supersedes_stale_history():
    manager = _manager()
    payload = {
        "dispatchId": "dispatch-1",
        "autonomyPolicy": {
            "mode": "dangerously_skip_permissions",
            "enabledToolCategories": ["browser_navigation", "form_fill", "form_submit"],
            "staleContextPolicy": "current_policy_supersedes_old_chat",
        },
    }
    policy = manager._autonomy_policy_from_payload(payload)
    fake_agent = SimpleNamespace(
        tools=[
            {"function": {"name": "browser_navigate"}},
            {"function": {"name": "browser_click"}},
            {"function": {"name": "browser_type"}},
            {"function": {"name": "browser_snapshot"}},
        ]
    )

    matrix = manager._build_tool_policy_matrix(payload, policy, fake_agent, [])
    prompt, metadata = manager._autonomy_policy_prompt(policy, matrix)

    assert metadata["autonomyPolicyPresent"] is True
    assert metadata["autonomyMode"] == "dangerously_skip_permissions"
    assert prompt.startswith("CURRENT AUTONOMY POLICY FOR THIS APP: dangerously_skip_permissions")
    assert "superseded where they conflict" in prompt
    assert "form_submit: policy=allowed; toolStatus=attached" in prompt
    assert "Do not fake results" in prompt


def test_safe_default_policy_keeps_external_actions_approval_required():
    manager = _manager()
    policy = manager._autonomy_policy_from_payload({
        "dispatchId": "dispatch-1",
        "autonomyPolicy": {"mode": "safe_default", "selectedCapabilities": ["read", "draft"]},
    })

    assert manager._policy_allows_category(policy, "read") == "allowed"
    assert manager._policy_allows_category(policy, "draft") == "allowed"
    assert manager._policy_allows_category(policy, "form_submit") == "approval_required"
    assert "browser" in manager._disabled_toolsets_for_policy(["session_search"], policy)


def test_email_send_allowed_without_mailbox_reports_tool_unavailable():
    manager = _manager()
    payload = {
        "dispatchId": "dispatch-1",
        "autonomyPolicy": {
            "mode": "dangerously_skip_permissions",
            "enabledToolCategories": ["email_send"],
        },
    }
    policy = manager._autonomy_policy_from_payload(payload)
    matrix = manager._build_tool_policy_matrix(payload, policy, SimpleNamespace(tools=[]), [])

    email = matrix["categories"]["email_send"]
    assert email["policy"] == "allowed"
    assert email["tool"] == "unavailable"
    assert email["reason"] == "email_send unavailable: no configured mailbox/sender tool."


def test_marketplace_tools_count_zero_diagnostic_is_agent_visible():
    manager = _manager()
    payload = {
        "dispatchId": "dispatch-1",
        "autonomyPolicy": {
            "mode": "dangerously_skip_permissions",
            "enabledToolCategories": ["task_update"],
        },
        "marketplaceTools": [],
    }
    policy = manager._autonomy_policy_from_payload(payload)
    matrix = manager._build_tool_policy_matrix(payload, policy, SimpleNamespace(tools=[]), [])
    prompt, _metadata = manager._autonomy_policy_prompt(policy, matrix)

    assert matrix["diagnostics"]["marketplaceToolsCount"] == 0
    assert "marketplaceToolsCount=0; reason: no descriptors from ClawChat" in prompt
    assert "LinkCrest/OpenClaw tools unavailable because ClawChat did not provide descriptors." in prompt


def test_policy_blocks_email_send_no_missing_tool_request():
    manager = _manager()
    payload = {
        "dispatchId": "dispatch-1",
        "autonomyPolicy": {
            "mode": "custom_policy",
            "disabledToolCategories": ["email_send"],
        },
    }
    policy = manager._autonomy_policy_from_payload(payload)
    matrix = manager._build_tool_policy_matrix(payload, policy, SimpleNamespace(tools=[]), [])

    assert matrix["categories"]["email_send"]["policy"] == "disabled"
    assert matrix["missingToolRequests"] == []


def test_form_submit_allowed_without_browser_tool_emits_missing_tool_request():
    manager = _manager()
    payload = {
        "dispatchId": "dispatch-1",
        "threadId": "thread-1",
        "autonomyPolicy": {
            "mode": "dangerously_skip_permissions",
            "appSlug": "local-linkcrest",
            "enabledToolCategories": ["public_form_submit"],
        },
    }
    policy = manager._autonomy_policy_from_payload(payload)
    matrix = manager._build_tool_policy_matrix(payload, policy, SimpleNamespace(tools=[]), [])

    request = next(item for item in matrix["missingToolRequests"] if item["requestedCapability"] == "public_form_submit")
    assert request["policyAllowed"] is True
    assert request["toolAvailable"] is False
    assert request["requiredForAction"] == "submit public form"


def test_external_search_allowed_without_tool_emits_missing_tool_request():
    manager = _manager()
    payload = {
        "dispatchId": "dispatch-1",
        "threadId": "thread-1",
        "autonomyPolicy": {
            "mode": "dangerously_skip_permissions",
            "appSlug": "local-linkcrest",
            "enabledToolCategories": ["external_search"],
        },
    }
    policy = manager._autonomy_policy_from_payload(payload)
    matrix = manager._build_tool_policy_matrix(payload, policy, SimpleNamespace(tools=[]), [])

    request = next(item for item in matrix["missingToolRequests"] if item["requestedCapability"] == "external_search")
    assert request["policyAllowed"] is True
    assert request["toolAvailable"] is False


def test_backlink_verification_allowed_without_tool_emits_missing_tool_request():
    manager = _manager()
    payload = {
        "dispatchId": "dispatch-1",
        "threadId": "thread-1",
        "autonomyPolicy": {
            "mode": "dangerously_skip_permissions",
            "appSlug": "local-linkcrest",
            "enabledToolCategories": ["backlink_verification"],
        },
    }
    policy = manager._autonomy_policy_from_payload(payload)
    matrix = manager._build_tool_policy_matrix(payload, policy, SimpleNamespace(tools=[]), [])

    request = next(item for item in matrix["missingToolRequests"] if item["requestedCapability"] == "backlink_verification")
    assert request["requiredForAction"] == "verify backlink"


def test_linkcrest_backlink_execution_auto_requests_minimum_missing_tools():
    manager = _manager()
    payload = {
        "dispatchId": "dispatch-1",
        "threadId": "thread-1",
        "campaignName": "AI YouTube Channels Backlink Campaign",
        "inputText": "Execute backlink outreach and directory submissions.",
        "autonomyPolicy": {
            "mode": "dangerously_skip_permissions",
            "appSlug": "local-linkcrest",
        },
    }
    policy = manager._autonomy_policy_from_payload(payload)
    matrix = manager._build_tool_policy_matrix(payload, policy, SimpleNamespace(tools=[]), [])
    requested = {item["requestedCapability"] for item in matrix["missingToolRequests"]}

    assert {
        "email_send",
        "external_search",
        "public_form_submit",
        "backlink_verification",
        "index_checking",
        "credential_use",
    }.issubset(requested)


def test_linkcrest_backlink_requests_use_nested_runtime_context():
    manager = _manager()
    payload = {
        "dispatchId": "dispatch-1",
        "threadId": "thread-1",
        "inputText": "Execute backlink outreach and verification.",
        "marketplaceRuntimeContext": {
            "appSlug": "linkcrest",
            "linkedAppId": "linked-app-1",
            "campaignId": "campaign-1",
            "campaignName": "Campaign A",
        },
        "autonomyPolicy": {
            "mode": "dangerously_skip_permissions",
        },
    }
    policy = manager._autonomy_policy_from_payload(payload)
    matrix = manager._build_tool_policy_matrix(payload, policy, SimpleNamespace(tools=[]), [])
    requests = {item["requestedCapability"]: item for item in matrix["missingToolRequests"]}

    for capability in {
        "email_send",
        "external_search",
        "public_form_submit",
        "backlink_verification",
        "index_checking",
        "credential_use",
    }:
        assert requests[capability]["appSlug"] == "linkcrest"
        assert requests[capability]["linkedAppId"] == "linked-app-1"
        assert requests[capability]["campaignId"] == "campaign-1"
        assert requests[capability]["campaignName"] == "Campaign A"


def test_linkcrest_backlink_dispatch_without_policy_posts_six_requests(tmp_path, monkeypatch):
    manager = _manager()
    manager.missing_tool_queue_path = tmp_path / "missing_tool_requests.jsonl"
    captured = []

    def fake_urlopen(request, timeout):
        captured.append(json.loads(request.data.decode("utf-8")))
        return _FakeHttpResponse(200, {"created": True})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    payload = {
        "dispatchId": "runtime-dispatch-1",
        "runtimeSessionId": "runtime-1",
        "agentId": "agent-1",
        "externalAgentId": "linc_jr_hermes",
        "threadId": "thread-1",
        "marketplaceRuntimeContext": {
            "appSlug": "linkcrest",
            "linkedAppId": "linked-app-1",
            "campaignId": "campaign-1",
            "campaignName": "AI YouTube Channels Backlink Campaign",
        },
        "inputText": "Execute backlink outreach, directory submissions, verification, and index checks.",
    }
    policy = manager._autonomy_policy_from_payload(payload)
    assert policy is not None
    matrix = manager._build_tool_policy_matrix(payload, policy, SimpleNamespace(tools=[]), [])

    manager._emit_missing_tool_requests(
        ActiveRun("runtime-dispatch-1", "runtime-1", external_agent_id="linc_jr_hermes"),
        payload,
        matrix["missingToolRequests"],
    )

    posted = {item["requestedCapability"]: item for item in captured}
    assert {
        "email_send",
        "external_search",
        "public_form_submit",
        "backlink_verification",
        "index_checking",
        "credential_use",
    }.issubset(posted)
    for item in posted.values():
        assert item["appSlug"] == "linkcrest"
        assert item["linkedAppId"] == "linked-app-1"
        assert item["campaignId"] == "campaign-1"
        assert item["campaignName"] == "AI YouTube Channels Backlink Campaign"
    assert not manager.missing_tool_queue_path.exists()


@pytest.mark.parametrize(
    "category,requested",
    [
        ("email_send", "email_send"),
        ("external_search", "external_search"),
        ("form_submit", "public_form_submit"),
        ("backlink_verify", "backlink_verification"),
        ("index_check", "index_checking"),
        ("credential_use", "credential_use"),
    ],
)
def test_missing_tool_requests_post_six_linkcrest_capabilities(tmp_path, monkeypatch, category, requested):
    manager = _manager()
    manager.missing_tool_queue_path = tmp_path / "missing_tool_requests.jsonl"
    captured = []

    def fake_urlopen(request, timeout):
        captured.append({
            "url": request.full_url,
            "body": json.loads(request.data.decode("utf-8")),
            "timeout": timeout,
            "authorization": request.headers.get("Authorization"),
        })
        return _FakeHttpResponse(201, {"created": True})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    payload = {
        "dispatchId": "runtime-dispatch-1",
        "runtimeSessionId": "runtime-1",
        "agentId": "agent-1",
        "externalAgentId": "linc_jr_hermes",
        "threadId": "thread-1",
        "linkedAppId": "linked-app-1",
        "campaignId": "campaign-1",
        "campaignName": "Campaign A",
        "autonomyPolicy": {
            "mode": "dangerously_skip_permissions",
            "appSlug": "local-linkcrest",
            "enabledToolCategories": [category],
        },
    }
    policy = manager._autonomy_policy_from_payload(payload)
    matrix = manager._build_tool_policy_matrix(payload, policy, SimpleNamespace(tools=[]), [])
    run = ActiveRun("runtime-dispatch-1", "runtime-1", external_agent_id="linc_jr_hermes")

    manager._emit_missing_tool_requests(run, payload, matrix["missingToolRequests"])

    posted = next(item for item in captured if item["body"]["requestedCapability"] == requested)
    assert posted["url"] == "https://example.test/api/v1/bridge/runtime-dispatches/runtime-dispatch-1/tool-requests"
    assert posted["authorization"] == "Bearer token"
    assert posted["body"]["runtimeDispatchId"] == "runtime-dispatch-1"
    assert posted["body"]["linkedAppId"] == "linked-app-1"
    assert posted["body"]["campaignId"] == "campaign-1"
    assert posted["body"]["policyAllowed"] is True
    assert posted["body"]["toolAvailable"] is False
    assert not manager.missing_tool_queue_path.exists()


def test_missing_tool_payload_includes_app_thread_campaign_context():
    manager = _manager()
    payload = {
        "dispatchId": "dispatch-1",
        "runtimeDispatchId": "runtime-dispatch-1",
        "runtimeSessionId": "runtime-1",
        "agentId": "agent-db-id",
        "externalAgentId": "linc_jr_hermes",
        "teamId": "team-1",
        "threadId": "thread-1",
        "linkedAppId": "app-1",
        "campaignId": "campaign-1",
        "relatedTaskId": "task-1",
        "relatedRecordType": "backlink",
        "relatedRecordId": "record-1",
        "autonomyPolicy": {
            "mode": "dangerously_skip_permissions",
            "appSlug": "local-linkcrest",
            "campaignName": "Campaign A",
            "enabledToolCategories": ["email_send"],
        },
    }
    policy = manager._autonomy_policy_from_payload(payload)
    matrix = manager._build_tool_policy_matrix(payload, policy, SimpleNamespace(tools=[]), [])
    request = next(item for item in matrix["missingToolRequests"] if item["requestedCapability"] == "email_send")

    assert request["runtimeDispatchId"] == "runtime-dispatch-1"
    assert request["agentId"] == "agent-db-id"
    assert request["externalAgentId"] == "linc_jr_hermes"
    assert request["threadId"] == "thread-1"
    assert request["teamId"] == "team-1"
    assert request["linkedAppId"] == "app-1"
    assert request["appSlug"] == "local-linkcrest"
    assert request["campaignId"] == "campaign-1"
    assert request["campaignName"] == "Campaign A"
    assert request["relatedTaskId"] == "task-1"
    assert request["relatedRecordType"] == "backlink"
    assert request["relatedRecordId"] == "record-1"


def test_linkcrest_openclaw_missing_descriptor_emits_missing_tool_request():
    manager = _manager()
    payload = {
        "dispatchId": "dispatch-1",
        "threadId": "thread-1",
        "autonomyPolicy": {
            "mode": "dangerously_skip_permissions",
            "appSlug": "local-linkcrest",
            "enabledToolCategories": ["linkcrest_openclaw_tools"],
        },
        "marketplaceTools": [],
    }
    policy = manager._autonomy_policy_from_payload(payload)
    matrix = manager._build_tool_policy_matrix(payload, policy, SimpleNamespace(tools=[]), [])

    request = next(item for item in matrix["missingToolRequests"] if item["requestedCapability"] == "linkcrest_openclaw_tools")
    assert request["suggestedMarketplaceApps"] == ["local-linkcrest"]
    assert "ClawChat did not provide descriptors" in request["reason"]


def test_connected_but_not_granted_emits_missing_tool_request():
    manager = _manager()
    payload = {
        "dispatchId": "dispatch-1",
        "autonomyPolicy": {
            "mode": "dangerously_skip_permissions",
            "enabledToolCategories": ["email_send"],
            "toolStatusByCategory": {
                "email_send": {"connected": True, "granted": False, "reason": "mailbox connected but grant missing"}
            },
        },
    }
    policy = manager._autonomy_policy_from_payload(payload)
    matrix = manager._build_tool_policy_matrix(payload, policy, SimpleNamespace(tools=[]), [])
    request = matrix["missingToolRequests"][0]

    assert matrix["categories"]["email_send"]["toolStatus"] == "connected_but_not_granted"
    assert request["toolConnected"] is True
    assert request["toolGranted"] is False


def test_missing_credentials_emits_missing_tool_request_with_metadata():
    manager = _manager()
    payload = {
        "dispatchId": "dispatch-1",
        "autonomyPolicy": {
            "mode": "dangerously_skip_permissions",
            "enabledToolCategories": ["credential_use"],
            "toolStatusByCategory": {
                "credential_use": {"connected": True, "granted": True, "missingCredentials": True}
            },
        },
    }
    policy = manager._autonomy_policy_from_payload(payload)
    matrix = manager._build_tool_policy_matrix(payload, policy, SimpleNamespace(tools=[]), [])
    request = matrix["missingToolRequests"][0]

    assert matrix["categories"]["credential_use"]["toolStatus"] == "missing_credentials"
    assert request["metadata"]["toolStatus"] == "missing_credentials"


def test_repeated_missing_tool_request_has_stable_dedupe_key():
    manager = _manager()
    payload = {
        "dispatchId": "dispatch-1",
        "threadId": "thread-1",
        "relatedTaskId": "task-1",
        "autonomyPolicy": {
            "mode": "dangerously_skip_permissions",
            "appSlug": "local-linkcrest",
            "campaignName": "Campaign A",
            "enabledToolCategories": ["email_send"],
        },
    }
    policy = manager._autonomy_policy_from_payload(payload)
    first = manager._build_tool_policy_matrix(payload, policy, SimpleNamespace(tools=[]), [])["missingToolRequests"][0]
    second = manager._build_tool_policy_matrix(payload, policy, SimpleNamespace(tools=[]), [])["missingToolRequests"][0]

    assert first["dedupeKey"] == second["dedupeKey"]


def test_agent_visible_runtime_matrix_distinguishes_unavailable_and_policy_disabled():
    manager = _manager()
    payload = {
        "dispatchId": "dispatch-1",
        "autonomyPolicy": {
            "mode": "custom_policy",
            "enabledToolCategories": ["email_send"],
            "disabledToolCategories": ["external_search"],
        },
    }
    policy = manager._autonomy_policy_from_payload(payload)
    matrix = manager._build_tool_policy_matrix(payload, policy, SimpleNamespace(tools=[]), [])
    prompt, _metadata = manager._autonomy_policy_prompt(policy, matrix)

    assert "email_send: policy=allowed; toolStatus=unavailable" in prompt
    assert "external_search: policy=disabled" in prompt
    assert "Do not say \"not allowed\" unless policy actually blocks it" in prompt


def test_missing_tool_request_fallback_is_queued(tmp_path):
    manager = _manager()
    manager.missing_tool_queue_path = tmp_path / "missing_tool_requests.jsonl"
    request = {
        "requestedCapability": "email_send",
        "reason": "email_send unavailable",
        "dedupeKey": "local-linkcrest|thread-1|campaign|email_send|send outreach email|task-1",
    }

    manager._queue_missing_tool_request(request)

    queued = json.loads(manager.missing_tool_queue_path.read_text(encoding="utf-8").strip())
    assert queued["requestedCapability"] == "email_send"
    assert queued["dedupeKey"] == request["dedupeKey"]


def test_policy_allows_email_send_but_no_tool_queues_status_when_postback_unavailable(tmp_path):
    manager = _manager()
    manager.bridge.access_token = ""
    manager.missing_tool_queue_path = tmp_path / "missing_tool_requests.jsonl"
    payload = {
        "dispatchId": "dispatch-1",
        "runtimeSessionId": "runtime-1",
        "threadId": "thread-1",
        "autonomyPolicy": {
            "mode": "dangerously_skip_permissions",
            "appSlug": "local-linkcrest",
            "enabledToolCategories": ["email_send"],
        },
    }
    policy = manager._autonomy_policy_from_payload(payload)
    matrix = manager._build_tool_policy_matrix(payload, policy, SimpleNamespace(tools=[]), [])
    run = ActiveRun("dispatch-1", "runtime-1", external_agent_id="linc_jr_hermes")

    manager._emit_missing_tool_requests(run, payload, matrix["missingToolRequests"])

    event = run.event_queue.get_nowait()
    assert event["type"] == "run.status"
    assert event["code"] == "missing_tool_request.fallback_queued"
    assert event["metadata"]["requestedCapability"] == "email_send"
    queued = json.loads(manager.missing_tool_queue_path.read_text(encoding="utf-8").strip())
    assert queued["requestedCapability"] == "email_send"


def test_missing_tool_request_posts_to_clawchat_endpoint(tmp_path, monkeypatch):
    manager = _manager()
    manager.missing_tool_queue_path = tmp_path / "missing_tool_requests.jsonl"
    captured = {}

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["method"] = request.get_method()
        captured["timeout"] = timeout
        captured["authorization"] = request.headers.get("Authorization")
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return _FakeHttpResponse(201)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    payload = {
        "dispatchId": "dispatch-1",
        "runtimeDispatchId": "runtime-dispatch-1",
        "runtimeSessionId": "runtime-1",
        "threadId": "thread-1",
        "autonomyPolicy": {
            "mode": "dangerously_skip_permissions",
            "appSlug": "local-linkcrest",
            "enabledToolCategories": ["email_send"],
        },
    }
    policy = manager._autonomy_policy_from_payload(payload)
    matrix = manager._build_tool_policy_matrix(payload, policy, SimpleNamespace(tools=[]), [])
    run = ActiveRun("dispatch-1", "runtime-1", external_agent_id="linc_jr_hermes")

    manager._emit_missing_tool_requests(run, payload, matrix["missingToolRequests"])

    assert captured["url"] == "https://example.test/api/v1/bridge/runtime-dispatches/runtime-dispatch-1/tool-requests"
    assert captured["method"] == "POST"
    assert captured["authorization"] == "Bearer token"
    assert captured["body"]["requestedCapability"] == "email_send"
    assert captured["body"]["externalAgentId"] is None or captured["body"]["externalAgentId"] == "linc_jr_hermes"
    assert not manager.missing_tool_queue_path.exists()
    with pytest.raises(Exception):
        run.event_queue.get_nowait()


def test_missing_tool_request_endpoint_failure_falls_back_to_jsonl(tmp_path, monkeypatch):
    manager = _manager()
    manager.missing_tool_queue_path = tmp_path / "missing_tool_requests.jsonl"

    def fake_urlopen(_request, *_args, **_kwargs):
        raise urllib.error.HTTPError("https://example.test/fail", 503, "unavailable", {}, None)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    payload = {
        "dispatchId": "dispatch-1",
        "runtimeDispatchId": "runtime-dispatch-1",
        "runtimeSessionId": "runtime-1",
        "autonomyPolicy": {
            "mode": "dangerously_skip_permissions",
            "enabledToolCategories": ["email_send"],
        },
    }
    policy = manager._autonomy_policy_from_payload(payload)
    matrix = manager._build_tool_policy_matrix(payload, policy, SimpleNamespace(tools=[]), [])
    run = ActiveRun("dispatch-1", "runtime-1", external_agent_id="linc_jr_hermes")

    manager._emit_missing_tool_requests(run, payload, matrix["missingToolRequests"])

    queued = json.loads(manager.missing_tool_queue_path.read_text(encoding="utf-8").strip())
    assert queued["requestedCapability"] == "email_send"
    assert queued["fallbackReason"].startswith("http_503")
    assert queued["fallbackDiagnostics"]["statusCode"] == 503
    event = run.event_queue.get_nowait()
    assert event["type"] == "run.status"
    assert event["code"] == "missing_tool_request.fallback_queued"
    assert event["metadata"]["requestedCapability"] == "email_send"


@pytest.mark.parametrize("status_code", [401, 404, 500])
def test_missing_tool_request_http_errors_fall_back_with_reason(tmp_path, monkeypatch, status_code):
    manager = _manager()
    manager.missing_tool_queue_path = tmp_path / "missing_tool_requests.jsonl"

    def fake_urlopen(_request, *_args, **_kwargs):
        raise urllib.error.HTTPError(
            "https://example.test/fail",
            status_code,
            "failed",
            {},
            _FakeErrorBody(b'{"error":"nope"}'),
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    payload = {
        "dispatchId": "runtime-dispatch-1",
        "runtimeSessionId": "runtime-1",
        "autonomyPolicy": {
            "mode": "dangerously_skip_permissions",
            "enabledToolCategories": ["email_send"],
        },
    }
    policy = manager._autonomy_policy_from_payload(payload)
    matrix = manager._build_tool_policy_matrix(payload, policy, SimpleNamespace(tools=[]), [])
    run = ActiveRun("runtime-dispatch-1", "runtime-1", external_agent_id="linc_jr_hermes")

    manager._emit_missing_tool_requests(run, payload, matrix["missingToolRequests"])

    queued = json.loads(manager.missing_tool_queue_path.read_text(encoding="utf-8").strip())
    assert queued["fallbackReason"].startswith(f"http_{status_code}")
    assert queued["fallbackDiagnostics"]["statusCode"] == status_code


def test_missing_tool_logs_status_without_secrets(tmp_path, monkeypatch, caplog):
    manager = _manager()
    manager.bridge.access_token = "secret-bearer-token"
    manager.missing_tool_queue_path = tmp_path / "missing_tool_requests.jsonl"

    def fake_urlopen(_request, _timeout):
        raise urllib.error.HTTPError("https://example.test/fail", 503, "unavailable", {}, None)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    payload = {
        "dispatchId": "dispatch-1",
        "runtimeDispatchId": "runtime-dispatch-1",
        "runtimeSessionId": "runtime-1",
        "autonomyPolicy": {
            "mode": "dangerously_skip_permissions",
            "enabledToolCategories": ["email_send"],
        },
    }
    policy = manager._autonomy_policy_from_payload(payload)
    matrix = manager._build_tool_policy_matrix(payload, policy, SimpleNamespace(tools=[]), [])
    run = ActiveRun("dispatch-1", "runtime-1", external_agent_id="linc_jr_hermes")

    with caplog.at_level("INFO", logger="clawchat.hermes_bridge"):
        manager._emit_missing_tool_requests(run, payload, matrix["missingToolRequests"])

    assert "fallback_jsonl_written" in caplog.text
    assert "secret-bearer-token" not in caplog.text


def test_missing_runtime_dispatch_id_falls_back_to_jsonl(tmp_path, monkeypatch):
    manager = _manager()
    manager.missing_tool_queue_path = tmp_path / "missing_tool_requests.jsonl"

    def fake_urlopen(_request, _timeout):
        raise AssertionError("should not post without a runtime dispatch id")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    request = {
        "requestedCapability": "email_send",
        "reason": "email_send unavailable",
        "dedupeKey": "k",
    }
    run = ActiveRun("", "runtime-1", external_agent_id="linc_jr_hermes")

    manager._emit_missing_tool_requests(run, {}, [request])

    queued = json.loads(manager.missing_tool_queue_path.read_text(encoding="utf-8").strip())
    assert queued["requestedCapability"] == "email_send"
    assert queued["fallbackReason"] == "missing_runtimeDispatchId"


def test_missing_bridge_auth_falls_back_to_jsonl_with_reason(tmp_path, monkeypatch):
    manager = _manager()
    manager.bridge.access_token = ""
    manager.missing_tool_queue_path = tmp_path / "missing_tool_requests.jsonl"

    def fake_urlopen(_request, _timeout):
        raise AssertionError("should not post without bridge auth")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    payload = {
        "dispatchId": "runtime-dispatch-1",
        "runtimeSessionId": "runtime-1",
        "autonomyPolicy": {
            "mode": "dangerously_skip_permissions",
            "enabledToolCategories": ["email_send"],
        },
    }
    policy = manager._autonomy_policy_from_payload(payload)
    matrix = manager._build_tool_policy_matrix(payload, policy, SimpleNamespace(tools=[]), [])
    run = ActiveRun("runtime-dispatch-1", "runtime-1", external_agent_id="linc_jr_hermes")

    manager._emit_missing_tool_requests(run, payload, matrix["missingToolRequests"])

    queued = json.loads(manager.missing_tool_queue_path.read_text(encoding="utf-8").strip())
    assert queued["fallbackReason"] == "missing_bridge_access_token"


def test_missing_tool_success_200_and_201_do_not_write_fallback(tmp_path, monkeypatch):
    for status_code in (200, 201):
        manager = _manager()
        manager.missing_tool_queue_path = tmp_path / f"missing_tool_requests_{status_code}.jsonl"

        def fake_urlopen(_request, *_args, status_code=status_code, **_kwargs):
            return _FakeHttpResponse(status_code, {"deduped": status_code == 200, "created": status_code == 201})

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        payload = {
            "dispatchId": f"runtime-dispatch-{status_code}",
            "runtimeSessionId": "runtime-1",
            "autonomyPolicy": {
                "mode": "dangerously_skip_permissions",
                "enabledToolCategories": ["email_send"],
            },
        }
        policy = manager._autonomy_policy_from_payload(payload)
        matrix = manager._build_tool_policy_matrix(payload, policy, SimpleNamespace(tools=[]), [])
        run = ActiveRun(f"runtime-dispatch-{status_code}", "runtime-1", external_agent_id="linc_jr_hermes")

        manager._emit_missing_tool_requests(run, payload, matrix["missingToolRequests"])

        assert not manager.missing_tool_queue_path.exists()


class _FakeWs:
    closed = False

    def __init__(self):
        self.messages = []

    async def send_str(self, text):
        self.messages.append(json.loads(text))


def test_terminal_events_are_sent_with_ack_metadata_and_removed_on_ack(tmp_path):
    async def run():
        bridge = ClawChatHermesBridge.__new__(ClawChatHermesBridge)
        bridge.ws = _FakeWs()
        bridge._send_lock = asyncio.Lock()
        bridge._terminal_outbox = {}
        bridge._terminal_outbox_path = tmp_path / "terminal_event_outbox.json"
        bridge._terminal_outbox_lock = asyncio.Lock()
        bridge._stop = asyncio.Event()

        await bridge.send_event(
            {
                "type": "run.completed",
                "dispatchId": "dispatch-1",
                "finalText": "<section>Done</section>",
            }
        )

        assert len(bridge.ws.messages) == 1
        sent = bridge.ws.messages[0]
        assert sent["type"] == "hermes_runtime_event"
        assert sent["requiresAck"] is True
        assert sent["event"]["type"] == "run.completed"
        assert sent["event"]["eventId"] == sent["eventId"]
        assert list(bridge._terminal_outbox) == [sent["eventId"]]

        await bridge._handle_runtime_event_ack(
            {
                "type": "hermes_runtime_event_ack",
                "data": {"eventId": sent["eventId"]},
            }
        )

        assert bridge._terminal_outbox == {}

    asyncio.run(run())
