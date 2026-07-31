import json
import logging
from pathlib import Path

import pytest

from clawchat_bridge.main import (
    BRIDGE_CAPABILITIES,
    BridgeConfig,
    ClawChatHermesBridge,
    MarketplaceLocalAppAgentApiSetup,
)


SECRET = "lc_test_secret_bearer"


class FakeSetup(MarketplaceLocalAppAgentApiSetup):
    def __init__(self, responses, bearer=SECRET):
        self.responses = responses
        self.bearer = bearer
        self.requests = []
        self.rotations = 0

    def _request(self, method, url, *, headers=None, body=None):
        self.requests.append({"method": method, "url": url, "headers": headers or {}, "body": body})
        auth = (headers or {}).get("Authorization")
        if method == "GET" and url == "http://localhost:3052":
            return 200, {}
        if method == "POST" and url == "http://localhost:3052/api/settings/agent-key":
            self.rotations += 1
            return 200, {"apiKey": self.bearer}
        if auth and self.bearer in auth:
            if "/settings?" in url:
                return 200, {"data": {"hasAgentApiKey": True}}
            if "/campaigns?" in url:
                return 200, {"data": self.responses.get("campaigns", [])}
            if "/tasks?" in url:
                return 200, {"data": []}
        if "/settings?" in url:
            return self.responses.get("settings_unauth", 401), {"error": "Missing bearer token."}
        return 404, {"error": "not found"}


class FakePolicySetup(FakeSetup):
    def _request(self, method, url, *, headers=None, body=None):
        if method == "POST" and "/api/openclaw/autonomy/" in url:
            self.requests.append({"method": method, "url": url, "headers": headers or {}, "body": body})
            if url.endswith("/autonomy/get_policy"):
                return 200, {"data": {"policy": {"campaignId": "camp_1", "mode": "safe_default", "allowInternalWrites": True}}}
            if url.endswith("/autonomy/update_policy"):
                return 200, {"data": {"ok": True, "bearerKey": SECRET}}
            if url.endswith("/autonomy/explain_effective_policy"):
                return 200, {"data": "effective"}
        return super()._request(method, url, headers=headers, body=body)


def _setup_result(fake, tmp_path, **payload):
    repo = tmp_path / "LinkCrest"
    repo.mkdir()
    base = {"repoPath": str(repo), "localAppUrl": "http://localhost:3052"}
    base.update(payload)
    return fake.setup(base)


def test_capability_list_includes_local_app_agent_api_setup():
    assert "marketplaceLocalAppAgentApiSetup" in BRIDGE_CAPABILITIES


def test_event_handler_exists_and_sends_result(monkeypatch):
    bridge = ClawChatHermesBridge(
        BridgeConfig(
            api_url="http://clawchat.local",
            device_public_id="device",
            device_token="token",
            external_agent_ids=["agent"],
        )
    )
    bridge.marketplace_local_app_agent_api_setup = type(
        "StubSetup",
        (),
        {"setup": lambda self, data: {"requestId": data["requestId"], "status": "ok", "bearerKey": SECRET}},
    )()
    sent = []

    async def fake_send_raw(payload):
        sent.append(payload)

    monkeypatch.setattr(bridge, "_send_raw", fake_send_raw)

    message = json.dumps({"type": "marketplace.localAppAgentApiSetup", "data": {"requestId": "req_1"}})
    import asyncio

    asyncio.run(bridge._handle_ws_text(message))

    assert sent[0]["type"] == "marketplace.localAppAgentApiSetup.result"
    assert sent[0]["data"]["status"] == "ok"


def test_derives_agent_api_url():
    assert (
        MarketplaceLocalAppAgentApiSetup().derive_agent_api_base_url("http://localhost:3052")
        == "http://localhost:3052/api/openclaw"
    )


