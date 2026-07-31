import asyncio
from types import SimpleNamespace

import pytest

import clawchat_bridge.main as bridge_main
from clawchat_bridge.main import (
    BACKFILL_ENDPOINT_PATH,
    BridgeConfig,
    ClawChatHermesBridge,
    DispatchStateStore,
)


@pytest.fixture(autouse=True)
def isolated_bridge_state(tmp_path, monkeypatch):
    state_dir = tmp_path / "clawchat_bridge"
    monkeypatch.setattr(bridge_main, "_config_dir", lambda: state_dir)


def _config() -> BridgeConfig:
    return BridgeConfig(
        api_url="http://clawchat.local",
        device_public_id="device-1",
        device_token="token-1",
        workspace_id="workspace-1",
        external_agent_ids=["social_hermes", "linc_jr_hermes", "linc_snr_hermes", "postman_hermes"],
    )


def _dispatch(dispatch_id: str = "dispatch-1", external_agent_id: str = "social_hermes") -> dict:
    return {
        "dispatchId": dispatch_id,
        "runtimeRunId": f"run-{dispatch_id}",
        "runtimeSessionId": f"session-{dispatch_id}",
        "externalAgentId": external_agent_id,
        "inputText": "recover this",
        "status": "pending",
    }


def test_reconnect_backfill_uses_registered_agent_scope(monkeypatch):
    bridge = ClawChatHermesBridge(_config())
    bridge.session = SimpleNamespace()
    captured = {}

    async def fake_fetch(agent_ids):
        captured["agent_ids"] = agent_ids
        return []

    monkeypatch.setattr(bridge, "_fetch_backfill_dispatches", fake_fetch)

    asyncio.run(bridge._run_reconnect_backfill(reason="test"))

    assert captured["agent_ids"] == [
        "social_hermes",
        "linc_jr_hermes",
        "linc_snr_hermes",
        "postman_hermes",
    ]


def test_missed_social_dispatch_is_fetched_and_started(monkeypatch):
    bridge = ClawChatHermesBridge(_config())
    bridge.session = SimpleNamespace()
    started = []

    async def fake_fetch(_agent_ids):
        return [_dispatch("c00e1aac-e6fc-43b1-beb4-a03139a2e48a", "social_hermes")]

    def fake_start(payload, *, source="websocket"):
        started.append((payload, source))

    monkeypatch.setattr(bridge, "_fetch_backfill_dispatches", fake_fetch)
    monkeypatch.setattr(bridge.run_manager, "start", fake_start)

    asyncio.run(bridge._run_reconnect_backfill(reason="test"))

    assert len(started) == 1
    assert started[0][0]["dispatchId"] == "c00e1aac-e6fc-43b1-beb4-a03139a2e48a"
    assert started[0][0]["externalAgentId"] == "social_hermes"
    assert started[0][0]["_dispatchSource"] == "backfill"
    assert started[0][1] == "backfill"


def test_backfill_skips_unregistered_agents(monkeypatch):
    bridge = ClawChatHermesBridge(_config())
    started = []

    monkeypatch.setattr(bridge.run_manager, "start", lambda payload, *, source="websocket": started.append(payload))

    asyncio.run(bridge._accept_backfilled_dispatch(_dispatch("dispatch-1", "other_hermes")))

    assert started == []


def test_backfill_dedupe_skips_already_running_dispatch(monkeypatch):
    bridge = ClawChatHermesBridge(_config())
    payload = _dispatch("dispatch-running", "social_hermes")
    bridge.run_manager.dispatch_state.record_start(
        payload["dispatchId"],
        payload["runtimeRunId"],
        payload["externalAgentId"],
        source="websocket",
    )
    started = []
    monkeypatch.setattr(bridge.run_manager, "start", lambda item, *, source="websocket": started.append(item))

    asyncio.run(bridge._accept_backfilled_dispatch(payload))

    assert started == []


@pytest.mark.parametrize("terminal_type", ["run.completed", "run.failed", "run.cancelled"])
def test_backfill_dedupe_skips_locally_terminal_dispatch(monkeypatch, terminal_type):
    bridge = ClawChatHermesBridge(_config())
    payload = _dispatch(f"dispatch-{terminal_type}", "social_hermes")
    bridge.run_manager.dispatch_state.record_terminal(
        payload["dispatchId"],
        payload["runtimeRunId"],
        payload["externalAgentId"],
        terminal_type,
    )
    started = []
    monkeypatch.setattr(bridge.run_manager, "start", lambda item, *, source="websocket": started.append(item))

    asyncio.run(bridge._accept_backfilled_dispatch(payload))

    assert started == []


@pytest.mark.parametrize("remote_status", ["cancelled", "timed_out", "completed", "failed"])
def test_backfill_skips_remote_terminal_status(monkeypatch, remote_status):
    bridge = ClawChatHermesBridge(_config())
    payload = {**_dispatch("dispatch-terminal", "social_hermes"), "status": remote_status}
    started = []
    monkeypatch.setattr(bridge.run_manager, "start", lambda item, *, source="websocket": started.append(item))

    asyncio.run(bridge._accept_backfilled_dispatch(payload))

    assert started == []


def test_backfill_handles_network_failure_safely(monkeypatch):
    bridge = ClawChatHermesBridge(_config())
    bridge.session = SimpleNamespace()
    attempts = {"count": 0}

    async def fail(_agent_ids):
        attempts["count"] += 1
        raise RuntimeError("dns failure")

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr(bridge, "_fetch_backfill_dispatches", fail)
    monkeypatch.setattr(asyncio, "sleep", no_sleep)

    asyncio.run(bridge._run_reconnect_backfill(reason="test"))

    assert attempts["count"] == 3


def test_unknown_cancel_does_not_emit_terminal_retry(monkeypatch):
    bridge = ClawChatHermesBridge(_config())
    sent = []
    monkeypatch.setattr(bridge, "send_event", lambda event: sent.append(event))

    asyncio.run(bridge._handle_ws_text('{"type":"hermes.run.cancel","data":{"dispatchId":"unknown"}}'))

    assert sent == []


def test_backfill_endpoint_contract_path_is_explicit():
    assert BACKFILL_ENDPOINT_PATH == "/api/v1/bridge/runtime-dispatches/backfill"


def test_dispatch_state_store_persists_terminal_dedupe(tmp_path):
    path = tmp_path / "dispatch_state.json"
    store = DispatchStateStore(path)
    store.record_terminal("dispatch-1", "runtime-1", "social_hermes", "run.completed")

    restored = DispatchStateStore(path)

    assert restored.dedupe_reason("dispatch-1", "runtime-1") == "local_terminal_completed"
