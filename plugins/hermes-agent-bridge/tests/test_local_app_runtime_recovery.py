import asyncio
import json
import logging
import urllib.error
from pathlib import Path

import pytest

from clawchat_bridge.main import (
    BRIDGE_CAPABILITIES,
    BridgeConfig,
    ClawChatHermesBridge,
    LocalAppRuntimeManager,
    MarketplaceLocalAppAgentApiRequestProxy,
)


def _profile(repo: Path | str, **updates):
    profile = {
        "repoPath": str(repo),
        "appUrl": "http://localhost:3052",
        "agentApiUrl": "http://localhost:3052/api/openclaw",
        "startCommand": "pnpm dev",
        "healthCheckUrl": "http://localhost:3052",
        "backendHealthCheckUrl": "http://localhost:3210",
        "autoStartAllowed": True,
        "expectedPorts": [3052, 3210],
        "recoveryTimeoutSeconds": 0.01,
    }
    profile.update(updates)
    return profile


def _repo(tmp_path):
    repo = tmp_path / "app"
    repo.mkdir()
    (repo / "package.json").write_text(json.dumps({"scripts": {"dev": "vite --host 0.0.0.0"}}), encoding="utf-8")
    return repo


def _payload(profile, **updates):
    payload = {"requestId": "req_runtime", "appSlug": "linkcrest", "runtimeProfile": profile}
    payload.update(updates)
    return payload


def test_capability_advertised_local_app_runtime_recovery():
    assert "localAppRuntimeRecovery" in BRIDGE_CAPABILITIES


def test_get_runtime_status_reports_stopped_app(tmp_path, monkeypatch):
    manager = LocalAppRuntimeManager()
    repo = _repo(tmp_path)
    monkeypatch.setattr(manager, "_url_reachable", lambda _url: False)
    monkeypatch.setattr(manager, "_port_open", lambda _host, _port: False)
    monkeypatch.setattr(manager, "_matching_processes", lambda _repo, _command: [])

    result = manager.get_runtime_status(_payload(_profile(repo)))

    assert result["status"] == "ok"
    assert result["runtimeState"] == "stopped"
    assert result["appReachable"] is False
    assert result["agentApiReachable"] is False
    assert result["backendHealthReachable"] is False
    assert result["expectedPortsOpen"] is False


def test_ensure_running_starts_stopped_app_when_allowed(tmp_path, monkeypatch):
    manager = LocalAppRuntimeManager()
    repo = _repo(tmp_path)
    calls = {"reachable": 0, "started": []}

    def fake_reachable(url):
        calls["reachable"] += 1
        return calls["reachable"] > 3

    monkeypatch.setattr(manager, "_url_reachable", fake_reachable)
    monkeypatch.setattr(manager, "_port_open", lambda _host, _port: False)
    monkeypatch.setattr(manager, "_matching_processes", lambda _repo, _command: [])
    monkeypatch.setattr(manager, "_command_appears_running", lambda _repo, _command: False)

    def fake_start(repo_root, command):
        calls["started"].append((repo_root, command))
        return {"started": True, "pid": 123, "command": "pnpm"}

    monkeypatch.setattr(manager, "_start_runtime_command", fake_start)

    result = manager.ensure_running(_payload(_profile(repo)))

    assert result["status"] == "ok"
    assert result["started"] is True
    assert calls["started"] == [(repo.resolve(), "pnpm dev")]


def test_already_running_does_not_start_duplicate_process(tmp_path, monkeypatch):
    manager = LocalAppRuntimeManager()
    repo = _repo(tmp_path)
    monkeypatch.setattr(manager, "_url_reachable", lambda _url: False)
    monkeypatch.setattr(manager, "_port_open", lambda _host, _port: False)
    monkeypatch.setattr(manager, "_matching_processes", lambda _repo, _command: [{"pid": 7, "command": "pnpm dev"}])
    monkeypatch.setattr(manager, "_command_appears_running", lambda _repo, _command: True)
    monkeypatch.setattr(manager, "_start_runtime_command", lambda *_args: (_ for _ in ()).throw(AssertionError("duplicate start")))

    result = manager.ensure_running(_payload(_profile(repo)))

    assert result["status"] == "ok"
    assert result["alreadyRunning"] is True


def test_wrong_repo_path_returns_repo_not_found(tmp_path):
    manager = LocalAppRuntimeManager()

    result = manager.ensure_running(_payload(_profile(tmp_path / "missing")))

    assert result["status"] == "failed"
    assert result["error"]["code"] == "repo_not_found"


