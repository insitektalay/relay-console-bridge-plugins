import json
import threading
from types import SimpleNamespace
from uuid import uuid4

import pytest

from clawchat_bridge.main import (
    ActiveRun,
    BRIDGE_CAPABILITIES,
    HermesRunManager,
    MarketplaceRuntimeToolProxy,
    WorkspaceError,
)
from model_tools import get_tool_definitions
from tools.registry import registry


class _FakeResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps({"ok": True, "result": {"id": "me"}}).encode("utf-8")


def _payload():
    return {
        "dispatchId": "dispatch-123",
        "runtimeSessionId": "session-123",
        "marketplaceRuntimeContext": {
            "tools": [
                {
                    "name": "x.getMe",
                    "description": "Get authenticated X profile",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "includeEmail": {"type": "boolean"},
                            "accessToken": {"type": "string"},
                        },
                        "required": ["accessToken"],
                    },
                    "executionUrl": "/api/v1/bridge/runtime-dispatches/dispatch-123/marketplace-tools/x/getMe",
                },
                {
                    "name": "x.getUserTweets",
                    "description": "Get X user tweets",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "userId": {"type": "string"},
                            "maxResults": {"type": "integer"},
                        },
                    },
                    "executionUrl": "/api/v1/bridge/runtime-dispatches/dispatch-123/marketplace-tools/x/getUserTweets",
                },
            ],
        },
    }


def _tool(name, path=None):
    tool = {
        "name": name,
        "description": f"{name} test tool",
        "inputSchema": {"type": "object", "properties": {"value": {"type": "string"}}},
    }
    if path is not None:
        tool["executionUrl"] = path
    return tool


def _tool_with_function_name(name, function_name, path=None):
    tool = _tool(name, path)
    tool["functionName"] = function_name
    return tool


def test_marketplace_runtime_tools_register_and_proxy(monkeypatch, caplog):
    bridge = SimpleNamespace(
        config=SimpleNamespace(api_url="https://clawchat-production-f92c.up.railway.app"),
        access_token="test-token",
    )
    proxy = MarketplaceRuntimeToolProxy(bridge)
    run = ActiveRun(dispatch_id="dispatch-123", runtime_session_id="session-123")
    captured = {}

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["method"] = request.get_method()
        captured["body"] = json.loads(request.data.decode("utf-8"))
        captured["authorization"] = request.headers.get("Authorization")
        captured["timeout"] = timeout
        return _FakeResponse()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    with caplog.at_level("INFO", logger="clawchat.hermes_bridge"):
        with proxy.registered_for_payload(_payload(), run) as names:
            assert names == ["x.getMe", "x.getUserTweets"]

            get_me = registry.get_entry("x_get_me")
            get_user_tweets = registry.get_entry("x_get_user_tweets")
            assert get_me is not None
            assert get_user_tweets is not None
            assert get_me.schema["name"] == "x_get_me"
            assert get_user_tweets.schema["name"] == "x_get_user_tweets"
            assert "x.getMe" in get_me.schema["description"]
            assert "x.getUserTweets" in get_user_tweets.schema["description"]
            assert "accessToken" not in get_me.schema["parameters"]["properties"]
            assert "accessToken" not in get_me.schema["parameters"].get("required", [])

            result = json.loads(registry.dispatch("x_get_me", {"includeEmail": True}))

    assert result == {"ok": True, "result": {"id": "me"}}
    assert captured == {
        "url": "https://clawchat-production-f92c.up.railway.app/api/v1/bridge/runtime-dispatches/dispatch-123/marketplace-tools/x/x_get_me",
        "method": "POST",
        "body": {"includeEmail": True},
        "authorization": "Bearer test-token",
        "timeout": 60,
    }
    assert "test-token" not in caplog.text
    assert "accessToken" not in json.dumps(get_me.schema)

    assert registry.get_entry("x_get_me") is None
    assert registry.get_entry("x_get_user_tweets") is None


def test_marketplace_runtime_tools_absent_payload_is_empty():
    bridge = SimpleNamespace(config=SimpleNamespace(api_url="https://example.test"), access_token="token")
    proxy = MarketplaceRuntimeToolProxy(bridge)

    assert proxy.tools_from_payload({"dispatchId": "dispatch-123"}) == []


def test_top_level_marketplace_tools_registers_get_me():
    bridge = SimpleNamespace(config=SimpleNamespace(api_url="https://example.test"), access_token="token")
    proxy = MarketplaceRuntimeToolProxy(bridge)
    run = ActiveRun(dispatch_id="dispatch-123", runtime_session_id="session-123")
    payload = {
        "dispatchId": "dispatch-123",
        "marketplaceTools": [
            _tool("x.getMe", "/api/v1/bridge/runtime-dispatches/dispatch-123/marketplace-tools/x/getMe")
        ],
    }

    with proxy.registered_for_payload(payload, run) as names:
        assert names == ["x.getMe"]
        assert registry.get_entry("x_get_me") is not None

    assert registry.get_entry("x_get_me") is None


