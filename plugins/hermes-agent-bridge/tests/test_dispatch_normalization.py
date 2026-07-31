import asyncio
import json

from clawchat_bridge.main import (
    BridgeConfig,
    ClawChatHermesBridge,
    DispatchPayloadNormalizer,
    HermesRunManager,
)


def _x_tool(name: str, function_name: str | None = None) -> dict:
    tool = {
        "name": name,
        "description": f"{name} descriptor",
        "inputSchema": {
            "type": "object",
            "properties": {
                "value": {"type": "string"},
                "accessToken": {"type": "string"},
            },
            "required": ["accessToken"],
        },
        "executionUrl": f"/api/v1/bridge/runtime-dispatches/social-hello/marketplace-tools/x/{function_name or name}",
    }
    if function_name:
        tool["functionName"] = function_name
    return tool


def _bridge() -> ClawChatHermesBridge:
    return ClawChatHermesBridge(
        BridgeConfig(
            api_url="http://clawchat.local",
            device_public_id="device",
            device_token="token",
            external_agent_ids=["social_hermes", "plain_hermes"],
        )
    )


def _dispatch(payload: dict) -> str:
    return json.dumps({"type": "hermes.run.dispatch", "data": payload})


def _bloated_social_payload() -> dict:
    x_get_me = _x_tool("x.getMe", "x_get_me")
    x_get_user_tweets = _x_tool("x.getUserTweets", "x_get_user_tweets")
    repeated_runtime_instruction = {
        "text": "Use the allowed X tools only when the user asks for social account information.",
        "autonomyPolicy": {"mode": "safe_default", "selectedCapabilities": ["read"]},
    }
    response_contract = {
        "responsePresentation": "plain_text",
        "format": "short chat response",
    }
    return {
        "dispatchId": "social-hello",
        "runtimeSessionId": "runtime-social",
        "externalAgentId": "social_hermes",
        "agentId": "agent-social",
        "threadId": "thread-social",
        "inputText": "hello",
        "workspaceRoot": "/tmp",
        "runtimeToolsets": {
            "additive": ["browser", "browser"],
            "disabled": ["web", "web"],
        },
        "availableRuntimeTools": ["browser_navigate", "browser_snapshot"],
        "runtimeInstruction": repeated_runtime_instruction,
        "systemInstruction": "You are rendering a ClawChat message.",
        "responseContract": response_contract,
        "marketplaceTools": [x_get_me, x_get_user_tweets, x_get_me],
        "availableMarketplaceTools": [x_get_me],
        "marketplaceRuntimeContext": {
            "appSlug": "x",
            "linkedAppId": "app-x",
            "tools": [x_get_me, x_get_user_tweets],
            "runtimeToolsets": {"additive": ["browser"], "disabled": ["web"]},
            "availableRuntimeTools": ["browser_click"],
            "runtimeInstruction": repeated_runtime_instruction,
            "responseContract": response_contract,
        },
        "dispatchMetadata": {
            "marketplaceTools": [x_get_me, x_get_user_tweets],
            "availableMarketplaceTools": [x_get_me],
            "marketplaceRuntimeContext": {
                "appSlug": "x",
                "linkedAppId": "app-x",
                "tools": [x_get_me, x_get_user_tweets],
                "runtimeToolsets": {"additive": ["browser"], "disabled": ["web"]},
                "availableRuntimeTools": ["browser_type"],
                "runtimeInstruction": repeated_runtime_instruction,
            },
            "runtimeToolsets": {"additive": ["browser"], "disabled": ["web"]},
            "runtimeInstruction": repeated_runtime_instruction,
            "systemInstruction": "You are rendering a ClawChat message.",
            "responseContract": response_contract,
            "largeRedundantBlob": "x" * 4000,
        },
    }


def test_bloated_social_dispatch_is_normalized_before_run_manager_start(monkeypatch, caplog):
    bridge = _bridge()
    started = []

    monkeypatch.setattr(bridge.run_manager, "start", lambda payload: started.append(payload))

    with caplog.at_level("INFO", logger="clawchat.hermes_bridge"):
        asyncio.run(bridge._handle_ws_text(_dispatch(_bloated_social_payload())))

    assert len(started) == 1
    normalized = started[0]
    diagnostics = normalized["_normalizationDiagnostics"]

    assert diagnostics["incomingSizeBytes"] > diagnostics["normalizedSizeBytes"]
    assert diagnostics["toolCountBefore"] == 11
    assert diagnostics["toolCountAfter"] == 2
    assert diagnostics["instructionCharsBefore"] > diagnostics["instructionCharsAfter"]
    assert "dispatchMetadata.marketplaceTools" in diagnostics["droppedDuplicateFields"]
    assert "marketplaceRuntimeContext.tools" in diagnostics["droppedDuplicateFields"]

    assert [tool["functionName"] for tool in normalized["marketplaceTools"]] == [
        "x_get_me",
        "x_get_user_tweets",
    ]
    assert "availableMarketplaceTools" not in normalized
    assert "tools" not in normalized["marketplaceRuntimeContext"]
    assert "marketplaceTools" not in normalized["dispatchMetadata"]
    assert "runtimeInstruction" not in normalized["dispatchMetadata"]
    assert normalized["runtimeToolsets"] == {"additive": ["browser"], "disabled": ["web"]}
    assert set(normalized["availableRuntimeTools"]) >= {"browser_navigate", "browser_snapshot", "browser_click", "browser_type"}
    assert "Hermes dispatch normalization dispatchId=social-hello" in caplog.text


def test_plain_agent_with_empty_canonical_tools_does_not_receive_stale_nested_x_tools(monkeypatch):
    bridge = _bridge()
    stale_x = _x_tool("x.getMe", "x_get_me")
    payload = {
        "dispatchId": "plain-hello",
        "runtimeSessionId": "runtime-plain",
        "externalAgentId": "plain_hermes",
        "inputText": "hello",
        "marketplaceTools": [],
        "dispatchMetadata": {
            "marketplaceTools": [stale_x],
            "availableMarketplaceTools": [stale_x],
        },
    }
    started = []
    monkeypatch.setattr(bridge.run_manager, "start", lambda normalized: started.append(normalized))

    asyncio.run(bridge._handle_ws_text(_dispatch(payload)))

    assert len(started) == 1
    normalized = started[0]
    assert "marketplaceTools" not in normalized
    assert normalized["_normalizationDiagnostics"]["toolCountBefore"] == 2
    assert normalized["_normalizationDiagnostics"]["toolCountAfter"] == 0


def test_runtime_toolsets_additive_and_disabled_survive_normalization():
    payload = _bloated_social_payload()
    normalized, _diagnostics = DispatchPayloadNormalizer().normalize(payload)
    run_manager = HermesRunManager(_bridge())

    assert run_manager._enabled_toolsets_from_payload(normalized) == ["browser"]
    assert run_manager._disabled_toolsets_from_payload(normalized) == ["web"]
