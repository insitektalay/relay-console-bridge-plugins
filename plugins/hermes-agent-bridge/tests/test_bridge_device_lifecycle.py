import asyncio
import json

from clawchat_bridge import main
from clawchat_bridge.main import BridgeConfig, rotate_device_credential


def test_bridge_auth_tokens_keep_http_and_websocket_scopes_separate():
    access_token, websocket_token = main._bridge_auth_tokens(
        {"tokens": {"accessToken": "http-access", "wsToken": "websocket-access"}}
    )

    assert access_token == "http-access"
    assert websocket_token == "websocket-access"


def test_bridge_auth_tokens_do_not_cross_fallback_between_scopes():
    assert main._bridge_auth_tokens({"tokens": {"accessToken": "http-only"}}) == (
        "http-only",
        None,
    )
    assert main._bridge_auth_tokens({"tokens": {"wsToken": "websocket-only"}}) == (
        None,
        "websocket-only",
    )


def test_bridge_auth_tokens_support_legacy_single_token():
    assert main._bridge_auth_tokens({"token": "legacy"}) == ("legacy", "legacy")


def test_bridge_metadata_declares_runtime_host_and_protocol_contracts():
    metadata = main._bridge_device_metadata()

    assert metadata["runtimeType"] == "hermes"
    assert metadata["hostType"] in {"macos-launchd", "linux-systemd"}
    assert metadata["pluginVersion"] == "0.3.0-rc.6"
    assert metadata["apiContractVersion"] == "v2"
    assert metadata["websocketContractVersion"] == "bridge.v1"
    assert "clawchat.runtime.hermes" in metadata["capabilities"]
    assert "clawchat.runtime_connector.v2" in metadata["capabilities"]
    assert "clawchat.bridge.rotating_credentials.v1" in metadata["capabilities"]


def test_authentication_persists_replacement_before_returning_tokens(tmp_path):
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
                    "tokens": {"accessToken": "access", "wsToken": "websocket"},
                    "credentials": {
                        "devicePublicId": "bdev_public",
                        "deviceToken": "replacement-secret",
                    },
                }
            )

    class FakeSession:
        def post(self, url, json):
            captured["url"] = url
            captured["payload"] = json
            return FakeResponse()

    path = tmp_path / "config.json"
    config = BridgeConfig(
        api_url="https://relay.example.com",
        workspace_id="workspace-1",
        device_public_id="bdev_public",
        device_token="current-secret",
        external_agent_ids=["main"],
    )
    config.save(path)
    bridge = object.__new__(main.ClawChatHermesBridge)
    bridge.config = config
    bridge.config_path = path

    response = asyncio.run(bridge._authenticate_device(FakeSession()))

    assert response["tokens"]["accessToken"] == "access"
    assert captured["payload"]["deviceToken"] == "current-secret"
    assert captured["payload"]["apiContractVersion"] == "v2"
    assert json.loads(path.read_text(encoding="utf-8"))["deviceToken"] == "replacement-secret"


def test_authentication_withholds_tokens_when_replacement_cannot_be_saved(monkeypatch, tmp_path):
    class FakeResponse:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def text(self):
            return json.dumps(
                {
                    "tokens": {"accessToken": "must-not-be-used"},
                    "credentials": {
                        "devicePublicId": "bdev_public",
                        "deviceToken": "replacement-secret",
                    },
                }
            )

    class FakeSession:
        def post(self, _url, json):
            return FakeResponse()

    config = BridgeConfig(
        api_url="https://relay.example.com",
        workspace_id="workspace-1",
        device_public_id="bdev_public",
        device_token="current-secret",
        external_agent_ids=["main"],
    )
    bridge = object.__new__(main.ClawChatHermesBridge)
    bridge.config = config
    bridge.config_path = tmp_path / "config.json"

    def fail_save(_path):
        raise OSError("disk full")

    monkeypatch.setattr(config, "save", fail_save)

    try:
        asyncio.run(bridge._authenticate_device(FakeSession()))
        raise AssertionError("authentication unexpectedly returned bearer tokens")
    except RuntimeError as exc:
        assert "durable persistence failed" in str(exc)
    assert config.device_token == "replacement-secret"


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