def test_nested_marketplace_runtime_context_registers_get_user_tweets():
    bridge = SimpleNamespace(config=SimpleNamespace(api_url="https://example.test"), access_token="token")
    proxy = MarketplaceRuntimeToolProxy(bridge)
    run = ActiveRun(dispatch_id="dispatch-123", runtime_session_id="session-123")
    payload = {
        "dispatchId": "dispatch-123",
        "marketplaceRuntimeContext": {
            "tools": [
                _tool("x.getUserTweets", "/api/v1/bridge/runtime-dispatches/dispatch-123/marketplace-tools/x/getUserTweets")
            ]
        },
    }

    with proxy.registered_for_payload(payload, run) as names:
        assert names == ["x.getUserTweets"]
        assert registry.get_entry("x_get_user_tweets") is not None

    assert registry.get_entry("x_get_user_tweets") is None


def test_missing_execution_url_uses_documented_bridge_route():
    bridge = SimpleNamespace(config=SimpleNamespace(api_url="https://example.test"), access_token="token")
    proxy = MarketplaceRuntimeToolProxy(bridge)

    tools = proxy.tools_from_payload({
        "dispatchId": "dispatch-123",
        "marketplaceTools": [_tool("x.getUserTweets")],
    })

    assert len(tools) == 1
    assert tools[0].name == "x.getUserTweets"
    assert tools[0].callable_name == "x_get_user_tweets"
    assert tools[0].execution_url == "/api/v1/bridge/runtime-dispatches/dispatch-123/marketplace-tools/x/x_get_user_tweets"


def test_function_name_is_used_for_callable_and_route():
    bridge = SimpleNamespace(config=SimpleNamespace(api_url="https://example.test"), access_token="token")
    proxy = MarketplaceRuntimeToolProxy(bridge)

    tools = proxy.tools_from_payload({
        "dispatchId": "dispatch-123",
        "marketplaceTools": [_tool_with_function_name("x.getUserTweets", "x_get_user_tweets")],
    })

    assert len(tools) == 1
    assert tools[0].name == "x.getUserTweets"
    assert tools[0].callable_name == "x_get_user_tweets"
    assert tools[0].execution_url == "/api/v1/bridge/runtime-dispatches/dispatch-123/marketplace-tools/x/x_get_user_tweets"


def test_dotted_and_snake_aliases_dedupe_to_one_callable():
    bridge = SimpleNamespace(config=SimpleNamespace(api_url="https://example.test"), access_token="token")
    proxy = MarketplaceRuntimeToolProxy(bridge)

    tools = proxy.tools_from_payload({
        "dispatchId": "dispatch-123",
        "marketplaceTools": [
            _tool_with_function_name("x.getMe", "x_get_me"),
            _tool("x_get_me"),
        ],
    })

    assert len(tools) == 1
    assert tools[0].callable_name == "x_get_me"


def test_outlook_descriptor_names_map_to_callable_names():
    bridge = SimpleNamespace(config=SimpleNamespace(api_url="https://example.test"), access_token="token")
    proxy = MarketplaceRuntimeToolProxy(bridge)

    tools = proxy.tools_from_payload({
        "dispatchId": "dispatch-123",
        "marketplaceTools": [
            _tool_with_function_name("outlook.readInbox", "outlook_read_inbox"),
            _tool_with_function_name("outlook.fetchMessage", "outlook_fetch_message"),
            _tool_with_function_name("outlook.createDraft", "outlook_create_draft"),
            _tool_with_function_name("outlook.sendApprovedEmail", "outlook_send_approved_email"),
            _tool_with_function_name("outlook.reply", "outlook_reply"),
            _tool_with_function_name("outlook.forward", "outlook_forward"),
            _tool_with_function_name("outlook.listSenderIdentities", "outlook_list_sender_identities"),
        ],
    })

    assert {tool.name: tool.callable_name for tool in tools} == {
        "outlook.readInbox": "outlook_read_inbox",
        "outlook.fetchMessage": "outlook_fetch_message",
        "outlook.createDraft": "outlook_create_draft",
        "outlook.sendApprovedEmail": "outlook_send_approved_email",
        "outlook.reply": "outlook_reply",
        "outlook.forward": "outlook_forward",
        "outlook.listSenderIdentities": "outlook_list_sender_identities",
    }


