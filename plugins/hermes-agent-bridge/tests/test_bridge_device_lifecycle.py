import asyncio
import json

from clawchat_bridge import main
from clawchat_bridge.main import BridgeConfig, rotate_device_credential


def test_bridge_metadata_declares_runtime_host_and_protocol_contracts():
    metadata = main._bridge_device_metadata()

    assert metadata["runtimeType"] == "hermes"
    assert metadata["hostType"] in {"macos-launchd", "linux-systemd"}
    assert metadata["pluginVersion"] == "0.3.0-rc.1"
    assert metadata["apiContractVersion"] == "v1"
    assert metadata["websocketContractVersion"] == "bridge.v1"
    assert "clawchat.runtime.hermes" in metadata["capabilities"]
    assert "clawchat.runtime_connector.v2" in metadata["capabilities"]


def test_rotation_atomically_replaces_saved_credential_without_printing_it(monkeypatch, tmp_path, capsys):
    captured = {}

    class FakeResponse:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def text(self):
            return json.dumps(
                {
                    "credentials": {
                        "devicePublicId": "bdev_public",
                        "deviceToken": "replacement-secret",
                    }
                }
            )

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def post(self, url, json):
            captured["url"] = url
            captured["payload"] = json
            return FakeResponse()

    monkeypatch.setattr(main.aiohttp, "ClientSession", FakeSession)
    path = tmp_path / "config.json"
    config = BridgeConfig(
        api_url="https://relay.example.com",
        workspace_id="workspace-1",
        device_public_id="bdev_public",
        device_token="current-secret",
        external_agent_ids=["main"],
    )
    config.save(path)

    asyncio.run(rotate_device_credential(config, path))

    assert captured["url"] == "https://relay.example.com/api/v1/bridge/device/rotate"
    assert captured["payload"]["deviceToken"] == "current-secret"
    assert captured["payload"]["runtimeType"] == "hermes"
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["deviceToken"] == "replacement-secret"
    assert "replacement-secret" not in capsys.readouterr().out
