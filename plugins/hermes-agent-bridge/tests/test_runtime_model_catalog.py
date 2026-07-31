import asyncio
import json
import sys
import types

from clawchat_bridge import main
from clawchat_bridge.main import BridgeConfig, ClawChatHermesBridge


class FakeResponse:
    def __init__(self, status=200, body=None):
        self.status = status
        self._body = json.dumps(body or {"success": True})

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def text(self):
        return self._body


class FakeSession:
    def __init__(self, status=200):
        self.status = status
        self.requests = []

    def post(self, url, *, json, headers):
        self.requests.append({"url": url, "json": json, "headers": headers})
        return FakeResponse(self.status)


def test_runtime_model_catalog_matches_hermes_discovery(monkeypatch):
    monkeypatch.setattr(
        main,
        "_configured_default_model",
        lambda: "gpt-5.5",
    )
    hermes_cli = types.ModuleType("hermes_cli")
    codex_models = types.ModuleType("hermes_cli.codex_models")
    codex_models.get_codex_model_ids = lambda: [
        "gpt-5.6-sol",
        "gpt-5.6-terra",
        "gpt-5.5",
        "gpt-5.6-sol",
    ]
    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_cli)
    monkeypatch.setitem(sys.modules, "hermes_cli.codex_models", codex_models)

    catalog = main._runtime_model_catalog()

    assert catalog["runtimeType"] == "hermes"
    assert catalog["defaultModel"] == "gpt-5.5"
    assert catalog["models"] == [
        "gpt-5.6-sol",
        "gpt-5.6-terra",
        "gpt-5.5",
    ]
    assert catalog["source"] == "hermes-codex-discovery"


def test_authenticated_bridge_publishes_catalog_without_exposing_token(monkeypatch):
    monkeypatch.setattr(
        main,
        "_runtime_model_catalog",
        lambda: {
            "runtimeType": "hermes",
            "defaultModel": "gpt-5.5",
            "models": ["gpt-5.6-sol", "gpt-5.5"],
            "source": "hermes-codex-discovery",
            "observedAt": "2026-07-25T08:00:00Z",
        },
    )
    bridge = ClawChatHermesBridge(
        BridgeConfig(
            api_url="https://relay.example.com",
            workspace_id="workspace-1",
            device_public_id="bdev-1",
            device_token="device-token",
            external_agent_ids=["main"],
        )
    )
    bridge.access_token = "bridge-access-token"
    session = FakeSession()

    asyncio.run(bridge._publish_runtime_model_catalog(session))

    assert session.requests == [
        {
            "url": (
                "https://relay.example.com"
                "/api/v1/bridge/runtime-model-catalog"
            ),
            "json": {
                "runtimeType": "hermes",
                "defaultModel": "gpt-5.5",
                "models": ["gpt-5.6-sol", "gpt-5.5"],
                "source": "hermes-codex-discovery",
                "observedAt": "2026-07-25T08:00:00Z",
            },
            "headers": {"Authorization": "Bearer bridge-access-token"},
        }
    ]