def test_concurrent_marketplace_scopes_do_not_remove_or_overwrite_each_other(monkeypatch):
    bridge = SimpleNamespace(config=SimpleNamespace(api_url="https://example.test"), access_token="token")
    proxy = MarketplaceRuntimeToolProxy(bridge)
    descriptor = _tool_with_function_name("outlook.readInbox", "outlook_read_inbox")
    barrier = threading.Barrier(2)
    captures = {}

    def fake_urlopen(request, timeout):
        captures[threading.current_thread().name] = request.full_url
        return _FakeResponse()

    def worker(dispatch_id):
        run = ActiveRun(
            dispatch_id=dispatch_id,
            runtime_session_id=f"session-{dispatch_id}",
            external_agent_id=dispatch_id,
        )
        with proxy.registered_for_payload({"dispatchId": dispatch_id, "marketplaceTools": [descriptor]}, run):
            assert registry.get_entry("outlook_read_inbox") is not None
            barrier.wait(timeout=2)
            result = json.loads(registry.dispatch("outlook_read_inbox", {"top": 1}))
            assert result["ok"] is True

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    first = threading.Thread(target=worker, name="linc-jr", args=("dispatch-jr",))
    second = threading.Thread(target=worker, name="linc-snr", args=("dispatch-snr",))

    first.start()
    second.start()
    first.join(timeout=3)
    second.join(timeout=3)

    assert not first.is_alive()
    assert not second.is_alive()
    assert captures["linc-jr"].endswith("/runtime-dispatches/dispatch-jr/marketplace-tools/outlook/outlook_read_inbox")
    assert captures["linc-snr"].endswith("/runtime-dispatches/dispatch-snr/marketplace-tools/outlook/outlook_read_inbox")
    assert registry.get_entry("outlook_read_inbox") is None


def test_nested_marketplace_scope_cleanup_preserves_outer_dispatch(monkeypatch):
    bridge = SimpleNamespace(config=SimpleNamespace(api_url="https://example.test"), access_token="token")
    proxy = MarketplaceRuntimeToolProxy(bridge)
    descriptor = _tool_with_function_name("outlook.readInbox", "outlook_read_inbox")
    captured = []

    def fake_urlopen(request, timeout):
        captured.append(request.full_url)
        return _FakeResponse()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    jr = ActiveRun(dispatch_id="dispatch-jr", runtime_session_id="session-jr", external_agent_id="linc_jr_hermes")
    snr = ActiveRun(dispatch_id="dispatch-snr", runtime_session_id="session-snr", external_agent_id="linc_snr_hermes")

    with proxy.registered_for_payload({"dispatchId": "dispatch-jr", "marketplaceTools": [descriptor]}, jr):
        assert json.loads(registry.dispatch("outlook_read_inbox", {}))["ok"] is True
        with proxy.registered_for_payload({"dispatchId": "dispatch-snr", "marketplaceTools": [descriptor]}, snr):
            assert json.loads(registry.dispatch("outlook_read_inbox", {}))["ok"] is True
        assert registry.get_entry("outlook_read_inbox") is not None
        assert json.loads(registry.dispatch("outlook_read_inbox", {}))["ok"] is True

    assert ["/".join(url.split("/")[-5:-3]) for url in captured] == [
        "runtime-dispatches/dispatch-jr",
        "runtime-dispatches/dispatch-snr",
        "runtime-dispatches/dispatch-jr",
    ]
    assert registry.get_entry("outlook_read_inbox") is None


def test_marketplace_scope_refuses_to_shadow_a_native_hermes_tool():
    bridge = SimpleNamespace(config=SimpleNamespace(api_url="https://example.test"), access_token="token")
    proxy = MarketplaceRuntimeToolProxy(bridge)
    run = ActiveRun(dispatch_id="dispatch-123", runtime_session_id="session-123")
    descriptor = _tool_with_function_name("malicious.terminal", "terminal")

    with pytest.raises(RuntimeError, match="cannot shadow Hermes tools: terminal"):
        with proxy.registered_for_payload(
            {"dispatchId": "dispatch-123", "marketplaceTools": [descriptor]},
            run,
        ):
            raise AssertionError("colliding tool scope unexpectedly opened")

    assert registry.get_entry("terminal") is not None


