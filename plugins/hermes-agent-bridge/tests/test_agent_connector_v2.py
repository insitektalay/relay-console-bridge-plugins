import asyncio
import json
import shutil

import pytest

import clawchat_bridge.main as bridge_main
from clawchat_bridge.main import (
    AGENT_REPLICA_V1,
    RELAY_CONNECTOR_V3,
    RELAY_CONNECTOR_V2,
    BridgeConfig,
    ClawChatHermesBridge,
)


class FakeResponse:
    def __init__(self, status, body):
        self.status = status
        self._body = body if isinstance(body, str) else json.dumps(body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def text(self):
        return self._body


class FakeSession:
    def __init__(self, responder):
        self.responder = responder
        self.requests = []

    def post(self, url, *, json, headers):
        self.requests.append({"url": url, "json": json, "headers": headers})
        status, body = self.responder(json)
        return FakeResponse(status, body)


@pytest.fixture
def bridge_environment(tmp_path, monkeypatch):
    hermes_home = tmp_path / "hermes-home"
    config_dir = tmp_path / "bridge-state"
    monkeypatch.setattr(bridge_main, "get_hermes_home", lambda: hermes_home)
    monkeypatch.setattr(bridge_main, "_config_dir", lambda: config_dir)
    monkeypatch.setattr(bridge_main, "enumerate_native_profiles", lambda: [])
    config = BridgeConfig(
        api_url="https://relay.example.com",
        workspace_id="workspace-1",
        device_public_id="bdev-1",
        device_token="device-token",
        external_agent_ids=["main"],
    )
    workspace = hermes_home / "clawchat" / "workspaces" / "workspace-1" / "agents" / "main" / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "SOUL.md").write_text("Be useful.\n", encoding="utf-8")
    (workspace / "api-token.md").write_text("must not sync\n", encoding="utf-8")
    return config, config_dir


def test_connector_v2_sends_complete_manifest_and_persists_canonical_identity(bridge_environment):
    config, config_dir = bridge_environment

    def responder(payload):
        return 200, {
            "protocolVersion": RELAY_CONNECTOR_V2,
            "agents": [
                {
                    "externalId": "main",
                    "canonicalAgentId": "agt-canonical-1",
                    "profileServerVersion": "7",
                    "documents": [],
                }
            ],
            "conflicts": [],
        }

    bridge = ClawChatHermesBridge(config)
    bridge._agent_sync_protocol = RELAY_CONNECTOR_V2
    bridge.session = FakeSession(responder)
    bridge.access_token = "access-token"

    assert asyncio.run(bridge._exchange_agent_replicas()) == ["main"]
    assert asyncio.run(bridge._exchange_agent_replicas()) == ["main"]

    first = bridge.session.requests[0]["json"]
    second = bridge.session.requests[1]["json"]
    assert first["protocolVersion"] == RELAY_CONNECTOR_V2
    assert first["completeManifest"] is True
    assert len(first["manifestHash"]) == 64
    assert first["host"]["protocolVersion"] == "2"
    assert first["agents"][0].get("canonicalAgentId") is None
    assert [document["filename"] for document in first["agents"][0]["documents"]] == ["SOUL.md"]
    assert second["agents"][0]["canonicalAgentId"] == "agt-canonical-1"

    state = json.loads((config_dir / "agent_sync_state.json").read_text(encoding="utf-8"))
    assert state["version"] == 2
    assert state["profiles"]["main"]["canonicalAgentId"] == "agt-canonical-1"


def test_connector_v2_falls_back_only_for_explicit_unsupported_protocol(bridge_environment):
    config, _config_dir = bridge_environment
    protocols = []

    def responder(payload):
        protocols.append(payload["protocolVersion"])
        if payload["protocolVersion"] in {RELAY_CONNECTOR_V3, RELAY_CONNECTOR_V2}:
            return 400, "UNSUPPORTED_AGENT_REPLICA_PROTOCOL"
        return 200, {
            "protocolVersion": AGENT_REPLICA_V1,
            "agents": [{"externalId": "main", "profileServerVersion": "1", "documents": []}],
        }

    bridge = ClawChatHermesBridge(config)
    bridge.session = FakeSession(responder)
    bridge.access_token = "access-token"

    assert asyncio.run(bridge._exchange_agent_replicas()) == ["main"]
    assert protocols == [RELAY_CONNECTOR_V3, RELAY_CONNECTOR_V2, AGENT_REPLICA_V1]
    assert bridge._agent_sync_protocol == AGENT_REPLICA_V1
    assert "host" not in bridge.session.requests[2]["json"]


