import asyncio
import json
import logging
import socket
import urllib.error
from pathlib import Path

import pytest

from clawchat_bridge.main import (
    BRIDGE_CAPABILITIES,
    BridgeConfig,
    ClawChatHermesBridge,
    MarketplaceLocalAppAgentApiRequestProxy,
)


SECRET = "lc_proxy_secret_bearer"


class FakeProxy(MarketplaceLocalAppAgentApiRequestProxy):
    def __init__(self, status=200, body=None, headers=None):
        self.status = status
        self.body = {"data": {"ok": True}} if body is None else body
        self.headers = {"Content-Type": "application/json"} if headers is None else headers
        self.calls = []

    def _execute(self, method, url, headers, body, timeout_s):
        self.calls.append({"method": method, "url": url, "headers": headers, "body": body, "timeout_s": timeout_s})
        payload = self.body
        if isinstance(payload, bytes):
            raw = payload
        elif isinstance(payload, str):
            raw = payload.encode("utf-8")
        else:
            raw = json.dumps(payload).encode("utf-8")
        return self.status, self.headers, raw


class TimeoutProxy(FakeProxy):
    def _execute(self, method, url, headers, body, timeout_s):
        raise socket.timeout()


class UnreachableProxy(FakeProxy):
    def _execute(self, method, url, headers, body, timeout_s):
        raise urllib.error.URLError("connection refused")


def _payload(**updates):
    payload = {
        "requestId": "req_proxy",
        "appSlug": "local-linkcrest",
        "baseUrl": "http://localhost:3052",
        "method": "GET",
        "path": "/api/openclaw/settings",
        "contractVersion": "2026-03-18",
        "bearerKey": SECRET,
    }
    payload.update(updates)
    return payload


def test_capability_confirms_marketplace_tools():
    assert "clawchat.marketplace.tools" in BRIDGE_CAPABILITIES
    assert "marketplaceLocalAppAgentApiRequest" in BRIDGE_CAPABILITIES


def test_handler_exists_and_sends_proxy_result(monkeypatch):
    bridge = ClawChatHermesBridge(
        BridgeConfig(
            api_url="http://clawchat.local",
            device_public_id="device",
            device_token="token",
            external_agent_ids=["agent"],
        )
    )
    bridge.marketplace_local_app_agent_api_request_proxy = type(
        "StubProxy",
        (),
        {"handle": lambda self, data: {"requestId": data["requestId"], "status": "ok", "httpStatus": 200}},
    )()
    sent = []

    async def fake_send_raw(payload):
        sent.append(payload)

    monkeypatch.setattr(bridge, "_send_raw", fake_send_raw)

    asyncio.run(bridge._handle_ws_text(json.dumps({"type": "marketplace.localAppAgentApiRequest", "data": _payload()})))

    assert sent[0]["type"] == "marketplace.localAppAgentApiRequest.result"
    assert sent[0]["requestId"] == "req_proxy"
    assert sent[0]["data"]["httpStatus"] == 200


def test_accepts_localhost_3052_openclaw_settings_and_returns_json_body():
    proxy = FakeProxy(body={"data": {"hasAgentApiKey": True}})
    result = proxy.handle(_payload())

    assert result["status"] == "ok"
    assert result["httpStatus"] == 200
    assert result["body"] == {"data": {"hasAgentApiKey": True}}
    assert result["data"] == {"data": {"hasAgentApiKey": True}}
    assert proxy.calls[0]["url"] == "http://localhost:3052/api/openclaw/settings?contractVersion=2026-03-18"


def test_accepts_full_url_target_under_openclaw():
    proxy = FakeProxy()
    result = proxy.handle(
        _payload(
            path="/api/not-openclaw/stale",
            endpoint="http://localhost:3052/api/openclaw/settings?contractVersion=2026-03-18",
        )
    )

    assert result["ok"] is True
    assert proxy.calls[0]["url"] == "http://localhost:3052/api/openclaw/settings?contractVersion=2026-03-18"


def test_accepts_127_0_0_1_full_url_target_under_openclaw():
    proxy = FakeProxy()
    result = proxy.handle(_payload(targetUrl="http://127.0.0.1:3052/api/openclaw/settings?contractVersion=2026-03-18"))

    assert result["ok"] is True
    assert proxy.calls[0]["url"] == "http://127.0.0.1:3052/api/openclaw/settings?contractVersion=2026-03-18"