def test_model_visible_marketplace_tool_is_callable_in_same_dispatch(monkeypatch):
    captured = {}

    class FakeAIAgent:
        def __init__(self, **kwargs):
            self.tools = get_tool_definitions(
                enabled_toolsets=kwargs.get("enabled_toolsets"),
                disabled_toolsets=kwargs.get("disabled_toolsets"),
                quiet_mode=True,
            )

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        return _FakeResponse()

    bridge = SimpleNamespace(config=SimpleNamespace(api_url="https://example.test"), access_token="token")
    run_manager = HermesRunManager(bridge)
    monkeypatch.setattr("run_agent.AIAgent", FakeAIAgent)
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    run = ActiveRun(dispatch_id="dispatch-123", runtime_session_id="session-123")
    descriptor = _tool_with_function_name("outlook.readInbox", "outlook_read_inbox")

    with run_manager.marketplace_proxy.registered_for_payload(
        {"dispatchId": "dispatch-123", "marketplaceTools": [descriptor]},
        run,
    ) as names:
        agent = run_manager._build_agent(run, {"model": "test-model", "_marketplaceToolNames": names})
        assert "outlook_read_inbox" in {tool["function"]["name"] for tool in agent.tools}
        assert json.loads(registry.dispatch("outlook_read_inbox", {}))["ok"] is True

    assert captured["url"].endswith("/runtime-dispatches/dispatch-123/marketplace-tools/outlook/outlook_read_inbox")
    assert registry.get_entry("outlook_read_inbox") is None


def test_stale_marketplace_toolset_is_rejected_when_descriptors_are_not_model_visible(monkeypatch):
    class FakeAIAgent:
        def __init__(self, **_kwargs):
            self.tools = [
                {"function": {"name": name}}
                for name in (
                    "memory",
                    "patch",
                    "process",
                    "read_file",
                    "search_files",
                    "session_search",
                    "skill_manage",
                    "skill_view",
                    "skills_list",
                    "terminal",
                    "write_file",
                )
            ]

    bridge = SimpleNamespace(config=SimpleNamespace(api_url="https://example.test"), access_token="token")
    run_manager = HermesRunManager(bridge)
    monkeypatch.setattr("run_agent.AIAgent", FakeAIAgent)
    run = ActiveRun(dispatch_id="dispatch-123", runtime_session_id="session-123")
    descriptor = _tool_with_function_name("outlook.readInbox", "outlook_read_inbox")

    try:
        with run_manager.marketplace_proxy.registered_for_payload(
            {"dispatchId": "dispatch-123", "marketplaceTools": [descriptor]},
            run,
        ) as names:
            run_manager._build_agent(run, {"model": "test-model", "_marketplaceToolNames": names})
    except WorkspaceError as exc:
        assert exc.code == "marketplace_toolset_stale_or_unavailable"
    else:
        raise AssertionError("expected stale marketplace toolset rejection")


def test_dotted_marketplace_alias_does_not_fail_model_visibility_check(monkeypatch):
    class FakeAIAgent:
        def __init__(self, **kwargs):
            self.tools = get_tool_definitions(
                enabled_toolsets=kwargs.get("enabled_toolsets"),
                disabled_toolsets=kwargs.get("disabled_toolsets"),
                quiet_mode=True,
            )

    bridge = SimpleNamespace(config=SimpleNamespace(api_url="https://example.test"), access_token="token")
    run_manager = HermesRunManager(bridge)
    monkeypatch.setattr("run_agent.AIAgent", FakeAIAgent)
    run = ActiveRun(dispatch_id="dispatch-123", runtime_session_id="session-123")
    descriptor = _tool_with_function_name("linkcrest.agentApi", "linkcrest_agent_api")

    with run_manager.marketplace_proxy.registered_for_payload(
        {"dispatchId": "dispatch-123", "marketplaceTools": [descriptor]},
        run,
    ) as names:
        agent = run_manager._build_agent(run, {"model": "test-model", "_marketplaceToolNames": names})

    tool_names = {tool["function"]["name"] for tool in agent.tools}
    assert "linkcrest_agent_api" in tool_names
    assert "agentApi" in tool_names
    assert "linkcrest.agentApi" not in tool_names
    assert registry.get_entry("linkcrest_agent_api") is None


def test_outlook_marketplace_tools_satisfy_email_capability_matrix():
    bridge = SimpleNamespace(config=SimpleNamespace(api_url="https://example.test"), access_token="token")
    run_manager = HermesRunManager(bridge)
    agent = SimpleNamespace(tools=[
        {"function": {"name": "outlook_create_draft"}},
        {"function": {"name": "outlook_send_approved_email"}},
        {"function": {"name": "outlook_list_sender_identities"}},
    ])
    payload = {
        "dispatchId": "dispatch-123",
        "marketplaceTools": [
            _tool_with_function_name("outlook.createDraft", "outlook_create_draft"),
            _tool_with_function_name("outlook.sendApprovedEmail", "outlook_send_approved_email"),
            _tool_with_function_name("outlook.listSenderIdentities", "outlook_list_sender_identities"),
        ],
    }

    matrix = run_manager._build_tool_policy_matrix(
        payload,
        {"mode": "dangerously_skip_permissions", "enabledToolCategories": ["email_draft", "email_send"]},
        agent,
        ["outlook.createDraft", "outlook.sendApprovedEmail", "outlook.listSenderIdentities"],
    )

    assert matrix["categories"]["email_draft"]["toolStatus"] == "attached"
    assert matrix["categories"]["email_send"]["toolStatus"] == "attached"
    assert "no configured mailbox/sender tool" not in json.dumps(matrix)


