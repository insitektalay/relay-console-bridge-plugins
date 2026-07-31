from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

try:
    from .worker_protocol import decode_worker_message, encode_worker_message
except ImportError:  # pragma: no cover - direct source execution
    from worker_protocol import decode_worker_message, encode_worker_message


logger = logging.getLogger("clawchat.hermes_profile_supervisor")
MAX_PROFILE_WORKERS = 16
MAX_RESTARTS_PER_MINUTE = 5
DEFAULT_IDLE_TIMEOUT_S = 300.0
BRIDGE_ENVIRONMENT_PREFIXES = (
    "CLAWCHAT_",
    "RELAY_CONSOLE_",
)
BRIDGE_ENVIRONMENT_NAMES = {
    "RELAY_ACCESS_TOKEN",
    "RELAY_BRIDGE_DEVICE_TOKEN",
    "RELAY_ENROLLMENT_CODE",
}


@dataclass
class ProfileWorker:
    external_id: str
    profile_home: Path
    binding_epoch: str
    process: asyncio.subprocess.Process
    write_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    reader_task: asyncio.Task[None] | None = None
    stderr_task: asyncio.Task[None] | None = None
    last_used_monotonic: float = field(default_factory=time.monotonic)


class HermesProfileSupervisor:
    """Owns fixed-HERMES_HOME child processes; the parent keeps device credentials."""

    def __init__(
        self,
        *,
        worker_script: Path,
        api_url: str,
        workspace_id: str,
        event_handler: Callable[[dict[str, Any]], Awaitable[None]],
        message_handler: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        max_workers: int = MAX_PROFILE_WORKERS,
        idle_timeout_s: float = DEFAULT_IDLE_TIMEOUT_S,
    ) -> None:
        self.worker_script = worker_script.resolve()
        self.api_url = api_url
        self.workspace_id = workspace_id
        self.event_handler = event_handler
        self.message_handler = message_handler
        self.max_workers = max(1, min(MAX_PROFILE_WORKERS, max_workers))
        self.idle_timeout_s = max(30.0, idle_timeout_s)
        self._workers: dict[str, ProfileWorker] = {}
        self._registry_lock = asyncio.Lock()
        self._restart_times: dict[str, list[float]] = {}
        self._reaper_task: asyncio.Task[None] | None = None

    async def dispatch(
        self,
        *,
        external_id: str,
        profile_home: Path,
        binding_epoch: str,
        payload: dict[str, Any],
    ) -> None:
        worker = await self._worker_for(external_id, profile_home, binding_epoch)
        await self._send(worker, {"type": "dispatch", "payload": payload})

    async def cancel(self, external_id: str, dispatch_id: str) -> bool:
        worker = self._workers.get(external_id)
        if not worker or worker.process.returncode is not None:
            return False
        await self._send(
            worker,
            {"type": "cancel", "dispatchId": dispatch_id},
        )
        return True

    async def dispatch_structured(
        self,
        *,
        external_id: str,
        profile_home: Path,
        binding_epoch: str,
        payload: dict[str, Any],
    ) -> None:
        worker = await self._worker_for(external_id, profile_home, binding_epoch)
        await self._send(worker, {"type": "structured_job", "payload": payload})

    async def install_skill(
        self,
        *,
        external_id: str,
        profile_home: Path,
        binding_epoch: str,
        payload: dict[str, Any],
    ) -> None:
        worker = await self._worker_for(external_id, profile_home, binding_epoch)
        await self._send(worker, {"type": "install_skill", "payload": payload})

    async def host_command(
        self,
        *,
        external_id: str,
        profile_home: Path,
        binding_epoch: str,
        command_type: str,
        payload: dict[str, Any],
    ) -> None:
        worker = await self._worker_for(external_id, profile_home, binding_epoch)
        await self._send(
            worker,
            {
                "type": "host_command",
                "commandType": command_type,
                "payload": payload,
            },
        )

    async def shutdown(self) -> None:
        if self._reaper_task and not self._reaper_task.done():
            self._reaper_task.cancel()
        self._reaper_task = None
        async with self._registry_lock:
            workers = list(self._workers.values())
            self._workers.clear()
        for worker in workers:
            await self._stop_worker(worker)

    async def _worker_for(
        self,
        external_id: str,
        profile_home: Path,
        binding_epoch: str,
    ) -> ProfileWorker:
        resolved_home = profile_home.expanduser().resolve()
        if not resolved_home.is_dir() or resolved_home.is_symlink():
            raise RuntimeError("profile_unavailable")
        self._ensure_reaper()
        async with self._registry_lock:
            current = self._workers.get(external_id)
            if (
                current
                and current.process.returncode is None
                and current.binding_epoch == binding_epoch
                and current.profile_home == resolved_home
            ):
                current.last_used_monotonic = time.monotonic()
                return current
            if current:
                await self._stop_worker(current)
                self._workers.pop(external_id, None)
            if len(self._workers) >= self.max_workers:
                raise RuntimeError("profile_worker_limit_reached")
            self._record_restart(external_id)
            worker = await self._start_worker(
                external_id,
                resolved_home,
                binding_epoch,
            )
            self._workers[external_id] = worker
            return worker

    def _ensure_reaper(self) -> None:
        if self._reaper_task and not self._reaper_task.done():
            return
        self._reaper_task = asyncio.create_task(self._reap_idle_workers())

    async def _reap_idle_workers(self) -> None:
        try:
            while True:
                await asyncio.sleep(min(60.0, self.idle_timeout_s))
                cutoff = time.monotonic() - self.idle_timeout_s
                async with self._registry_lock:
                    idle = [
                        (external_id, worker)
                        for external_id, worker in self._workers.items()
                        if worker.last_used_monotonic <= cutoff
                    ]
                    for external_id, worker in idle:
                        await self._stop_worker(worker)
                        self._workers.pop(external_id, None)
                        logger.info(
                            "stopped idle Hermes profile worker profile=%s",
                            external_id,
                        )
        except asyncio.CancelledError:
            return

    def _record_restart(self, external_id: str) -> None:
        now = time.monotonic()
        recent = [
            observed
            for observed in self._restart_times.get(external_id, [])
            if now - observed < 60
        ]
        if len(recent) >= MAX_RESTARTS_PER_MINUTE:
            raise RuntimeError("profile_worker_restart_rate_limited")
        recent.append(now)
        self._restart_times[external_id] = recent

    async def _start_worker(
        self,
        external_id: str,
        profile_home: Path,
        binding_epoch: str,
    ) -> ProfileWorker:
        # The worker needs the host's normal Hermes/provider environment, but
        # it must never inherit Relay bridge enrollment or control-plane
        # credentials. Profile-specific Hermes configuration remains under the
        # fixed HERMES_HOME set below.
        environment = {
            key: value
            for key, value in os.environ.items()
            if key not in BRIDGE_ENVIRONMENT_NAMES
            and not key.startswith(BRIDGE_ENVIRONMENT_PREFIXES)
        }
        environment["HERMES_HOME"] = str(profile_home)
        environment["RELAY_HERMES_PROFILE_EXTERNAL_ID"] = external_id
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(self.worker_script),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(profile_home),
            env=environment,
            limit=4 * 1024 * 1024 + 1024,
        )
        worker = ProfileWorker(
            external_id=external_id,
            profile_home=profile_home,
            binding_epoch=binding_epoch,
            process=process,
        )
        worker.reader_task = asyncio.create_task(self._read_worker(worker))
        worker.stderr_task = asyncio.create_task(self._read_stderr(worker))
        await self._send(
            worker,
            {
                "type": "initialize",
                "externalAgentId": external_id,
                "bindingEpoch": binding_epoch,
                "apiUrl": self.api_url,
                "workspaceId": self.workspace_id,
            },
        )
        return worker

    async def _send(self, worker: ProfileWorker, message: dict[str, Any]) -> None:
        if worker.process.returncode is not None or not worker.process.stdin:
            raise RuntimeError("worker_failed")
        encoded = encode_worker_message(message)
        async with worker.write_lock:
            worker.process.stdin.write(encoded)
            await worker.process.stdin.drain()
        worker.last_used_monotonic = time.monotonic()

    async def _read_worker(self, worker: ProfileWorker) -> None:
        assert worker.process.stdout
        try:
            while True:
                line = await worker.process.stdout.readline()
                if not line:
                    break
                message = decode_worker_message(line)
                if message.get("type") == "event" and isinstance(message.get("event"), dict):
                    await self.event_handler(message["event"])
                elif (
                    message.get("type")
                    in {
                        "structured_job.result",
                        "structured_job.error",
                        "marketplace.installHermesSkill.result",
                        "host_command.result",
                    }
                    and self.message_handler
                ):
                    await self.message_handler(message)
                elif message.get("type") == "worker.error":
                    logger.warning(
                        "Hermes profile worker error profile=%s code=%s message=%s",
                        worker.external_id,
                        message.get("code"),
                        message.get("message"),
                    )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "Hermes profile worker protocol failed profile=%s",
                worker.external_id,
                exc_info=True,
            )

    async def _read_stderr(self, worker: ProfileWorker) -> None:
        assert worker.process.stderr
        try:
            while True:
                line = await worker.process.stderr.readline()
                if not line:
                    break
                logger.info(
                    "Hermes profile worker profile=%s: %s",
                    worker.external_id,
                    line.decode("utf8", errors="replace").rstrip(),
                )
        except asyncio.CancelledError:
            raise

    async def _stop_worker(self, worker: ProfileWorker) -> None:
        if worker.process.returncode is None:
            try:
                await self._send(worker, {"type": "shutdown"})
                await asyncio.wait_for(worker.process.wait(), timeout=5)
            except Exception:
                worker.process.terminate()
                try:
                    await asyncio.wait_for(worker.process.wait(), timeout=3)
                except Exception:
                    worker.process.kill()
                    await worker.process.wait()
        for task in (worker.reader_task, worker.stderr_task):
            if task and not task.done():
                task.cancel()