def test_missing_start_command_returns_start_command_missing(tmp_path):
    manager = LocalAppRuntimeManager()
    repo = _repo(tmp_path)

    result = manager.ensure_running(_payload(_profile(repo, startCommand="")))

    assert result["error"]["code"] == "start_command_missing"


def test_linkcrest_runtime_profile_pnpm_dev_is_allowed(tmp_path):
    manager = LocalAppRuntimeManager()
    repo = _repo(tmp_path)

    decision = manager._validate_start_command(repo, "pnpm dev")

    assert decision["allowed"] is True
    assert decision["executable"] == "pnpm"
    assert decision["args"] == ["dev"]
    assert decision["scriptName"] == "dev"


def test_pnpm_install_is_rejected(tmp_path):
    manager = LocalAppRuntimeManager()
    repo = _repo(tmp_path)

    decision = manager._validate_start_command(repo, "pnpm install")

    assert decision["allowed"] is False
    assert decision["code"] == "hard_stop_required"
    assert decision["reason"] == "hard_stop_command_token"


def test_command_chaining_is_rejected(tmp_path):
    manager = LocalAppRuntimeManager()
    repo = _repo(tmp_path)

    decision = manager._validate_start_command(repo, "pnpm dev && rm -rf .")

    assert decision["allowed"] is False
    assert decision["reason"] == "shell_metacharacter"


def test_missing_package_script_is_rejected(tmp_path):
    manager = LocalAppRuntimeManager()
    repo = _repo(tmp_path)

    decision = manager._validate_start_command(repo, "npm run start")

    assert decision["allowed"] is False
    assert decision["reason"] == "missing_package_script"


def test_hard_stop_prompt_returns_hard_stop_required(tmp_path, monkeypatch):
    manager = LocalAppRuntimeManager()
    repo = _repo(tmp_path)
    monkeypatch.setattr(manager, "_url_reachable", lambda _url: False)
    monkeypatch.setattr(manager, "_port_open", lambda _host, _port: False)
    monkeypatch.setattr(manager, "_matching_processes", lambda _repo, _command: [])
    monkeypatch.setattr(manager, "_command_appears_running", lambda _repo, _command: False)
    monkeypatch.setattr(
        manager,
        "_start_runtime_command",
        lambda *_args: {
            "started": False,
            "hardStop": {"reason": "migration", "line": "Convex migration required"},
            "outputTail": ["API_KEY=[REDACTED_SECRET_VALUE]"],
        },
    )

    result = manager.ensure_running(_payload(_profile(repo)))

    assert result["error"]["code"] == "hard_stop_required"
    assert result["diagnostics"]["hardStop"]["reason"] == "migration"


def test_health_check_failure_returns_health_check_failed(tmp_path, monkeypatch):
    manager = LocalAppRuntimeManager()
    repo = _repo(tmp_path)
    monkeypatch.setattr(manager, "_url_reachable", lambda url: "3052/api/openclaw" in str(url) or "3210" in str(url))
    monkeypatch.setattr(manager, "_port_open", lambda _host, _port: False)
    monkeypatch.setattr(manager, "_matching_processes", lambda _repo, _command: [])
    monkeypatch.setattr(manager, "_command_appears_running", lambda _repo, _command: False)
    monkeypatch.setattr(manager, "_start_runtime_command", lambda *_args: {"started": True, "pid": 123})

    result = manager.ensure_running(_payload(_profile(repo)))

    assert result["error"]["code"] == "health_check_failed"


def test_backend_health_check_failure_returns_backend_health_check_failed(tmp_path, monkeypatch):
    manager = LocalAppRuntimeManager()
    repo = _repo(tmp_path)
    monkeypatch.setattr(manager, "_url_reachable", lambda url: "3210" not in str(url))
    monkeypatch.setattr(manager, "_port_open", lambda _host, _port: False)
    monkeypatch.setattr(manager, "_matching_processes", lambda _repo, _command: [])
    monkeypatch.setattr(manager, "_command_appears_running", lambda _repo, _command: False)
    monkeypatch.setattr(manager, "_start_runtime_command", lambda *_args: {"started": True, "pid": 123})

    result = manager.ensure_running(_payload(_profile(repo)))

    assert result["error"]["code"] == "backend_health_check_failed"


class _RecoveringProxy(MarketplaceLocalAppAgentApiRequestProxy):
    def __init__(self, runtime_manager):
        super().__init__(runtime_manager)
        self.calls = 0

    def _execute(self, method, url, headers, body, timeout_s):
        self.calls += 1
        if self.calls == 1:
            raise urllib.error.URLError("connection refused")
        return 200, {"Content-Type": "application/json"}, b'{"ok": true}'