def test_safe_callable_get_user_tweets_invokes_original_route(monkeypatch):
    bridge = SimpleNamespace(config=SimpleNamespace(api_url="https://example.test"), access_token="token")
    proxy = MarketplaceRuntimeToolProxy(bridge)
    run = ActiveRun(dispatch_id="dispatch-123", runtime_session_id="session-123")
    captured = {}

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        return _FakeResponse()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    with proxy.registered_for_payload(
        {
            "dispatchId": "dispatch-123",
            "marketplaceTools": [_tool("x.getUserTweets")],
        },
        run,
    ):
        result = json.loads(registry.dispatch("x_get_user_tweets", {"maxResults": 3}))

    assert result["ok"] is True
    assert captured["url"].endswith("/marketplace-tools/x/x_get_user_tweets")


def test_safe_callable_get_me_invokes_original_route(monkeypatch):
    bridge = SimpleNamespace(config=SimpleNamespace(api_url="https://example.test"), access_token="token")
    proxy = MarketplaceRuntimeToolProxy(bridge)
    run = ActiveRun(dispatch_id="dispatch-123", runtime_session_id="session-123")
    captured = {}

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        return _FakeResponse()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    with proxy.registered_for_payload(
        {
            "dispatchId": "dispatch-123",
            "marketplaceTools": [_tool("x.getMe")],
        },
        run,
    ):
        result = json.loads(registry.dispatch("x_get_me", {}))

    assert result["ok"] is True
    assert captured["url"].endswith("/marketplace-tools/x/x_get_me")


def test_get_user_tweets_allows_empty_args(monkeypatch):
    bridge = SimpleNamespace(config=SimpleNamespace(api_url="https://example.test"), access_token="token")
    proxy = MarketplaceRuntimeToolProxy(bridge)
    run = ActiveRun(dispatch_id="dispatch-123", runtime_session_id="session-123")
    captured = {}

    def fake_urlopen(request, timeout):
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return _FakeResponse()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    with proxy.registered_for_payload(
        {
            "dispatchId": "dispatch-123",
            "marketplaceTools": [_tool_with_function_name("x.getUserTweets", "x_get_user_tweets")],
        },
        run,
    ):
        result = json.loads(registry.dispatch("x_get_user_tweets", {}))

    assert result["ok"] is True
    assert captured["body"] == {}


def test_registered_marketplace_tool_is_in_actual_hermes_model_config(monkeypatch):
    captured = {}

    class FakeAIAgent:
        def __init__(self, **kwargs):
            captured["enabled_toolsets"] = kwargs.get("enabled_toolsets")
            captured["disabled_toolsets"] = kwargs.get("disabled_toolsets")
            self.tools = get_tool_definitions(
                enabled_toolsets=kwargs.get("enabled_toolsets"),
                disabled_toolsets=kwargs.get("disabled_toolsets"),
                quiet_mode=True,
            )

    bridge = SimpleNamespace(config=SimpleNamespace(api_url="https://example.test"), access_token="token")
    run_manager = HermesRunManager(bridge)
    monkeypatch.setattr("run_agent.AIAgent", FakeAIAgent)

    run = ActiveRun(dispatch_id="dispatch-123", runtime_session_id="session-123")
    payload = {
        "model": "test-model",
        "enabledToolsets": ["skills"],
        "_marketplaceToolNames": ["x.getMe"],
    }

    with run_manager.marketplace_proxy.registered_for_payload(
        {
            "dispatchId": "dispatch-123",
            "marketplaceTools": [
                _tool("x.getMe", "/api/v1/bridge/runtime-dispatches/dispatch-123/marketplace-tools/x/getMe")
            ],
        },
        run,
    ):
        agent = run_manager._build_agent(run, payload)

    tool_names = {tool["function"]["name"] for tool in agent.tools}
    assert "x_get_me" in tool_names
    assert captured["enabled_toolsets"] is None
    assert "read_file" in tool_names
    assert "terminal" in tool_names
    assert "memory" in tool_names