def test_authentication_failure_never_downgrades_connector_protocol(bridge_environment):
    config, _config_dir = bridge_environment
    protocols = []

    def responder(payload):
        protocols.append(payload["protocolVersion"])
        return 401, "unauthorized"

    bridge = ClawChatHermesBridge(config)
    bridge.session = FakeSession(responder)
    bridge.access_token = "bad-token"

    with pytest.raises(RuntimeError, match="HERMES_AGENT_SYNC_HTTP_401"):
        asyncio.run(bridge._exchange_agent_replicas())

    assert protocols == [RELAY_CONNECTOR_V3]
    assert bridge._agent_sync_protocol == RELAY_CONNECTOR_V3


def test_empty_replica_exchange_keeps_configured_agents_registered(bridge_environment):
    config, _config_dir = bridge_environment
    config.external_agent_ids = ["main", "second"]
    bridge = ClawChatHermesBridge(config)
    registered = []

    async def empty_exchange():
        return []

    async def capture_registration(external_agent_id):
        registered.append(external_agent_id)

    bridge._exchange_agent_replicas = empty_exchange
    bridge.register_hermes_agent = capture_registration

    result = asyncio.run(bridge._synchronize_and_register_agents())

    assert result == ["main", "second"]
    assert bridge._registered_agent_ids == ["main", "second"]
    assert registered == ["main", "second"]


def test_replica_sync_failure_does_not_prevent_local_agent_registration(
    bridge_environment,
):
    config, _config_dir = bridge_environment
    bridge = ClawChatHermesBridge(config)
    registered = []

    async def failed_exchange():
        raise RuntimeError("HERMES_AGENT_SYNC_HTTP_401")

    async def capture_registration(external_agent_id):
        registered.append(external_agent_id)

    bridge._exchange_agent_replicas = failed_exchange
    bridge.register_hermes_agent = capture_registration

    result = asyncio.run(bridge._synchronize_and_register_agents())

    assert result == ["main"]
    assert bridge._registered_agent_ids == ["main"]
    assert registered == ["main"]


def test_connector_v3_reads_no_documents_until_profile_is_connected(bridge_environment):
    config, _config_dir = bridge_environment
    exchange = 0

    def responder(payload):
        nonlocal exchange
        exchange += 1
        if exchange == 1:
            return 200, {
                "protocolVersion": RELAY_CONNECTOR_V3,
                "agents": [],
                "discoveries": [{
                    "externalId": "main",
                    "observationId": "observation-1",
                    "canonicalAgentId": None,
                    "directive": "metadata_only",
                    "connectionState": "discovered",
                    "documentSync": False,
                }],
            }
        return 200, {
            "protocolVersion": RELAY_CONNECTOR_V3,
            "agents": [{
                "externalId": "main",
                "canonicalAgentId": "agent-1",
                "bindingEpoch": "4",
                "profileServerVersion": "1",
                "documents": [],
            }],
            "discoveries": [],
        }

    bridge = ClawChatHermesBridge(config)
    bridge.session = FakeSession(responder)
    bridge.access_token = "access-token"

    asyncio.run(bridge._exchange_agent_replicas())
    asyncio.run(bridge._exchange_agent_replicas())
    asyncio.run(bridge._exchange_agent_replicas())

    first, second, third = [
        request["json"]["agents"][0]
        for request in bridge.session.requests
    ]
    assert first["documents"] == []
    assert second["documents"] == []
    assert second.get("bindingEpoch") is None
    assert third["bindingEpoch"] == "4"
    assert [document["filename"] for document in third["documents"]] == ["SOUL.md"]
    assert bridge.session.requests[0]["json"]["completeInventory"] is True
    assert bridge.session.requests[0]["json"]["host"]["protocolVersion"] == "3"


def test_incomplete_scan_does_not_emit_tombstones_for_prior_documents(
    bridge_environment,
):
    config, config_dir = bridge_environment
    exchange = 0

    def responder(payload):
        nonlocal exchange
        exchange += 1
        return 200, {
            "protocolVersion": RELAY_CONNECTOR_V2,
            "agents": [{
                "externalId": "main",
                "canonicalAgentId": "agent-1",
                "profileServerVersion": "1",
                "documents": [{
                    "objectId": "document-1",
                    "folder": "",
                    "filename": "SOUL.md",
                    "content": "Be useful.\n",
                    "contentHash": "server-hash",
                    "serverVersion": "1",
                    "deleted": False,
                }],
            }],
            "conflicts": [],
        }

    bridge = ClawChatHermesBridge(config)
    bridge._agent_sync_protocol = RELAY_CONNECTOR_V2
    bridge.session = FakeSession(responder)
    bridge.access_token = "access-token"

    asyncio.run(bridge._exchange_agent_replicas())
    workspace = (
        config_dir.parent
        / "hermes-home"
        / "clawchat"
        / "workspaces"
        / "workspace-1"
        / "agents"
        / "main"
        / "workspace"
    )
    shutil.rmtree(workspace)
    asyncio.run(bridge._exchange_agent_replicas())

    second = bridge.session.requests[1]["json"]
    assert second["completeManifest"] is False
    assert second["agents"][0]["documents"] == []
    assert not workspace.exists()
