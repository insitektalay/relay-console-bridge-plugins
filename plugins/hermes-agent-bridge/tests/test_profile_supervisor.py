import asyncio
from pathlib import Path

from clawchat_bridge.profile_supervisor import HermesProfileSupervisor


DUMMY_WORKER = r"""
import json
import os
import sys

for line in sys.stdin:
    message = json.loads(line)
    if message["type"] == "dispatch":
        payload = message["payload"]
        print(json.dumps({
            "type": "event",
            "event": {
                "type": "run.completed",
                "dispatchId": payload["dispatchId"],
                "finalText": os.environ["HERMES_HOME"],
            },
        }), flush=True)
    elif message["type"] == "shutdown":
        raise SystemExit(0)
"""

DUMMY_ENV_WORKER = r"""
import json
import os
import sys

for line in sys.stdin:
    message = json.loads(line)
    if message["type"] == "dispatch":
        print(json.dumps({
            "type": "event",
            "event": {
                "type": "run.completed",
                "dispatchId": message["payload"]["dispatchId"],
                "finalText": json.dumps({
                    "provider": os.environ.get("OPENAI_API_KEY"),
                    "clawchat": os.environ.get("CLAWCHAT_BRIDGE_DEVICE_TOKEN"),
                    "relayConsole": os.environ.get("RELAY_CONSOLE_BRIDGE_DEVICE_TOKEN"),
                    "relayLegacy": os.environ.get("RELAY_ACCESS_TOKEN"),
                }),
            },
        }), flush=True)
    elif message["type"] == "shutdown":
        raise SystemExit(0)
"""


def test_two_profile_workers_keep_immutable_distinct_hermes_homes(tmp_path):
    async def run():
        worker_script = tmp_path / "dummy_worker.py"
        worker_script.write_text(DUMMY_WORKER, encoding="utf8")
        sales = tmp_path / "sales"
        operations = tmp_path / "operations"
        sales.mkdir()
        operations.mkdir()
        events = []

        async def receive(event):
            events.append(event)

        supervisor = HermesProfileSupervisor(
            worker_script=worker_script,
            api_url="https://relay.example.com",
            workspace_id="workspace-1",
            event_handler=receive,
        )
        await asyncio.gather(
            supervisor.dispatch(
                external_id="profile:sales",
                profile_home=sales,
                binding_epoch="1",
                payload={"dispatchId": "sales-run"},
            ),
            supervisor.dispatch(
                external_id="profile:operations",
                profile_home=operations,
                binding_epoch="1",
                payload={"dispatchId": "operations-run"},
            ),
        )
        for _ in range(50):
            if len(events) == 2:
                break
            await asyncio.sleep(0.01)
        await supervisor.shutdown()

        assert {
            event["dispatchId"]: Path(event["finalText"]).resolve()
            for event in events
        } == {
            "sales-run": sales.resolve(),
            "operations-run": operations.resolve(),
        }

    asyncio.run(run())


def test_profile_worker_keeps_provider_environment_but_not_bridge_credentials(
    tmp_path,
    monkeypatch,
):
    async def run():
        worker_script = tmp_path / "dummy_env_worker.py"
        worker_script.write_text(DUMMY_ENV_WORKER, encoding="utf8")
        profile = tmp_path / "profile"
        profile.mkdir()
        events = []
        monkeypatch.setenv("OPENAI_API_KEY", "provider-key")
        monkeypatch.setenv("CLAWCHAT_BRIDGE_DEVICE_TOKEN", "clawchat-secret")
        monkeypatch.setenv("RELAY_CONSOLE_BRIDGE_DEVICE_TOKEN", "relay-secret")
        monkeypatch.setenv("RELAY_ACCESS_TOKEN", "legacy-secret")

        async def receive(event):
            events.append(event)

        supervisor = HermesProfileSupervisor(
            worker_script=worker_script,
            api_url="https://relay.example.com",
            workspace_id="workspace-1",
            event_handler=receive,
        )
        await supervisor.dispatch(
            external_id="profile:secure",
            profile_home=profile,
            binding_epoch="1",
            payload={"dispatchId": "environment-run"},
        )
        for _ in range(50):
            if events:
                break
            await asyncio.sleep(0.01)
        await supervisor.shutdown()

        import json

        environment = json.loads(events[0]["finalText"])
        assert environment == {
            "provider": "provider-key",
            "clawchat": None,
            "relayConsole": None,
            "relayLegacy": None,
        }

    asyncio.run(run())