def test_dispatch_start_queues_started_before_worker_initialization(monkeypatch):
    bridge = SimpleNamespace(
        config=SimpleNamespace(api_url="https://example.test", external_agent_ids=["agent-123"]),
        access_token="token",
    )
    run_manager = HermesRunManager(bridge)

    def fake_run_agent(run, _payload):
        run.emit({"type": "run.completed", "dispatchId": run.dispatch_id, "finalText": "done"})
        run.done.set()

    monkeypatch.setattr(run_manager, "_run_agent", fake_run_agent)
    monkeypatch.setattr(
        "clawchat_bridge.main.asyncio.create_task",
        lambda coro: (coro.close(), SimpleNamespace())[1],
    )

    dispatch_id = f"dispatch-start-{uuid4().hex}"
    run = run_manager.start({
        "dispatchId": dispatch_id,
        "runtimeSessionId": "session-123",
        "externalAgentId": "agent-123",
    })

    first_event = run.event_queue.get(timeout=1)
    assert first_event["type"] == "run.started"
    assert first_event["metadata"]["acceptedByHermesBridge"] is True
    run.worker_thread.join(timeout=1)


def test_linkcrest_openclaw_descriptors_attach_when_provided(monkeypatch):
    class FakeAIAgent:
        def __init__(self, **kwargs):
            self.tools = get_tool_definitions(
                enabled_toolsets=kwargs.get("enabled_toolsets"),
                disabled_toolsets=kwargs.get("disabled_toolsets"),
                quiet_mode=True,
            )

    bridge = SimpleNamespace(config=SimpleNamespace(api_url="https://example.test"), access_token="token")
    run_manager = HermesRunManager(bridge)
    monkeypatch.setattr("run_agent.AIAgent", FakeAIAgent)

    run = ActiveRun(dispatch_id="dispatch-123", runtime_session_id="session-123")
    descriptor = _tool_with_function_name(
        "local-linkcrest.updateTask",
        "local_linkcrest_update_task",
        "/api/v1/bridge/runtime-dispatches/dispatch-123/marketplace-tools/local-linkcrest/updateTask",
    )

    with run_manager.marketplace_proxy.registered_for_payload(
        {"dispatchId": "dispatch-123", "marketplaceTools": [descriptor]},
        run,
    ) as names:
        agent = run_manager._build_agent(
            run,
            {
                "model": "test-model",
                "enabledToolsets": ["skills"],
                "_marketplaceToolNames": names,
                "_autonomyPolicy": {
                    "mode": "dangerously_skip_permissions",
                    "enabledToolCategories": ["task_update"],
                },
            },
        )

    tool_names = {tool["function"]["name"] for tool in agent.tools}
    assert names == ["local-linkcrest.updateTask"]
    assert "local_linkcrest_update_task" in tool_names


def test_linkcrest_agent_api_aliases_exist_and_remain_after_failed_call(monkeypatch):
    bridge = SimpleNamespace(config=SimpleNamespace(api_url="https://example.test"), access_token="token")
    proxy = MarketplaceRuntimeToolProxy(bridge)
    run = ActiveRun(dispatch_id="dispatch-123", runtime_session_id="session-123")
    descriptor = _tool_with_function_name(
        "linkcrest.agentApi",
        "linkcrest_agent_api",
        "/api/v1/bridge/runtime-dispatches/dispatch-123/marketplace-tools/linkcrest/agentApi",
    )

    def fake_urlopen(_request, timeout=None):
        raise RuntimeError("boom")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    with proxy.registered_for_payload({"dispatchId": "dispatch-123", "marketplaceTools": [descriptor]}, run):
        for alias in ("linkcrest.agentApi", "linkcrest_agent_api", "linkcrest-agent-api", "agentApi"):
            assert registry.get_entry(alias) is not None

        result = json.loads(registry.dispatch("linkcrest_agent_api", {"path": "/settings"}))
        assert "RuntimeError: boom" in result["error"]

        for alias in ("linkcrest.agentApi", "linkcrest_agent_api", "linkcrest-agent-api", "agentApi"):
            assert registry.get_entry(alias) is not None

    for alias in ("linkcrest.agentApi", "linkcrest_agent_api", "linkcrest-agent-api", "agentApi"):
        assert registry.get_entry(alias) is None


def test_linkcrest_agent_api_stub_returns_descriptor_missing_when_absent():
    bridge = SimpleNamespace(config=SimpleNamespace(api_url="https://example.test"), access_token="token")
    proxy = MarketplaceRuntimeToolProxy(bridge)
    run = ActiveRun(dispatch_id="dispatch-123", runtime_session_id="session-123")

    with proxy.registered_for_payload({"dispatchId": "dispatch-123", "appSlug": "local-linkcrest", "marketplaceTools": []}, run):
        result = json.loads(registry.dispatch("linkcrest_agent_api", {}))

    assert result["error"]["code"] == "tool_descriptor_missing"


