import threading
import time
from contextlib import contextmanager
from types import SimpleNamespace

from clawchat_bridge.main import ActiveRun, HermesRunCancelled, HermesRunManager


def _manager():
    bridge = SimpleNamespace(config=SimpleNamespace(api_url="https://example.test"))
    manager = HermesRunManager(bridge)
    manager._lock_timeout_s = 2
    manager._lock_status_interval_s = 0.05
    return manager


def test_execution_lock_is_scoped_by_external_agent_id():
    manager = _manager()
    first_run = ActiveRun("dispatch-1", "runtime-1", external_agent_id="linc_hermes")
    second_run = ActiveRun("dispatch-2", "runtime-2", external_agent_id="social_hermes")

    with manager._scoped_execution(
        first_run,
        {"dispatchId": "dispatch-1", "externalAgentId": "linc_hermes"},
        reason="test",
    ):
        acquired = []

        def acquire_other_agent():
            with manager._scoped_execution(
                second_run,
                {"dispatchId": "dispatch-2", "externalAgentId": "social_hermes"},
                reason="test",
            ):
                acquired.append(True)

        thread = threading.Thread(target=acquire_other_agent)
        thread.start()
        thread.join(timeout=0.5)

    assert acquired == [True]


def test_execution_lock_serializes_same_external_agent_id():
    manager = _manager()
    first_run = ActiveRun("dispatch-1", "runtime-1", external_agent_id="linc_hermes")
    second_run = ActiveRun("dispatch-2", "runtime-2", external_agent_id="linc_hermes")

    with manager._scoped_execution(
        first_run,
        {"dispatchId": "dispatch-1", "externalAgentId": "linc_hermes"},
        reason="test",
    ):
        acquired = []

        def acquire_same_agent():
            with manager._scoped_execution(
                second_run,
                {"dispatchId": "dispatch-2", "externalAgentId": "linc_hermes"},
                reason="test",
            ):
                acquired.append(True)

        thread = threading.Thread(target=acquire_same_agent)
        thread.start()
        time.sleep(0.1)
        assert acquired == []

    thread.join(timeout=0.5)
    assert acquired == [True]


def test_timeout_watchdog_releases_stale_same_agent_lock():
    manager = _manager()
    first_run = ActiveRun("dispatch-1", "runtime-1", external_agent_id="linc_snr_hermes")
    second_run = ActiveRun("dispatch-2", "runtime-2", external_agent_id="linc_snr_hermes")

    with manager._scoped_execution(
        first_run,
        {"dispatchId": "dispatch-1", "externalAgentId": "linc_snr_hermes"},
        reason="test",
    ):
        watchdog = manager._start_run_timeout_watchdog(
            first_run,
            {"dispatchId": "dispatch-1", "externalAgentId": "linc_snr_hermes", "timeoutMs": 1000},
        )
        try:
            time.sleep(1.2)
            assert first_run.done.is_set()
            terminal = first_run.event_queue.get_nowait()
            assert terminal["type"] == "run.failed"
            assert terminal["dispatchId"] == "dispatch-1"
            assert terminal["externalAgentId"] == "linc_snr_hermes"
            assert terminal["code"] == "timeout"

            acquired = []

            def acquire_same_agent_after_timeout():
                with manager._scoped_execution(
                    second_run,
                    {"dispatchId": "dispatch-2", "externalAgentId": "linc_snr_hermes"},
                    reason="test",
                ):
                    acquired.append(True)

            thread = threading.Thread(target=acquire_same_agent_after_timeout)
            thread.start()
            thread.join(timeout=0.5)
            assert acquired == [True]
        finally:
            if watchdog:
                watchdog.cancel()


def test_cancel_releases_lock_and_emits_single_cancelled_terminal():
    manager = _manager()
    first_run = ActiveRun("dispatch-1", "runtime-1", external_agent_id="linc_snr_hermes")
    second_run = ActiveRun("dispatch-2", "runtime-2", external_agent_id="linc_snr_hermes")
    with manager._runs_lock:
        manager._runs[first_run.dispatch_id] = first_run

    with manager._scoped_execution(
        first_run,
        {"dispatchId": "dispatch-1", "externalAgentId": "linc_snr_hermes"},
        reason="test",
    ):
        assert manager.cancel("dispatch-1") is True
        first_run.emit({"type": "run.completed", "dispatchId": "dispatch-1", "finalText": "late"})
        events = []
        while not first_run.event_queue.empty():
            events.append(first_run.event_queue.get_nowait())
        terminals = [event for event in events if event["type"] in {"run.completed", "run.failed", "run.cancelled"}]
        assert [event["type"] for event in terminals] == ["run.cancelled"]
        assert terminals[0]["dispatchId"] == "dispatch-1"
        assert terminals[0]["externalAgentId"] == "linc_snr_hermes"

        with manager._scoped_execution(
            second_run,
            {"dispatchId": "dispatch-2", "externalAgentId": "linc_snr_hermes"},
            reason="test",
        ):
            pass