def test_accepts_relative_openclaw_path_with_query():
    proxy = FakeProxy()
    result = proxy.handle(_payload(path="/api/openclaw/settings?contractVersion=2026-03-18"))

    assert result["ok"] is True
    assert proxy.calls[0]["url"] == "http://localhost:3052/api/openclaw/settings?contractVersion=2026-03-18"


def test_rejects_non_openclaw_paths():
    result = FakeProxy().handle(_payload(path="/api/settings/agent-key"))

    assert result["status"] == "failed"
    assert result["error"]["code"] == "source_host_rejected_target"

    full_url = FakeProxy().handle(_payload(path="http://localhost:3052/api/not-openclaw/settings"))

    assert full_url["status"] == "failed"
    assert full_url["error"]["code"] == "source_host_rejected_target"


def test_rejects_unrelated_localhost_ports():
    result = FakeProxy().handle(_payload(baseUrl="http://localhost:8080"))

    assert result["status"] == "failed"
    assert result["error"]["code"] == "source_host_rejected_target"

    full_url = FakeProxy().handle(_payload(targetUrl="http://localhost:3210/api/openclaw/settings"))

    assert full_url["status"] == "failed"
    assert full_url["error"]["code"] == "source_host_rejected_target"


def test_rejects_external_hosts_and_non_http_protocols():
    external = FakeProxy().handle(_payload(baseUrl="https://example.com"))
    external_target = FakeProxy().handle(_payload(targetUrl="http://evil.com/api/openclaw/settings"))
    file_url = FakeProxy().handle(_payload(targetUrl="file:///api/openclaw/settings"))

    assert external["error"]["code"] == "source_host_rejected_target"
    assert external_target["error"]["code"] == "source_host_rejected_target"
    assert file_url["error"]["code"] == "source_host_rejected_target"


def test_rejects_path_traversal():
    result = FakeProxy().handle(_payload(path="/api/openclaw/../settings"))
    full_url = FakeProxy().handle(_payload(targetUrl="http://localhost:3052/../api/openclaw/settings"))

    assert result["status"] == "failed"
    assert result["error"]["code"] == "source_host_rejected_target"
    assert full_url["status"] == "failed"
    assert full_url["error"]["code"] == "source_host_rejected_target"


def test_attaches_authorization_internally_but_redacts_logs_and_output(caplog):
    proxy = FakeProxy()
    caplog.set_level(logging.INFO)
    result = proxy.handle(_payload(headers={"X-Trace-Id": "trace", "Authorization": "Bearer attacker"}))

    assert proxy.calls[0]["headers"]["Authorization"] == f"Bearer {SECRET}"
    assert proxy.calls[0]["headers"]["X-Trace-Id"] == "trace"
    assert "attacker" not in json.dumps(proxy.calls[0]["headers"])
    assert SECRET not in json.dumps(result)
    assert SECRET not in caplog.text
    assert "Authorization" not in caplog.text


def test_maps_401_to_linkcrest_auth_failed():
    result = FakeProxy(status=401, body={"error": "Invalid bearer token."}).handle(_payload())

    assert result["status"] == "failed"
    assert result["httpStatus"] == 401
    assert result["error"]["code"] == "linkcrest_auth_failed"


def test_maps_timeout_to_linkcrest_timeout():
    result = TimeoutProxy().handle(_payload(timeoutMs=1))

    assert result["status"] == "failed"
    assert result["error"]["code"] == "linkcrest_timeout"


def test_maps_unreachable_app_to_linkcrest_unreachable():
    result = UnreachableProxy().handle(_payload())

    assert result["status"] == "failed"
    assert result["error"]["code"] == "linkcrest_unreachable"


def test_supports_get_and_post_with_contract_version():
    get_proxy = FakeProxy()
    post_proxy = FakeProxy()

    get_result = get_proxy.handle(_payload(method="GET", path="/api/openclaw/campaigns", query={"status": "active"}))
    post_result = post_proxy.handle(
        _payload(
            method="POST",
            path="/api/openclaw/autonomy/get_policy",
            body={"input": {"campaignId": "camp_1"}},
        )
    )

    assert get_result["ok"] is True
    assert "status=active" in get_proxy.calls[0]["url"]
    assert "contractVersion=2026-03-18" in get_proxy.calls[0]["url"]
    assert post_result["ok"] is True
    assert json.loads(post_proxy.calls[0]["body"].decode("utf-8")) == {
        "contractVersion": "2026-03-18",
        "input": {"campaignId": "camp_1"},
    }