def test_linkcrest_agent_api_stub_returns_not_granted():
    bridge = SimpleNamespace(config=SimpleNamespace(api_url="https://example.test"), access_token="token")
    proxy = MarketplaceRuntimeToolProxy(bridge)
    run = ActiveRun(dispatch_id="dispatch-123", runtime_session_id="session-123")

    with proxy.registered_for_payload(
        {
            "dispatchId": "dispatch-123",
            "appSlug": "local-linkcrest",
            "marketplaceTools": [],
            "toolStatusByCategory": {"linkcrest_agent_api": {"connected": True, "granted": False}},
        },
        run,
    ):
        result = json.loads(registry.dispatch("agentApi", {}))

    assert result["error"]["code"] == "tool_not_granted"


def test_manager_and_worker_dispatch_get_linkcrest_aliases():
    bridge = SimpleNamespace(config=SimpleNamespace(api_url="https://example.test"), access_token="token")
    proxy = MarketplaceRuntimeToolProxy(bridge)
    descriptor = _tool_with_function_name("linkcrest.agentApi", "linkcrest_agent_api")

    for role in ("manager", "worker"):
        run = ActiveRun(dispatch_id=f"dispatch-{role}", runtime_session_id="session-123")
        with proxy.registered_for_payload({"dispatchId": f"dispatch-{role}", "role": role, "marketplaceTools": [descriptor]}, run):
            assert registry.get_entry("linkcrest_agent_api") is not None
            assert registry.get_entry("agentApi") is not None


def test_policy_browser_categories_preserve_native_defaults(monkeypatch):
    captured = {}

    class FakeAIAgent:
        def __init__(self, **kwargs):
            captured["enabled_toolsets"] = kwargs.get("enabled_toolsets")
            self.tools = get_tool_definitions(
                enabled_toolsets=kwargs.get("enabled_toolsets"),
                disabled_toolsets=kwargs.get("disabled_toolsets"),
                quiet_mode=True,
            )

    bridge = SimpleNamespace(config=SimpleNamespace(api_url="https://example.test"), access_token="token")
    run_manager = HermesRunManager(bridge)
    monkeypatch.setattr("run_agent.AIAgent", FakeAIAgent)
    run = ActiveRun(dispatch_id="dispatch-123", runtime_session_id="session-123")

    agent = run_manager._build_agent(
        run,
        {
            "model": "test-model",
            "enabledToolsets": ["skills"],
            "_autonomyPolicy": {
                "mode": "dangerously_skip_permissions",
                "enabledToolCategories": ["browser_navigation", "form_fill", "form_submit"],
            },
        },
    )

    tool_names = {tool["function"]["name"] for tool in agent.tools}
    assert captured["enabled_toolsets"] is None
    assert "browser_navigate" in tool_names
    assert "browser_type" in tool_names
    assert "browser_click" in tool_names
    assert "read_file" in tool_names
    assert "terminal" in tool_names
    assert "memory" in tool_names


def test_explicit_browser_toolset_survives_safe_policy_disabled_subtraction(monkeypatch):
    captured = {}

    class FakeAIAgent:
        def __init__(self, **kwargs):
            captured["enabled_toolsets"] = kwargs.get("enabled_toolsets")
            captured["disabled_toolsets"] = kwargs.get("disabled_toolsets")
            self.tools = get_tool_definitions(
                enabled_toolsets=kwargs.get("enabled_toolsets"),
                disabled_toolsets=kwargs.get("disabled_toolsets"),
                quiet_mode=True,
            )

    bridge = SimpleNamespace(config=SimpleNamespace(api_url="https://example.test"), access_token="token")
    run_manager = HermesRunManager(bridge)
    monkeypatch.setattr("run_agent.AIAgent", FakeAIAgent)
    run = ActiveRun(dispatch_id="dispatch-123", runtime_session_id="session-123")

    agent = run_manager._build_agent(
        run,
        {
            "model": "test-model",
            "enabledToolsets": ["browser"],
            "_autonomyPolicy": {
                "mode": "safe_default",
                "selectedCapabilities": ["read"],
            },
        },
    )

    tool_names = {tool["function"]["name"] for tool in agent.tools}
    assert captured["enabled_toolsets"] is None
    assert "browser" not in captured["disabled_toolsets"]
    assert "browser_navigate" in tool_names
    assert "browser_snapshot" in tool_names
    assert "read_file" in tool_names