def test_cancelled_waiting_run_does_not_acquire_later():
    manager = _manager()
    first_run = ActiveRun("dispatch-1", "runtime-1", external_agent_id="linc_snr_hermes")
    waiting_run = ActiveRun("dispatch-2", "runtime-2", external_agent_id="linc_snr_hermes")
    with manager._runs_lock:
        manager._runs[waiting_run.dispatch_id] = waiting_run

    with manager._scoped_execution(
        first_run,
        {"dispatchId": "dispatch-1", "externalAgentId": "linc_snr_hermes"},
        reason="test",
    ):
        acquired = []

        def wait_for_same_agent():
            try:
                with manager._scoped_execution(
                    waiting_run,
                    {"dispatchId": "dispatch-2", "externalAgentId": "linc_snr_hermes"},
                    reason="test",
                ):
                    acquired.append(True)
            except HermesRunCancelled:
                pass

        thread = threading.Thread(target=wait_for_same_agent)
        thread.start()
        time.sleep(0.1)
        assert manager.cancel("dispatch-2") is True

    thread.join(timeout=1.5)
    assert acquired == []
    events = []
    while not waiting_run.event_queue.empty():
        events.append(waiting_run.event_queue.get_nowait())
    terminals = [event for event in events if event["type"] in {"run.completed", "run.failed", "run.cancelled"}]
    assert [event["type"] for event in terminals] == ["run.cancelled"]


def test_senior_and_junior_have_separate_execution_locks():
    manager = _manager()
    senior = ActiveRun("dispatch-snr", "runtime-snr", external_agent_id="linc_snr_hermes")
    junior = ActiveRun("dispatch-jr", "runtime-jr", external_agent_id="linc_jr_hermes")

    assert manager._lock_key_for_run(senior) == "externalAgentId:linc_snr_hermes"
    assert manager._lock_key_for_run(junior) == "externalAgentId:linc_jr_hermes"
    assert manager._lock_key_for_run(senior) != manager._lock_key_for_run(junior)


def test_sequential_dispatch_policy_uses_team_lock_for_manager_and_worker():
    manager = _manager()
    policy = {
        "mode": "dangerously_skip_permissions",
        "teamId": "team-1",
        "appSlug": "local-linkcrest",
        "managerFirst": True,
        "sequentialDispatch": True,
    }

    senior_key = manager._lock_key_for_payload({
        "dispatchId": "snr",
        "runtimeSessionId": "runtime",
        "externalAgentId": "linc_snr_hermes",
        "autonomyPolicy": policy,
    })
    junior_key = manager._lock_key_for_payload({
        "dispatchId": "jr",
        "runtimeSessionId": "runtime",
        "externalAgentId": "linc_jr_hermes",
        "autonomyPolicy": policy,
    })

    assert senior_key == junior_key
    assert senior_key == "teamDispatch:local-linkcrest:team-1"


def test_run_agent_refreshes_snapshot_after_execution_lock(monkeypatch):
    manager = _manager()
    lock_entered = {"value": False}
    load_observed = {"after_lock": False}

    @contextmanager
    def scoped(_run, _payload, *, reason):
        assert reason == "agent_run"
        lock_entered["value"] = True
        yield

    @contextmanager
    def noop_context(*_args, **_kwargs):
        yield []

    class FakeAgent:
        tools = []

        def run_conversation(self, **kwargs):
            assert load_observed["after_lock"] is True
            return {
                "completed": True,
                "final_response": "done",
                "messages": [*kwargs["conversation_history"], {"role": "assistant", "content": "done"}],
            }

    def load(_runtime_session_id):
        load_observed["after_lock"] = lock_entered["value"]
        return [{"role": "user", "content": "old no external action"}]

    monkeypatch.setattr(manager, "_scoped_execution", scoped)
    monkeypatch.setattr(manager, "_workspace_context", noop_context)
    monkeypatch.setattr(manager, "_skills_context", noop_context)
    monkeypatch.setattr(manager, "_reference_tracking_context", noop_context)
    monkeypatch.setattr(manager.local_app_runtime, "prepare_for_run", lambda _run, _payload: "")
    monkeypatch.setattr(manager.marketplace_proxy, "registered_for_payload", noop_context)
    monkeypatch.setattr(manager.snapshot_store, "load", load)
    monkeypatch.setattr(manager.snapshot_store, "save", lambda _runtime_session_id, _messages: None)
    monkeypatch.setattr(manager, "_build_agent", lambda _run, _payload: FakeAgent())
    monkeypatch.setattr(manager, "_build_default_skills_prompt", lambda _run, _payload: (None, []))
    monkeypatch.setattr(manager, "_response_contract_prompt", lambda _payload: (None, {}))

    run = ActiveRun("dispatch-1", "runtime-1", external_agent_id="linc_jr_hermes")
    manager._run_agent(
        run,
        {
            "dispatchId": "dispatch-1",
            "runtimeSessionId": "runtime-1",
            "externalAgentId": "linc_jr_hermes",
            "inputText": "continue",
            "autonomyPolicy": {
                "mode": "dangerously_skip_permissions",
                "staleContextPolicy": "current_policy_supersedes_old_chat",
            },
        },
    )

    assert load_observed["after_lock"] is True
    assert run.terminal_event_type == "run.completed"