def test_missing_bearer_returns_structured_error():
    result = FakeProxy().handle(_payload(bearerKey=""))

    assert result["status"] == "failed"
    assert result["error"]["code"] == "missing_bearer"


def test_accepts_bridge_only_bearer_alias():
    proxy = FakeProxy()
    result = proxy.handle(_payload(bearerKey="", bearerCredential=SECRET))

    assert result["ok"] is True
    assert proxy.calls[0]["headers"]["Authorization"] == f"Bearer {SECRET}"


def test_accepts_credential_authorization_header(caplog):
    proxy = FakeProxy()
    caplog.set_level(logging.INFO)
    result = proxy.handle(_payload(bearerKey="", credential={"authorizationHeader": f"Bearer {SECRET}", "tokenExposure": "bridge_only"}))

    assert result["ok"] is True
    assert proxy.calls[0]["headers"]["Authorization"] == f"Bearer {SECRET}"
    assert "bearerPresent=True" in caplog.text
    assert "bearerSource=credential" in caplog.text
    assert SECRET not in caplog.text


def test_accepts_bridge_only_credential_authorization_header(caplog):
    proxy = FakeProxy()
    caplog.set_level(logging.INFO)
    result = proxy.handle(
        _payload(
            bearerKey="",
            bridgeOnlyCredential={"authorizationHeader": f"Bearer {SECRET}", "tokenExposure": "bridge_only"},
        )
    )

    assert result["ok"] is True
    assert proxy.calls[0]["headers"]["Authorization"] == f"Bearer {SECRET}"
    assert "bearerSource=bridgeOnlyCredential" in caplog.text
    assert SECRET not in caplog.text


def test_accepts_bridge_only_bearer_credential_authorization_header(caplog):
    proxy = FakeProxy()
    caplog.set_level(logging.INFO)
    result = proxy.handle(
        _payload(
            bearerKey="",
            bridgeOnlyBearerCredential={"authorizationHeader": f"Bearer {SECRET}", "tokenExposure": "bridge_only"},
        )
    )

    assert result["ok"] is True
    assert proxy.calls[0]["headers"]["Authorization"] == f"Bearer {SECRET}"
    assert "bearerSource=bridgeOnlyBearerCredential" in caplog.text
    assert SECRET not in caplog.text


def test_accepts_nested_data_credential_authorization_header(caplog):
    proxy = FakeProxy()
    caplog.set_level(logging.INFO)
    result = proxy.handle(
        _payload(
            bearerKey="",
            data={"bridgeOnlyCredential": {"authorizationHeader": SECRET, "tokenExposure": "bridge_only"}},
        )
    )

    assert result["ok"] is True
    assert proxy.calls[0]["headers"]["Authorization"] == f"Bearer {SECRET}"
    assert "bearerSource=nested-data.bridgeOnlyCredential" in caplog.text
    assert "bridgeOnlyCredentialObjectPresent=True" in caplog.text
    assert SECRET not in caplog.text


def test_accepts_nested_input_credential_authorization_header(caplog):
    proxy = FakeProxy()
    caplog.set_level(logging.INFO)
    result = proxy.handle(
        _payload(
            bearerKey="",
            input={"bridgeOnlyBearerCredential": {"authorizationHeader": SECRET, "tokenExposure": "bridge_only"}},
        )
    )

    assert result["ok"] is True
    assert proxy.calls[0]["headers"]["Authorization"] == f"Bearer {SECRET}"
    assert "bearerSource=nested-input.bridgeOnlyBearerCredential" in caplog.text
    assert "bridgeOnlyBearerCredentialObjectPresent=True" in caplog.text
    assert SECRET not in caplog.text


def test_overlay_copy_matches_installed_bridge_for_proxy():
    pytest.skip("Overlay copy belongs to current ClawChat repo; do not read stale local ClawChat checkout in Hermes bridge tests.")
    installed_root = Path(__file__).resolve().parents[2]
    overlay_root = Path("/home/alexkerss/repos/ClawChat/hermes-runtime/clawchat-bridge-overlay")
    assert (overlay_root / "clawchat_bridge/main.py").read_text(encoding="utf-8") == (
        installed_root / "clawchat_bridge/main.py"
    ).read_text(encoding="utf-8")
    assert (
        overlay_root / "tests/clawchat_bridge/test_marketplace_local_app_agent_api_request.py"
    ).read_text(encoding="utf-8") == Path(__file__).read_text(encoding="utf-8")
