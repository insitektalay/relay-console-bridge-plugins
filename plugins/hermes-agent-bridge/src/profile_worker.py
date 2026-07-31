from __future__ import annotations

# HERMES_HOME is fixed by the supervisor before this process imports any Hermes
# runtime module, including the bridge's main module.
import asyncio
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    from .worker_protocol import decode_worker_message, encode_worker_message
except ImportError:  # pragma: no cover - direct source execution
    from worker_protocol import decode_worker_message, encode_worker_message

if not os.environ.get("HERMES_HOME"):
    raise SystemExit("Hermes profile worker requires a fixed HERMES_HOME")

try:
    from . import main as bridge_main
except ImportError:  # pragma: no cover - direct source execution
    import main as bridge_main


logger = logging.getLogger("clawchat.hermes_profile_worker")


@dataclass
class WorkerConfig:
    api_url: str = ""
    workspace_id: str | None = None
    external_agent_ids: list[str] = field(default_factory=list)


class WorkerBridge:
    def __init__(self) -> None:
        self.config = WorkerConfig()
        self._stdout_lock = asyncio.Lock()
        self.run_manager: bridge_main.HermesRunManager | None = None
        self.structured_job_runner: bridge_main.HermesStructuredJobRunner | None = None
        self.marketplace_installer: bridge_main.MarketplaceSkillInstaller | None = None

    def initialize(self, message: dict[str, Any]) -> None:
        external_id = str(message.get("externalAgentId") or "").strip()
        if not external_id:
            raise ValueError("profile worker externalAgentId is required")
        self.config = WorkerConfig(
            api_url=str(message.get("apiUrl") or "").strip(),
            workspace_id=str(message.get("workspaceId") or "").strip() or None,
            external_agent_ids=[external_id],
        )
        self.run_manager = bridge_main.HermesRunManager(self)
        self.structured_job_runner = bridge_main.HermesStructuredJobRunner(self)
        self.marketplace_installer = bridge_main.MarketplaceSkillInstaller()
        self.marketplace_installer.native_profile_roots = {
            external_id: Path(os.environ["HERMES_HOME"]).resolve()
        }

    async def send_event(self, event: dict[str, Any]) -> None:
        await self._write({"type": "event", "event": event})

    async def post_structured_job_result(
        self,
        job_id: str,
        data: dict[str, Any],
    ) -> None:
        await self._write(
            {"type": "structured_job.result", "jobId": job_id, "data": data}
        )

    async def post_structured_job_error(
        self,
        job_id: str,
        data: dict[str, Any],
    ) -> None:
        await self._write(
            {"type": "structured_job.error", "jobId": job_id, "data": data}
        )

    async def _write(self, message: dict[str, Any]) -> None:
        encoded = encode_worker_message(message)
        async with self._stdout_lock:
            sys.stdout.buffer.write(encoded)
            sys.stdout.buffer.flush()

    @staticmethod
    def _control_commands(payload: dict[str, Any]) -> list[str] | None:
        raw = str(payload.get("inputText") or payload.get("content") or "").strip()
        lines = [line.strip() for line in raw.splitlines() if line.strip()]
        if not lines or any(not line.startswith("/") for line in lines):
            return None
        commands: list[str] = []
        for line in lines:
            command = line[1:].partition(" ")[0].strip().lower().replace("_", "-")
            if command not in {"new", "reset", "reload-skills", "stop"}:
                return None
            commands.append(command)
        return commands

    async def handle_dispatch(self, payload: dict[str, Any]) -> None:
        assert self.run_manager
        scoped = dict(payload)
        scoped["workspaceRoot"] = os.environ["HERMES_HOME"]
        commands = self._control_commands(scoped)
        if not commands:
            self.run_manager.start(scoped, source="profile_worker")
            return
        dispatch_id = str(scoped.get("dispatchId") or "")
        runtime_session_id = str(scoped.get("runtimeSessionId") or "")
        external_id = self.config.external_agent_ids[0]
        completed: list[str] = []
        for command in commands:
            if command in {"new", "reset"}:
                if not runtime_session_id:
                    raise ValueError("runtimeSessionId is required for /new and /reset")
                self.run_manager.reset_runtime_session(runtime_session_id)
                completed.append(f"Hermes session reset for {external_id}.")
            elif command == "reload-skills":
                self.run_manager.reload_skills_for_payload(scoped)
                completed.append(f"Hermes skills reloaded for {external_id}.")
            elif command == "stop":
                cancelled = self.run_manager.cancel_for_external_agent(external_id)
                completed.append(f"Cancel requested for {cancelled} active Hermes run(s).")
        await self.send_event(
            {
                "type": "run.completed",
                "dispatchId": dispatch_id,
                "externalAgentId": external_id,
                "finalText": "\n".join(completed),
                "metadata": {
                    "hermesBridge": True,
                    "runtimeControlCommand": True,
                    "isolatedProfileWorker": True,
                },
            }
        )

    async def handle_host_command(
        self,
        command_type: str,
        payload: dict[str, Any],
    ) -> None:
        if command_type == "clawchat.host.cron.list":
            from cron.jobs import list_jobs

            data = {
                "requestId": payload.get("requestId"),
                "externalAgentId": self.config.external_agent_ids[0],
                "jobs": await asyncio.to_thread(list_jobs, True),
            }
            result_type = "clawchat.host.cron.list.result"
        elif command_type == "clawchat.host.scheduler.maintain":
            from cron.jobs import resume_job

            job_id = str(payload.get("jobId") or "").strip()
            action = str(payload.get("action") or "").strip()
            if not job_id or action not in {"activate", "recover"}:
                raise ValueError("invalid Hermes scheduler maintenance request")
            job = await asyncio.to_thread(resume_job, job_id)
            if not job:
                raise ValueError("Hermes cron job was not found")
            data = {
                "requestId": payload.get("requestId"),
                "externalAgentId": self.config.external_agent_ids[0],
                "jobId": job_id,
                "action": action,
                "acknowledged": True,
                "job": job,
            }
            result_type = "clawchat.host.scheduler.maintain.result"
        else:
            raise ValueError(f"unsupported Hermes host command {command_type!r}")
        await self._write(
            {
                "type": "host_command.result",
                "resultType": result_type,
                "data": data,
            }
        )