def test_local_app_tool_call_triggers_ensure_running_and_retries(tmp_path):
    repo = _repo(tmp_path)
    recovery_calls = []

    class Runtime:
        def ensure_running(self, payload, start_if_needed=True):
            recovery_calls.append((payload, start_if_needed))
            return {"status": "ok", "runtimeState": "running"}

    proxy = _RecoveringProxy(Runtime())
    result = proxy.handle(
        {
            "requestId": "req_tool",
            "appSlug": "linkcrest",
            "baseUrl": "http://localhost:3052",
            "method": "GET",
            "path": "/api/openclaw/settings",
            "bearerKey": "secret_bearer_value",
            "runtimeProfile": _profile(repo),
        }
    )

    assert result["status"] == "ok"
    assert proxy.calls == 2
    assert recovery_calls[0][0]["reason"] == "marketplace_local_app_unreachable"


def test_linkcrest_profile_starts_pnpm_dev_safely(tmp_path, monkeypatch):
    manager = LocalAppRuntimeManager()
    repo = _repo(tmp_path)
    started = []
    monkeypatch.setattr(manager, "_url_reachable", lambda _url: False)
    monkeypatch.setattr(manager, "_port_open", lambda _host, _port: False)
    monkeypatch.setattr(manager, "_matching_processes", lambda _repo, _command: [])
    monkeypatch.setattr(manager, "_command_appears_running", lambda _repo, _command: False)
    monkeypatch.setattr(manager, "_start_runtime_command", lambda repo_root, command: started.append((repo_root, command)) or {"started": True, "pid": 123})

    manager.ensure_running(_payload(_profile(repo)))

    assert started == [(repo.resolve(), "pnpm dev")]


def test_no_secrets_are_logged(tmp_path, monkeypatch, caplog):
    manager = LocalAppRuntimeManager()
    repo = _repo(tmp_path)
    secret = "sk_live_secret_value_123456"
    monkeypatch.setattr(manager, "_url_reachable", lambda _url: False)
    monkeypatch.setattr(manager, "_port_open", lambda _host, _port: False)
    monkeypatch.setattr(manager, "_matching_processes", lambda _repo, _command: [])
    monkeypatch.setattr(manager, "_command_appears_running", lambda _repo, _command: False)
    monkeypatch.setattr(manager, "_start_runtime_command", lambda *_args: {"started": False, "hardStop": {"reason": "secret_exposure", "line": "API_KEY=[REDACTED_SECRET_VALUE]"}, "outputTail": ["API_KEY=[REDACTED_SECRET_VALUE]"]})

    with caplog.at_level(logging.INFO, logger="clawchat.hermes_bridge"):
        result = manager.handle_action("localApp.ensureRunning", _payload(_profile(repo, secret=secret)))

    assert result["error"]["code"] == "hard_stop_required"
    assert secret not in caplog.text
    assert secret not in json.dumps(result)


def test_bridge_handles_local_app_runtime_action(tmp_path, monkeypatch):
    bridge = ClawChatHermesBridge(
        BridgeConfig(
            api_url="http://clawchat.local",
            device_public_id="device",
            device_token="token",
            external_agent_ids=["agent"],
        )
    )
    sent = []
    monkeypatch.setattr(bridge.run_manager.local_app_runtime, "handle_action", lambda action, data: {"requestId": data["requestId"], "status": "ok", "runtimeState": "running"})

    async def fake_send_raw(payload):
        sent.append(payload)

    monkeypatch.setattr(bridge, "_send_raw", fake_send_raw)

    asyncio.run(bridge._handle_ws_text(json.dumps({"type": "localApp.getRuntimeStatus", "data": _payload(_profile(tmp_path), requestId="req_action")})))

    assert sent[0]["type"] == "localApp.getRuntimeStatus.result"
    assert sent[0]["data"]["runtimeState"] == "running"


def test_overlay_copy_matches_installed_bridge_for_runtime_recovery():
    pytest.skip("Overlay copy belongs to current ClawChat repo; do not read stale local ClawChat checkout in Hermes bridge tests.")
    installed_root = Path(__file__).resolve().parents[2]
    overlay_root = Path("/home/alexkerss/repos/ClawChat/hermes-runtime/clawchat-bridge-overlay")
    assert (overlay_root / "clawchat_bridge/main.py").read_text(encoding="utf-8") == (
        installed_root / "clawchat_bridge/main.py"
    ).read_text(encoding="utf-8")
    assert (
        overlay_root / "tests/clawchat_bridge/test_local_app_runtime_recovery.py"
    ).read_text(encoding="utf-8") == Path(__file__).read_text(encoding="utf-8")
