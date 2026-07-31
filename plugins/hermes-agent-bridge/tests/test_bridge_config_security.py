import json
import stat

import pytest

from clawchat_bridge.main import BridgeConfig, _normalize_api_url


def test_relay_api_url_requires_https(monkeypatch):
    monkeypatch.delenv("RELAY_CONSOLE_BRIDGE_ALLOW_INSECURE_HTTP", raising=False)
    assert _normalize_api_url("https://relay.example.com/api/v1") == "https://relay.example.com"
    with pytest.raises(ValueError, match="requires an https:// API URL"):
        _normalize_api_url("http://relay.example.com")


def test_bridge_config_uses_owner_only_permissions(tmp_path):
    path = tmp_path / "state" / "config.json"
    config = BridgeConfig(
        api_url="https://relay.example.com",
        device_public_id="device-public-id",
        device_token="device-token-sensitive",
        workspace_id="workspace-id",
        external_agent_ids=["my-hermes-agent"],
    )

    config.save(path)

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert json.loads(path.read_text())["deviceToken"] == "device-token-sensitive"