def test_detects_401_as_route_exists_and_rotates_bearer(tmp_path):
    fake = FakeSetup({"settings_unauth": 401, "campaigns": []})
    result = _setup_result(fake, tmp_path)

    assert result["agentApiRouteReachable"] is True
    assert result["diagnostics"]["settingsUnauthStatus"] == 401
    assert result["diagnostics"]["rotatedBearer"] is True
    assert result["bearerKey"] == SECRET


def test_detects_503_as_key_not_configured_and_generates_bearer(tmp_path):
    fake = FakeSetup({"settings_unauth": 503, "campaigns": []})
    result = _setup_result(fake, tmp_path)

    assert result["agentApiRouteReachable"] is True
    assert result["diagnostics"]["settingsUnauthStatus"] == 503
    assert result["diagnostics"]["generatedNewBearer"] is True
    assert result["bearerConfigured"] is True


def test_returns_bearer_only_outside_diagnostics_and_does_not_log(caplog, tmp_path):
    fake = FakeSetup({"settings_unauth": 401, "campaigns": []})
    caplog.set_level(logging.INFO)
    result = _setup_result(fake, tmp_path)

    assert result["bearerKey"] == SECRET
    assert SECRET not in json.dumps(result["diagnostics"])
    assert SECRET not in caplog.text


def test_lists_campaigns_and_auto_selects_one_active_campaign(tmp_path):
    campaign = {"_id": "camp_1", "name": "One", "status": "active"}
    fake = FakeSetup({"settings_unauth": 401, "campaigns": [campaign]})
    result = _setup_result(fake, tmp_path)

    assert result["campaigns"] == [campaign]
    assert result["selectedCampaign"] == campaign
    assert result["diagnostics"]["tasksStatus"] == 200


def test_requires_selection_for_multiple_active_campaigns(tmp_path):
    campaigns = [
        {"_id": "camp_1", "name": "One", "status": "active"},
        {"_id": "camp_2", "name": "Two", "status": "active"},
    ]
    fake = FakeSetup({"settings_unauth": 401, "campaigns": campaigns})
    result = _setup_result(fake, tmp_path)

    assert result["campaigns"] == campaigns
    assert result["selectedCampaign"] is None
    assert result["diagnostics"]["tasksStatus"] is None


def test_syncs_policy_for_selected_campaign_and_redacts_policy_response(tmp_path):
    campaign = {"_id": "camp_1", "name": "One", "status": "active"}
    fake = FakePolicySetup({"settings_unauth": 401, "campaigns": [campaign]})
    result = _setup_result(fake, tmp_path, autonomyMode="dangerously_skip_permissions")

    policy_requests = [request for request in fake.requests if "/autonomy/update_policy" in request["url"]]
    assert policy_requests[0]["body"]["input"]["mode"] == "dangerously_skip_permissions"
    assert result["policySync"]["status"] == "ok"
    assert SECRET not in json.dumps(result["policySync"])


def test_no_bearer_appears_in_diagnostics(tmp_path):
    fake = FakeSetup({"settings_unauth": 401, "campaigns": [{"_id": "camp_1", "status": "active"}]})
    result = _setup_result(fake, tmp_path)

    assert SECRET not in json.dumps(result["diagnostics"])
    assert result["diagnostics"]["secretMaterialLogged"] is False


def test_update_safe_overlay_copy_matches_installed_bridge():
    pytest.skip("Overlay copy belongs to current ClawChat repo; do not read stale local ClawChat checkout in Hermes bridge tests.")
    installed_root = Path(__file__).resolve().parents[2]
    overlay_root = Path("/home/alexkerss/repos/ClawChat/hermes-runtime/clawchat-bridge-overlay")
    assert (overlay_root / "clawchat_bridge/main.py").read_text(encoding="utf-8") == (
        installed_root / "clawchat_bridge/main.py"
    ).read_text(encoding="utf-8")
    assert (
        overlay_root / "tests/clawchat_bridge/test_marketplace_local_app_agent_api_setup.py"
    ).read_text(encoding="utf-8") == Path(__file__).read_text(encoding="utf-8")