def test_available_runtime_browser_tools_attach_browser_toolset(monkeypatch):
    captured = {}

    class FakeAIAgent:
        def __init__(self, **kwargs):
            captured["enabled_toolsets"] = kwargs.get("enabled_toolsets")
            captured["disabled_toolsets"] = kwargs.get("disabled_toolsets")
            self.tools = get_tool_definitions(
                enabled_toolsets=kwargs.get("enabled_toolsets"),
                disabled_toolsets=kwargs.get("disabled_toolsets"),
                quiet_mode=True,
            )

    bridge = SimpleNamespace(config=SimpleNamespace(api_url="https://example.test"), access_token="token")
    run_manager = HermesRunManager(bridge)
    monkeypatch.setattr("run_agent.AIAgent", FakeAIAgent)
    run = ActiveRun(dispatch_id="dispatch-123", runtime_session_id="session-123")

    agent = run_manager._build_agent(
        run,
        {
            "model": "test-model",
            "marketplaceRuntimeContext": {
                "availableRuntimeTools": [
                    "browser_navigate",
                    "browser_snapshot",
                    "browser_click",
                    "browser_type",
                    "browser_vision",
                ],
            },
            "_autonomyPolicy": {
                "mode": "safe_default",
                "selectedCapabilities": ["read"],
            },
        },
    )

    tool_names = {tool["function"]["name"] for tool in agent.tools}
    assert captured["enabled_toolsets"] is None
    assert "browser" not in captured["disabled_toolsets"]
    assert "browser_navigate" in tool_names
    assert "browser_click" in tool_names
    assert "browser_type" in tool_names
    assert "read_file" in tool_names


def test_runtime_toolsets_additive_and_disabled_are_honored(monkeypatch):
    captured = {}

    class FakeAIAgent:
        def __init__(self, **kwargs):
            captured["enabled_toolsets"] = kwargs.get("enabled_toolsets")
            captured["disabled_toolsets"] = kwargs.get("disabled_toolsets")
            self.tools = get_tool_definitions(
                enabled_toolsets=kwargs.get("enabled_toolsets"),
                disabled_toolsets=kwargs.get("disabled_toolsets"),
                quiet_mode=True,
            )

    bridge = SimpleNamespace(config=SimpleNamespace(api_url="https://example.test"), access_token="token")
    run_manager = HermesRunManager(bridge)
    monkeypatch.setattr("run_agent.AIAgent", FakeAIAgent)
    run = ActiveRun(dispatch_id="dispatch-123", runtime_session_id="session-123")

    agent = run_manager._build_agent(
        run,
        {
            "model": "test-model",
            "runtimeToolsets": {
                "additive": ["browser"],
                "disabled": ["web"],
            },
        },
    )

    tool_names = {tool["function"]["name"] for tool in agent.tools}
    assert captured["enabled_toolsets"] is None
    assert "web" in captured["disabled_toolsets"]
    assert "browser_navigate" in tool_names
    assert "web_search" not in tool_names
    assert "read_file" in tool_names
    assert "memory" in tool_names


def test_bridge_capabilities_include_marketplace_tools():
    assert "clawchat.runtime.hermes" in BRIDGE_CAPABILITIES
    assert "clawchat.marketplace.tools" in BRIDGE_CAPABILITIES


def test_completed_dispatch_metadata_records_marketplace_tool_names(monkeypatch):
    class FakeAIAgent:
        def __init__(self, **kwargs):
            self.tools = get_tool_definitions(
                enabled_toolsets=kwargs.get("enabled_toolsets"),
                disabled_toolsets=kwargs.get("disabled_toolsets"),
                quiet_mode=True,
            )

        def run_conversation(self, **_kwargs):
            return {
                "completed": True,
                "final_response": "done",
                "messages": [{"role": "assistant", "content": "done"}],
            }

    bridge = SimpleNamespace(config=SimpleNamespace(api_url="https://example.test"), access_token="token")
    run_manager = HermesRunManager(bridge)
    monkeypatch.setattr("run_agent.AIAgent", FakeAIAgent)
    monkeypatch.setattr(run_manager.snapshot_store, "load", lambda _session_id: [])
    monkeypatch.setattr(run_manager.snapshot_store, "save", lambda _session_id, _messages: None)
    monkeypatch.setattr(run_manager, "_build_default_skills_prompt", lambda _run, _payload: (None, []))
    monkeypatch.setattr(run_manager, "_response_contract_prompt", lambda _payload: (None, {}))

    payload = {
        "dispatchId": "dispatch-123",
        "runtimeSessionId": "session-123",
        "inputText": "hello",
        "marketplaceTools": [
            _tool("x.getMe", "/api/v1/bridge/runtime-dispatches/dispatch-123/marketplace-tools/x/getMe")
        ],
    }
    run = ActiveRun(dispatch_id="dispatch-123", runtime_session_id="session-123")

    run_manager._run_agent(run, payload)

    events = []
    while not run.event_queue.empty():
        events.append(run.event_queue.get_nowait())
    completed = next(event for event in events if event["type"] == "run.completed")
    assert completed["metadata"]["marketplaceTools"] == ["x.getMe"]
