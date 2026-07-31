import asyncio
import json

from clawchat_bridge.main import BridgeConfig, ClawChatHermesBridge, SnapshotStore


def _bridge(tmp_path):
    bridge = ClawChatHermesBridge(
        BridgeConfig(
            api_url="http://clawchat.local",
            device_public_id="device",
            device_token="token",
            external_agent_ids=["linc_snr_hermes", "linc_jr_hermes"],
        )
    )
    bridge.run_manager.snapshot_store = SnapshotStore(tmp_path / "runtime_sessions")
    return bridge


def _dispatch(**updates):
    payload = {
        "dispatchId": "dispatch-1",
        "runtimeSessionId": "runtime-1",
        "externalAgentId": "linc_snr_hermes",
        "inputText": "/new",
    }
    payload.update(updates)
    return json.dumps({"type": "hermes.run.dispatch", "data": payload})


def test_new_control_command_does_not_start_agent_and_clears_snapshot(tmp_path, monkeypatch):
    bridge = _bridge(tmp_path)
    bridge.run_manager.snapshot_store.save("runtime-1", [{"role": "user", "content": "old"}])
    sent = []

    def fail_start(_payload):
        raise AssertionError("control command must not reach HermesRunManager.start")

    async def fake_send_event(event):
        sent.append(event)

    monkeypatch.setattr(bridge.run_manager, "start", fail_start)
    monkeypatch.setattr(bridge, "send_event", fake_send_event)

    asyncio.run(bridge._handle_ws_text(_dispatch(inputText="/new refreshed-context")))

    assert bridge.run_manager.snapshot_store.load("runtime-1") == []
    assert sent[0]["type"] == "run.completed"
    assert sent[0]["metadata"]["authoredBy"] == "hermes_bridge"
    assert sent[0]["metadata"]["runtimeControlCommand"] is True
    assert "Acknowledged" not in sent[0]["finalText"]
    assert "Hermes session reset for linc_snr_hermes." in sent[0]["finalText"]


def test_reset_alias_clears_snapshot_without_forwarding_to_model(tmp_path, monkeypatch):
    bridge = _bridge(tmp_path)
    bridge.run_manager.snapshot_store.save("runtime-1", [{"role": "assistant", "content": "old"}])
    sent = []
    started = []

    async def fake_send_event(event):
        sent.append(event)

    monkeypatch.setattr(bridge.run_manager, "start", lambda payload: started.append(payload))
    monkeypatch.setattr(bridge, "send_event", fake_send_event)

    asyncio.run(bridge._handle_ws_text(_dispatch(inputText="/reset")))

    assert started == []
    assert bridge.run_manager.snapshot_store.load("runtime-1") == []
    assert sent[0]["metadata"]["snapshotDeleted"] is True


def test_reload_skills_control_command_uses_real_reload_path(tmp_path, monkeypatch):
    bridge = _bridge(tmp_path)
    sent = []
    calls = []

    def fake_reload_skills():
        calls.append(True)
        return {"added": [], "removed": [], "unchanged": ["alpha"], "total": 1, "commands": 1}

    async def fake_send_event(event):
        sent.append(event)

    monkeypatch.setattr("agent.skill_commands.reload_skills", fake_reload_skills)
    monkeypatch.setattr(bridge.run_manager, "start", lambda _payload: (_ for _ in ()).throw(AssertionError("must not start")))
    monkeypatch.setattr(bridge, "send_event", fake_send_event)

    asyncio.run(bridge._handle_ws_text(_dispatch(inputText="/reload-skills")))

    assert calls == [True]
    assert sent[0]["type"] == "run.completed"
    assert sent[0]["metadata"]["reloadSkills"]["total"] == 1
    assert "Hermes skills reloaded for linc_snr_hermes" in sent[0]["finalText"]
    assert "Acknowledged" not in sent[0]["finalText"]


def test_normal_message_dispatch_still_starts_run(tmp_path, monkeypatch):
    bridge = _bridge(tmp_path)
    started = []
    sent = []

    async def fake_send_event(event):
        sent.append(event)

    monkeypatch.setattr(bridge.run_manager, "start", lambda payload: started.append(payload))
    monkeypatch.setattr(bridge, "send_event", fake_send_event)

    asyncio.run(bridge._handle_ws_text(_dispatch(inputText="hello /new but not command-only")))

    assert len(started) == 1
    assert started[0]["inputText"] == "hello /new but not command-only"
    assert sent == []


def test_team_threads_require_one_control_dispatch_per_hermes_member(tmp_path, monkeypatch):
    bridge = _bridge(tmp_path)
    bridge.run_manager.snapshot_store.save("runtime-snr", [{"role": "user", "content": "snr old"}])
    bridge.run_manager.snapshot_store.save("runtime-jr", [{"role": "user", "content": "jr old"}])
    sent = []

    async def fake_send_event(event):
        sent.append(event)

    monkeypatch.setattr(bridge.run_manager, "start", lambda _payload: (_ for _ in ()).throw(AssertionError("must not start")))
    monkeypatch.setattr(bridge, "send_event", fake_send_event)

    asyncio.run(bridge._handle_ws_text(_dispatch(runtimeSessionId="runtime-snr", inputText="/new")))

    assert bridge.run_manager.snapshot_store.load("runtime-snr") == []
    assert bridge.run_manager.snapshot_store.load("runtime-jr") == [{"role": "user", "content": "jr old"}]
    assert sent[0]["metadata"]["teamControlScope"] == "single_runtime_session"


def test_stop_control_command_cancels_active_runs_for_same_agent(tmp_path, monkeypatch):
    bridge = _bridge(tmp_path)
    sent = []
    cancelled = []

    async def fake_send_event(event):
        sent.append(event)

    monkeypatch.setattr(bridge.run_manager, "cancel_for_external_agent", lambda external_agent_id: cancelled.append(external_agent_id) or 2)
    monkeypatch.setattr(bridge.run_manager, "start", lambda _payload: (_ for _ in ()).throw(AssertionError("must not start")))
    monkeypatch.setattr(bridge, "send_event", fake_send_event)

    asyncio.run(bridge._handle_ws_text(_dispatch(inputText="/stop")))

    assert cancelled == ["linc_snr_hermes"]
    assert sent[0]["metadata"]["cancelledRuns"] == 2
    assert "Cancel requested for 2 active Hermes run(s)" in sent[0]["finalText"]