async def _readline() -> bytes:
    return await asyncio.to_thread(sys.stdin.buffer.readline)


async def run_worker() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    bridge = WorkerBridge()
    while True:
        line = await _readline()
        if not line:
            return 0
        try:
            message = decode_worker_message(line)
            message_type = str(message.get("type") or "")
            if message_type == "initialize":
                if bridge.run_manager is not None:
                    raise ValueError("profile worker is already initialized")
                bridge.initialize(message)
                continue
            if bridge.run_manager is None:
                raise ValueError("profile worker must be initialized first")
            if message_type == "dispatch":
                payload = message.get("payload")
                if not isinstance(payload, dict):
                    raise ValueError("profile worker dispatch payload must be an object")
                await bridge.handle_dispatch(payload)
                continue
            if message_type == "cancel":
                bridge.run_manager.cancel(str(message.get("dispatchId") or ""))
                continue
            if message_type == "structured_job":
                payload = message.get("payload")
                if not isinstance(payload, dict):
                    raise ValueError("profile worker structured payload must be an object")
                assert bridge.structured_job_runner
                await bridge.structured_job_runner.handle(payload)
                continue
            if message_type == "install_skill":
                payload = message.get("payload")
                if not isinstance(payload, dict):
                    raise ValueError("profile worker skill payload must be an object")
                assert bridge.marketplace_installer
                result = await asyncio.to_thread(
                    bridge.marketplace_installer.install,
                    payload,
                )
                await bridge._write(
                    {
                        "type": "marketplace.installHermesSkill.result",
                        "data": result,
                    }
                )
                continue
            if message_type == "host_command":
                payload = message.get("payload")
                if not isinstance(payload, dict):
                    raise ValueError("profile worker host command payload must be an object")
                await bridge.handle_host_command(
                    str(message.get("commandType") or ""),
                    payload,
                )
                continue
            if message_type == "shutdown":
                return 0
            raise ValueError(f"unsupported profile worker message {message_type!r}")
        except bridge_main.HermesDispatchDedupe as exc:
            await bridge._write(
                {"type": "worker.error", "code": "dispatch_duplicate", "message": str(exc)}
            )
        except Exception as exc:
            logger.exception("profile worker command failed")
            if str(message.get("type") or "") == "host_command":
                command_type = str(message.get("commandType") or "")
                payload = message.get("payload")
                request_id = payload.get("requestId") if isinstance(payload, dict) else None
                await bridge._write(
                    {
                        "type": "host_command.result",
                        "resultType": f"{command_type}.error",
                        "data": {
                            "requestId": request_id,
                            "error": str(exc)[:1000],
                        },
                    }
                )
                continue
            await bridge._write(
                {
                    "type": "worker.error",
                    "code": "worker_command_failed",
                    "message": str(exc)[:1000],
                }
            )


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run_worker()))
