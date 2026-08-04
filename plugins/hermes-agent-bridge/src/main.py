from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import importlib.metadata
import json
import logging
import os
import queue
import re
import shlex
import socket
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextvars import ContextVar
from datetime import datetime, timezone
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import MethodType
from typing import Any
from uuid import uuid4

try:
    import aiohttp
except ImportError as exc:  # pragma: no cover - dependency error path
    raise SystemExit(
        "aiohttp is required for the Relay Console Hermes bridge. "
        "Install Hermes with the messaging extra or run: pip install aiohttp"
    ) from exc

from hermes_constants import get_hermes_home

try:
    from .document_policy import (
        safe_profile_document_path,
        scan_profile_documents,
    )
    from .native_profiles import (
        NativeHermesProfile,
        create_native_profile,
        enumerate_native_profiles,
        profile_name_from_external_id,
    )
    from .profile_supervisor import HermesProfileSupervisor
    from .provisioning import provision_native_profile
except ImportError:  # pragma: no cover - direct source execution
    from document_policy import safe_profile_document_path, scan_profile_documents
    from native_profiles import (
        NativeHermesProfile,
        create_native_profile,
        enumerate_native_profiles,
        profile_name_from_external_id,
    )
    from profile_supervisor import HermesProfileSupervisor
    from provisioning import provision_native_profile

CAPABILITY = "clawchat.runtime.hermes"
MARKETPLACE_TOOLS_CAPABILITY = "clawchat.marketplace.tools"
MARKETPLACE_SKILL_INSTALL_CAPABILITY = "marketplaceHermesSkillInstall"
MARKETPLACE_LOCAL_REPO_DOCS_READ_CAPABILITY = "marketplaceLocalRepoDocsRead"
MARKETPLACE_LOCAL_REPO_DOCS_WRITE_CAPABILITY = "marketplaceLocalRepoDocsWrite"
MARKETPLACE_LOCAL_APP_AGENT_API_SETUP_CAPABILITY = "marketplaceLocalAppAgentApiSetup"
MARKETPLACE_LOCAL_APP_AGENT_API_REQUEST_CAPABILITY = "marketplaceLocalAppAgentApiRequest"
LOCAL_APP_RUNTIME_RECOVERY_CAPABILITY = "localAppRuntimeRecovery"
STRUCTURED_JOBS_CAPABILITY = "clawchat.runtime.structured_jobs"
STRUCTURED_OUTPUT_CAPABILITY = "clawchat.runtime.structured_output"
HERMES_AGENT_PROVISIONING_CAPABILITY = "clawchat.runtime.hermes_agent_provisioning"
AGENT_REPLICA_SYNC_CAPABILITY = "clawchat.agent_replica_sync"
RUNTIME_CONNECTOR_V2_CAPABILITY = "clawchat.runtime_connector.v2"
RUNTIME_CONNECTOR_V3_CAPABILITY = "clawchat.runtime_connector.v3"
ROTATING_CREDENTIALS_CAPABILITY = "clawchat.bridge.rotating_credentials.v1"
RUNTIME_MODEL_CATALOG_CAPABILITY = "clawchat.runtime.model_catalog"
HOST_CRON_CAPABILITY = "clawchat.host.cron_management"
HOST_SCHEDULER_CAPABILITY = "clawchat.host.scheduler_maintenance"
RELAY_CONNECTOR_V3 = "relay-connector.v3"
RELAY_CONNECTOR_V2 = "relay-connector.v2"
AGENT_REPLICA_V1 = "agent-replica.v1"
BRIDGE_CAPABILITIES = [
    CAPABILITY,
    MARKETPLACE_TOOLS_CAPABILITY,
    MARKETPLACE_SKILL_INSTALL_CAPABILITY,
    MARKETPLACE_LOCAL_REPO_DOCS_READ_CAPABILITY,
    MARKETPLACE_LOCAL_REPO_DOCS_WRITE_CAPABILITY,
    MARKETPLACE_LOCAL_APP_AGENT_API_SETUP_CAPABILITY,
    MARKETPLACE_LOCAL_APP_AGENT_API_REQUEST_CAPABILITY,
    LOCAL_APP_RUNTIME_RECOVERY_CAPABILITY,
    STRUCTURED_JOBS_CAPABILITY,
    STRUCTURED_OUTPUT_CAPABILITY,
    HERMES_AGENT_PROVISIONING_CAPABILITY,
    AGENT_REPLICA_SYNC_CAPABILITY,
    RUNTIME_CONNECTOR_V3_CAPABILITY,
    RUNTIME_CONNECTOR_V2_CAPABILITY,
    ROTATING_CREDENTIALS_CAPABILITY,
    RUNTIME_MODEL_CATALOG_CAPABILITY,
    HOST_CRON_CAPABILITY,
    HOST_SCHEDULER_CAPABILITY,
]
PLUGIN_VERSION = "0.3.0-rc.2"
API_CONTRACT_VERSION = "v2"
WEBSOCKET_CONTRACT_VERSION = "bridge.v1"


def _safe_native_provision_error_code(error: Exception) -> str:
    candidate = str(error).strip()
    if re.fullmatch(r"[A-Z][A-Z0-9_]{0,119}", candidate):
        return candidate
    return "HERMES_NATIVE_PROVISION_FAILED"


def _safe_agent_sync_error_code(error: Exception) -> str:
    candidate = str(error).strip()
    if re.fullmatch(r"[A-Z][A-Z0-9_]{0,119}", candidate):
        return candidate
    return "HERMES_AGENT_SYNC_FAILED"


def _detect_host_type() -> str:
    if sys.platform == "darwin":
        return "macos-launchd"
    if sys.platform.startswith("linux"):
        return "linux-systemd"
    return f"unsupported-{sys.platform}"


def _detect_hermes_runtime_version() -> str | None:
    configured = os.getenv("HERMES_AGENT_VERSION", "").strip()
    if configured:
        return configured
    try:
        checkout = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            ["git", "-C", str(checkout), "describe", "--tags", "--exact-match"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        version = importlib.metadata.version("hermes-agent").strip()
        return version or None
    except importlib.metadata.PackageNotFoundError:
        return None


OPEN_CORE_VERSION = _detect_hermes_runtime_version()
HOST_TYPE = _detect_host_type()


def _bridge_device_metadata() -> dict[str, Any]:
    return {
        "pluginVersion": PLUGIN_VERSION,
        "openCoreVersion": OPEN_CORE_VERSION,
        "runtimeType": "hermes",
        "hostType": HOST_TYPE,
        "apiContractVersion": API_CONTRACT_VERSION,
        "websocketContractVersion": WEBSOCKET_CONTRACT_VERSION,
        "capabilities": BRIDGE_CAPABILITIES,
    }
DEFAULT_DEVICE_LABEL = "Relay Console Hermes bridge"
MAX_ENROLLMENT_CODE_BYTES = 4_096
DEFAULT_MODEL = "gpt-5.4"
DEFAULT_DISABLED_TOOLSETS: list[str] = []
NATIVE_HARNESS_REQUIRED_TOOLS = {
    "memory",
    "session_search",
    "read_file",
    "write_file",
    "patch",
    "search_files",
    "terminal",
    "process",
    "skills_list",
    "skill_view",
    "skill_manage",
}
NATIVE_HARNESS_TOOLSET_TO_TOOLS = {
    "memory": {"memory"},
    "session_search": {"session_search"},
    "file": {"read_file", "write_file", "patch", "search_files"},
    "files": {"read_file", "write_file", "patch", "search_files"},
    "file_tools": {"read_file", "write_file", "patch", "search_files"},
    "terminal": {"terminal", "process"},
    "terminal_tools": {"terminal", "process"},
    "process": {"process"},
    "skills": {"skills_list", "skill_view", "skill_manage"},
    "skill": {"skills_list", "skill_view", "skill_manage"},
    "skills_tools": {"skills_list", "skill_view", "skill_manage"},
}
MAX_WORKSPACE_FILE_BYTES = 2_000_000
MAX_MARKETPLACE_SKILL_FILE_BYTES = 500_000
TERMINAL_EVENT_TYPES = {"run.completed", "run.failed", "run.cancelled"}
TERMINAL_EVENT_MAX_ATTEMPTS = 8
TERMINAL_EVENT_RETRY_INTERVAL_S = 5.0
BACKFILL_ENDPOINT_PATH = "/api/v1/bridge/runtime-dispatches/backfill"
BACKFILL_REQUEST_TIMEOUT_S = 10.0
BACKFILL_MAX_ATTEMPTS = 3
BACKFILL_RETRY_BASE_S = 1.0
BACKFILL_RETRY_MAX_S = 10.0
TERMINAL_DISPATCH_STATE = {"completed", "failed", "cancelled", "skipped_terminal"}
LOCAL_APP_ALLOWED_START_COMMANDS = {
    "pnpm dev",
    "pnpm dlx convex dev",
    "pnpm convex dev",
}
LOCAL_APP_COMMAND_SHELL_META_RE = re.compile(r"&&|\|\||[;|<>`$]")
LOCAL_APP_REJECTED_COMMAND_TOKENS = {
    "install",
    "add",
    "upgrade",
    "migrate",
    "migration",
    "reset",
    "deploy",
    "prisma",
}
SECRET_FIELD_RE = re.compile(
    r"(?:^|[_\-.])(api[_\-.]?key|access[_\-.]?token|refresh[_\-.]?token|id[_\-.]?token|token|secret|password|passwd|private[_\-.]?key|client[_\-.]?secret|webhook[_\-.]?secret|credentials?)(?:$|[_\-.])",
    re.IGNORECASE,
)
logger = logging.getLogger("clawchat.hermes_bridge")


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _csv(value: str | None) -> list[str]:
    return [part.strip() for part in (value or "").split(",") if part.strip()]


def _normalize_api_url(value: str) -> str:
    value = value.strip().rstrip("/")
    if value.endswith("/api/v1"):
        value = value[: -len("/api/v1")]
    if not value:
        raise ValueError("Relay Console API URL is required")
    if value.startswith("http://") and os.getenv("RELAY_CONSOLE_BRIDGE_ALLOW_INSECURE_HTTP", "").lower() not in {"1", "true", "yes"}:
        raise ValueError("Relay Console bridge requires an https:// API URL")
    if not value.startswith("https://") and not value.startswith("http://"):
        raise ValueError("Relay Console API URL must start with https://")
    return value


def _ws_url_for(api_url: str) -> str:
    if api_url.startswith("https://"):
        return "wss://" + api_url[len("https://") :]
    if api_url.startswith("http://"):
        return "ws://" + api_url[len("http://") :]
    raise ValueError("api_url must start with http:// or https://")


def _config_dir() -> Path:
    return get_hermes_home() / "clawchat_bridge"


def _config_path() -> Path:
    return _config_dir() / "config.json"


def _configured_default_model() -> str:
    try:
        from hermes_cli.config import load_config

        model_config = load_config().get("model")
        if isinstance(model_config, dict):
            value = str(model_config.get("default") or "").strip()
        else:
            value = str(model_config or "").strip()
        return value or DEFAULT_MODEL
    except Exception:
        logger.debug("failed to load Hermes default model", exc_info=True)
        return DEFAULT_MODEL


def _runtime_model_catalog() -> dict[str, Any]:
    """Return the same OpenAI Codex model catalogue Hermes exposes locally."""
    try:
        from hermes_cli.codex_models import get_codex_model_ids

        discovered = get_codex_model_ids()
        models = list(
            dict.fromkeys(
                model.strip()
                for model in discovered
                if isinstance(model, str) and model.strip()
            )
        )
    except Exception:
        logger.warning(
            "failed to discover Hermes Codex model catalogue",
            exc_info=True,
        )
        models = []

    configured_default = _configured_default_model()
    if configured_default and configured_default not in models:
        models.insert(0, configured_default)
    if not models:
        models = [configured_default or DEFAULT_MODEL]

    return {
        "runtimeType": "hermes",
        "defaultModel": (
            configured_default
            if configured_default in models
            else models[0]
        ),
        "models": models,
        "source": "hermes-codex-discovery",
        "observedAt": _now_iso(),
    }


def _safe_segment(value: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "_" for ch in value.strip())
    return safe.strip("._") or "agent"


def _is_safe_segment(value: str) -> bool:
    return bool(value) and _safe_segment(value) == value


def _as_prompt_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        return text or None
    try:
        return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
    except TypeError:
        text = str(value).strip()
        return text or None


AUTONOMY_MODES = {
    "safe_default",
    "internal_write",
    "supervised_external",
    "dangerously_skip_permissions",
    "custom_policy",
}
AUTONOMY_CATEGORY_ALIASES = {
    "read": "read",
    "draft": "draft",
    "write": "write_internal",
    "write_internal": "write_internal",
    "internal_write": "write_internal",
    "task_update": "task_update",
    "status_update": "status_update_internal",
    "status_update_internal": "status_update_internal",
    "browser": "browser_navigation",
    "browser_navigation": "browser_navigation",
    "form_fill": "form_fill",
    "public_form_fill": "form_fill",
    "form_submit": "form_submit",
    "public_form_submit": "form_submit",
    "search": "external_search",
    "web_search": "external_search",
    "external_search": "external_search",
    "email_draft": "email_draft",
    "email_send": "email_send",
    "account_create": "account_create",
    "account_creation": "account_create",
    "credential_use": "credential_use",
    "external_publish": "external_publish",
    "external_publishing": "external_publish",
    "publish": "external_publish",
    "backlink_verify": "backlink_verify",
    "backlink_verification": "backlink_verify",
    "index_check": "index_check",
    "index_checking": "index_check",
    "contacted_submitted": "lifecycle_contacted_submitted",
    "lifecycle_contacted_submitted": "lifecycle_contacted_submitted",
    "live_indexed": "lifecycle_live_indexed",
    "lifecycle_live_indexed": "lifecycle_live_indexed",
    "linkcrest_agent_api": "linkcrest_agent_api",
    "linkcrest_agentapi": "linkcrest_agent_api",
    "linkcrest.agentapi": "linkcrest_agent_api",
    "agentapi": "linkcrest_agent_api",
    "linkcrest_openclaw_tools": "linkcrest_openclaw_tools",
    "local_app_record_write": "local_app_record_write",
}
AUTONOMY_CATEGORIES = [
    "read",
    "draft",
    "write_internal",
    "task_update",
    "status_update_internal",
    "browser_navigation",
    "form_fill",
    "form_submit",
    "external_search",
    "email_draft",
    "email_send",
    "account_create",
    "credential_use",
    "external_publish",
    "backlink_verify",
    "index_check",
    "lifecycle_contacted_submitted",
    "lifecycle_live_indexed",
    "linkcrest_agent_api",
    "linkcrest_openclaw_tools",
    "local_app_record_write",
]
EXTERNAL_AUTONOMY_CATEGORIES = {
    "browser_navigation",
    "form_fill",
    "form_submit",
    "external_search",
    "email_draft",
    "email_send",
    "account_create",
    "credential_use",
    "external_publish",
    "backlink_verify",
    "index_check",
    "lifecycle_contacted_submitted",
    "lifecycle_live_indexed",
    "linkcrest_agent_api",
}
AUTONOMY_TOOL_CANDIDATES = {
    "read": ("read_file", "search_files"),
    "draft": ("write_file", "patch"),
    "write_internal": ("write_file", "patch", "terminal"),
    "task_update": ("terminal",),
    "status_update_internal": ("terminal",),
    "browser_navigation": ("browser_navigate", "browser_snapshot"),
    "form_fill": ("browser_type", "browser_click", "browser_snapshot"),
    "form_submit": ("browser_click", "browser_press", "browser_snapshot"),
    "external_search": ("web_search",),
    "email_draft": (),
    "email_send": (),
    "account_create": ("browser_navigate", "browser_type", "browser_click"),
    "credential_use": (),
    "external_publish": ("browser_navigate", "browser_type", "browser_click"),
    "backlink_verify": ("browser_navigate", "browser_snapshot", "web_search", "web_extract"),
    "index_check": ("web_search",),
    "lifecycle_contacted_submitted": (),
    "lifecycle_live_indexed": (),
    "linkcrest_agent_api": (),
    "linkcrest_openclaw_tools": (),
    "local_app_record_write": (),
}
AUTONOMY_MARKETPLACE_HINTS = {
    "task_update": ("task", "openclaw", "linkcrest"),
    "status_update_internal": ("status", "record", "openclaw", "linkcrest"),
    "email_draft": ("outlook", "email", "mail", "gmail", "resend", "smtp", "outreach"),
    "email_send": ("outlook", "email", "mail", "gmail", "resend", "smtp", "send", "outreach"),
    "credential_use": ("credential", "secret", "account"),
    "external_publish": ("publish", "post"),
    "backlink_verify": ("verify", "backlink"),
    "index_check": ("index", "search"),
    "lifecycle_contacted_submitted": ("contacted", "submitted", "lifecycle", "status"),
    "lifecycle_live_indexed": ("live", "indexed", "verify", "lifecycle", "status"),
    "linkcrest_agent_api": ("linkcrest", "agent", "api"),
    "linkcrest_openclaw_tools": ("linkcrest", "openclaw"),
    "local_app_record_write": ("record", "write", "local"),
}
MISSING_TOOL_CAPABILITY_LABELS = {
    "form_fill": "public_form_fill",
    "form_submit": "public_form_submit",
    "account_create": "account_creation",
    "external_publish": "external_publishing",
    "backlink_verify": "backlink_verification",
    "index_check": "index_checking",
}
MISSING_TOOL_SUGGESTIONS = {
    "browser_navigation": (["browser"], ["browser_navigation"]),
    "form_fill": (["browser"], ["public_form_fill"]),
    "form_submit": (["browser/form executor", "Playwright/browser automation"], ["browser", "form_submission"]),
    "external_search": (["SERP API", "web search", "Exa", "Tavily", "Google/Bing search provider"], ["search", "prospect_discovery"]),
    "email_draft": (["Outlook", "Gmail", "Mailgun", "Resend"], ["email_draft"]),
    "email_send": (["Gmail", "Outlook", "Resend", "SMTP"], ["email", "mailbox", "sender_identity"]),
    "account_create": (["browser", "credential_manager"], ["account_creation", "credential_use"]),
    "credential_use": (["credential vault / account connector"], ["credentials", "account_access"]),
    "external_publish": (["cms", "browser", "social_publisher"], ["external_publishing"]),
    "backlink_verify": (["SEO crawler", "backlink checker", "browser verifier"], ["crawler", "seo", "verification"]),
    "index_check": (["SERP/index checker", "Google Search Console"], ["index_check", "search"]),
    "lifecycle_contacted_submitted": (["local-linkcrest"], ["lifecycle_contacted_submitted"]),
    "lifecycle_live_indexed": (["local-linkcrest", "seo_verifier"], ["lifecycle_live_indexed"]),
    "linkcrest_agent_api": (["local-linkcrest"], ["linkcrest_agent_api"]),
    "linkcrest_openclaw_tools": (["local-linkcrest"], ["linkcrest_openclaw_tools"]),
    "local_app_record_write": (["local-linkcrest"], ["local_app_record_write"]),
}
LINKCREST_BACKLINK_REQUIRED_CATEGORIES = {
    "email_send",
    "external_search",
    "form_submit",
    "backlink_verify",
    "index_check",
    "credential_use",
}
DEFAULT_HARD_STOPS = [
    "Do not fake results or claim contacted/submitted/live/indexed without evidence.",
    "Do not expose secrets, tokens, cookies, private keys, or credential values.",
    "Do not perform destructive data loss, resets, or bulk deletion.",
    "Do not bypass CAPTCHA, rate limits, paywalls, authentication, or access controls.",
    "Do not make payments or donations unless explicitly configured by current policy.",
    "Do not accept legal commitments, contracts, or terms on behalf of the user unless explicitly configured by current policy.",
    "Do not use unavailable tools. Report the missing tool instead.",
]


def _normalize_policy_category(value: Any) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    key = re.sub(r"[^A-Za-z0-9]+", "_", raw).strip("_").lower()
    return AUTONOMY_CATEGORY_ALIASES.get(key, key if key in AUTONOMY_CATEGORIES else None)


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        raw_values = re.split(r"[,;\s]+", value)
    elif isinstance(value, (list, tuple, set)):
        raw_values = [str(item) for item in value if item is not None]
    else:
        raw_values = [str(value)]
    values: list[str] = []
    seen: set[str] = set()
    for raw in raw_values:
        text = str(raw).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        values.append(text)
    return values


def _bool_from_policy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "enabled", "preferred", "required"}
    return bool(value)


def _iso_from_mtime(mtime: float) -> str:
    return datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _json_size_bytes(value: Any) -> int:
    try:
        return len(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8"))
    except Exception:
        return len(str(value).encode("utf-8", errors="replace"))


def _tool_descriptor_name(item: Any) -> str:
    if isinstance(item, str):
        return item.strip()
    if not isinstance(item, dict):
        return ""
    return str(
        item.get("functionName")
        or item.get("function_name")
        or item.get("name")
        or item.get("toolName")
        or ""
    ).strip()


def _tool_descriptor_key(item: Any) -> str:
    raw = _tool_descriptor_name(item)
    if not raw:
        return ""
    return re.sub(r"[^A-Za-z0-9_-]+", "_", raw).strip("_").lower()


@dataclass
class DispatchNormalizationDiagnostics:
    incoming_size_bytes: int
    normalized_size_bytes: int
    dropped_duplicate_fields: list[str] = field(default_factory=list)
    tool_count_before: int = 0
    tool_count_after: int = 0
    instruction_chars_before: int = 0
    instruction_chars_after: int = 0


class DispatchPayloadNormalizer:
    """Bridge-boundary cleanup for ClawChat dispatch payloads.

    ClawChat may include the same runtime data under several legacy aliases.
    Hermes only needs one canonical copy before the harness starts: IDs,
    current user input, selected tools, runtime toolsets/tool names, policy,
    response contract fields, and app/runtime metadata.  This normalizer keeps
    those concepts deterministic and removes duplicate nested copies.
    """

    TOOL_LIST_FIELDS = ("marketplaceTools", "availableMarketplaceTools")
    INSTRUCTION_FIELDS = (
        "systemInstruction",
        "runtimeInstruction",
        "responseFormatContract",
        "responseContract",
        "expectedContentFormat",
        "responsePresentation",
    )
    RUNTIME_TOOL_NAME_FIELDS = (
        "availableRuntimeTools",
        "available_runtime_tools",
        "requiredToolNames",
        "required_tool_names",
        "requiredRuntimeTools",
        "required_runtime_tools",
        "runtimeToolNames",
        "runtime_tool_names",
        "browserToolNames",
        "browser_tool_names",
    )

    def normalize(self, payload: dict[str, Any]) -> tuple[dict[str, Any], DispatchNormalizationDiagnostics]:
        incoming_size = _json_size_bytes(payload)
        normalized = copy.deepcopy(payload)
        dropped: list[str] = []

        tools_before = self._count_tool_descriptors(payload)
        instruction_chars_before = self._instruction_chars(payload)

        canonical_tools, tool_drops = self._collect_marketplace_tools(payload)
        dropped.extend(tool_drops)
        self._write_canonical_marketplace_tools(normalized, canonical_tools, dropped)

        canonical_toolsets, toolset_drops = self._collect_runtime_toolsets(payload)
        dropped.extend(toolset_drops)
        self._write_canonical_runtime_toolsets(normalized, canonical_toolsets, dropped)

        runtime_tool_names, runtime_tool_drops = self._collect_runtime_tool_names(payload)
        dropped.extend(runtime_tool_drops)
        self._write_canonical_runtime_tool_names(normalized, runtime_tool_names, dropped)

        instruction_values, instruction_drops = self._collect_instruction_fields(payload)
        dropped.extend(instruction_drops)
        self._write_canonical_instructions(normalized, instruction_values, dropped)

        self._drop_empty_duplicate_containers(normalized)

        diagnostics = DispatchNormalizationDiagnostics(
            incoming_size_bytes=incoming_size,
            normalized_size_bytes=0,
            dropped_duplicate_fields=list(dict.fromkeys(dropped)),
            tool_count_before=tools_before,
            tool_count_after=len(canonical_tools),
            instruction_chars_before=instruction_chars_before,
            instruction_chars_after=sum(len(_as_prompt_text(value) or "") for value in instruction_values.values()),
        )
        normalized["_normalizationDiagnostics"] = {
            "incomingSizeBytes": diagnostics.incoming_size_bytes,
            "normalizedSizeBytes": 0,
            "droppedDuplicateFields": diagnostics.dropped_duplicate_fields,
            "toolCountBefore": diagnostics.tool_count_before,
            "toolCountAfter": diagnostics.tool_count_after,
            "instructionCharsBefore": diagnostics.instruction_chars_before,
            "instructionCharsAfter": diagnostics.instruction_chars_after,
        }
        diagnostics.normalized_size_bytes = _json_size_bytes(normalized)
        normalized["_normalizationDiagnostics"]["normalizedSizeBytes"] = diagnostics.normalized_size_bytes
        return normalized, diagnostics

    def _containers_with_paths(self, payload: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        containers: list[tuple[str, dict[str, Any]]] = [("$", payload)]
        for key in ("runtimeContext", "marketplaceRuntimeContext", "localAppRuntimeContext", "dispatchMetadata", "configMetadata", "runtimeToolsets"):
            value = payload.get(key)
            if isinstance(value, dict):
                containers.append((key, value))
        for parent_key in ("dispatchMetadata", "configMetadata"):
            parent = payload.get(parent_key)
            if not isinstance(parent, dict):
                continue
            for key in ("runtimeContext", "marketplaceRuntimeContext", "localAppRuntimeContext", "runtimeToolsets"):
                value = parent.get(key)
                if isinstance(value, dict):
                    containers.append((f"{parent_key}.{key}", value))
        return containers

    def _count_tool_descriptors(self, payload: dict[str, Any]) -> int:
        count = 0
        for _path, container in self._containers_with_paths(payload):
            for key in self.TOOL_LIST_FIELDS:
                value = container.get(key)
                if isinstance(value, list):
                    count += len(value)
            tools = container.get("tools")
            if isinstance(tools, list) and any(_tool_descriptor_name(item) for item in tools):
                count += len(tools)
        return count

    def _instruction_chars(self, payload: dict[str, Any]) -> int:
        total = 0
        for _path, container in self._containers_with_paths(payload):
            for key in self.INSTRUCTION_FIELDS:
                if key in container:
                    total += len(_as_prompt_text(container.get(key)) or "")
        return total

    def _collect_marketplace_tools(self, payload: dict[str, Any]) -> tuple[list[Any], list[str]]:
        tools: list[Any] = []
        seen: set[str] = set()
        dropped: list[str] = []
        primary_sources_present = False
        top_context = payload.get("marketplaceRuntimeContext")
        if isinstance(top_context, dict) and isinstance(top_context.get("tools"), list):
            primary_sources_present = True
        if any(isinstance(payload.get(key), list) for key in self.TOOL_LIST_FIELDS):
            primary_sources_present = True
        for path, container in self._containers_with_paths(payload):
            if primary_sources_present and path not in {"$", "marketplaceRuntimeContext"}:
                for key in (*self.TOOL_LIST_FIELDS, "tools"):
                    if isinstance(container.get(key), list):
                        dropped.append(f"{path}.{key}")
                continue
            sources: list[tuple[str, Any]] = [(key, container.get(key)) for key in self.TOOL_LIST_FIELDS]
            if path.endswith("marketplaceRuntimeContext") or path == "marketplaceRuntimeContext":
                sources.insert(0, ("tools", container.get("tools")))
            for key, value in sources:
                if not isinstance(value, list):
                    continue
                for item in value:
                    dedupe_key = _tool_descriptor_key(item)
                    if not dedupe_key:
                        continue
                    if dedupe_key in seen:
                        dropped.append(f"{path}.{key}[]:{dedupe_key}")
                        continue
                    seen.add(dedupe_key)
                    tools.append(copy.deepcopy(item))
        return tools, dropped

    def _write_canonical_marketplace_tools(self, payload: dict[str, Any], tools: list[Any], dropped: list[str]) -> None:
        if tools:
            payload["marketplaceTools"] = tools
        else:
            payload.pop("marketplaceTools", None)
        if "availableMarketplaceTools" in payload:
            payload.pop("availableMarketplaceTools", None)
            dropped.append("$.availableMarketplaceTools")
        self._remove_nested_fields(payload, {"marketplaceTools", "availableMarketplaceTools"}, dropped)
        for path, container in self._containers_with_paths(payload):
            if (path == "marketplaceRuntimeContext" or path.endswith(".marketplaceRuntimeContext")) and "tools" in container:
                container.pop("tools", None)
                dropped.append(f"{path}.tools")

    def _collect_runtime_toolsets(self, payload: dict[str, Any]) -> tuple[dict[str, list[str]], list[str]]:
        additive: list[str] = []
        disabled: list[str] = []
        dropped: list[str] = []
        for path, container in self._containers_with_paths(payload):
            additive.extend(self._coerce_toolset_list(container.get("enabledToolsets")))
            additive.extend(self._coerce_toolset_list(container.get("enabled_toolsets")))
            additive.extend(self._coerce_toolset_list(container.get("additive")))
            additive.extend(self._coerce_toolset_list(container.get("additiveToolsets")))
            additive.extend(self._coerce_toolset_list(container.get("additive_toolsets")))
            disabled.extend(self._coerce_toolset_list(container.get("disabledToolsets")))
            disabled.extend(self._coerce_toolset_list(container.get("disabled_toolsets")))
            disabled.extend(self._coerce_toolset_list(container.get("disabled")))
            nested = container.get("runtimeToolsets") or container.get("runtime_toolsets")
            if isinstance(nested, dict):
                additive.extend(self._coerce_toolset_list(nested.get("enabledToolsets")))
                additive.extend(self._coerce_toolset_list(nested.get("enabled_toolsets")))
                additive.extend(self._coerce_toolset_list(nested.get("additive")))
                additive.extend(self._coerce_toolset_list(nested.get("additiveToolsets")))
                additive.extend(self._coerce_toolset_list(nested.get("additive_toolsets")))
                disabled.extend(self._coerce_toolset_list(nested.get("disabledToolsets")))
                disabled.extend(self._coerce_toolset_list(nested.get("disabled_toolsets")))
                disabled.extend(self._coerce_toolset_list(nested.get("disabled")))
                if path != "$":
                    dropped.append(f"{path}.runtimeToolsets")
        return {
            "additive": list(dict.fromkeys(additive)),
            "disabled": list(dict.fromkeys(disabled)),
        }, dropped

    def _write_canonical_runtime_toolsets(self, payload: dict[str, Any], toolsets: dict[str, list[str]], dropped: list[str]) -> None:
        if toolsets["additive"] or toolsets["disabled"]:
            payload["runtimeToolsets"] = {
                "additive": toolsets["additive"],
                "disabled": toolsets["disabled"],
            }
        else:
            payload.pop("runtimeToolsets", None)
        self._remove_nested_fields(
            payload,
            {"enabledToolsets", "enabled_toolsets", "additive", "additiveToolsets", "additive_toolsets", "disabledToolsets", "disabled_toolsets", "disabled", "runtimeToolsets", "runtime_toolsets"},
            dropped,
            keep_top_runtime_toolsets=True,
        )

    def _collect_runtime_tool_names(self, payload: dict[str, Any]) -> tuple[list[str], list[str]]:
        names: list[str] = []
        dropped: list[str] = []
        for path, container in self._containers_with_paths(payload):
            for key in self.RUNTIME_TOOL_NAME_FIELDS:
                value = container.get(key)
                if value is None:
                    continue
                names.extend(self._tool_names_from_value(value))
                if path != "$":
                    dropped.append(f"{path}.{key}")
        return list(dict.fromkeys(names)), dropped

    def _write_canonical_runtime_tool_names(self, payload: dict[str, Any], names: list[str], dropped: list[str]) -> None:
        if names:
            payload["availableRuntimeTools"] = names
        else:
            payload.pop("availableRuntimeTools", None)
        self._remove_nested_fields(payload, set(self.RUNTIME_TOOL_NAME_FIELDS), dropped)

    def _collect_instruction_fields(self, payload: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
        values: dict[str, Any] = {}
        dropped: list[str] = []
        for key in self.INSTRUCTION_FIELDS:
            seen_value = False
            for path, container in self._containers_with_paths(payload):
                if key not in container:
                    continue
                value = container.get(key)
                if not seen_value and _as_prompt_text(value):
                    values[key] = copy.deepcopy(value)
                    seen_value = True
                else:
                    dropped.append(f"{path}.{key}")
        return values, dropped

    def _write_canonical_instructions(self, payload: dict[str, Any], values: dict[str, Any], dropped: list[str]) -> None:
        for key in self.INSTRUCTION_FIELDS:
            if key in values:
                payload[key] = values[key]
            else:
                payload.pop(key, None)
        self._remove_nested_fields(payload, set(self.INSTRUCTION_FIELDS), dropped)

    def _remove_nested_fields(
        self,
        payload: dict[str, Any],
        fields: set[str],
        dropped: list[str],
        *,
        keep_top_runtime_toolsets: bool = False,
    ) -> None:
        for path, container in self._containers_with_paths(payload):
            for key in list(fields):
                if keep_top_runtime_toolsets and path == "$" and key == "runtimeToolsets":
                    continue
                if keep_top_runtime_toolsets and path == "runtimeToolsets" and key in {"additive", "disabled"}:
                    continue
                if path == "$" and key in {"marketplaceTools", "runtimeToolsets", "availableRuntimeTools", *self.INSTRUCTION_FIELDS}:
                    continue
                if key in container:
                    container.pop(key, None)
                    dropped.append(f"{path}.{key}")

    def _drop_empty_duplicate_containers(self, payload: dict[str, Any]) -> None:
        for parent_key in ("dispatchMetadata", "configMetadata"):
            parent = payload.get(parent_key)
            if not isinstance(parent, dict):
                continue
            for key in ("marketplaceRuntimeContext", "runtimeContext", "localAppRuntimeContext", "runtimeToolsets"):
                value = parent.get(key)
                if isinstance(value, dict) and not value:
                    parent.pop(key, None)

    def _coerce_toolset_list(self, value: Any) -> list[str]:
        return _string_list(value)

    def _tool_names_from_value(self, value: Any) -> list[str]:
        if isinstance(value, str):
            return [value.strip()] if value.strip() else []
        if not isinstance(value, (list, tuple, set)):
            return []
        names: list[str] = []
        for item in value:
            name = _tool_descriptor_name(item)
            if name:
                names.append(name)
        return names


class WorkspaceError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class StructuredJobError(Exception):
    def __init__(self, code: str, message: str, retryable: bool = False, metadata: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.metadata = metadata


def _is_json_object(value: Any) -> bool:
    return isinstance(value, dict)


def _extract_json_object(text: str) -> dict[str, Any]:
    text = (text or "").strip()
    if not text:
        raise StructuredJobError("malformed_output", "Hermes structured job produced empty output")
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    fenced = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, flags=re.IGNORECASE)
    if fenced:
        try:
            parsed = json.loads(fenced.group(1).strip())
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError as exc:
            raise StructuredJobError("malformed_output", f"Hermes structured job returned invalid fenced JSON: {exc}") from exc

    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            parsed = json.loads(text[start:end + 1])
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError as exc:
            raise StructuredJobError("malformed_output", f"Hermes structured job returned invalid JSON object: {exc}") from exc

    raise StructuredJobError("malformed_output", "Hermes structured job output did not contain a JSON object")


def _matches_json_type(value: Any, schema_type: str) -> bool:
    if schema_type == "object":
        return isinstance(value, dict)
    if schema_type == "array":
        return isinstance(value, list)
    if schema_type == "string":
        return isinstance(value, str)
    if schema_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if schema_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if schema_type == "boolean":
        return isinstance(value, bool)
    if schema_type == "null":
        return value is None
    return True


def _validate_json_schema(value: Any, schema: Any, path: str = "$") -> list[str]:
    if schema is True or schema is None:
        return []
    if schema is False:
        return [f"{path}: schema is false"]
    if not isinstance(schema, dict):
        return []

    errors: list[str] = []
    raw_type = schema.get("type")
    allowed_types = raw_type if isinstance(raw_type, list) else [raw_type] if isinstance(raw_type, str) else []
    if allowed_types and not any(_matches_json_type(value, str(item)) for item in allowed_types):
        return [f"{path}: expected {' or '.join(str(item) for item in allowed_types)}"]

    enum_values = schema.get("enum")
    if isinstance(enum_values, list) and value not in enum_values:
        errors.append(f"{path}: value is not in enum")

    variants = schema.get("oneOf") if isinstance(schema.get("oneOf"), list) else schema.get("anyOf")
    if isinstance(variants, list) and variants:
        if not any(not _validate_json_schema(value, variant, path) for variant in variants):
            errors.append(f"{path}: did not match any schema variant")

    if isinstance(value, dict):
        required = [str(item) for item in schema.get("required", []) if isinstance(item, str)]
        for key in required:
            if key not in value:
                errors.append(f"{path}.{key}: required property missing")
        properties = schema.get("properties")
        if isinstance(properties, dict):
            for key, child_schema in properties.items():
                if key in value:
                    errors.extend(_validate_json_schema(value[key], child_schema, f"{path}.{key}"))

    if isinstance(value, list) and "items" in schema:
        for index, item in enumerate(value):
            errors.extend(_validate_json_schema(item, schema.get("items"), f"{path}[{index}]"))

    return errors


def _safe_log_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    if not parsed.scheme and not parsed.netloc:
        return parsed.path or url
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))


def _redact_secret_fields(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, child in value.items():
            if SECRET_FIELD_RE.search(str(key)):
                redacted[str(key)] = "[REDACTED_SECRET_VALUE]"
            else:
                redacted[str(key)] = _redact_secret_fields(child)
        return redacted
    if isinstance(value, list):
        return [_redact_secret_fields(item) for item in value]
    return value


def _strip_secret_schema_fields(schema: Any) -> Any:
    if isinstance(schema, list):
        return [_strip_secret_schema_fields(item) for item in schema]
    if not isinstance(schema, dict):
        return schema

    cleaned: dict[str, Any] = {}
    removed_properties: set[str] = set()
    for key, value in schema.items():
        if SECRET_FIELD_RE.search(str(key)):
            continue
        if key == "properties" and isinstance(value, dict):
            next_properties: dict[str, Any] = {}
            for prop_name, prop_schema in value.items():
                if SECRET_FIELD_RE.search(str(prop_name)):
                    removed_properties.add(str(prop_name))
                    continue
                next_properties[str(prop_name)] = _strip_secret_schema_fields(prop_schema)
            cleaned[key] = next_properties
        elif key == "required" and isinstance(value, list):
            cleaned[key] = [item for item in value if str(item) not in removed_properties and not SECRET_FIELD_RE.search(str(item))]
        else:
            cleaned[key] = _strip_secret_schema_fields(value)
    return cleaned


class HermesWorkspaceManager:
    def __init__(self) -> None:
        self.hermes_home = get_hermes_home()
        self.clawchat_home = self.hermes_home / "clawchat"
        self.native_profile_roots: dict[str, Path] = {}
        self.project_root = os.getenv("CLAWCHAT_HERMES_PROJECT_ROOT", "").strip()

    def handle(self, message_type: str, data: dict[str, Any]) -> dict[str, Any]:
        request_id = str(data.get("requestId") or data.get("request_id") or "")
        response: dict[str, Any] = {
            "requestId": request_id,
            "ok": True,
            "folder": data.get("folder"),
            "path": data.get("path") or "/",
            "filename": data.get("filename"),
        }
        try:
            if message_type == "hermes.workspace.list":
                response.update(self.list(data))
            elif message_type == "hermes.workspace.read":
                response.update(self.read(data))
            elif message_type == "hermes.workspace.write":
                response.update(self.write(data))
            elif message_type == "hermes.workspace.delete":
                response.update(self.delete(data))
            elif message_type == "hermes.workspace.mkdir":
                response.update(self.mkdir(data))
            else:
                raise WorkspaceError("unsupported_operation", f"Unsupported workspace operation: {message_type}")
        except WorkspaceError as exc:
            response["ok"] = False
            response["error"] = {"code": exc.code, "message": exc.message}
        except Exception as exc:
            logger.exception("Hermes workspace operation failed")
            response["ok"] = False
            response["error"] = {"code": "workspace_error", "message": str(exc)}
        return response

    def list(self, data: dict[str, Any]) -> dict[str, Any]:
        root, rel_dir, target = self._resolve_target(data, require_filename=False)
        if not target.exists():
            if data.get("folder") in {"agent", "shared"}:
                target.mkdir(parents=True, exist_ok=True)
            else:
                raise WorkspaceError("not_found", "Folder does not exist")
        if not target.is_dir():
            raise WorkspaceError("not_directory", "Path is not a directory")

        entries = []
        for child in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
            if self._is_hidden_or_secret(child.name):
                continue
            try:
                resolved = child.resolve()
                if not self._is_within(resolved, root):
                    continue
                stat = child.stat()
            except OSError:
                continue
            if (
                root in self.native_profile_roots.values()
                and child.is_file()
                and child.suffix.lower() not in {".md", ".markdown"}
            ):
                continue
            entries.append({
                "name": child.name,
                "type": "folder" if child.is_dir() else "file",
                "size": stat.st_size,
                "mtime": _iso_from_mtime(stat.st_mtime),
                "readonly": self._is_readonly_folder(str(data.get("folder") or "")),
            })
        return {"path": self._display_path(rel_dir), "entries": entries}

    def read(self, data: dict[str, Any]) -> dict[str, Any]:
        _root, rel_dir, target = self._resolve_target(data, require_filename=True)
        if not target.exists():
            raise WorkspaceError("not_found", "File does not exist")
        if not target.is_file():
            raise WorkspaceError("not_file", "Path is not a file")
        size = target.stat().st_size
        if size > MAX_WORKSPACE_FILE_BYTES:
            raise WorkspaceError("file_too_large", f"File is too large to read ({size} bytes)")
        try:
            content = target.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            raise WorkspaceError("binary_file", "Only UTF-8 text files are supported")
        stat = target.stat()
        return {
            "path": self._display_path(rel_dir),
            "filename": target.name,
            "content": content,
            "encoding": "utf8",
            "size": stat.st_size,
            "mtime": _iso_from_mtime(stat.st_mtime),
        }

    def write(self, data: dict[str, Any]) -> dict[str, Any]:
        folder = str(data.get("folder") or "")
        if self._is_readonly_folder(folder):
            raise WorkspaceError("not_allowed", "Folder is read-only")
        _root, rel_dir, target = self._resolve_target(data, require_filename=True)
        content = data.get("content")
        if not isinstance(content, str):
            raise WorkspaceError("invalid_request", "content must be a string")
        encoded = content.encode("utf-8")
        if len(encoded) > MAX_WORKSPACE_FILE_BYTES:
            raise WorkspaceError("file_too_large", f"File is too large to write ({len(encoded)} bytes)")
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = target.with_name(f".{target.name}.tmp")
        tmp_path.write_text(content, encoding="utf-8")
        tmp_path.replace(target)
        stat = target.stat()
        return {
            "path": self._display_path(rel_dir),
            "filename": target.name,
            "size": stat.st_size,
            "mtime": _iso_from_mtime(stat.st_mtime),
        }

    def delete(self, data: dict[str, Any]) -> dict[str, Any]:
        folder = str(data.get("folder") or "")
        if self._is_readonly_folder(folder):
            raise WorkspaceError("not_allowed", "Folder is read-only")
        _root, rel_dir, target = self._resolve_target(data, require_filename=True)
        if not target.exists():
            raise WorkspaceError("not_found", "File or folder does not exist")
        if target.is_dir():
            try:
                target.rmdir()
            except OSError:
                raise WorkspaceError("directory_not_empty", "Directory is not empty")
        elif target.is_file():
            target.unlink()
        else:
            raise WorkspaceError("not_allowed", "Only files and empty directories can be deleted")
        return {"path": self._display_path(rel_dir), "filename": target.name}

    def mkdir(self, data: dict[str, Any]) -> dict[str, Any]:
        folder = str(data.get("folder") or "")
        if self._is_readonly_folder(folder):
            raise WorkspaceError("not_allowed", "Folder is read-only")
        _root, rel_dir, target = self._resolve_target(data, require_filename=True)
        target.mkdir(parents=True, exist_ok=True)
        stat = target.stat()
        return {
            "path": self._display_path(rel_dir),
            "filename": target.name,
            "type": "folder",
            "mtime": _iso_from_mtime(stat.st_mtime),
        }

    def _root_for(self, data: dict[str, Any]) -> Path:
        folder = str(data.get("folder") or "agent").strip() or "agent"
        raw_external_agent_id = str(data.get("externalAgentId") or "agent").strip()
        external_agent_id = _safe_segment(raw_external_agent_id)
        if folder == "agent":
            root = self.native_profile_roots.get(raw_external_agent_id)
            if root is None:
                root = self.clawchat_home / "agents" / external_agent_id / "workspace"
        elif folder == "shared":
            root = self.clawchat_home / "shared"
        elif folder == "sessions":
            root = self.clawchat_home / "runtime_sessions"
        elif folder == "project":
            raw_root = str(data.get("workspaceRoot") or self.project_root or "").strip()
            if not raw_root:
                raise WorkspaceError("project_root_missing", "No Hermes project workspaceRoot is configured")
            root = Path(raw_root).expanduser()
        else:
            raise WorkspaceError("invalid_folder", f"Unsupported folder: {folder}")
        return root.resolve()

    def _resolve_target(self, data: dict[str, Any], *, require_filename: bool) -> tuple[Path, Path, Path]:
        root = self._root_for(data)
        folder = str(data.get("folder") or "")
        if folder in {"agent", "shared"}:
            root.mkdir(parents=True, exist_ok=True)
        if not root.exists():
            raise WorkspaceError("not_found", "Workspace root does not exist")

        rel_dir = self._safe_rel_path(data.get("path") or "/")
        filename = data.get("filename")
        if require_filename:
            if not isinstance(filename, str) or not filename.strip():
                raise WorkspaceError("invalid_request", "filename is required")
            if "/" in filename or "\\" in filename:
                raise WorkspaceError("invalid_path", "filename must not contain path separators")
            if self._is_hidden_or_secret(filename):
                raise WorkspaceError("not_allowed", "This filename is not allowed")
            rel_target = rel_dir / filename
        else:
            rel_target = rel_dir

        target = (root / rel_target).resolve()
        if not self._is_within(target, root):
            raise WorkspaceError("invalid_path", "Path escapes workspace root")
        if target.exists() and target.is_symlink():
            raise WorkspaceError("not_allowed", "Symlinks are not supported")
        if (
            folder == "agent"
            and root in self.native_profile_roots.values()
            and require_filename
        ):
            try:
                safe_profile_document_path(
                    root,
                    rel_dir.as_posix() if rel_dir != Path() else "",
                    str(filename),
                )
            except ValueError as exc:
                raise WorkspaceError("not_allowed", str(exc)) from exc
        return root, rel_dir, target

    def _safe_rel_path(self, value: Any) -> Path:
        raw = str(value or "/").replace("\\", "/")
        if raw.startswith("/"):
            raw = raw[1:]
        parts = []
        for part in raw.split("/"):
            if not part or part == ".":
                continue
            if part == "..":
                raise WorkspaceError("invalid_path", "Parent path segments are not allowed")
            if self._is_hidden_or_secret(part):
                raise WorkspaceError("not_allowed", "Hidden or secret paths are not allowed")
            parts.append(part)
        return Path(*parts) if parts else Path()

    def _display_path(self, rel_path: Path) -> str:
        raw = rel_path.as_posix()
        return "/" if raw == "." else f"/{raw}".rstrip("/")

    def _is_readonly_folder(self, folder: str) -> bool:
        return folder == "sessions"

    def _is_within(self, path: Path, root: Path) -> bool:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False

    def _is_hidden_or_secret(self, name: str) -> bool:
        lowered = name.lower()
        if name.startswith("."):
            return True
        return lowered in {
            ".env",
            "auth.json",
            "config.yaml",
            "state.db",
            "state.db-shm",
            "state.db-wal",
            "credentials.json",
            "token.json",
        }


class SnapshotStore:
    def __init__(self, base_dir: Path):
        self._base_dir = base_dir
        self._base_dir.mkdir(parents=True, exist_ok=True)

    def _path_for(self, runtime_session_id: str) -> Path:
        safe_name = "".join(
            ch if ch.isalnum() or ch in {"-", "_", "."} else "_"
            for ch in runtime_session_id
        )
        return self._base_dir / f"{safe_name}.json"

    def load(self, runtime_session_id: str) -> list[dict[str, Any]]:
        path = self._path_for(runtime_session_id)
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            logger.warning("failed to read snapshot %s", path, exc_info=True)
            return []
        return data if isinstance(data, list) else []

    def save(self, runtime_session_id: str, messages: list[dict[str, Any]]) -> None:
        path = self._path_for(runtime_session_id)
        tmp_path = path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(messages, ensure_ascii=True, indent=2), encoding="utf-8")
        tmp_path.replace(path)

    def delete(self, runtime_session_id: str) -> bool:
        path = self._path_for(runtime_session_id)
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return False


class DispatchStateStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._records: dict[str, dict[str, Any]] = self._load()

    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except Exception:
            logger.warning("failed to load ClawChat dispatch state ledger", exc_info=True)
            return {}
        if not isinstance(raw, dict):
            return {}
        records = raw.get("dispatches") if isinstance(raw.get("dispatches"), dict) else raw
        return {
            str(dispatch_id): record
            for dispatch_id, record in records.items()
            if isinstance(record, dict)
        }

    def _persist_locked(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"dispatches": self._records}
            tmp = self._path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True), encoding="utf-8")
            tmp.replace(self._path)
        except Exception:
            logger.warning("failed to persist ClawChat dispatch state ledger", exc_info=True)

    def record_start(
        self,
        dispatch_id: str,
        runtime_run_id: str,
        external_agent_id: str,
        *,
        source: str,
    ) -> None:
        if not dispatch_id:
            return
        now = _now_iso()
        with self._lock:
            previous = self._records.get(dispatch_id) or {}
            self._records[dispatch_id] = {
                **previous,
                "dispatchId": dispatch_id,
                "runtimeRunId": runtime_run_id or previous.get("runtimeRunId") or dispatch_id,
                "externalAgentId": external_agent_id,
                "state": "running",
                "source": source,
                "startedAt": previous.get("startedAt") or now,
                "updatedAt": now,
            }
            self._persist_locked()

    def record_terminal(
        self,
        dispatch_id: str,
        runtime_run_id: str,
        external_agent_id: str,
        terminal_type: str,
    ) -> None:
        if not dispatch_id:
            return
        state = {
            "run.completed": "completed",
            "run.failed": "failed",
            "run.cancelled": "cancelled",
        }.get(terminal_type, "terminal")
        now = _now_iso()
        with self._lock:
            previous = self._records.get(dispatch_id) or {}
            self._records[dispatch_id] = {
                **previous,
                "dispatchId": dispatch_id,
                "runtimeRunId": runtime_run_id or previous.get("runtimeRunId") or dispatch_id,
                "externalAgentId": external_agent_id,
                "state": state,
                "terminalType": terminal_type,
                "terminalAt": previous.get("terminalAt") or now,
                "updatedAt": now,
            }
            self._persist_locked()

    def record_skipped_terminal(
        self,
        dispatch_id: str,
        runtime_run_id: str,
        external_agent_id: str,
        *,
        reason: str,
    ) -> None:
        if not dispatch_id:
            return
        now = _now_iso()
        with self._lock:
            previous = self._records.get(dispatch_id) or {}
            self._records[dispatch_id] = {
                **previous,
                "dispatchId": dispatch_id,
                "runtimeRunId": runtime_run_id or previous.get("runtimeRunId") or dispatch_id,
                "externalAgentId": external_agent_id,
                "state": "skipped_terminal",
                "skipReason": reason,
                "updatedAt": now,
            }
            self._persist_locked()

    def dedupe_reason(self, dispatch_id: str, runtime_run_id: str | None = None) -> str | None:
        with self._lock:
            record = self._records.get(dispatch_id)
            if not record and runtime_run_id:
                record = next(
                    (
                        item for item in self._records.values()
                        if str(item.get("runtimeRunId") or "") == runtime_run_id
                    ),
                    None,
                )
            if not record:
                return None
            state = str(record.get("state") or "")
        if state == "running":
            return "already_running_or_previously_started"
        if state in TERMINAL_DISPATCH_STATE:
            return f"local_terminal_{state}"
        return f"local_state_{state or 'recorded'}"


class MarketplaceSkillInstallError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        skipped_files: list[str] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.skipped_files = skipped_files or []


class MarketplaceSkillInstaller:
    MANIFEST_FILENAME = ".clawchat-marketplace-manifest.json"
    GENERATED_BY = "clawchat-marketplace"
    SAFE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
    SECRET_PATTERNS = [
        re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
        re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
        re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
        re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}\b"),
        re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
        re.compile(r"\bwhsec_[A-Za-z0-9]{20,}\b"),
        re.compile(r"\bsk-[A-Za-z0-9]{24,}\b"),
        re.compile(r"(?i)\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|webhook[_-]?secret|client[_-]?secret)\b\s*[:=]\s*[\"']?[A-Za-z0-9_./+=:-]{20,}"),
    ]

    def __init__(self) -> None:
        self.hermes_home = get_hermes_home()
        self.clawchat_home = self.hermes_home / "clawchat"
        self.native_profile_roots: dict[str, Path] = {}

    def install(self, payload: dict[str, Any]) -> dict[str, Any]:
        request_id = str(payload.get("requestId") or "")
        agent_id = str(payload.get("agentId") or "")
        external_agent_id = str(payload.get("externalAgentId") or "")
        app_slug = str(payload.get("appSlug") or "")
        installed_files: list[str] = []
        skipped_files: list[str] = []
        try:
            agent_id = self._require_safe_name(agent_id, "agentId")
            external_agent_id = self._require_safe_name(external_agent_id or agent_id, "externalAgentId")
            app_slug = self._require_safe_name(app_slug, "appSlug")
            skill_name = self._require_safe_name(str(payload.get("skillName") or ""), "skillName")
            self._validate_request_shape(payload, skill_name)
            self._reject_secrets(payload)

            root = self._agent_workspace_root(external_agent_id)
            target_root = self._resolve_target_root(root, skill_name, payload.get("targetRoot"))
            files = self._validated_files(payload, target_root)
            manifest = self._load_manifest(target_root)
            skipped_files = self._check_overwrite_policy(payload, files, manifest)
            if skipped_files:
                raise MarketplaceSkillInstallError(
                    "unmanaged_file_conflict",
                    "Refusing to overwrite existing files that are not managed by this marketplace skill pack.",
                    skipped_files=skipped_files,
                )

            target_root.mkdir(parents=True, exist_ok=True)
            for rel_path, content, _expected_hash, target in files:
                target.parent.mkdir(parents=True, exist_ok=True)
                tmp_path = target.with_name(f".{target.name}.tmp")
                tmp_path.write_text(content, encoding="utf-8")
                tmp_path.replace(target)
                installed_files.append(rel_path)

            self._save_manifest(target_root, payload, installed_files, files)
            self._reload_skills(target_root.parent)
            return self._result(
                request_id,
                "installed",
                agent_id,
                external_agent_id,
                app_slug,
                installed_files=installed_files,
                skipped_files=skipped_files,
            )
        except MarketplaceSkillInstallError as exc:
            logger.warning("Hermes marketplace skill install rejected: %s", exc.message)
            return self._result(
                request_id,
                "rejected",
                agent_id,
                external_agent_id or agent_id,
                app_slug,
                installed_files=installed_files,
                skipped_files=exc.skipped_files or skipped_files,
                error={"code": exc.code, "message": exc.message},
            )
        except Exception as exc:
            logger.exception("Hermes marketplace skill install failed")
            return self._result(
                request_id,
                "failed",
                agent_id,
                external_agent_id or agent_id,
                app_slug,
                installed_files=installed_files,
                skipped_files=skipped_files,
                error={"code": "install_failed", "message": str(exc)},
            )

    def _validate_request_shape(self, payload: dict[str, Any], skill_name: str) -> None:
        if payload.get("runtimeFormat") != "hermes":
            raise MarketplaceSkillInstallError("invalid_runtime_format", "runtimeFormat must be hermes")
        if payload.get("type") != "marketplace.installHermesSkill":
            raise MarketplaceSkillInstallError("invalid_request_type", "Unsupported marketplace request type")
        if payload.get("workspaceId") is not None and not str(payload.get("workspaceId")).strip():
            raise MarketplaceSkillInstallError("invalid_workspace", "workspaceId is required")
        policy = payload.get("policy")
        if not isinstance(policy, dict):
            raise MarketplaceSkillInstallError("invalid_policy", "policy is required")
        if policy.get("overwrite") != "managed_files_only":
            raise MarketplaceSkillInstallError("unsupported_overwrite_policy", "Only managed_files_only overwrite is supported")
        if policy.get("removeStaleManagedFiles") is not False:
            raise MarketplaceSkillInstallError("unsupported_stale_policy", "removeStaleManagedFiles must be false")
        metadata = payload.get("metadata")
        if not isinstance(metadata, dict) or metadata.get("generatedBy") != self.GENERATED_BY:
            raise MarketplaceSkillInstallError("invalid_metadata", "metadata.generatedBy must be clawchat-marketplace")
        target_root = str(payload.get("targetRoot") or "").replace("\\", "/").strip("/")
        if target_root != f"skills/{skill_name}":
            raise MarketplaceSkillInstallError(
                "invalid_target_root",
                f"targetRoot must be skills/{skill_name}",
            )
        if not isinstance(payload.get("files"), list) or not payload.get("files"):
            raise MarketplaceSkillInstallError("invalid_files", "files must be a non-empty array")

    def _validated_files(
        self,
        payload: dict[str, Any],
        target_root: Path,
    ) -> list[tuple[str, str, str, Path]]:
        files: list[tuple[str, str, str, Path]] = []
        seen: set[str] = set()
        for item in payload.get("files") or []:
            if not isinstance(item, dict):
                raise MarketplaceSkillInstallError("invalid_file", "Each file entry must be an object")
            rel_path = self._safe_relative_path(item.get("relativePath"))
            if rel_path in seen:
                raise MarketplaceSkillInstallError("duplicate_file", f"Duplicate file path: {rel_path}")
            seen.add(rel_path)
            content = item.get("content")
            expected_hash = str(item.get("sha256") or "").strip().lower()
            if not isinstance(content, str):
                raise MarketplaceSkillInstallError("invalid_file_content", f"{rel_path} content must be a string")
            if len(content.encode("utf-8")) > MAX_MARKETPLACE_SKILL_FILE_BYTES:
                raise MarketplaceSkillInstallError("file_too_large", f"{rel_path} exceeds the marketplace skill file size limit")
            if not re.fullmatch(r"[a-f0-9]{64}", expected_hash):
                raise MarketplaceSkillInstallError("invalid_hash", f"{rel_path} sha256 must be a lowercase hex digest")
            computed_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
            if computed_hash != expected_hash:
                raise MarketplaceSkillInstallError("hash_mismatch", f"{rel_path} sha256 does not match content")
            target = (target_root / rel_path).resolve()
            if not self._is_within(target, target_root):
                raise MarketplaceSkillInstallError("invalid_path", f"{rel_path} escapes targetRoot")
            files.append((rel_path, content, expected_hash, target))

        if "SKILL.md" not in seen:
            raise MarketplaceSkillInstallError("missing_skill_file", "SKILL.md is required")
        return files

    def _check_overwrite_policy(
        self,
        payload: dict[str, Any],
        files: list[tuple[str, str, str, Path]],
        manifest: dict[str, Any] | None,
    ) -> list[str]:
        managed_files = set()
        if manifest and manifest.get("generatedBy") == self.GENERATED_BY:
            if manifest.get("skillName") == payload.get("skillName") and manifest.get("appSlug") == payload.get("appSlug"):
                managed_files = {
                    str(item.get("relativePath") or "")
                    for item in manifest.get("files", [])
                    if isinstance(item, dict)
                }
        skipped_files: list[str] = []
        for rel_path, _content, _expected_hash, target in files:
            if target.exists() and rel_path not in managed_files:
                skipped_files.append(rel_path)
        return skipped_files

    def _safe_relative_path(self, value: Any) -> str:
        raw = str(value or "").replace("\\", "/")
        if not raw or raw.startswith("/") or Path(raw).expanduser().is_absolute() or raw.startswith("~"):
            raise MarketplaceSkillInstallError("invalid_path", "File relativePath must be relative")
        parts = []
        for part in raw.split("/"):
            if not part or part == ".":
                continue
            if part == "..":
                raise MarketplaceSkillInstallError("invalid_path", "Parent path segments are not allowed")
            if part.startswith("."):
                raise MarketplaceSkillInstallError("invalid_path", "Hidden file paths are not allowed")
            parts.append(part)
        if not parts:
            raise MarketplaceSkillInstallError("invalid_path", "File relativePath is empty")
        if parts[0] == "skills":
            raise MarketplaceSkillInstallError("invalid_path", "File relativePath must be relative to targetRoot")
        return Path(*parts).as_posix()

    def _resolve_target_root(self, workspace_root: Path, skill_name: str, target_root: Any) -> Path:
        raw = str(target_root or "").replace("\\", "/")
        if raw.startswith("/") or Path(raw).expanduser().is_absolute() or raw.startswith("~"):
            raise MarketplaceSkillInstallError("invalid_target_root", "targetRoot must be relative")
        safe_root = self._safe_path(raw)
        expected = Path("skills") / skill_name
        if safe_root != expected:
            raise MarketplaceSkillInstallError("invalid_target_root", f"targetRoot must be {expected.as_posix()}")
        target = (workspace_root / safe_root).resolve()
        if not self._is_within(target, workspace_root):
            raise MarketplaceSkillInstallError("invalid_target_root", "targetRoot escapes agent workspace")
        return target

    def _safe_path(self, value: str) -> Path:
        parts = []
        for part in value.split("/"):
            if not part or part == ".":
                continue
            if part == "..":
                raise MarketplaceSkillInstallError("invalid_path", "Parent path segments are not allowed")
            parts.append(part)
        return Path(*parts) if parts else Path()

    def _agent_workspace_root(self, agent_id: str) -> Path:
        native = self.native_profile_roots.get(agent_id)
        if native:
            return native
        root = (self.clawchat_home / "agents" / _safe_segment(agent_id) / "workspace").resolve()
        root.mkdir(parents=True, exist_ok=True)
        return root

    def _load_manifest(self, target_root: Path) -> dict[str, Any] | None:
        path = target_root / self.MANIFEST_FILENAME
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
        except Exception:
            logger.warning("failed to read marketplace skill manifest %s", path, exc_info=True)
            return None

    def _save_manifest(
        self,
        target_root: Path,
        payload: dict[str, Any],
        installed_files: list[str],
        files: list[tuple[str, str, str, Path]],
    ) -> None:
        hashes = {rel_path: expected_hash for rel_path, _content, expected_hash, _target in files}
        manifest = {
            "generatedBy": self.GENERATED_BY,
            "installedAt": _now_iso(),
            "workspaceId": payload.get("workspaceId"),
            "agentId": payload.get("agentId"),
            "externalAgentId": payload.get("externalAgentId") or payload.get("agentId"),
            "appSlug": payload.get("appSlug"),
            "marketplaceInstallId": payload.get("marketplaceInstallId"),
            "skillName": payload.get("skillName"),
            "targetRoot": payload.get("targetRoot"),
            "metadata": payload.get("metadata") or {},
            "files": [
                {"relativePath": rel_path, "sha256": hashes[rel_path]}
                for rel_path in installed_files
            ],
        }
        path = target_root / self.MANIFEST_FILENAME
        tmp_path = path.with_name(f".{path.name}.tmp")
        tmp_path.write_text(json.dumps(manifest, ensure_ascii=True, indent=2), encoding="utf-8")
        tmp_path.replace(path)

    def _reject_secrets(self, payload: dict[str, Any]) -> None:
        metadata_text = json.dumps(payload.get("metadata") or {}, ensure_ascii=True)
        connection_text = json.dumps(payload.get("connection") or {}, ensure_ascii=True)
        self._reject_secret_text(metadata_text, "metadata")
        self._reject_secret_text(connection_text, "connection")
        for item in payload.get("files") or []:
            if isinstance(item, dict):
                self._reject_secret_text(str(item.get("content") or ""), str(item.get("relativePath") or "file"))

    def _reject_secret_text(self, text: str, label: str) -> None:
        for pattern in self.SECRET_PATTERNS:
            if pattern.search(text):
                raise MarketplaceSkillInstallError(
                    "secret_detected",
                    f"Potential raw secret detected in {label}; marketplace skill packs must not contain secrets.",
                )

    def _reload_skills(self, skills_root: Path) -> None:
        old_prepend = os.environ.get("HERMES_PREPEND_SKILLS_DIRS")
        try:
            roots = [str(skills_root)]
            if old_prepend:
                roots.extend(part for part in old_prepend.split(os.pathsep) if part)
            os.environ["HERMES_PREPEND_SKILLS_DIRS"] = os.pathsep.join(roots)
            try:
                from agent.skill_commands import reload_skills

                reload_skills()
            except Exception:
                logger.debug("Hermes skill reload failed after marketplace install", exc_info=True)
        finally:
            if old_prepend is None:
                os.environ.pop("HERMES_PREPEND_SKILLS_DIRS", None)
            else:
                os.environ["HERMES_PREPEND_SKILLS_DIRS"] = old_prepend

    def _require_safe_name(self, value: str, field: str) -> str:
        value = value.strip()
        if field == "externalAgentId" and profile_name_from_external_id(value):
            return value
        if not value or not self.SAFE_NAME_PATTERN.fullmatch(value):
            raise MarketplaceSkillInstallError("invalid_identifier", f"{field} is not a safe identifier")
        return value

    def _is_within(self, path: Path, root: Path) -> bool:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False

    def _result(
        self,
        request_id: str,
        status: str,
        agent_id: str,
        external_agent_id: str,
        app_slug: str,
        *,
        installed_files: list[str],
        skipped_files: list[str] | None = None,
        error: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "requestId": request_id,
            "status": status,
            "agentId": agent_id,
            "externalAgentId": external_agent_id,
            "appSlug": app_slug,
            "installedFiles": installed_files,
            "bridgeCapabilities": BRIDGE_CAPABILITIES,
        }
        if skipped_files:
            result["skippedFiles"] = skipped_files
        if error:
            result["error"] = error
        return result


class MarketplaceLocalRepoDocsReader:
    DEFAULT_DOCS_SOURCE_PATH = ".clawchat/"
    DEFAULT_INCLUDE_GLOBS = [
        ".clawchat/app_manifest.json",
        ".clawchat/roles_manifest.json",
        ".clawchat/clawchat.config.json",
        ".clawchat/api/openapi.json",
        ".clawchat/api/endpoints.md",
        ".clawchat/agent-docs-source/*.md",
        ".clawchat/agent-docs-source/**/*.md",
        ".clawchat/worker-docs-source/*.md",
        ".clawchat/worker-docs-source/**/*.md",
        ".clawchat/auditor-docs-source/*.md",
        ".clawchat/auditor-docs-source/**/*.md",
        ".clawchat/manager-docs-source/*.md",
        ".clawchat/manager-docs-source/**/*.md",
    ]
    MARKDOWN_DOC_ROOTS = (
        "agent-docs-source",
        "worker-docs-source",
        "auditor-docs-source",
        "manager-docs-source",
    )
    STATIC_DOC_FILES = [
        "app_manifest.json",
        "roles_manifest.json",
        "clawchat.config.json",
        "api/openapi.json",
        "api/endpoints.md",
    ]
    SECRET_KEY_RE = re.compile(
        r"(?:^|[_-])(api[_-]?key|access[_-]?token|refresh[_-]?token|id[_-]?token|token|secret|password|passwd|private[_-]?key|client[_-]?secret|webhook[_-]?secret)(?:$|[_-])",
        re.IGNORECASE,
    )
    PRIVATE_KEY_BLOCK_RE = re.compile(
        r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY-----"
    )
    BEARER_RE = re.compile(r"\b(Bearer\s+)([A-Za-z0-9._~+/=-]{16,})\b")
    ASSIGNMENT_SECRET_RE = re.compile(
        r"\b([A-Za-z0-9_.-]*(?:API[_-]?KEY|ACCESS[_-]?TOKEN|REFRESH[_-]?TOKEN|ID[_-]?TOKEN|TOKEN|SECRET|PASSWORD|PASSWD|PRIVATE[_-]?KEY|CLIENT[_-]?SECRET|WEBHOOK[_-]?SECRET)[A-Za-z0-9_.-]*)\s*([:=])\s*(['\"]?)([^<>'\"\s,}\]]{4,})(\3)",
        re.IGNORECASE,
    )

    def read(self, payload: dict[str, Any]) -> dict[str, Any]:
        request_id = str(payload.get("requestId") or "")
        repo_path = payload.get("repoPath")
        docs_source_path = str(payload.get("docsSourcePath") or self.DEFAULT_DOCS_SOURCE_PATH)
        try:
            repo_root = self._resolve_repo_root(repo_path)
            docs_source_path, docs_root = self._resolve_docs_root(repo_root, docs_source_path)
            git_commit = self._git_value(repo_root, ["rev-parse", "HEAD"])
            dirty_state = self._git_dirty_state(repo_root)
            if not docs_root.exists():
                return self._result(
                    request_id,
                    "not_found",
                    str(repo_root),
                    docs_source_path,
                    files=[],
                    missing_files=list(self.STATIC_DOC_FILES),
                    errors=[f"Docs source path not found: {docs_source_path}"],
                    git_commit=git_commit,
                    dirty_state=dirty_state,
                )
            docs_root = docs_root.resolve(strict=True)
            if not self._is_within(docs_root, repo_root):
                raise ValueError(f"docsSourcePath resolves outside repoPath: {docs_source_path}")

            include_globs = payload.get("includeGlobs")
            if not isinstance(include_globs, list) or not include_globs:
                include_globs = list(self.DEFAULT_INCLUDE_GLOBS)
            include_globs = [str(item) for item in include_globs if str(item).strip()]

            candidates = set(self._requested_static_files(include_globs, docs_source_path))
            if self._wants_markdown_docs(include_globs, docs_source_path):
                candidates.update(self._list_markdown_files_for_includes(docs_root, include_globs, docs_source_path))

            files: list[dict[str, Any]] = []
            missing_files: list[str] = []
            errors: list[str] = []
            for rel_path in sorted(candidates):
                item, error, redacted = self._read_allowed_file(repo_root, docs_root, rel_path)
                if error:
                    errors.append(error)
                    continue
                if item is None:
                    if rel_path in self.STATIC_DOC_FILES:
                        missing_files.append(rel_path)
                    continue
                if redacted:
                    errors.append(f"Redacted secret-looking value(s) in {rel_path}")
                files.append(item)

            return self._result(
                request_id,
                "ok" if files else "not_found",
                str(repo_root),
                docs_source_path,
                files=files,
                missing_files=missing_files,
                errors=errors,
                git_commit=git_commit,
                dirty_state=dirty_state,
            )
        except Exception as exc:
            return self._result(
                request_id,
                "failed",
                str(repo_path) if isinstance(repo_path, str) else None,
                docs_source_path,
                files=[],
                missing_files=[],
                errors=[str(exc)],
                git_commit=None,
                dirty_state=None,
            )

    def _resolve_repo_root(self, value: Any) -> Path:
        raw = str(value or "").strip()
        if not raw:
            raise ValueError("repoPath is required")
        root = Path(raw).expanduser().resolve(strict=True)
        if not root.is_dir():
            raise ValueError(f"repoPath is not a directory: {raw}")
        return root

    def _resolve_docs_root(self, repo_root: Path, docs_source_path: str) -> tuple[str, Path]:
        raw = docs_source_path.strip() or self.DEFAULT_DOCS_SOURCE_PATH
        candidate = Path(raw).expanduser()
        if candidate.is_absolute():
            raise ValueError("docsSourcePath must be relative to repoPath")
        parts = []
        for part in raw.replace("\\", "/").split("/"):
            if not part or part == ".":
                continue
            if part == "..":
                raise ValueError(f"Invalid docsSourcePath: {raw}")
            parts.append(part)
        if not parts:
            raise ValueError(f"Invalid docsSourcePath: {raw}")
        normalized = Path(*parts).as_posix()
        docs_root = (repo_root / normalized).resolve()
        if not self._is_within(docs_root, repo_root):
            raise ValueError(f"docsSourcePath escapes repoPath: {raw}")

        # Some ClawChat builds send the config file path as docsSourcePath.
        # Treat that as a request for the containing .clawchat docs root.
        if docs_root.is_file():
            if normalized in {f".clawchat/{item}" for item in self.STATIC_DOC_FILES}:
                return ".clawchat/", (repo_root / ".clawchat").resolve()
            raise ValueError(f"docsSourcePath must be a ClawChat docs directory or known docs file: {raw}")
        return f"{normalized}/", docs_root

    def _relative_from_include(self, glob: str, docs_source_path: str) -> str | None:
        normalized = Path(str(glob).replace("\\", "/")).as_posix().lstrip("/")
        docs_prefix = docs_source_path.rstrip("/")
        if normalized == docs_prefix:
            return None
        if normalized.startswith(f"{docs_prefix}/"):
            return normalized[len(docs_prefix) + 1 :]
        if normalized.startswith(".clawchat/"):
            return normalized[len(".clawchat/") :]
        return normalized

    def _requested_static_files(self, include_globs: list[str], docs_source_path: str) -> list[str]:
        requested: list[str] = []
        for item in include_globs:
            rel_path = self._relative_from_include(item, docs_source_path)
            if rel_path in self.STATIC_DOC_FILES and rel_path not in requested:
                requested.append(rel_path)
        return requested

    def _wants_markdown_docs(self, include_globs: list[str], docs_source_path: str) -> bool:
        for item in include_globs:
            rel_path = self._relative_from_include(item, docs_source_path)
            if not rel_path:
                continue
            for root in self.MARKDOWN_DOC_ROOTS:
                if rel_path in {f"{root}/*.md", f"{root}/**/*.md", f"{root}/**.md"}:
                    return True
                if rel_path.startswith(f"{root}/") and rel_path.endswith(".md"):
                    return True
        return False

    def _requested_markdown_roots(self, include_globs: list[str], docs_source_path: str) -> list[str]:
        requested: list[str] = []
        for item in include_globs:
            rel_path = self._relative_from_include(item, docs_source_path)
            if not rel_path:
                continue
            for root in self.MARKDOWN_DOC_ROOTS:
                if rel_path in {f"{root}/*.md", f"{root}/**/*.md", f"{root}/**.md"} or (
                    rel_path.startswith(f"{root}/") and rel_path.endswith(".md")
                ):
                    if root not in requested:
                        requested.append(root)
        return requested or list(self.MARKDOWN_DOC_ROOTS)

    def _is_allowed_markdown_file(self, rel_path: str) -> bool:
        return any(
            rel_path == root or rel_path.startswith(f"{root}/")
            for root in self.MARKDOWN_DOC_ROOTS
        ) and rel_path.endswith(".md")

    def _is_blocked_doc_path(self, path: Path) -> bool:
        blocked_parts = {"node_modules", ".git", "dist", "build", ".cache", ".next", "cache", "logs"}
        return any(part in blocked_parts for part in path.parts)

    def _requested_explicit_markdown_files(self, include_globs: list[str], docs_source_path: str) -> list[str]:
        requested: list[str] = []
        for item in include_globs:
            rel_path = self._relative_from_include(item, docs_source_path)
            if not rel_path or any(char in rel_path for char in "*?["):
                continue
            if self._is_allowed_markdown_file(rel_path) and rel_path not in requested:
                requested.append(rel_path)
        return requested

    def _include_glob_needs_recursive_markdown(self, rel_path: str, root: str) -> bool:
        return rel_path in {f"{root}/**/*.md", f"{root}/**.md"} or rel_path.startswith(f"{root}/**/")

    def _include_glob_needs_direct_markdown(self, rel_path: str, root: str) -> bool:
        return rel_path == f"{root}/*.md"

    def _include_glob_matches_markdown(self, include_globs: list[str], docs_source_path: str, rel_path: str) -> bool:
        for item in include_globs:
            include_rel = self._relative_from_include(item, docs_source_path)
            if not include_rel:
                continue
            for root in self.MARKDOWN_DOC_ROOTS:
                if rel_path == include_rel:
                    return True
                if self._include_glob_needs_recursive_markdown(include_rel, root) and rel_path.startswith(f"{root}/"):
                    return True
                if self._include_glob_needs_direct_markdown(include_rel, root):
                    child = rel_path[len(root) + 1 :] if rel_path.startswith(f"{root}/") else ""
                    if child and "/" not in child:
                        return True
        return False

    def _list_markdown_files_for_includes(
        self,
        docs_root: Path,
        include_globs: list[str],
        docs_source_path: str,
    ) -> list[str]:
        roots = self._requested_markdown_roots(include_globs, docs_source_path)
        files: list[str] = []
        for root in roots:
            start = docs_root / root
            if not start.exists():
                continue
            for path in sorted(start.rglob("*.md")):
                if self._is_blocked_doc_path(path):
                    continue
                try:
                    resolved = path.resolve(strict=True)
                except OSError:
                    continue
                if not resolved.is_file() or not self._is_within(resolved, docs_root):
                    continue
                rel_path = resolved.relative_to(docs_root).as_posix()
                if self._include_glob_matches_markdown(include_globs, docs_source_path, rel_path):
                    files.append(rel_path)
        return files

    def _list_markdown_files(self, docs_root: Path) -> list[str]:
        files: list[str] = []
        for root in self.MARKDOWN_DOC_ROOTS:
            start = docs_root / root
            if not start.exists():
                continue
            for path in sorted(start.rglob("*.md")):
                if self._is_blocked_doc_path(path):
                    continue
                try:
                    resolved = path.resolve(strict=True)
                except OSError:
                    continue
                if resolved.is_file() and self._is_within(resolved, docs_root):
                    files.append(resolved.relative_to(docs_root).as_posix())
        return files

    def _legacy_wants_agent_markdown_docs(self, include_globs: list[str], docs_source_path: str) -> bool:
        for item in include_globs:
            rel_path = self._relative_from_include(item, docs_source_path)
            for root in self.MARKDOWN_DOC_ROOTS:
                if rel_path in {f"{root}/**/*.md", f"{root}/**.md"}:
                    return True
        return False

    def _read_allowed_file(
        self,
        repo_root: Path,
        docs_root: Path,
        rel_path: str,
    ) -> tuple[dict[str, Any] | None, str | None, bool]:
        safe_rel = self._safe_relative_path(rel_path)
        file_path = (docs_root / safe_rel).resolve()
        if not self._is_within(file_path, docs_root) or not self._is_within(file_path, repo_root):
            return None, f"Blocked path escape for {rel_path}", False
        if not file_path.exists():
            return None, None, False
        real = file_path.resolve(strict=True)
        if not self._is_within(real, docs_root) or not self._is_within(real, repo_root):
            return None, f"Blocked symlink escape for {rel_path}", False
        if not real.is_file():
            return None, f"Not a regular file: {rel_path}", False
        size = real.stat().st_size
        if size > 1_000_000:
            return None, f"File too large to return safely: {rel_path}", False
        content = real.read_text(encoding="utf-8")
        content, redacted = self._redact_secret_looking_values(content, safe_rel.as_posix())
        return {
            "relativePath": safe_rel.as_posix(),
            "content": content,
            "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            "sizeBytes": len(content.encode("utf-8")),
        }, None, redacted

    def _safe_relative_path(self, value: str) -> Path:
        raw = str(value or "").replace("\\", "/")
        if not raw or raw.startswith("/") or Path(raw).expanduser().is_absolute() or "\0" in raw:
            raise ValueError(f"Blocked unsafe relative path: {value}")
        parts = []
        for part in raw.split("/"):
            if not part or part == ".":
                continue
            if part == "..":
                raise ValueError(f"Blocked unsafe relative path: {value}")
            parts.append(part)
        if not parts:
            raise ValueError(f"Blocked unsafe relative path: {value}")
        return Path(*parts)

    def _redact_secret_looking_values(self, content: str, rel_path: str) -> tuple[str, bool]:
        redacted = False
        next_content = content
        if rel_path.endswith(".json"):
            try:
                parsed = json.loads(content)
                parsed, json_redacted = self._redact_json_secrets(parsed)
                if json_redacted:
                    redacted = True
                    next_content = json.dumps(parsed, ensure_ascii=True, indent=2) + "\n"
            except json.JSONDecodeError:
                pass
        next_content, count = self.PRIVATE_KEY_BLOCK_RE.subn("[REDACTED_PRIVATE_KEY]", next_content)
        redacted = redacted or count > 0
        next_content, count = self.BEARER_RE.subn(r"\1[REDACTED_BEARER_TOKEN]", next_content)
        redacted = redacted or count > 0
        next_content, count = self.ASSIGNMENT_SECRET_RE.subn(
            lambda match: f"{match.group(1)}{match.group(2)}{match.group(3)}[REDACTED_SECRET_VALUE]{match.group(5)}",
            next_content,
        )
        redacted = redacted or count > 0
        return next_content, redacted

    def _redact_json_secrets(self, value: Any, parent_key: str = "") -> tuple[Any, bool]:
        if isinstance(value, list):
            redacted = False
            next_items = []
            for item in value:
                next_item, item_redacted = self._redact_json_secrets(item, parent_key)
                redacted = redacted or item_redacted
                next_items.append(next_item)
            return next_items, redacted
        if isinstance(value, dict):
            redacted = False
            next_obj: dict[str, Any] = {}
            for key, child in value.items():
                if self.SECRET_KEY_RE.search(str(key)) and isinstance(child, str) and child.strip():
                    next_obj[str(key)] = "[REDACTED_SECRET_VALUE]"
                    redacted = True
                    continue
                next_child, child_redacted = self._redact_json_secrets(child, str(key))
                next_obj[str(key)] = next_child
                redacted = redacted or child_redacted
            return next_obj, redacted
        if self.SECRET_KEY_RE.search(parent_key) and isinstance(value, str) and value.strip():
            return "[REDACTED_SECRET_VALUE]", True
        return value, False

    def _git_value(self, repo_root: Path, args: list[str]) -> str | None:
        try:
            result = subprocess.run(
                ["git", *args],
                cwd=repo_root,
                text=True,
                capture_output=True,
                timeout=5,
                check=False,
            )
        except Exception:
            return None
        if result.returncode != 0:
            return None
        return result.stdout.strip() or None

    def _git_dirty_state(self, repo_root: Path) -> str | None:
        porcelain = self._git_value(repo_root, ["status", "--short"])
        if porcelain is None:
            return None
        return "dirty" if porcelain else "clean"

    def _is_within(self, path: Path, root: Path) -> bool:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False

    def _result(
        self,
        request_id: str,
        status: str,
        repo_path: str | None,
        docs_source_path: str,
        *,
        files: list[dict[str, Any]],
        missing_files: list[str],
        errors: list[str],
        git_commit: str | None,
        dirty_state: str | None,
    ) -> dict[str, Any]:
        return {
            "requestId": request_id,
            "status": status,
            "repoPath": repo_path,
            "docsSourcePath": docs_source_path,
            "files": files,
            "missingFiles": missing_files,
            "errors": errors,
            "gitCommit": git_commit,
            "dirtyState": dirty_state,
        }


class MarketplaceLocalRepoDocsWriter:
    MAX_WRITE_BYTES = 1_000_000

    def __init__(self) -> None:
        self.reader = MarketplaceLocalRepoDocsReader()

    def apply(self, payload: dict[str, Any]) -> dict[str, Any]:
        request_id = str(payload.get("requestId") or "")
        repo_path = payload.get("repoPath")
        docs_source_path = str(payload.get("docsSourcePath") or MarketplaceLocalRepoDocsReader.DEFAULT_DOCS_SOURCE_PATH)
        files_written: list[dict[str, Any]] = []
        files_skipped: list[dict[str, Any]] = []
        errors: list[str] = []
        git_commit_before: str | None = None
        git_commit_after: str | None = None
        git_status_after: str | None = None
        dirty_state_after: str | None = None
        try:
            repo_root = self.reader._resolve_repo_root(repo_path)
            docs_source_path, docs_root = self.reader._resolve_docs_root(repo_root, docs_source_path)
            if docs_source_path != MarketplaceLocalRepoDocsReader.DEFAULT_DOCS_SOURCE_PATH:
                raise ValueError("marketplace.applyLocalRepoDocs only writes under .clawchat/")
            git_commit_before = self.reader._git_value(repo_root, ["rev-parse", "HEAD"])

            if docs_root.exists():
                docs_root = docs_root.resolve(strict=True)
                if not self.reader._is_within(docs_root, repo_root):
                    raise ValueError(f"docsSourcePath resolves outside repoPath: {docs_source_path}")
            else:
                docs_root = repo_root / ".clawchat"

            updates = self._extract_updates(payload)
            if not updates:
                errors.append("No approved documentation updates provided")

            for index, update in enumerate(updates):
                rel_path = self._update_path(update)
                if not rel_path:
                    files_skipped.append({"index": index, "reason": "missing_relative_path"})
                    continue
                try:
                    if not self._is_approved(update):
                        files_skipped.append({"relativePath": rel_path, "reason": "not_approved"})
                        continue
                    content = self._update_content(update)
                    if content is None:
                        files_skipped.append({"relativePath": rel_path, "reason": "missing_content"})
                        continue
                    item = self._write_allowed_file(repo_root, docs_root, rel_path, content)
                    files_written.append(item)
                except Exception as exc:
                    files_skipped.append({"relativePath": rel_path, "reason": "rejected"})
                    errors.append(f"{rel_path}: {exc}")

            git_commit_after = self.reader._git_value(repo_root, ["rev-parse", "HEAD"])
            git_status_after = self.reader._git_value(repo_root, ["status", "--short"])
            dirty_state_after = "dirty" if git_status_after else ("clean" if git_status_after == "" else None)
            return self._result(
                request_id,
                "ok" if files_written and not errors else ("partial" if files_written else "failed"),
                str(repo_root),
                docs_source_path,
                files_written=files_written,
                files_skipped=files_skipped,
                errors=errors,
                git_commit_before=git_commit_before,
                git_commit_after=git_commit_after,
                git_status_after=git_status_after,
                dirty_state_after=dirty_state_after,
            )
        except Exception as exc:
            return self._result(
                request_id,
                "failed",
                str(repo_path) if isinstance(repo_path, str) else None,
                docs_source_path,
                files_written=files_written,
                files_skipped=files_skipped,
                errors=[*errors, str(exc)],
                git_commit_before=git_commit_before,
                git_commit_after=git_commit_after,
                git_status_after=git_status_after,
                dirty_state_after=dirty_state_after,
            )

    def _extract_updates(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        for key in ("files", "patches", "updates", "filePatches", "fileUpdates", "approvedFiles"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        proposal = payload.get("proposal")
        if isinstance(proposal, dict):
            return self._extract_updates(proposal)
        return []

    def _update_path(self, update: dict[str, Any]) -> str:
        for key in ("relativePath", "path", "filePath", "targetPath"):
            value = update.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    def _update_content(self, update: dict[str, Any]) -> str | None:
        for key in ("content", "newContent", "text", "body"):
            value = update.get(key)
            if isinstance(value, str):
                content, _redacted = self.reader._redact_secret_looking_values(value, self._update_path(update))
                return content
        return None

    def _is_approved(self, update: dict[str, Any]) -> bool:
        if update.get("approved") is False or update.get("selected") is False:
            return False
        status = update.get("status")
        if isinstance(status, str) and status.strip().lower() in {"rejected", "skipped", "pending"}:
            return False
        return True

    def _write_allowed_file(
        self,
        repo_root: Path,
        docs_root: Path,
        rel_path: str,
        content: str,
    ) -> dict[str, Any]:
        safe_rel = self.reader._safe_relative_path(self._strip_docs_prefix(rel_path))
        rel_posix = safe_rel.as_posix()
        if not self._is_allowed_doc_file(rel_posix):
            raise ValueError("Path is not an allowed .clawchat documentation file")
        if self.reader._is_blocked_doc_path(safe_rel):
            raise ValueError("Blocked documentation path")
        encoded = content.encode("utf-8")
        if len(encoded) > self.MAX_WRITE_BYTES:
            raise ValueError("File too large to write safely")

        docs_root = docs_root.resolve() if docs_root.exists() else docs_root
        target = docs_root / safe_rel
        self._ensure_parent_dir(repo_root, docs_root, target.parent)
        resolved_parent = target.parent.resolve(strict=True)
        if not self.reader._is_within(resolved_parent, docs_root) or not self.reader._is_within(resolved_parent, repo_root):
            raise ValueError("Blocked parent path escape")
        if target.exists():
            resolved_target = target.resolve(strict=True)
            if not self.reader._is_within(resolved_target, docs_root) or not self.reader._is_within(resolved_target, repo_root):
                raise ValueError("Blocked symlink escape")
            if not resolved_target.is_file():
                raise ValueError("Target is not a regular file")
        else:
            resolved_target = resolved_parent / target.name

        tmp_path = resolved_parent / f".{target.name}.{uuid4().hex}.tmp"
        tmp_path.write_bytes(encoded)
        os.replace(tmp_path, resolved_target)
        return {
            "relativePath": rel_posix,
            "sha256": hashlib.sha256(encoded).hexdigest(),
            "sizeBytes": len(encoded),
        }

    def _strip_docs_prefix(self, rel_path: str) -> str:
        normalized = str(rel_path).replace("\\", "/").strip()
        if normalized.startswith(".clawchat/"):
            return normalized[len(".clawchat/") :]
        return normalized

    def _is_allowed_doc_file(self, rel_path: str) -> bool:
        if rel_path in MarketplaceLocalRepoDocsReader.STATIC_DOC_FILES:
            return True
        return self.reader._is_allowed_markdown_file(rel_path)

    def _ensure_parent_dir(self, repo_root: Path, docs_root: Path, parent: Path) -> None:
        docs_root_parent = docs_root.parent.resolve(strict=True)
        if not self.reader._is_within(docs_root_parent, repo_root):
            raise ValueError("docs root parent escapes repoPath")
        if not docs_root.exists():
            docs_root.mkdir(mode=0o755)
        resolved_docs_root = docs_root.resolve(strict=True)
        if not self.reader._is_within(resolved_docs_root, repo_root):
            raise ValueError("docs root escapes repoPath")
        relative_parent = parent.relative_to(docs_root)
        current = resolved_docs_root
        for part in relative_parent.parts:
            current = current / part
            if current.exists():
                resolved = current.resolve(strict=True)
                if not resolved.is_dir():
                    raise ValueError(f"Parent path component is not a directory: {part}")
                if not self.reader._is_within(resolved, resolved_docs_root) or not self.reader._is_within(resolved, repo_root):
                    raise ValueError("Blocked parent symlink escape")
                current = resolved
            else:
                current.mkdir(mode=0o755)

    def _result(
        self,
        request_id: str,
        status: str,
        repo_path: str | None,
        docs_source_path: str,
        *,
        files_written: list[dict[str, Any]],
        files_skipped: list[dict[str, Any]],
        errors: list[str],
        git_commit_before: str | None,
        git_commit_after: str | None,
        git_status_after: str | None,
        dirty_state_after: str | None,
    ) -> dict[str, Any]:
        return {
            "requestId": request_id,
            "status": status,
            "repoPath": repo_path,
            "docsSourcePath": docs_source_path,
            "filesWritten": files_written,
            "filesSkipped": files_skipped,
            "errors": errors,
            "gitCommitBefore": git_commit_before,
            "gitCommitAfter": git_commit_after,
            "gitStatusAfter": git_status_after,
            "dirtyStateAfter": dirty_state_after,
        }


class MarketplaceLocalAppAgentApiSetup:
    CONTRACT_VERSION = "2026-03-18"
    SAFE_AUTONOMY_MODES = {
        "safe_default",
        "internal_write",
        "supervised_external",
        "dangerously_skip_permissions",
        "custom_policy",
    }

    def setup(self, payload: dict[str, Any]) -> dict[str, Any]:
        request_id = str(payload.get("requestId") or "")
        repo_path = self._pick_string(payload, "repoPath")
        app_url = (self._pick_string(payload, "localAppUrl", "appUrl", "url") or "http://localhost:3052").rstrip("/")
        agent_api_base_url = (
            self._pick_string(payload, "agentApiBaseUrl", "openClawBaseUrl", "openclawBaseUrl")
            or self.derive_agent_api_base_url(app_url)
        ).rstrip("/")
        diagnostics: dict[str, Any] = {
            "settingsUnauthStatus": None,
            "settingsAuthStatus": None,
            "campaignsStatus": None,
            "tasksStatus": None,
            "generatedNewBearer": False,
            "rotatedBearer": False,
            "secretMaterialLogged": False,
        }
        result: dict[str, Any] = {
            "requestId": request_id,
            "status": "ok",
            "sourceHostReachable": True,
            "repoPath": repo_path,
            "appReachable": False,
            "appUrl": app_url,
            "agentApiBaseUrl": agent_api_base_url,
            "agentApiRouteReachable": False,
            "bearerConfigured": False,
            "campaigns": [],
            "selectedCampaign": None,
            "policySync": None,
            "diagnostics": diagnostics,
            "errors": [],
        }
        bearer_key: str | None = None
        try:
            if repo_path:
                repo = Path(repo_path).expanduser().resolve()
                if not repo.exists() or not repo.is_dir():
                    raise ValueError(f"repoPath does not exist or is not a directory: {repo_path}")
                result["repoPath"] = str(repo)

            app_status, _app_body = self._request("GET", app_url)
            result["appReachable"] = app_status is not None

            settings_status, _settings_body = self._agent_api_get(agent_api_base_url, "settings")
            diagnostics["settingsUnauthStatus"] = settings_status
            result["agentApiRouteReachable"] = settings_status in {200, 401, 503}
            result["bearerConfigured"] = settings_status == 401

            if settings_status == 503:
                bearer_key = self._generate_or_rotate_bearer(app_url, repo_path)
                diagnostics["generatedNewBearer"] = True
            elif settings_status == 401:
                bearer_key = self._generate_or_rotate_bearer(app_url, repo_path)
                diagnostics["rotatedBearer"] = True
            elif settings_status == 200:
                bearer_key = self._pick_string(payload, "bearerKey", "agentApiBearerKey", "agentApiKey")
                if not bearer_key:
                    bearer_key = self._generate_or_rotate_bearer(app_url, repo_path)
                    diagnostics["rotatedBearer"] = True
            else:
                result["status"] = "failed"
                result["errors"].append(f"Agent API settings route returned HTTP {settings_status}")
                return result

            result["bearerConfigured"] = True
            result["bearerKey"] = bearer_key

            settings_auth_status, _settings_auth_body = self._agent_api_get(agent_api_base_url, "settings", bearer_key)
            diagnostics["settingsAuthStatus"] = settings_auth_status
            if settings_auth_status != 200:
                result["status"] = "failed"
                result["errors"].append(f"Authenticated Agent API settings check returned HTTP {settings_auth_status}")
                return result

            campaigns_status, campaigns_body = self._agent_api_get(agent_api_base_url, "campaigns", bearer_key)
            diagnostics["campaignsStatus"] = campaigns_status
            campaigns = self._extract_data_list(campaigns_body)
            result["campaigns"] = campaigns
            if campaigns_status != 200:
                result["status"] = "failed"
                result["errors"].append(f"Authenticated Agent API campaigns check returned HTTP {campaigns_status}")
                return result

            selected_campaign = self._select_campaign(campaigns, payload)
            result["selectedCampaign"] = selected_campaign
            if selected_campaign:
                campaign_id = str(selected_campaign.get("_id") or selected_campaign.get("id") or "")
                tasks_status, _tasks_body = self._agent_api_get(
                    agent_api_base_url,
                    f"tasks?campaignId={urllib.parse.quote(campaign_id)}",
                    bearer_key,
                )
                diagnostics["tasksStatus"] = tasks_status

            autonomy_mode = self._normalize_autonomy_mode(self._pick_string(payload, "autonomyMode"))
            if autonomy_mode and selected_campaign:
                result["policySync"] = self._sync_policy(agent_api_base_url, bearer_key, selected_campaign, autonomy_mode)
            elif autonomy_mode and not selected_campaign:
                result["policySync"] = {"status": "selection_required", "mode": autonomy_mode}

            return result
        except Exception as exc:
            result["status"] = "failed"
            result["errors"].append(str(exc))
            result.pop("bearerKey", None)
            return result

    def derive_agent_api_base_url(self, app_url: str) -> str:
        value = str(app_url or "").strip().rstrip("/")
        if value.endswith("/api/openclaw"):
            return value
        return f"{value}/api/openclaw"

    def _pick_string(self, payload: dict[str, Any], *keys: str) -> str | None:
        for key in keys:
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    def _agent_api_get(self, base_url: str, path_and_query: str, bearer: str | None = None) -> tuple[int | None, Any]:
        separator = "&" if "?" in path_and_query else "?"
        url = f"{base_url.rstrip('/')}/{path_and_query.lstrip('/')}{separator}contractVersion={self.CONTRACT_VERSION}"
        headers = {"Authorization": f"Bearer {bearer}"} if bearer else None
        return self._request("GET", url, headers=headers)

    def _agent_api_post(
        self,
        base_url: str,
        path: str,
        bearer: str,
        input_payload: dict[str, Any],
    ) -> tuple[int | None, Any]:
        url = f"{base_url.rstrip('/')}/{path.lstrip('/')}"
        return self._request(
            "POST",
            url,
            headers={"Authorization": f"Bearer {bearer}", "Content-Type": "application/json"},
            body={"contractVersion": self.CONTRACT_VERSION, "input": input_payload},
        )

    def _request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
    ) -> tuple[int | None, Any]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(url, data=data, method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                text = response.read().decode("utf-8", errors="replace")
                return response.status, self._parse_json(text)
        except urllib.error.HTTPError as exc:
            text = exc.read().decode("utf-8", errors="replace")
            return exc.code, self._parse_json(text)
        except urllib.error.URLError:
            return None, None

    def _parse_json(self, text: str) -> Any:
        try:
            return json.loads(text or "{}")
        except json.JSONDecodeError:
            return text

    def _generate_or_rotate_bearer(self, app_url: str, repo_path: str | None) -> str:
        status, body = self._request("POST", f"{app_url.rstrip('/')}/api/settings/agent-key")
        key = self._extract_api_key(body)
        if status == 200 and key:
            return key
        if not repo_path:
            raise RuntimeError("Unable to rotate Agent API key through local app and repoPath was not provided")
        return self._rotate_bearer_via_convex(Path(repo_path).expanduser().resolve())

    def _rotate_bearer_via_convex(self, repo_root: Path) -> str:
        if not repo_root.exists() or not repo_root.is_dir():
            raise RuntimeError("Unable to rotate Agent API key because repoPath is not available")
        commands = [
            ["pnpm", "exec", "convex", "run", "settings:resetAgentApiKey"],
            ["npx", "convex", "run", "settings:resetAgentApiKey"],
        ]
        for command in commands:
            try:
                completed = subprocess.run(
                    command,
                    cwd=repo_root,
                    text=True,
                    capture_output=True,
                    timeout=45,
                    check=False,
                )
            except FileNotFoundError:
                continue
            except Exception as exc:
                raise RuntimeError(f"Agent API key rotation command failed: {type(exc).__name__}") from exc
            if completed.returncode != 0:
                continue
            key = self._extract_api_key(self._parse_json(completed.stdout.strip()))
            if key:
                return key
        raise RuntimeError("Agent API key rotation command did not return a key")

    def _extract_api_key(self, body: Any) -> str | None:
        if isinstance(body, dict):
            for key in ("apiKey", "agentApiKey", "bearerKey"):
                value = body.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            data = body.get("data")
            if isinstance(data, dict):
                return self._extract_api_key(data)
        return None

    def _extract_data_list(self, body: Any) -> list[dict[str, Any]]:
        data = body.get("data") if isinstance(body, dict) else body
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        if isinstance(data, dict):
            for key in ("campaigns", "items", "rows"):
                value = data.get(key)
                if isinstance(value, list):
                    return [item for item in value if isinstance(item, dict)]
        return []

    def _select_campaign(self, campaigns: list[dict[str, Any]], payload: dict[str, Any]) -> dict[str, Any] | None:
        selected_id = self._pick_string(payload, "selectedCampaignId", "campaignId")
        selected_name = self._pick_string(payload, "selectedCampaignName", "campaignName", "selectedCampaign")
        if selected_id:
            for campaign in campaigns:
                if str(campaign.get("_id") or campaign.get("id") or "") == selected_id:
                    return campaign
        if selected_name:
            for campaign in campaigns:
                if str(campaign.get("name") or "").strip().lower() == selected_name.lower():
                    return campaign
        active = [campaign for campaign in campaigns if str(campaign.get("status") or "active").lower() == "active"]
        if len(active) == 1:
            return active[0]
        return None

    def _normalize_autonomy_mode(self, value: str | None) -> str | None:
        if not value:
            return None
        normalized = value.strip()
        aliases = {
            "dangerous": "dangerously_skip_permissions",
            "skip_permissions": "dangerously_skip_permissions",
            "autonomous": "dangerously_skip_permissions",
        }
        normalized = aliases.get(normalized, normalized)
        if normalized not in self.SAFE_AUTONOMY_MODES:
            raise ValueError(f"Unsupported LinkCrest autonomy mode: {value}")
        return normalized

    def _sync_policy(
        self,
        base_url: str,
        bearer: str,
        selected_campaign: dict[str, Any],
        autonomy_mode: str,
    ) -> dict[str, Any]:
        campaign_id = str(selected_campaign.get("_id") or selected_campaign.get("id") or "")
        if not campaign_id:
            return {"status": "failed", "error": "selected campaign is missing an id"}
        get_status, get_body = self._agent_api_post(base_url, "autonomy/get_policy", bearer, {"campaignId": campaign_id})
        current_policy = {}
        if isinstance(get_body, dict):
            data = get_body.get("data")
            if isinstance(data, dict):
                current_policy = data.get("policy") if isinstance(data.get("policy"), dict) else data
        policy = self._policy_for_mode({**current_policy, "campaignId": campaign_id}, autonomy_mode)
        update_status, update_body = self._agent_api_post(base_url, "autonomy/update_policy", bearer, policy)
        explain_status, explain_body = self._agent_api_post(
            base_url,
            "autonomy/explain_effective_policy",
            bearer,
            {"campaignId": campaign_id},
        )
        return {
            "status": "ok" if get_status == 200 and update_status == 200 and explain_status == 200 else "failed",
            "mode": autonomy_mode,
            "campaignId": campaign_id,
            "getPolicyStatus": get_status,
            "updatePolicyStatus": update_status,
            "explainEffectivePolicyStatus": explain_status,
            "updatedPolicy": self._redact_secrets(update_body),
            "effectivePolicyExplanation": self._redact_secrets(explain_body),
        }

    def _policy_for_mode(self, existing: dict[str, Any], mode: str) -> dict[str, Any]:
        policy = {
            "campaignId": str(existing.get("campaignId") or ""),
            "mode": mode,
            "allowInternalWrites": True,
            "allowOutreachSend": False,
            "allowPublicFormSubmit": False,
            "allowAccountCreation": False,
            "allowEmailSend": False,
            "allowExternalPublish": False,
            "allowContactedStatusUpdate": False,
            "allowSubmittedStatusUpdate": False,
            "allowLiveStatusUpdate": False,
            "allowIndexedStatusUpdate": False,
            "allowPayment": False,
            "allowCaptchaHandling": False,
            "allowCredentialUse": False,
            "requireEvidenceForExternalActions": True,
            "requireEvidenceForLifecycleStatus": True,
            "hardStopPaymentUnlessExplicit": True,
            "hardStopCaptchaBypass": True,
            "hardStopSecretExposure": True,
            "hardStopDestructiveDataLoss": True,
            "hardStopLegalCommitmentUnlessExplicit": True,
        }
        for key, value in existing.items():
            if key in policy:
                policy[key] = value
        policy["mode"] = mode
        if mode == "internal_write":
            policy["allowInternalWrites"] = True
        if mode in {"supervised_external", "dangerously_skip_permissions"}:
            policy.update({
                "allowInternalWrites": True,
                "allowOutreachSend": True,
                "allowPublicFormSubmit": True,
                "allowEmailSend": True,
                "allowExternalPublish": True,
                "allowContactedStatusUpdate": True,
                "allowSubmittedStatusUpdate": True,
                "allowLiveStatusUpdate": True,
                "allowIndexedStatusUpdate": True,
            })
        if mode == "dangerously_skip_permissions":
            policy["requireEvidenceForExternalActions"] = False
            policy["requireEvidenceForLifecycleStatus"] = False
        return policy

    def _redact_secrets(self, value: Any) -> Any:
        if isinstance(value, dict):
            result: dict[str, Any] = {}
            for key, child in value.items():
                normalized_key = str(key).lower()
                secret_key = SECRET_FIELD_RE.search(str(key)) or any(
                    token in normalized_key
                    for token in ("apikey", "bearer", "token", "secret", "password", "credential")
                )
                if secret_key and isinstance(child, str) and child.strip():
                    result[str(key)] = "[REDACTED_SECRET_VALUE]"
                else:
                    result[str(key)] = self._redact_secrets(child)
            return result
        if isinstance(value, list):
            return [self._redact_secrets(item) for item in value]
        return value


class MarketplaceLocalAppAgentApiRequestProxy:
    DEFAULT_TIMEOUT_MS = 15_000
    MAX_TIMEOUT_MS = 60_000
    ALLOWED_LOCAL_HOSTS = {"localhost", "127.0.0.1"}
    ALLOWED_PORTS = {3052}
    ALLOWED_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}
    BODY_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
    SECRET_HEADER_NAMES = {"authorization", "proxy-authorization", "cookie", "set-cookie", "x-api-key"}

    def __init__(self, runtime_manager: LocalAppRuntimeManager | None = None) -> None:
        self.runtime_manager = runtime_manager

    def handle(self, payload: dict[str, Any]) -> dict[str, Any]:
        request_id = str(payload.get("requestId") or "")
        started = time.monotonic()
        try:
            self._log_payload_shape(request_id, payload)
            bearer, bearer_source = self._extract_bearer(payload)
            logger.info(
                "marketplace local Agent API proxy bearer check requestId=%s bearerPresent=%s bearerSource=%s authorizationHeaderAttached=%s",
                request_id,
                bool(bearer),
                bearer_source or "<none>",
                bool(bearer),
            )
            if not bearer:
                return self._error(request_id, "missing_bearer", "Missing bridge-only bearer credential.")

            method = str(payload.get("method") or "GET").upper()
            if method not in self.ALLOWED_METHODS:
                return self._error(request_id, "invalid_payload", f"Unsupported method: {method}")

            timeout_ms = self._timeout_ms(payload.get("timeoutMs"))
            url, parsed = self._build_validated_url(payload, method)
            body = self._body_for_request(payload, method)
            headers = self._headers_for_request(payload, bearer, body)
            logger.info(
                "marketplace local Agent API proxy request requestId=%s appSlug=%s method=%s url=%s path=%s target=%s:%s",
                request_id,
                payload.get("appSlug"),
                method,
                self._redacted_url(url),
                parsed.path,
                parsed.hostname,
                parsed.port,
            )
            status, response_headers, response_body = self._execute_with_recovery(
                payload,
                method,
                url,
                headers,
                body,
                timeout_ms / 1000,
            )
            duration_ms = int((time.monotonic() - started) * 1000)
            code = self._code_for_http_status(status)
            logger.info(
                "marketplace local Agent API proxy result requestId=%s appSlug=%s method=%s path=%s status=%s durationMs=%s code=%s",
                request_id,
                payload.get("appSlug"),
                method,
                parsed.path,
                status,
                duration_ms,
                code or "ok",
            )
            parsed_body, is_json = self._parse_response_body(response_body)
            result = {
                "requestId": request_id,
                "status": "ok" if 200 <= status < 400 else "failed",
                "ok": 200 <= status < 400,
                "httpStatus": status,
                "statusCode": status,
                "body": self._redact_secrets(parsed_body),
                "data": self._redact_secrets(parsed_body),
                "bodyKind": "json" if is_json else "text",
                "headers": self._safe_response_headers(response_headers),
                "diagnostics": {
                    "durationMs": duration_ms,
                    "targetHost": parsed.hostname,
                    "targetPort": parsed.port,
                    "path": parsed.path,
                    "secretMaterialLogged": False,
                },
            }
            if code:
                result["error"] = {"code": code, "message": f"LinkCrest Agent API returned HTTP {status}."}
            return result
        except MarketplaceLocalAppAgentApiRequestRejected as exc:
            return self._error(request_id, exc.code, exc.message)
        except TimeoutError:
            return self._error(request_id, "linkcrest_timeout", "Timed out calling local LinkCrest Agent API.")
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", None)
            if isinstance(reason, TimeoutError) or isinstance(reason, socket.timeout):
                return self._error(request_id, "linkcrest_timeout", "Timed out calling local LinkCrest Agent API.")
            return self._error(request_id, "linkcrest_unreachable", "Local LinkCrest app is unreachable.")
        except socket.timeout:
            return self._error(request_id, "linkcrest_timeout", "Timed out calling local LinkCrest Agent API.")
        except Exception as exc:
            return self._error(request_id, "source_host_proxy_failed", f"Source-host proxy failed: {type(exc).__name__}")

    def _execute_with_recovery(
        self,
        payload: dict[str, Any],
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes | None,
        timeout_s: float,
    ) -> tuple[int, dict[str, str], bytes]:
        try:
            return self._execute(method, url, headers, body, timeout_s)
        except urllib.error.URLError:
            runtime_manager = getattr(self, "runtime_manager", None)
            if not runtime_manager or not isinstance(payload.get("runtimeProfile"), dict):
                raise
            request_id = str(payload.get("requestId") or "")
            recovery_payload = {
                "requestId": request_id,
                "appSlug": payload.get("appSlug"),
                "runtimeProfile": payload.get("runtimeProfile"),
                "reason": "marketplace_local_app_unreachable",
            }
            recovery = runtime_manager.ensure_running(recovery_payload, start_if_needed=True)
            if recovery.get("status") != "ok":
                raise MarketplaceLocalAppAgentApiRequestRejected(
                    "runtime_recovery_failed",
                    json.dumps({"runtimeRecovery": recovery}, ensure_ascii=False),
                )
            try:
                return self._execute(method, url, headers, body, timeout_s)
            except urllib.error.URLError as exc:
                raise MarketplaceLocalAppAgentApiRequestRejected(
                    "app_still_unreachable",
                    f"Local app remained unreachable after runtime recovery: {type(exc).__name__}",
                ) from exc

    def _extract_bearer(self, payload: dict[str, Any]) -> tuple[str | None, str | None]:
        for container_name in ("credential", "bridgeOnlyCredential", "bridgeOnlyBearerCredential"):
            value = payload.get(container_name)
            bearer = self._bearer_from_credential_object(value)
            if bearer:
                return bearer, container_name

        for nested_name in ("data", "input"):
            nested = payload.get(nested_name)
            if not isinstance(nested, dict):
                continue
            for container_name in ("credential", "bridgeOnlyCredential", "bridgeOnlyBearerCredential"):
                value = nested.get(container_name)
                bearer = self._bearer_from_credential_object(value)
                if bearer:
                    return bearer, f"nested-{nested_name}.{container_name}"

        for key in (
            "bearerKey",
            "bearer",
            "agentApiBearer",
            "agentApiBearerKey",
            "agentApiBearerToken",
            "agentApiKey",
            "bearerCredential",
            "bearerToken",
            "bridgeOnlyBearer",
            "bridgeOnlyBearerKey",
            "bridgeOnlyBearerToken",
            "credential",
            "credentialValue",
            "accessToken",
        ):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return self._normalize_authorization_value(value), key
        credentials = payload.get("credentials")
        if isinstance(credentials, dict):
            return self._extract_bearer(credentials)
        return None, None

    def _bearer_from_credential_object(self, value: Any) -> str | None:
        if not isinstance(value, dict):
            return None
        for key in ("authorizationHeader", "authorization", "bearer", "token", "value"):
            item = value.get(key)
            if isinstance(item, str) and item.strip():
                return self._normalize_authorization_value(item)
        return None

    def _normalize_authorization_value(self, value: str) -> str:
        stripped = value.strip()
        if stripped.lower().startswith("bearer "):
            return stripped[len("Bearer ") :].strip()
        return stripped

    def _log_payload_shape(self, request_id: str, payload: dict[str, Any]) -> None:
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        input_payload = payload.get("input") if isinstance(payload.get("input"), dict) else {}
        logger.info(
            "marketplace local Agent API proxy payload shape requestId=%s topLevelKeys=%s dataKeys=%s inputKeys=%s credentialObjectPresent=%s bridgeOnlyCredentialObjectPresent=%s bridgeOnlyBearerCredentialObjectPresent=%s",
            request_id,
            ",".join(sorted(str(key) for key in payload.keys())),
            ",".join(sorted(str(key) for key in data.keys())),
            ",".join(sorted(str(key) for key in input_payload.keys())),
            isinstance(payload.get("credential"), dict) or isinstance(data.get("credential"), dict) or isinstance(input_payload.get("credential"), dict),
            isinstance(payload.get("bridgeOnlyCredential"), dict)
            or isinstance(data.get("bridgeOnlyCredential"), dict)
            or isinstance(input_payload.get("bridgeOnlyCredential"), dict),
            isinstance(payload.get("bridgeOnlyBearerCredential"), dict)
            or isinstance(data.get("bridgeOnlyBearerCredential"), dict)
            or isinstance(input_payload.get("bridgeOnlyBearerCredential"), dict),
        )

    def _timeout_ms(self, value: Any) -> int:
        try:
            timeout_ms = int(value)
        except Exception:
            timeout_ms = self.DEFAULT_TIMEOUT_MS
        return max(1, min(timeout_ms, self.MAX_TIMEOUT_MS))

    def _build_validated_url(self, payload: dict[str, Any], method: str) -> tuple[str, urllib.parse.ParseResult]:
        base_url = self._required_string(payload, "baseUrl")
        raw_path = self._target_path_value(payload)
        parsed_base = urllib.parse.urlparse(base_url)
        raw_base_query = parsed_base.query
        if parsed_base.scheme not in {"http", "https"}:
            self._log_target_validation(payload, base_url, raw_path, "", "", False, "baseUrl must use http or https.")
            raise MarketplaceLocalAppAgentApiRequestRejected("source_host_rejected_target", "baseUrl must use http or https.")

        final_url = self._join_target_url(parsed_base, raw_path)
        parsed = urllib.parse.urlparse(final_url)
        path_allowed, rejection_reason = self._parsed_target_allowed(parsed)
        self._log_target_validation(payload, base_url, raw_path, final_url, parsed.path, path_allowed, rejection_reason)
        if not path_allowed:
            raise MarketplaceLocalAppAgentApiRequestRejected("source_host_rejected_target", rejection_reason)

        normalized_path = self._normalize_openclaw_path(parsed.path)
        query_items = self._query_items(raw_base_query, parsed.query, payload.get("query"))
        contract_version = self._contract_version(payload)
        if method == "GET" and contract_version and not any(key == "contractVersion" for key, _value in query_items):
            query_items.append(("contractVersion", contract_version))
        query = urllib.parse.urlencode(query_items, doseq=True)
        url = urllib.parse.urlunparse((parsed.scheme, parsed.netloc, normalized_path, "", query, ""))
        return url, urllib.parse.urlparse(url)

    def _target_path_value(self, payload: dict[str, Any]) -> str:
        values: list[tuple[str, str]] = []
        for key in ("targetUrl", "url", "endpoint", "path"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                values.append((key, value.strip()))
        for _key, value in values:
            if urllib.parse.urlparse(value).scheme:
                return value
        for key in ("path", "endpoint", "targetUrl", "url"):
            for candidate_key, value in values:
                if candidate_key == key:
                    return value
        return ""

    def _join_target_url(self, parsed_base: urllib.parse.ParseResult, raw_path: str) -> str:
        if urllib.parse.urlparse(raw_path).scheme:
            return raw_path
        if not raw_path:
            path = parsed_base.path or "/api/openclaw"
            query = parsed_base.query
            return urllib.parse.urlunparse((parsed_base.scheme, parsed_base.netloc, path, "", query, ""))
        path, query = self._split_path_query(raw_path)
        if path.startswith("/"):
            final_path = path
        else:
            base_path = parsed_base.path.rstrip("/")
            final_path = f"{base_path}/{path}" if base_path else f"/{path}"
        return urllib.parse.urlunparse((parsed_base.scheme, parsed_base.netloc, final_path, "", query, ""))

    def _parsed_target_allowed(self, parsed: urllib.parse.ParseResult) -> tuple[bool, str]:
        if parsed.scheme not in {"http", "https"}:
            return False, "target protocol must be http or https."
        host = (parsed.hostname or "").lower()
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        if host not in self.ALLOWED_LOCAL_HOSTS:
            return False, "target host must be localhost or 127.0.0.1."
        if port not in self.ALLOWED_PORTS:
            return False, "target port must be 3052."
        try:
            self._normalize_openclaw_path(parsed.path)
        except MarketplaceLocalAppAgentApiRequestRejected as exc:
            return False, exc.message
        return True, ""

    def _log_target_validation(
        self,
        payload: dict[str, Any],
        base_url: str,
        raw_path: str,
        final_url: str,
        parsed_pathname: str,
        path_allowed: bool,
        rejection_reason: str,
    ) -> None:
        logger.info(
            "marketplace local Agent API proxy target validation requestId=%s baseUrl=%s pathPresent=%s endpointPresent=%s targetUrlPresent=%s urlPresent=%s finalUrl=%s parsedPathname=%s pathAllowed=%s rejectionReason=%s",
            payload.get("requestId"),
            self._redacted_url(base_url),
            bool(payload.get("path")),
            bool(payload.get("endpoint")),
            bool(payload.get("targetUrl")),
            bool(payload.get("url")),
            self._redacted_url(final_url) if final_url else "",
            parsed_pathname,
            path_allowed,
            rejection_reason or "<none>",
        )

    def _required_string(self, payload: dict[str, Any], key: str) -> str:
        value = payload.get(key)
        if not isinstance(value, str) or not value.strip():
            raise MarketplaceLocalAppAgentApiRequestRejected("invalid_payload", f"Missing required field: {key}")
        return value.strip().rstrip("/")

    def _split_path_query(self, value: str) -> tuple[str, str]:
        parsed = urllib.parse.urlparse(value if value.startswith("/") else f"/{value}")
        return parsed.path, parsed.query

    def _normalize_openclaw_path(self, path: str) -> str:
        decoded = urllib.parse.unquote(path.replace("\\", "/"))
        if "\0" in decoded:
            raise MarketplaceLocalAppAgentApiRequestRejected("source_host_rejected_target", "path contains an invalid character.")
        parts = []
        for part in decoded.split("/"):
            if not part or part == ".":
                continue
            if part == "..":
                raise MarketplaceLocalAppAgentApiRequestRejected("source_host_rejected_target", "path traversal is not allowed.")
            parts.append(part)
        normalized = "/" + "/".join(parts)
        if normalized != "/api/openclaw" and not normalized.startswith("/api/openclaw/"):
            raise MarketplaceLocalAppAgentApiRequestRejected("source_host_rejected_target", "path must be under /api/openclaw.")
        return normalized

    def _query_items(self, *values: Any) -> list[tuple[str, str]]:
        items: list[tuple[str, str]] = []
        for value in values:
            if not value:
                continue
            if isinstance(value, str):
                items.extend((str(key), str(item)) for key, item in urllib.parse.parse_qsl(value, keep_blank_values=True))
            elif isinstance(value, dict):
                for key, item in value.items():
                    if isinstance(item, list):
                        items.extend((str(key), str(child)) for child in item)
                    elif item is not None:
                        items.append((str(key), str(item)))
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, (list, tuple)) and len(item) == 2:
                        items.append((str(item[0]), str(item[1])))
                    elif isinstance(item, dict) and "name" in item:
                        items.append((str(item.get("name")), str(item.get("value") or "")))
        return [(key, item) for key, item in items if key.lower() not in {"authorization", "bearer", "token"}]

    def _contract_version(self, payload: dict[str, Any]) -> str | None:
        value = payload.get("contractVersion")
        if isinstance(value, str) and value.strip():
            return value.strip()
        return MarketplaceLocalAppAgentApiSetup.CONTRACT_VERSION

    def _body_for_request(self, payload: dict[str, Any], method: str) -> bytes | None:
        if method not in self.BODY_METHODS:
            return None
        body = payload.get("body")
        if body is None:
            body = {}
        contract_version = self._contract_version(payload)
        if isinstance(body, dict):
            if contract_version and "contractVersion" not in body:
                body = {"contractVersion": contract_version, **body}
            return json.dumps(body).encode("utf-8")
        if isinstance(body, str):
            return body.encode("utf-8")
        return json.dumps(body).encode("utf-8")

    def _headers_for_request(self, payload: dict[str, Any], bearer: str, body: bytes | None) -> dict[str, str]:
        headers: dict[str, str] = {}
        raw_headers = payload.get("headers")
        if isinstance(raw_headers, dict):
            for key, value in raw_headers.items():
                name = str(key).strip()
                if not name or name.lower() in self.SECRET_HEADER_NAMES:
                    continue
                if "\n" in name or "\r" in name:
                    continue
                headers[name] = str(value)
        if body is not None and not any(key.lower() == "content-type" for key in headers):
            headers["Content-Type"] = "application/json"
        headers["Authorization"] = f"Bearer {bearer}"
        return headers

    def _execute(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes | None,
        timeout_s: float,
    ) -> tuple[int, dict[str, str], bytes]:
        request = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout_s) as response:
                return response.status, dict(response.headers.items()), response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers.items()), exc.read()

    def _parse_response_body(self, body: bytes) -> tuple[Any, bool]:
        text = body.decode("utf-8", errors="replace")
        try:
            return json.loads(text or "{}"), True
        except json.JSONDecodeError:
            return text, False

    def _safe_response_headers(self, headers: dict[str, str]) -> dict[str, str]:
        safe: dict[str, str] = {}
        for key, value in headers.items():
            if key.lower() in self.SECRET_HEADER_NAMES:
                safe[key] = "[REDACTED_SECRET_VALUE]"
            elif key.lower() in {"content-type", "content-length", "cache-control"}:
                safe[key] = str(value)
        return safe

    def _redacted_url(self, url: str) -> str:
        parsed = urllib.parse.urlparse(url)
        safe_items = []
        for key, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True):
            if key.lower() in {"authorization", "bearer", "token", "apikey", "api_key", "secret"}:
                safe_items.append((key, "[REDACTED_SECRET_VALUE]"))
            else:
                safe_items.append((key, value))
        return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", urllib.parse.urlencode(safe_items), ""))

    def _code_for_http_status(self, status: int) -> str | None:
        if status in {401, 403}:
            return "linkcrest_auth_failed"
        if status >= 400:
            return "linkcrest_agent_api_error"
        return None

    def _error(self, request_id: str, code: str, message: str) -> dict[str, Any]:
        return {
            "requestId": request_id,
            "status": "failed",
            "ok": False,
            "error": {"code": code, "message": message},
            "diagnostics": {"secretMaterialLogged": False},
        }

    def _redact_secrets(self, value: Any) -> Any:
        return MarketplaceLocalAppAgentApiSetup()._redact_secrets(value)


class MarketplaceLocalAppAgentApiRequestRejected(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class BridgeConfig:
    api_url: str
    device_public_id: str
    device_token: str
    workspace_id: str | None = None
    workspace_name: str | None = None
    external_agent_ids: list[str] = field(default_factory=list)
    device_label: str = DEFAULT_DEVICE_LABEL
    compatibility_level: str | None = None
    operating_mode: str | None = None
    enabled_capabilities: list[str] = field(default_factory=list)

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> "BridgeConfig":
        return cls(
            api_url=_normalize_api_url(str(data.get("apiUrl") or data.get("api_url") or "")),
            device_public_id=str(
                data.get("devicePublicId") or data.get("device_public_id") or ""
            ).strip(),
            device_token=str(data.get("deviceToken") or data.get("device_token") or "").strip(),
            workspace_id=(
                str(data.get("workspaceId") or data.get("workspace_id")).strip()
                if data.get("workspaceId") or data.get("workspace_id")
                else None
            ),
            workspace_name=(
                str(data.get("workspaceName") or data.get("workspace_name")).strip()
                if data.get("workspaceName") or data.get("workspace_name")
                else None
            ),
            external_agent_ids=[
                str(item).strip()
                for item in (data.get("externalAgentIds") or data.get("external_agent_ids") or [])
                if str(item).strip()
            ],
            device_label=str(data.get("deviceLabel") or data.get("device_label") or DEFAULT_DEVICE_LABEL),
            compatibility_level=(
                str(data.get("compatibilityLevel") or data.get("compatibility_level")).strip()
                if data.get("compatibilityLevel") or data.get("compatibility_level")
                else None
            ),
            operating_mode=(
                str(data.get("operatingMode") or data.get("operating_mode")).strip()
                if data.get("operatingMode") or data.get("operating_mode")
                else None
            ),
            enabled_capabilities=[
                str(item).strip()
                for item in (data.get("enabledCapabilities") or data.get("enabled_capabilities") or [])
                if str(item).strip()
            ],
        )

    @classmethod
    def load(cls, path: Path | None = None) -> "BridgeConfig":
        path = path or _config_path()
        if path.exists():
            return cls.from_mapping(json.loads(path.read_text(encoding="utf-8")))
        env_data = {
            "apiUrl": os.getenv("RELAY_CONSOLE_API_URL") or os.getenv("CLAWCHAT_API_URL", ""),
            "devicePublicId": os.getenv("RELAY_CONSOLE_BRIDGE_DEVICE_PUBLIC_ID") or os.getenv("CLAWCHAT_BRIDGE_DEVICE_PUBLIC_ID", ""),
            "deviceToken": os.getenv("RELAY_CONSOLE_BRIDGE_DEVICE_TOKEN") or os.getenv("CLAWCHAT_BRIDGE_DEVICE_TOKEN", ""),
            "workspaceId": os.getenv("RELAY_CONSOLE_WORKSPACE_ID") or os.getenv("CLAWCHAT_WORKSPACE_ID", ""),
            "externalAgentIds": _csv(os.getenv("RELAY_CONSOLE_HERMES_AGENTS") or os.getenv("CLAWCHAT_HERMES_AGENTS")),
            "deviceLabel": os.getenv("RELAY_CONSOLE_HERMES_DEVICE_LABEL") or os.getenv("CLAWCHAT_HERMES_DEVICE_LABEL", DEFAULT_DEVICE_LABEL),
        }
        return cls.from_mapping(env_data)

    def to_json(self) -> dict[str, Any]:
        return {
            "apiUrl": self.api_url,
            "workspaceId": self.workspace_id,
            "workspaceName": self.workspace_name,
            "devicePublicId": self.device_public_id,
            "deviceToken": self.device_token,
            "externalAgentIds": self.external_agent_ids,
            "deviceLabel": self.device_label,
            "compatibilityLevel": self.compatibility_level,
            "operatingMode": self.operating_mode,
            "enabledCapabilities": self.enabled_capabilities,
        }

    def save(self, path: Path | None = None) -> None:
        path = path or _config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(path.parent, 0o700)
        except OSError:
            pass
        if path.is_symlink():
            raise RuntimeError("Refusing to write bridge credentials through a symbolic link")
        tmp_path = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(tmp_path, flags, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self.to_json(), handle, ensure_ascii=True, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, path)
            os.chmod(path, 0o600)
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            try:
                tmp_path.unlink()
            except FileNotFoundError:
                pass

    def add_external_agent_id(self, external_agent_id: str) -> bool:
        existing = set(self.external_agent_ids)
        if external_agent_id in existing:
            return False
        self.external_agent_ids = list(dict.fromkeys([*self.external_agent_ids, external_agent_id]))
        return True

    def validate_for_run(self) -> None:
        missing = []
        if not self.api_url:
            missing.append("apiUrl")
        if not self.device_public_id:
            missing.append("devicePublicId")
        if not self.device_token:
            missing.append("deviceToken")
        if missing:
            raise RuntimeError(
                f"Relay Console Hermes bridge config is missing: {', '.join(missing)}. "
                "Run `hermes-clawchat-bridge enroll ...` first."
            )


@dataclass
class ActiveRun:
    dispatch_id: str
    runtime_session_id: str
    external_agent_id: str = "agent"
    received_monotonic: float = 0.0
    accepted_monotonic: float = 0.0
    started_sent_monotonic: float = 0.0
    first_model_call_monotonic: float = 0.0
    first_delta_monotonic: float = 0.0
    first_thinking_monotonic: float = 0.0
    terminal_queued_monotonic: float = 0.0
    event_queue: "queue.Queue[dict[str, Any]]" = field(default_factory=queue.Queue)
    done: threading.Event = field(default_factory=threading.Event)
    agent: Any | None = None
    worker_thread: threading.Thread | None = None
    terminal_event_type: str | None = None
    cancel_requested: bool = False
    execution_lock_key: str | None = None
    _state_lock: threading.Lock = field(default_factory=threading.Lock)

    def elapsed_ms(self, now: float | None = None) -> int | None:
        if not self.received_monotonic:
            return None
        return int(((now if now is not None else time.monotonic()) - self.received_monotonic) * 1000)

    def emit(self, event: dict[str, Any]) -> None:
        event["dispatchId"] = self.dispatch_id
        event.setdefault("externalAgentId", self.external_agent_id)
        if event.get("type") in TERMINAL_EVENT_TYPES:
            with self._state_lock:
                if self.terminal_event_type:
                    logger.warning(
                        "dropping duplicate terminal event dispatchId=%s externalAgentId=%s existingType=%s duplicateType=%s",
                        self.dispatch_id,
                        self.external_agent_id,
                        self.terminal_event_type,
                        event.get("type"),
                    )
                    return
                self.terminal_event_type = str(event.get("type"))
                self.terminal_queued_monotonic = time.monotonic()
            logger.info(
                "queued terminal event dispatchId=%s externalAgentId=%s type=%s elapsedMs=%s",
                self.dispatch_id,
                self.external_agent_id,
                event.get("type"),
                self.elapsed_ms(),
            )
        self.event_queue.put(event)


@dataclass
class MarketplaceRuntimeTool:
    name: str
    callable_name: str
    description: str
    input_schema: dict[str, Any]
    execution_url: str
    method: str = "POST"
    aliases: tuple[str, ...] = ()


@dataclass
class PendingTerminalEvent:
    event_id: str
    event: dict[str, Any]
    attempts: int = 0
    acknowledged: bool = False
    exhausted_logged: bool = False
    first_attempt_monotonic: float = 0.0
    last_attempt_monotonic: float = 0.0
    last_error: str | None = None


@dataclass
class ScopedRunLock:
    lock: threading.Lock = field(default_factory=threading.Lock)
    ref_count: int = 0
    owner_dispatch_id: str | None = None
    owner_external_agent_id: str | None = None
    owner_reason: str | None = None
    owner_acquired_at: str | None = None


class HermesExecutionLockTimeout(RuntimeError):
    pass


class HermesRunCancelled(RuntimeError):
    pass


class HermesDispatchDedupe(RuntimeError):
    pass


class LocalAppRuntimeManager:
    SECRET_TEXT_RE = re.compile(
        r"(?i)\b([A-Za-z0-9_.-]*(?:API[_-]?KEY|ACCESS[_-]?TOKEN|REFRESH[_-]?TOKEN|ID[_-]?TOKEN|TOKEN|SECRET|PASSWORD|PASSWD|PRIVATE[_-]?KEY|CLIENT[_-]?SECRET|WEBHOOK[_-]?SECRET)[A-Za-z0-9_.-]*)\s*([:=])\s*(['\"]?)([^<>'\"\s,}\]]{4,})(\3)"
    )
    BEARER_RE = re.compile(r"\b(Bearer\s+)([A-Za-z0-9._~+/=-]{16,})\b")
    HARD_STOP_PATTERNS = {
        "install": re.compile(r"\b(run|install|missing)\b.*\b(npm|pnpm|yarn|bun)?\s*install\b|\binstall dependencies\b", re.I),
        "migration": re.compile(r"\bmigrat(e|ion)\b|\bconvex\b.*\bupgrade\b", re.I),
        "reset": re.compile(r"\b(reset|wipe|drop)\b.*\b(database|db|data)\b", re.I),
        "destructive_data_loss": re.compile(r"\b(destructive|data loss|delete all|erase all)\b", re.I),
        "secret_exposure": re.compile(r"\b(secret|token|api key|password|credential)\b.*\b(expose|print|share|rotate)\b", re.I),
        "payment": re.compile(r"\b(payment|billing|subscribe|purchase|charge)\b", re.I),
        "captcha_bypass": re.compile(r"\b(captcha|recaptcha)\b.*\b(bypass|solve)\b", re.I),
        "legal_commitment": re.compile(r"\b(terms|legal|ownership|contract|commitment)\b.*\b(accept|agree|confirm)\b", re.I),
        "unknown_interactive_prompt": re.compile(r"(\?|\b(y/n|yes/no|press enter|select an option)\b)", re.I),
    }

    def __init__(self) -> None:
        self._processes: dict[str, subprocess.Popen[str]] = {}
        self._lock = threading.Lock()

    def handle_action(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        request_id = str(payload.get("requestId") or "")
        started = time.monotonic()
        profile = self._profile_from_payload(payload)
        logger.info(
            "received local app runtime action action=%s requestId=%s appSlug=%s repoPath=%s command=%s appUrl=%s healthCheckUrl=%s backendHealthCheckUrl=%s ports=%s",
            action,
            request_id,
            payload.get("appSlug"),
            profile.get("repoPath"),
            self._command_name(str(profile.get("startCommand") or "")),
            profile.get("appUrl"),
            profile.get("healthCheckUrl"),
            profile.get("backendHealthCheckUrl"),
            ",".join(str(port) for port in self._expected_ports(profile)) or "<none>",
        )
        if not profile:
            result = self._runtime_error(request_id, "runtime_profile_missing", "runtimeProfile is required")
        elif action == "localApp.getRuntimeStatus":
            result = self.get_runtime_status(payload)
        elif action in {"localApp.ensureRunning", "localApp.start"}:
            result = self.ensure_running(payload, start_if_needed=True)
        elif action == "localApp.restart":
            result = self.restart(payload)
        else:
            result = self._runtime_error(request_id, "unsupported_action", f"Unsupported local app action: {action}")
        logger.info(
            "local app runtime action result action=%s requestId=%s appSlug=%s result=%s durationMs=%s hardStopReason=%s",
            action,
            request_id,
            payload.get("appSlug"),
            result.get("status"),
            int((time.monotonic() - started) * 1000),
            (result.get("error") or {}).get("code") if isinstance(result.get("error"), dict) else None,
        )
        return result

    def get_runtime_status(self, payload: dict[str, Any]) -> dict[str, Any]:
        request_id = str(payload.get("requestId") or "")
        profile = self._profile_from_payload(payload)
        if not profile:
            return self._runtime_error(request_id, "runtime_profile_missing", "runtimeProfile is required")
        return self._status_result(request_id, profile)

    def ensure_running(self, payload: dict[str, Any], *, start_if_needed: bool = True) -> dict[str, Any]:
        request_id = str(payload.get("requestId") or "")
        profile = self._profile_from_payload(payload)
        if not profile:
            return self._runtime_error(request_id, "runtime_profile_missing", "runtimeProfile is required")
        status = self._status_result(request_id, profile)
        if status["runtimeState"] == "running":
            status["status"] = "ok"
            status["alreadyRunning"] = True
            return status
        if not start_if_needed:
            return status
        if not bool(profile.get("autoStartAllowed")):
            return self._runtime_error(request_id, "auto_start_disabled", "Runtime profile does not allow auto-start", status=status)
        repo_path = str(profile.get("repoPath") or "").strip()
        if not repo_path:
            return self._runtime_error(request_id, "repo_not_found", "runtimeProfile.repoPath is required", status=status)
        repo_root = Path(repo_path).expanduser()
        if not repo_root.is_dir():
            return self._runtime_error(request_id, "repo_not_found", "runtimeProfile.repoPath does not exist", status=status)
        command = self._normalize_command(str(profile.get("startCommand") or ""))
        if not command:
            return self._runtime_error(request_id, "start_command_missing", "runtimeProfile.startCommand is required", status=status)
        validation = self._validate_start_command(repo_root, command)
        logger.info(
            "local app runtime command allowlist decision requestId=%s appSlug=%s executable=%s args=%s cwd=%s allowed=%s reason=%s",
            request_id,
            payload.get("appSlug"),
            validation.get("executable"),
            validation.get("args"),
            repo_root.resolve(),
            validation.get("allowed"),
            validation.get("reason"),
        )
        if not validation.get("allowed"):
            return self._runtime_error(
                request_id,
                str(validation.get("code") or "start_command_missing"),
                str(validation.get("message") or "Start command is not allowed."),
                status=status,
            )
        normalized = str(validation["command"])
        if self._command_appears_running(repo_root.resolve(), normalized):
            return self._status_result(request_id, profile, status_override="ok", already_running=True)
        if self._configured_app_port_occupied(status):
            if status["appReachable"] and status["agentApiReachable"] and status.get("backendHealthReachable") is not False:
                status["status"] = "ok"
                status["alreadyRunning"] = True
                return status
            return self._runtime_error(
                request_id,
                "health_check_failed",
                "Configured local app port is occupied but health checks are not passing; refusing duplicate start.",
                status={**status, "alreadyRunning": True},
            )
        start_result = self._start_runtime_command(repo_root.resolve(), normalized)
        if start_result.get("hardStop"):
            return self._runtime_error(
                request_id,
                "hard_stop_required",
                "Startup requires explicit approval before Hermes can continue.",
                status=status,
                diagnostics=start_result,
            )
        if not start_result.get("started"):
            return self._runtime_error(request_id, "start_command_failed", "Failed to start configured runtime command.", status=status, diagnostics=start_result)
        return self._wait_for_healthy(request_id, profile, start_result)

    def restart(self, payload: dict[str, Any]) -> dict[str, Any]:
        request_id = str(payload.get("requestId") or "")
        profile = self._profile_from_payload(payload)
        if not profile:
            return self._runtime_error(request_id, "runtime_profile_missing", "runtimeProfile is required")
        repo_path = str(profile.get("repoPath") or "").strip()
        command = self._normalize_command(str(profile.get("startCommand") or ""))
        if repo_path and command:
            key = f"{Path(repo_path).expanduser().resolve()}:{command}"
            with self._lock:
                process = self._processes.get(key)
                if process and process.poll() is None:
                    try:
                        os.killpg(process.pid, signal.SIGTERM)
                    except Exception:
                        process.terminate()
        return self.ensure_running(payload, start_if_needed=True)

    def _profile_from_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        profile = payload.get("runtimeProfile")
        if isinstance(profile, dict):
            return profile
        return {}

    def _status_result(
        self,
        request_id: str,
        profile: dict[str, Any],
        *,
        status_override: str | None = None,
        already_running: bool = False,
    ) -> dict[str, Any]:
        app_url = str(profile.get("appUrl") or profile.get("healthCheckUrl") or "").strip()
        agent_api_url = str(profile.get("agentApiUrl") or "").strip()
        health_url = str(profile.get("healthCheckUrl") or app_url).strip()
        backend_url = str(profile.get("backendHealthCheckUrl") or "").strip()
        repo_path = str(profile.get("repoPath") or "").strip()
        repo_root = Path(repo_path).expanduser().resolve() if repo_path else None
        command = self._normalize_command(str(profile.get("startCommand") or ""))
        expected_ports = self._expected_ports(profile)
        ports = [{"port": port, "open": self._port_open("localhost", port)} for port in expected_ports]
        matching = self._matching_processes(repo_root, command) if repo_root and command else []
        app_reachable = self._url_reachable(health_url or app_url)
        agent_api_reachable = self._url_reachable(self._agent_api_health_url(agent_api_url))
        backend_reachable = self._url_reachable(backend_url) if backend_url else None
        ports_ok = all(item["open"] for item in ports) if ports else None
        runtime_state = self._runtime_state(app_reachable, agent_api_reachable, backend_reachable, ports_ok, matching)
        logger.info(
            "local app runtime health check result requestId=%s repoPath=%s appUrl=%s appReachable=%s agentApiUrl=%s agentApiReachable=%s backendHealthCheckUrl=%s backendHealthReachable=%s ports=%s runtimeState=%s",
            request_id,
            repo_path or "<none>",
            app_url or "<none>",
            app_reachable,
            agent_api_url or "<none>",
            agent_api_reachable,
            backend_url or "<none>",
            backend_reachable,
            ",".join(f"{item['port']}:{'open' if item['open'] else 'closed'}" for item in ports) or "<none>",
            runtime_state,
        )
        return {
            "requestId": request_id,
            "status": status_override or "ok",
            "appReachable": app_reachable,
            "agentApiReachable": agent_api_reachable,
            "backendHealthReachable": backend_reachable,
            "expectedPortsOpen": ports_ok,
            "ports": ports,
            "matchingProcesses": matching,
            "runtimeState": runtime_state,
            "repoPath": repo_path or None,
            "appUrl": app_url or None,
            "agentApiUrl": agent_api_url or None,
            "healthCheckUrl": health_url or None,
            "backendHealthCheckUrl": backend_url or None,
            "alreadyRunning": already_running,
        }

    def _wait_for_healthy(self, request_id: str, profile: dict[str, Any], start_result: dict[str, Any]) -> dict[str, Any]:
        deadline = time.time() + float(profile.get("recoveryTimeoutSeconds") or 60)
        last_status = self._status_result(request_id, profile)
        while time.time() < deadline:
            if start_result.get("hardStop"):
                return self._runtime_error(
                    request_id,
                    "hard_stop_required",
                    "Startup requires explicit approval before Hermes can continue.",
                    status=last_status,
                    diagnostics=start_result,
                )
            last_status = self._status_result(request_id, profile)
            if last_status["appReachable"] and last_status["agentApiReachable"] and last_status.get("backendHealthReachable") is not False:
                last_status["status"] = "ok"
                last_status["started"] = True
                last_status["diagnostics"] = start_result
                return last_status
            time.sleep(1)
        if not last_status["appReachable"]:
            return self._runtime_error(request_id, "health_check_failed", "Frontend health check failed.", status=last_status, diagnostics=start_result)
        if not last_status["agentApiReachable"]:
            return self._runtime_error(request_id, "agent_api_still_unreachable", "Agent API is still unreachable.", status=last_status, diagnostics=start_result)
        if last_status.get("backendHealthReachable") is False:
            return self._runtime_error(request_id, "backend_health_check_failed", "Backend health check failed.", status=last_status, diagnostics=start_result)
        return self._runtime_error(request_id, "runtime_recovery_timeout", "Runtime recovery timed out.", status=last_status, diagnostics=start_result)

    def _start_runtime_command(self, repo_root: Path, command: str) -> dict[str, Any]:
        key = f"{repo_root}:{command}"
        with self._lock:
            existing = self._processes.get(key)
            if existing and existing.poll() is None:
                return {"started": False, "alreadyRunning": True, "pid": existing.pid}
            argv = shlex.split(command)
            if not argv:
                return {"started": False, "error": "empty_command"}
            try:
                process = subprocess.Popen(
                    argv,
                    cwd=repo_root,
                    text=True,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    env=dict(os.environ),
                    start_new_session=True,
                )
            except Exception as exc:
                return {"started": False, "error": type(exc).__name__}
            self._processes[key] = process
        lines: list[str] = []
        result: dict[str, Any] = {"started": True, "pid": process.pid, "command": self._command_name(command), "outputTail": lines}

        def capture() -> None:
            try:
                assert process.stdout is not None
                for raw_line in process.stdout:
                    line = self._sanitize_log(raw_line.strip())
                    if not line:
                        continue
                    if len(lines) < 40:
                        lines.append(line[:500])
                    detected = self._detect_hard_stop(line)
                    if detected and not result.get("hardStop"):
                        result["hardStop"] = detected
                        try:
                            os.killpg(process.pid, signal.SIGTERM)
                        except Exception:
                            process.terminate()
            except Exception:
                pass

        thread = threading.Thread(target=capture, name=f"clawchat-local-app-recovery-{process.pid}", daemon=True)
        thread.start()
        time.sleep(1.5)
        if result.get("hardStop"):
            result["started"] = False
            result["outputTail"] = lines[-20:]
            return result
        if process.poll() is not None:
            return {"started": False, "exitCode": process.returncode, "outputTail": lines[-20:], "pid": process.pid}
        result["outputTail"] = lines[-20:]
        return result

    def _detect_hard_stop(self, line: str) -> dict[str, str] | None:
        for reason, pattern in self.HARD_STOP_PATTERNS.items():
            if pattern.search(line):
                return {"reason": reason, "line": line[:500]}
        return None

    def _validate_start_command(self, repo_root: Path, command: str) -> dict[str, Any]:
        normalized = self._normalize_command(command)
        base: dict[str, Any] = {
            "allowed": False,
            "command": normalized,
            "executable": None,
            "args": [],
            "scriptName": None,
            "reason": "not_checked",
            "code": "start_command_missing",
        }
        if not normalized:
            return {**base, "reason": "empty_command", "message": "runtimeProfile.startCommand is required"}
        if LOCAL_APP_COMMAND_SHELL_META_RE.search(normalized):
            return {
                **base,
                "reason": "shell_metacharacter",
                "message": "Start command contains shell chaining or redirection.",
            }
        try:
            argv = shlex.split(normalized, posix=True)
        except ValueError:
            return {**base, "reason": "parse_failed", "message": "Start command could not be parsed safely."}
        if not argv:
            return {**base, "reason": "empty_command", "message": "runtimeProfile.startCommand is required"}
        executable = argv[0]
        args = argv[1:]
        base.update({"executable": executable, "args": args})
        lowered_tokens = {part.lower() for part in argv}
        if lowered_tokens & LOCAL_APP_REJECTED_COMMAND_TOKENS:
            return {
                **base,
                "reason": "hard_stop_command_token",
                "code": "hard_stop_required",
                "message": "Start command includes install, migration, reset, deploy, or other blocked operation.",
            }
        script_name = self._package_manager_script_name(executable, args)
        if not script_name:
            return {
                **base,
                "reason": "unsupported_command_shape",
                "message": "Only package-manager dev scripts are allowed for local app recovery.",
            }
        base["scriptName"] = script_name
        scripts = self._package_scripts(repo_root)
        if scripts is None or script_name not in scripts:
            return {
                **base,
                "reason": "missing_package_script",
                "message": f"package.json scripts does not include {script_name!r}.",
            }
        if not self._script_body_is_safe(str(scripts.get(script_name) or "")):
            return {
                **base,
                "reason": "blocked_package_script_body",
                "code": "hard_stop_required",
                "message": f"package.json script {script_name!r} contains a blocked operation.",
            }
        return {**base, "allowed": True, "reason": "package_manager_script_allowed", "code": None, "message": "allowed"}

    def _package_manager_script_name(self, executable: str, args: list[str]) -> str | None:
        if executable == "pnpm":
            if args == ["dev"]:
                return "dev"
            if len(args) == 2 and args[0] == "run" and args[1]:
                return args[1]
            return None
        if executable == "npm":
            if len(args) == 2 and args[0] == "run" and args[1]:
                return args[1]
            return None
        if executable == "yarn":
            if args == ["dev"]:
                return "dev"
            if len(args) == 2 and args[0] == "run" and args[1]:
                return args[1]
            return None
        return None

    def _package_scripts(self, repo_root: Path) -> dict[str, Any] | None:
        package_json = repo_root / "package.json"
        if not package_json.exists():
            return None
        try:
            data = json.loads(package_json.read_text(encoding="utf-8"))
        except Exception:
            return None
        scripts = data.get("scripts") if isinstance(data, dict) else None
        return scripts if isinstance(scripts, dict) else None

    def _script_body_is_safe(self, script: str) -> bool:
        if not script.strip():
            return False
        lowered = script.lower()
        if any(token in lowered for token in (" install", " migrate", " reset", " deploy", "prisma migrate", "convex deploy")):
            return False
        return True

    def _configured_app_port_occupied(self, status: dict[str, Any]) -> bool:
        for port in status.get("ports") or []:
            if isinstance(port, dict) and port.get("open") and port.get("port") == 3052:
                return True
        return False

    def _runtime_error(
        self,
        request_id: str,
        code: str,
        message: str,
        *,
        status: dict[str, Any] | None = None,
        diagnostics: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        result = {
            "requestId": request_id,
            "status": "failed",
            "ok": False,
            "error": {"code": code, "message": message},
        }
        if status:
            result["runtimeStatus"] = status
        if diagnostics:
            result["diagnostics"] = _redact_secret_fields(diagnostics)
        return result

    def _expected_ports(self, profile: dict[str, Any]) -> list[int]:
        ports: list[int] = []
        raw = profile.get("expectedPorts")
        if isinstance(raw, list):
            for item in raw:
                try:
                    ports.append(int(item))
                except Exception:
                    pass
        for key in ("appUrl", "agentApiUrl", "healthCheckUrl", "backendHealthCheckUrl"):
            parsed = urllib.parse.urlparse(str(profile.get(key) or ""))
            if parsed.port:
                ports.append(parsed.port)
        return sorted(set(port for port in ports if 0 < port < 65536))

    def _agent_api_health_url(self, agent_api_url: str) -> str | None:
        if not agent_api_url:
            return None
        base = agent_api_url.rstrip("/")
        if base.endswith("/settings") or "/settings?" in base:
            return base
        return f"{base}/settings?contractVersion=2026-03-18"

    def _runtime_state(self, app: bool, agent: bool, backend: bool | None, ports_ok: bool | None, processes: list[dict[str, Any]]) -> str:
        checks = [app, agent]
        if backend is not None:
            checks.append(backend)
        if ports_ok is not None:
            checks.append(ports_ok)
        if all(checks):
            return "running"
        if any(checks) or processes:
            return "partial"
        return "stopped"

    def _matching_processes(self, repo_root: Path | None, command: str) -> list[dict[str, Any]]:
        if not repo_root or not command:
            return []
        matches: list[dict[str, Any]] = []
        try:
            result = subprocess.run(["ps", "-eo", "pid=,args="], text=True, capture_output=True, timeout=3, check=False)
        except Exception:
            return matches
        needles = self._process_needles(command)
        for line in result.stdout.splitlines():
            if not all(needle in line for needle in needles):
                continue
            pid_text = line.strip().split(None, 1)[0] if line.strip() else ""
            try:
                pid = int(pid_text)
                cwd = Path(f"/proc/{pid}/cwd").resolve()
                if cwd == repo_root:
                    matches.append({"pid": pid, "command": self._sanitize_log(line.strip().split(None, 1)[1])[:300]})
            except Exception:
                continue
        return matches

    def _port_open(self, host: str, port: int) -> bool:
        try:
            with socket.create_connection((host, port), timeout=1):
                return True
        except OSError:
            return False

    def _command_name(self, command: str) -> str:
        parts = shlex.split(command) if command else []
        return parts[0] if parts else "<none>"

    def prepare_for_run(self, run: ActiveRun, payload: dict[str, Any]) -> str | None:
        context = self._runtime_context(payload)
        if not context:
            return None

        repo_root = self._resolve_repo_root(context, payload)
        if not repo_root:
            message = "Local app runtime: no repo path was provided by ClawChat."
            self._emit_status(run, "local_app.repo_missing", message)
            return message

        config, config_error = self._read_clawchat_config(repo_root)
        app_name = self._pick_string(context, "displayName", "appName", "name") or self._config_app_name(config) or "local app"
        app_url = self._pick_string(context, "appUrl", "localAppUrl", "url") or self._config_local_string(config, "appUrl")
        startup_order = self._startup_order(config)
        may_start = self._may_start_app(context)

        status = self._inspect_status(repo_root, app_url, startup_order)
        self._emit_status(run, "local_app.status", self._status_message(app_name, status, config_error))

        started_commands: list[str] = []
        refused_commands: list[str] = []
        if not status["appUrlReachable"] and may_start:
            for command in startup_order:
                normalized = self._normalize_command(command)
                if normalized not in LOCAL_APP_ALLOWED_START_COMMANDS:
                    refused_commands.append(normalized)
                    self._emit_status(
                        run,
                        "local_app.command_refused",
                        f"Refused local app command outside allowlist: {normalized}",
                    )
                    continue
                if self._command_appears_running(repo_root, normalized):
                    self._emit_status(run, "local_app.already_running", f"{normalized} already appears to be running")
                    continue
                if self._start_command(run, repo_root, normalized):
                    started_commands.append(normalized)

            if app_url and started_commands:
                self._wait_for_app_url(run, app_url, timeout_s=45)
            status = self._inspect_status(repo_root, app_url, startup_order)
            self._emit_status(run, "local_app.status_after_start", self._status_message(app_name, status, None))
        elif not status["appUrlReachable"] and not may_start:
            self._emit_status(
                run,
                "local_app.start_not_allowed",
                f"{app_name} is offline and this dispatch did not permit the Hermes bridge to start local services.",
            )

        return self._agent_context(app_name, repo_root, app_url, status, may_start, started_commands, refused_commands, config_error)

    def _runtime_context(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        candidates = [
            payload.get("marketplaceRuntimeContext"),
            payload.get("runtimeContext"),
            payload.get("localAppRuntimeContext"),
        ]
        metadata = payload.get("dispatchMetadata")
        if isinstance(metadata, dict):
            candidates.extend([
                metadata.get("marketplaceRuntimeContext"),
                metadata.get("runtimeContext"),
                metadata.get("localAppRuntimeContext"),
            ])
        config_metadata = payload.get("configMetadata")
        if isinstance(config_metadata, dict):
            candidates.extend([
                config_metadata.get("marketplaceRuntimeContext"),
                config_metadata.get("runtimeContext"),
                config_metadata.get("localAppRuntimeContext"),
            ])
        for candidate in candidates:
            if isinstance(candidate, dict):
                return candidate
        return None

    def _resolve_repo_root(self, context: dict[str, Any], payload: dict[str, Any]) -> Path | None:
        keys = {
            "repoPath",
            "localRepoPath",
            "appRepoPath",
            "repositoryPath",
            "observedRepoPath",
            "repoRoot",
        }
        values: list[str] = []
        self._collect_strings_for_keys(context, keys, values)
        workspace_root = str(payload.get("workspaceRoot") or "").strip()
        if workspace_root:
            values.append(workspace_root)

        for value in values:
            path = Path(value).expanduser()
            if not path.is_absolute() and workspace_root:
                path = Path(workspace_root).expanduser() / path
            try:
                resolved = path.resolve()
            except OSError:
                continue
            if resolved.is_dir():
                return resolved
        return None

    def _read_clawchat_config(self, repo_root: Path) -> tuple[dict[str, Any] | None, str | None]:
        path = repo_root / ".clawchat" / "clawchat.config.json"
        if not path.exists():
            return None, f"Missing {path.relative_to(repo_root).as_posix()}"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data, None
            return None, "clawchat.config.json is not a JSON object"
        except Exception as exc:
            return None, f"Failed to read clawchat.config.json: {exc}"

    def _startup_order(self, config: dict[str, Any] | None) -> list[str]:
        local = config.get("local") if isinstance(config, dict) else None
        if not isinstance(local, dict):
            return []
        raw_order = local.get("startupOrder")
        commands: list[str] = []
        if isinstance(raw_order, list):
            commands.extend(str(item).strip() for item in raw_order if str(item).strip())
        for key in ("backendStartCommand", "convexDevCommand", "startCommand", "devCommand"):
            value = local.get(key)
            if isinstance(value, str) and value.strip():
                commands.append(value.strip())
        deduped: list[str] = []
        for command in commands:
            normalized = self._normalize_command(command)
            if normalized and normalized not in deduped:
                deduped.append(normalized)
        return deduped

    def _inspect_status(self, repo_root: Path, app_url: str | None, commands: list[str]) -> dict[str, Any]:
        app_reachable = self._url_reachable(app_url) if app_url else False
        port_open = self._url_port_open(app_url) if app_url else False
        running_commands = [
            command for command in commands
            if self._command_appears_running(repo_root, command)
        ]
        return {
            "repoExists": repo_root.is_dir(),
            "repoPath": str(repo_root),
            "appUrl": app_url,
            "appUrlReachable": app_reachable,
            "appPortOpen": port_open,
            "runningCommands": running_commands,
        }

    def _start_command(self, run: ActiveRun, repo_root: Path, command: str) -> bool:
        key = f"{repo_root}:{command}"
        with self._lock:
            existing = self._processes.get(key)
            if existing and existing.poll() is None:
                self._emit_status(run, "local_app.already_running", f"{command} is already managed by the Hermes bridge")
                return False

            argv = shlex.split(command)
            if not argv:
                return False
            log_path = get_hermes_home() / "clawchat" / "local_app_logs" / f"{_safe_segment(repo_root.name)}-{_safe_segment(command)}.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_file = log_path.open("a", encoding="utf-8", buffering=1)
            process = subprocess.Popen(
                argv,
                cwd=repo_root,
                text=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=dict(os.environ),
                start_new_session=True,
            )
            self._processes[key] = process
            self._emit_status(run, "local_app.starting", f"Started local app command: {command}")
            threading.Thread(
                target=self._capture_logs,
                args=(run, process, command, log_file),
                name=f"clawchat-local-app-{process.pid}",
                daemon=True,
            ).start()
            return True

    def _capture_logs(self, run: ActiveRun, process: subprocess.Popen[str], command: str, log_file: Any) -> None:
        try:
            assert process.stdout is not None
            for raw_line in process.stdout:
                line = self._sanitize_log(raw_line.strip())
                if not line:
                    continue
                log_file.write(line + "\n")
                if self._is_interesting_startup_line(line):
                    self._emit_status(run, "local_app.log", f"{command}: {line[:500]}")
            code = process.wait()
            self._emit_status(run, "local_app.process_exit", f"{command} exited with code {code}")
        except Exception as exc:
            logger.warning("local app log capture failed for %s: %s", command, exc)
        finally:
            try:
                log_file.close()
            except Exception:
                pass

    def _command_appears_running(self, repo_root: Path, command: str) -> bool:
        for process in self._processes.values():
            if process.poll() is None:
                try:
                    if process.args == shlex.split(command):
                        return True
                except Exception:
                    pass
        try:
            result = subprocess.run(
                ["ps", "-eo", "pid=,args="],
                text=True,
                capture_output=True,
                timeout=3,
                check=False,
            )
        except Exception:
            return False
        needles = self._process_needles(command)
        for line in result.stdout.splitlines():
            if not all(needle in line for needle in needles):
                continue
            pid_text = line.strip().split(None, 1)[0] if line.strip() else ""
            try:
                cwd = Path(f"/proc/{int(pid_text)}/cwd").resolve()
                if cwd == repo_root:
                    return True
            except Exception:
                continue
        return False

    def _process_needles(self, command: str) -> list[str]:
        if "convex dev" in command:
            return ["convex", "dev"]
        if command == "pnpm dev":
            return ["pnpm", "dev"]
        return command.split()

    def _wait_for_app_url(self, run: ActiveRun, app_url: str, timeout_s: int) -> None:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if self._url_reachable(app_url):
                self._emit_status(run, "local_app.ready", f"Local app URL is reachable: {app_url}")
                return
            time.sleep(2)
        self._emit_status(run, "local_app.not_ready", f"Local app URL did not become reachable within {timeout_s}s: {app_url}")

    def _url_reachable(self, app_url: str | None) -> bool:
        if not app_url:
            return False
        try:
            request = urllib.request.Request(app_url, method="HEAD")
            with urllib.request.urlopen(request, timeout=2) as response:
                return 200 <= response.status < 500
        except urllib.error.HTTPError as exc:
            return 200 <= exc.code < 500
        except Exception:
            try:
                request = urllib.request.Request(app_url, method="GET")
                with urllib.request.urlopen(request, timeout=2) as response:
                    return 200 <= response.status < 500
            except Exception:
                return False

    def _url_port_open(self, app_url: str | None) -> bool:
        if not app_url:
            return False
        parsed = urllib.parse.urlparse(app_url)
        host = parsed.hostname
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        if not host:
            return False
        try:
            with socket.create_connection((host, port), timeout=1):
                return True
        except OSError:
            return False

    def _may_start_app(self, context: dict[str, Any]) -> bool:
        true_keys = {
            "mayStartApp",
            "mayStartLocalApp",
            "canStartLocalApp",
            "hostMayStartApp",
            "runtimeHostMayStartApp",
            "allowStart",
            "allowStartLocalApp",
            "startAllowed",
            "localAppStartAllowed",
        }
        values: list[bool] = []
        self._collect_bools_for_keys(context, true_keys, values)
        return any(values)

    def _status_message(self, app_name: str, status: dict[str, Any], extra: str | None) -> str:
        parts = [
            f"{app_name} repo exists: {'yes' if status['repoExists'] else 'no'}",
            f"app URL: {status.get('appUrl') or 'not configured'}",
            f"URL reachable: {'yes' if status['appUrlReachable'] else 'no'}",
            f"port open: {'yes' if status['appPortOpen'] else 'no'}",
            f"running commands: {', '.join(status['runningCommands']) if status['runningCommands'] else 'none detected'}",
        ]
        if extra:
            parts.append(extra)
        return "; ".join(parts)

    def _agent_context(
        self,
        app_name: str,
        repo_root: Path,
        app_url: str | None,
        status: dict[str, Any],
        may_start: bool,
        started_commands: list[str],
        refused_commands: list[str],
        config_error: str | None,
    ) -> str:
        lines = [
            "[ClawChat local app runtime]",
            f"App: {app_name}",
            f"Repo path: {repo_root}",
            f"Repo exists: {'yes' if status['repoExists'] else 'no'}",
            f"App URL: {app_url or 'not configured'}",
            f"App URL reachable: {'yes' if status['appUrlReachable'] else 'no'}",
            f"App port open: {'yes' if status['appPortOpen'] else 'no'}",
            f"Detected running commands: {', '.join(status['runningCommands']) if status['runningCommands'] else 'none'}",
            f"Bridge was allowed to start app: {'yes' if may_start else 'no'}",
        ]
        if started_commands:
            lines.append(f"Started commands: {', '.join(started_commands)}")
        if refused_commands:
            lines.append(f"Refused commands: {', '.join(refused_commands)}")
        if config_error:
            lines.append(f"Config note: {config_error}")
        lines.append("Do not print secrets or environment values. Do not run destructive commands, installs, migrations, resets, bulk writes, or arbitrary shell commands.")
        return "\n".join(lines)

    def _emit_status(self, run: ActiveRun, code: str, message: str) -> None:
        run.emit({
            "type": "run.status",
            "dispatchId": run.dispatch_id,
            "code": code,
            "message": self._sanitize_log(message)[:1000],
        })

    def _sanitize_log(self, text: str) -> str:
        text = self.BEARER_RE.sub(r"\1[REDACTED_BEARER_TOKEN]", text)
        text = self.SECRET_TEXT_RE.sub(
            lambda match: f"{match.group(1)}{match.group(2)}{match.group(3)}[REDACTED_SECRET_VALUE]{match.group(5)}",
            text,
        )
        return text

    def _is_interesting_startup_line(self, line: str) -> bool:
        lowered = line.lower()
        return any(token in lowered for token in ("ready", "started", "local:", "localhost", "compiled", "convex", "next.js", "error", "failed"))

    def _config_local_string(self, config: dict[str, Any] | None, key: str) -> str | None:
        local = config.get("local") if isinstance(config, dict) else None
        if isinstance(local, dict) and isinstance(local.get(key), str) and local.get(key).strip():
            return str(local.get(key)).strip()
        return None

    def _config_app_name(self, config: dict[str, Any] | None) -> str | None:
        if not isinstance(config, dict):
            return None
        for key in ("displayName", "name", "appSlug"):
            value = config.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    def _pick_string(self, obj: dict[str, Any], *keys: str) -> str | None:
        values: list[str] = []
        self._collect_strings_for_keys(obj, set(keys), values)
        return values[0] if values else None

    def _collect_strings_for_keys(self, value: Any, keys: set[str], out: list[str]) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key in keys and isinstance(child, str) and child.strip():
                    out.append(child.strip())
                elif isinstance(child, (dict, list)):
                    self._collect_strings_for_keys(child, keys, out)
        elif isinstance(value, list):
            for item in value:
                self._collect_strings_for_keys(item, keys, out)

    def _collect_bools_for_keys(self, value: Any, keys: set[str], out: list[bool]) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key in keys and isinstance(child, bool):
                    out.append(child)
                elif isinstance(child, (dict, list)):
                    self._collect_bools_for_keys(child, keys, out)
        elif isinstance(value, list):
            for item in value:
                self._collect_bools_for_keys(item, keys, out)

    def _normalize_command(self, command: str) -> str:
        return " ".join(command.strip().split())


_SCOPED_REGISTRY_INSTALL_LOCK = threading.Lock()


def _ensure_scoped_tool_registry(registry: Any) -> str:
    """Install a context-local registry overlay for pinned Hermes releases.

    Hermes v2026.7.7.2 serializes ordinary registry mutations but does not yet
    expose a scoped registration API. Relay dispatches can overlap and may use
    the same callable names with different handlers, so mutating the global
    registry would route one workspace's call through another dispatch. This
    compatibility layer keeps scoped entries in a ContextVar; Hermes propagates
    ContextVars into its parallel tool worker threads.

    A future Hermes release can provide its own ``scoped_tools`` method. The
    bridge detects and uses that native implementation without replacing it.
    """
    if getattr(registry, "_relay_scoped_tools_compat", False):
        return "relay_context_overlay"
    if callable(getattr(registry, "scoped_tools", None)):
        return "hermes_native"

    with _SCOPED_REGISTRY_INSTALL_LOCK:
        if getattr(registry, "_relay_scoped_tools_compat", False):
            return "relay_context_overlay"
        if callable(getattr(registry, "scoped_tools", None)):
            return "hermes_native"

        original_get_entry = registry.get_entry
        original_snapshot_entries = registry._snapshot_entries
        scope_stack: ContextVar[tuple[dict[str, Any], ...]] = ContextVar(
            f"relay_scoped_registry_{id(registry)}",
            default=(),
        )
        metadata_stack: ContextVar[tuple[dict[str, Any], ...]] = ContextVar(
            f"relay_scoped_registry_metadata_{id(registry)}",
            default=(),
        )

        def scoped_get_entry(_self: Any, name: str) -> Any:
            for entries in reversed(scope_stack.get()):
                entry = entries.get(name)
                if entry is not None:
                    return entry
            return original_get_entry(name)

        def scoped_snapshot_entries(_self: Any) -> list[Any]:
            merged = {entry.name: entry for entry in original_snapshot_entries()}
            for entries in scope_stack.get():
                merged.update(
                    {
                        name: entry
                        for name, entry in entries.items()
                        if MarketplaceRuntimeToolProxy.TOOL_NAME_PATTERN.fullmatch(name)
                    }
                )
            return list(merged.values())

        def active_scope_metadata(_self: Any) -> dict[str, Any]:
            active = metadata_stack.get()
            return dict(active[-1]) if active else {}

        def update_active_scope_metadata(_self: Any, **updates: Any) -> None:
            active = metadata_stack.get()
            if not active:
                return
            next_metadata = {**active[-1], **updates}
            metadata_stack.set((*active[:-1], next_metadata))

        @contextmanager
        def scoped_tools(
            self: Any,
            entries: dict[str, Any],
            *,
            metadata: dict[str, Any] | None = None,
        ):
            if not isinstance(entries, dict) or not entries:
                raise RuntimeError("Relay scoped tools require a non-empty entry mapping")
            malformed = sorted(
                name
                for name, entry in entries.items()
                if not isinstance(name, str) or not name or getattr(entry, "name", None) != name
            )
            if malformed:
                raise RuntimeError(
                    "Relay scoped tool entries have mismatched names: "
                    + ", ".join(malformed)
                )
            built_in_names = {entry.name for entry in original_snapshot_entries()}
            collisions = sorted(set(entries).intersection(built_in_names))
            if collisions:
                raise RuntimeError(
                    "Relay scoped tools cannot shadow Hermes tools: "
                    + ", ".join(collisions)
                )

            token = scope_stack.set((*scope_stack.get(), dict(entries)))
            metadata_token = metadata_stack.set(
                (*metadata_stack.get(), dict(metadata or {})),
            )
            with self._lock:
                self._generation += 1
            try:
                yield active_scope_metadata(self)
            finally:
                metadata_stack.reset(metadata_token)
                scope_stack.reset(token)
                with self._lock:
                    self._generation += 1

        registry.get_entry = MethodType(scoped_get_entry, registry)
        registry._snapshot_entries = MethodType(scoped_snapshot_entries, registry)
        registry.scoped_tools = MethodType(scoped_tools, registry)
        registry.active_scope_metadata = MethodType(active_scope_metadata, registry)
        registry.update_active_scope_metadata = MethodType(
            update_active_scope_metadata,
            registry,
        )
        registry._relay_scoped_tools_compat = True
        registry._relay_scoped_tool_stack = scope_stack
        registry._relay_scoped_tool_metadata_stack = metadata_stack

        # Hermes memoizes model-visible schemas by the process-global registry
        # generation. Concurrent Relay scopes need a context-local catalogue,
        # so bypass that memo only while an overlay is active. Ordinary Hermes
        # sessions keep the native cached path.
        import model_tools

        original_get_tool_definitions = model_tools.get_tool_definitions
        compute_tool_definitions = model_tools._compute_tool_definitions

        def scoped_get_tool_definitions(
            enabled_toolsets: list[str] | None = None,
            disabled_toolsets: list[str] | None = None,
            quiet_mode: bool = False,
            skip_tool_search_assembly: bool = False,
        ) -> list[dict[str, Any]]:
            if scope_stack.get():
                return compute_tool_definitions(
                    enabled_toolsets,
                    disabled_toolsets,
                    quiet_mode,
                    skip_tool_search_assembly=skip_tool_search_assembly,
                )
            return original_get_tool_definitions(
                enabled_toolsets=enabled_toolsets,
                disabled_toolsets=disabled_toolsets,
                quiet_mode=quiet_mode,
                skip_tool_search_assembly=skip_tool_search_assembly,
            )

        model_tools.get_tool_definitions = scoped_get_tool_definitions
        try:
            import run_agent

            run_agent.get_tool_definitions = scoped_get_tool_definitions
        except ImportError:
            pass

    return "relay_context_overlay"


class MarketplaceRuntimeToolProxy:
    TOOLSET = "clawchat_marketplace"
    TOOL_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
    CAMEL_BOUNDARY_PATTERN = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")

    def __init__(self, bridge: "ClawChatHermesBridge") -> None:
        self.bridge = bridge
        self._scope_lock = threading.Lock()
        self._active_scope_counts: dict[str, int] = {}

    def tools_from_payload(self, payload: dict[str, Any]) -> list[MarketplaceRuntimeTool]:
        tools: list[MarketplaceRuntimeTool] = []
        seen: set[str] = set()
        dispatch_id = payload.get("dispatchId")
        sources = self._tool_sources(payload)
        context_exists = any(source_name == "marketplaceRuntimeContext.tools" for source_name, _items in sources)
        logger.info(
            "marketplace runtime context diagnostics dispatchId=%s marketplaceRuntimeContextExists=%s",
            dispatch_id,
            context_exists,
        )

        for source_name, raw_tools in sources:
            names = self._names_for_log(raw_tools)
            logger.info(
                "marketplace runtime source dispatchId=%s source=%s count=%s names=%s",
                dispatch_id,
                source_name,
                len(raw_tools),
                ", ".join(names) if names else "<none>",
            )
            for item in raw_tools:
                tool = self._tool_from_descriptor(item, payload)
                if not tool or tool.callable_name in seen:
                    continue
                seen.add(tool.callable_name)
                tools.append(tool)

        logger.info(
            "final registered marketplace tool candidates dispatchId=%s count=%s names=%s",
            dispatch_id,
            len(tools),
            ", ".join(f"{tool.name}->{tool.callable_name}" for tool in tools) if tools else "<none>",
        )
        return tools

    def _tool_sources(self, payload: dict[str, Any]) -> list[tuple[str, list[Any]]]:
        sources: list[tuple[str, list[Any]]] = []
        context = self._runtime_context(payload)
        if isinstance(context, dict):
            raw_tools = context.get("tools")
            sources.append(("marketplaceRuntimeContext.tools", raw_tools if isinstance(raw_tools, list) else []))
        else:
            sources.append(("marketplaceRuntimeContext.tools", []))

        for key in ("marketplaceTools", "availableMarketplaceTools"):
            raw_tools = payload.get(key)
            sources.append((key, raw_tools if isinstance(raw_tools, list) else []))

        metadata = payload.get("dispatchMetadata")
        if isinstance(metadata, dict):
            for key in ("marketplaceTools", "availableMarketplaceTools"):
                raw_tools = metadata.get(key)
                if isinstance(raw_tools, list):
                    sources.append((f"dispatchMetadata.{key}", raw_tools))

        config_metadata = payload.get("configMetadata")
        if isinstance(config_metadata, dict):
            for key in ("marketplaceTools", "availableMarketplaceTools"):
                raw_tools = config_metadata.get(key)
                if isinstance(raw_tools, list):
                    sources.append((f"configMetadata.{key}", raw_tools))
        return sources

    def _names_for_log(self, raw_tools: list[Any]) -> list[str]:
        names: list[str] = []
        for item in raw_tools:
            if isinstance(item, dict):
                name = str(
                    item.get("functionName")
                    or item.get("function_name")
                    or item.get("name")
                    or item.get("toolName")
                    or ""
                ).strip()
            else:
                name = str(item or "").strip()
            if name:
                names.append(name)
        return names

    def _tool_from_descriptor(self, item: Any, payload: dict[str, Any]) -> MarketplaceRuntimeTool | None:
        if isinstance(item, str):
            item = {"name": item}
        if not isinstance(item, dict):
            return None
        function_name = str(item.get("functionName") or item.get("function_name") or "").strip()
        name = str(item.get("name") or item.get("toolName") or function_name or "").strip()
        route_tool_name = self._route_tool_name(name=name, function_name=function_name)
        execution_url = str(
            item.get("executionUrl")
            or item.get("execution_url")
            or item.get("url")
            or item.get("route")
            or item.get("path")
            or ""
        ).strip()
        if not name:
            return None
        documented_execution_url = self._fallback_execution_url(name, payload, route_tool_name)
        if documented_execution_url:
            if execution_url and execution_url != documented_execution_url:
                logger.info(
                    "using documented marketplace execution route dispatchId=%s tool=%s descriptorRoute=%s route=%s",
                    payload.get("dispatchId"),
                    name,
                    _safe_log_url(execution_url),
                    _safe_log_url(documented_execution_url),
                )
            execution_url = documented_execution_url
        elif not execution_url:
            execution_url = self._fallback_execution_url(name, payload, route_tool_name)
            if execution_url:
                logger.info(
                    "derived marketplace execution route dispatchId=%s tool=%s route=%s",
                    payload.get("dispatchId"),
                    name,
                    _safe_log_url(execution_url),
                )
        if not execution_url:
            logger.warning(
                "skipping marketplace tool without execution route dispatchId=%s tool=%s descriptorKeys=%s",
                payload.get("dispatchId"),
                name,
                ",".join(sorted(str(key) for key in item.keys())),
            )
            return None
        input_schema = item.get("inputSchema") or item.get("input_schema") or item.get("parameters") or {}
        if not isinstance(input_schema, dict):
            input_schema = {}
        input_schema = self._normalized_parameters(input_schema)
        callable_name = self._callable_name(function_name or route_tool_name or name)
        aliases = self._aliases_for_tool(name=name, function_name=function_name, callable_name=callable_name)
        base_description = str(item.get("description") or f"Call ClawChat marketplace tool {name}").strip()
        description = (
            f"{base_description}\n"
            f"ClawChat marketplace tool: {name}. Callable Hermes tool name: {callable_name}."
        )
        return MarketplaceRuntimeTool(
            name=name,
            callable_name=callable_name,
            description=description,
            input_schema=_strip_secret_schema_fields(input_schema),
            execution_url=execution_url,
            method=str(item.get("method") or item.get("httpMethod") or "POST").upper(),
            aliases=aliases,
        )

    def _aliases_for_tool(self, *, name: str, function_name: str, callable_name: str) -> tuple[str, ...]:
        normalized = re.sub(r"[^a-z0-9]+", "_", " ".join([name, function_name, callable_name]).lower())
        if "linkcrest" not in normalized or "agent" not in normalized or "api" not in normalized:
            return ()
        aliases = ["linkcrest.agentApi", "linkcrest_agent_api", "linkcrest-agent-api", "agentApi"]
        return tuple(alias for alias in aliases if alias != callable_name)

    def _fallback_execution_url(self, name: str, payload: dict[str, Any], route_tool_name: str | None = None) -> str:
        dispatch_id = str(payload.get("dispatchId") or "").strip()
        if not dispatch_id:
            return ""
        app_slug = self._app_slug(name)
        tool_name = route_tool_name or self._route_tool_name(name=name, function_name="")
        if not app_slug or not tool_name:
            return ""
        app_slug = urllib.parse.quote(app_slug, safe="")
        tool_name = urllib.parse.quote(tool_name, safe="")
        return f"/api/v1/bridge/runtime-dispatches/{dispatch_id}/marketplace-tools/{app_slug}/{tool_name}"

    def _app_slug(self, name: str) -> str:
        if "." in name:
            return name.split(".", 1)[0].strip()
        if "_" in name:
            return name.split("_", 1)[0].strip()
        return ""

    def _route_tool_name(self, *, name: str, function_name: str) -> str:
        if function_name:
            return function_name
        if "." in name:
            app_slug, dotted_tool_name = name.split(".", 1)
            snake_tool_name = self._camel_to_snake(dotted_tool_name)
            return f"{app_slug}_{snake_tool_name}" if app_slug and snake_tool_name else ""
        return name

    def _camel_to_snake(self, value: str) -> str:
        value = value.replace("-", "_")
        value = self.CAMEL_BOUNDARY_PATTERN.sub("_", value)
        value = re.sub(r"[^A-Za-z0-9_]+", "_", value)
        return re.sub(r"_+", "_", value).strip("_").lower()

    def _callable_name(self, name: str) -> str:
        if self.TOOL_NAME_PATTERN.fullmatch(name):
            return name
        candidate = re.sub(r"[^A-Za-z0-9_-]", "_", name).strip("_")
        if not candidate:
            candidate = "marketplace_tool"
        if len(candidate) > 64:
            digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]
            candidate = f"{candidate[:55]}_{digest}"
        return candidate

    @contextmanager
    def registered_for_payload(self, payload: dict[str, Any], run: ActiveRun):
        tools = self.tools_from_payload(payload)
        stub_aliases, stub_code, stub_message = self._linkcrest_agent_api_stub(payload, tools)
        if not tools and not stub_aliases:
            self._log_dispatch_diagnostic(payload, run, tools, [])
            yield []
            return

        from tools.registry import ToolEntry, registry

        registered_names: list[str] = []
        scoped_entries: dict[str, ToolEntry] = {}
        try:
            for alias in stub_aliases:
                schema = {
                    "name": alias,
                    "description": (
                        "LinkCrest Agent API placeholder. Returns a structured error because "
                        "ClawChat did not attach a usable LinkCrest Agent API tool descriptor for this dispatch."
                    ),
                    "parameters": {"type": "object", "properties": {}, "additionalProperties": True},
                }
                scoped_entries[alias] = ToolEntry(
                    name=alias,
                    toolset=self.TOOLSET,
                    schema=schema,
                    handler=self._structured_unavailable_handler(stub_code, stub_message, run.dispatch_id),
                    check_fn=None,
                    requires_env=[],
                    is_async=False,
                    description=schema["description"],
                    emoji="",
                    max_result_size_chars=20_000,
                )
            for tool in tools:
                schema = {
                    "name": tool.callable_name,
                    "description": tool.description,
                    "parameters": tool.input_schema,
                }
                scoped_entries[tool.callable_name] = ToolEntry(
                    name=tool.callable_name,
                    toolset=self.TOOLSET,
                    schema=schema,
                    handler=self._handler_for(tool, run.dispatch_id),
                    check_fn=None,
                    requires_env=[],
                    is_async=False,
                    description=tool.description,
                    emoji="",
                    max_result_size_chars=200_000,
                )
                for alias in tool.aliases:
                    alias_schema = {**schema, "name": alias}
                    scoped_entries[alias] = ToolEntry(
                        name=alias,
                        toolset=self.TOOLSET,
                        schema=alias_schema,
                        handler=self._handler_for(tool, run.dispatch_id, alias=alias),
                        check_fn=None,
                        requires_env=[],
                        is_async=False,
                        description=f"{tool.description}\nAlias for {tool.callable_name}.",
                        emoji="",
                        max_result_size_chars=200_000,
                    )
                registered_names.append(tool.name)
            callable_names = sorted(scoped_entries)
            toolset_revision = self._toolset_revision(payload, tools, callable_names)
            payload["_marketplaceToolsetRevision"] = toolset_revision
            payload["_marketplaceCallableNames"] = callable_names
            scope_counts = self._increment_scope_counts(callable_names)
            logger.info(
                "marketplace scoped tools registered dispatchId=%s runtimeSessionId=%s externalAgentId=%s agentId=%s count=%s names=%s callableNames=%s toolsetRevision=%s activeScopeCounts=%s dispatcher=scoped",
                run.dispatch_id,
                run.runtime_session_id,
                run.external_agent_id,
                payload.get("agentId") or payload.get("agent_id") or "<none>",
                len(registered_names),
                ", ".join(registered_names) if registered_names else "<none>",
                ", ".join(callable_names) if callable_names else "<none>",
                toolset_revision,
                self._counts_for_log(scope_counts),
            )
            metadata = {
                "dispatchId": run.dispatch_id,
                "runtimeSessionId": run.runtime_session_id,
                "externalAgentId": run.external_agent_id,
                "agentId": payload.get("agentId") or payload.get("agent_id") or "",
                "descriptorNames": list(registered_names),
                "callableNames": list(callable_names),
                "toolsetRevision": toolset_revision,
            }
            scoped_registry_source = _ensure_scoped_tool_registry(registry)
            with registry.scoped_tools(scoped_entries, metadata=metadata):
                registered_callable_names = [
                    name for name in callable_names if registry.get_entry(name) is not None
                ]
                logger.info(
                    "marketplace scoped registry lookup dispatchId=%s runtimeSessionId=%s externalAgentId=%s callableNames=%s registeredCallableNames=%s missing=%s registrySource=%s",
                    run.dispatch_id,
                    run.runtime_session_id,
                    run.external_agent_id,
                    ", ".join(callable_names) if callable_names else "<none>",
                    ", ".join(registered_callable_names) if registered_callable_names else "<none>",
                    ", ".join(sorted(set(callable_names) - set(registered_callable_names))) or "<none>",
                    scoped_registry_source,
                )
                self._log_dispatch_diagnostic(payload, run, tools, registered_callable_names)
                yield registered_names
        finally:
            callable_names = sorted(locals().get("scoped_entries", {}).keys())
            if callable_names:
                remaining_counts = self._decrement_scope_counts(callable_names)
                logger.info(
                    "marketplace scoped tools cleanup dispatchId=%s runtimeSessionId=%s externalAgentId=%s callableNames=%s remainingScopeCounts=%s dispatcher=scoped",
                    run.dispatch_id,
                    run.runtime_session_id,
                    run.external_agent_id,
                    ", ".join(callable_names),
                    self._counts_for_log(remaining_counts),
                )

    def _toolset_revision(
        self,
        payload: dict[str, Any],
        tools: list[MarketplaceRuntimeTool],
        callable_names: list[str],
    ) -> str:
        parts = {
            "dispatchId": payload.get("dispatchId"),
            "runtimeSessionId": payload.get("runtimeSessionId"),
            "tools": [
                {
                    "name": tool.name,
                    "callableName": tool.callable_name,
                    "aliases": list(tool.aliases),
                    "route": tool.execution_url,
                }
                for tool in tools
            ],
            "callableNames": callable_names,
        }
        return hashlib.sha1(json.dumps(parts, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]

    def _increment_scope_counts(self, names: list[str]) -> dict[str, int]:
        with self._scope_lock:
            for name in names:
                self._active_scope_counts[name] = self._active_scope_counts.get(name, 0) + 1
            return dict(self._active_scope_counts)

    def _decrement_scope_counts(self, names: list[str]) -> dict[str, int]:
        with self._scope_lock:
            for name in names:
                current = self._active_scope_counts.get(name, 0)
                if current <= 1:
                    self._active_scope_counts.pop(name, None)
                else:
                    self._active_scope_counts[name] = current - 1
            return dict(self._active_scope_counts)

    def _counts_for_log(self, counts: dict[str, int]) -> str:
        return ",".join(f"{name}:{count}" for name, count in sorted(counts.items())) or "<none>"

    def _linkcrest_agent_api_stub(self, payload: dict[str, Any], tools: list[MarketplaceRuntimeTool]) -> tuple[tuple[str, ...], str, str]:
        if any(tool.callable_name == "linkcrest_agent_api" or "linkcrest.agentApi" in tool.aliases for tool in tools):
            return (), "", ""
        app_slug = str(payload.get("appSlug") or "").strip().lower()
        if app_slug not in {"local-linkcrest", "linkcrest"}:
            return (), "", ""
        status = self._linkcrest_agent_api_status(payload)
        if status == "connected_but_not_granted":
            return (
                ("linkcrest.agentApi", "linkcrest_agent_api", "linkcrest-agent-api", "agentApi"),
                "tool_not_granted",
                "LinkCrest Agent API is connected but not granted to this dispatch role.",
            )
        return (
            ("linkcrest.agentApi", "linkcrest_agent_api", "linkcrest-agent-api", "agentApi"),
            "tool_descriptor_missing",
            "ClawChat did not include a LinkCrest Agent API descriptor for this dispatch.",
        )

    def _linkcrest_agent_api_status(self, payload: dict[str, Any]) -> str:
        for container in (payload.get("toolStatusByCategory"), payload.get("toolAvailability"), payload.get("toolGrants")):
            if not isinstance(container, dict):
                continue
            value = container.get("linkcrest_agent_api") or container.get("linkcrest.agentApi")
            if isinstance(value, dict):
                connected = _bool_from_policy(value.get("connected", False))
                granted = _bool_from_policy(value.get("granted", True))
                if connected and not granted:
                    return "connected_but_not_granted"
                raw = str(value.get("status") or "").strip().lower()
                if raw:
                    return raw
            elif isinstance(value, str):
                return value.strip().lower()
        return "missing"

    def _structured_unavailable_handler(self, code: str, message: str, dispatch_id: str):
        def _handler(args: dict[str, Any], **_kwargs: Any) -> str:
            return json.dumps({
                "ok": False,
                "error": {
                    "code": code,
                    "message": message,
                    "dispatchId": dispatch_id,
                },
            }, ensure_ascii=False)

        return _handler

    def callable_name_for_identifier(self, name: str) -> str:
        route_tool_name = self._route_tool_name(name=name, function_name="")
        return self._callable_name(route_tool_name or name)

    def _log_dispatch_diagnostic(
        self,
        payload: dict[str, Any],
        run: ActiveRun,
        tools: list[MarketplaceRuntimeTool],
        registered_callable_names: list[str],
    ) -> None:
        raw_marketplace_tools = payload.get("marketplaceTools")
        marketplace_tools_present = isinstance(raw_marketplace_tools, list)
        marketplace_tools = raw_marketplace_tools if marketplace_tools_present else []
        marketplace_tool_names = self._descriptor_names_for_log(marketplace_tools)
        callable_names = [tool.callable_name for tool in tools]
        linkcrest_tools = [
            tool
            for tool in tools
            if tool.callable_name == "linkcrest_agent_api" or "linkcrest.agentApi" in tool.aliases
        ]
        linkcrest_aliases = sorted({alias for tool in linkcrest_tools for alias in (tool.callable_name, *tool.aliases)})
        registered_set = set(registered_callable_names)
        expected_x_tools = ["x_get_me", "x_get_user_tweets"]
        logger.info(
            "safe marketplace dispatch diagnostic dispatchId=%s externalAgentId=%s agentId=%s capabilities=%s marketplaceToolsPresent=%s marketplaceToolsCount=%s marketplaceToolNames=%s marketplaceToolFunctionNames=%s marketplaceCallableCandidates=%s registeredCallableNames=%s x_get_me_registered=%s x_get_user_tweets_registered=%s allCandidatesRegistered=%s",
            run.dispatch_id,
            payload.get("externalAgentId") or "<none>",
            payload.get("agentId") or payload.get("agent_id") or "<none>",
            ",".join(BRIDGE_CAPABILITIES),
            marketplace_tools_present,
            len(marketplace_tools),
            ",".join(marketplace_tool_names["names"]) if marketplace_tool_names["names"] else "<none>",
            ",".join(marketplace_tool_names["function_names"]) if marketplace_tool_names["function_names"] else "<none>",
            ",".join(callable_names) if callable_names else "<none>",
            ",".join(registered_callable_names) if registered_callable_names else "<none>",
            "x_get_me" in registered_set,
            "x_get_user_tweets" in registered_set,
            bool(callable_names) and set(callable_names).issubset(registered_set),
        )
        missing_expected = [name for name in expected_x_tools if name in callable_names and name not in registered_set]
        if missing_expected:
            logger.warning(
                "marketplace dispatch registration gap dispatchId=%s missingRegisteredCallableNames=%s",
                run.dispatch_id,
                ",".join(missing_expected),
            )
        logger.info(
            "linkcrest agent api dispatch diagnostic dispatchId=%s externalAgentId=%s agentId=%s role=%s appSlug=%s toolsReceivedCount=%s descriptorReceived=%s aliasesRegistered=%s omittedReason=%s",
            run.dispatch_id,
            payload.get("externalAgentId") or "<none>",
            payload.get("agentId") or payload.get("agent_id") or "<none>",
            payload.get("role") or payload.get("agentRole") or payload.get("dispatchRole") or "<none>",
            payload.get("appSlug") or "<none>",
            len(marketplace_tools),
            bool(linkcrest_tools),
            ",".join(alias for alias in linkcrest_aliases if alias in registered_set) or "<none>",
            "<none>" if linkcrest_tools else self._linkcrest_omitted_reason(payload, marketplace_tools),
        )

    def _linkcrest_omitted_reason(self, payload: dict[str, Any], marketplace_tools: list[Any]) -> str:
        if not isinstance(marketplace_tools, list) or not marketplace_tools:
            return "tool_descriptor_missing"
        app_slug = str(payload.get("appSlug") or "").strip() or "<none>"
        return f"descriptor_absent_from_payload_for_app:{app_slug}"

    def _descriptor_names_for_log(self, raw_tools: list[Any]) -> dict[str, list[str]]:
        names: list[str] = []
        function_names: list[str] = []
        for item in raw_tools:
            if isinstance(item, dict):
                name = str(item.get("name") or item.get("toolName") or "").strip()
                function_name = str(item.get("functionName") or item.get("function_name") or "").strip()
                if name:
                    names.append(name)
                if function_name:
                    function_names.append(function_name)
            elif isinstance(item, str) and item.strip():
                names.append(item.strip())
        return {"names": names, "function_names": function_names}

    def _runtime_context(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        candidates = [payload.get("marketplaceRuntimeContext")]
        metadata = payload.get("dispatchMetadata")
        if isinstance(metadata, dict):
            candidates.append(metadata.get("marketplaceRuntimeContext"))
        config_metadata = payload.get("configMetadata")
        if isinstance(config_metadata, dict):
            candidates.append(config_metadata.get("marketplaceRuntimeContext"))
        for candidate in candidates:
            if isinstance(candidate, dict):
                return candidate
        return None

    def _normalized_parameters(self, schema: dict[str, Any]) -> dict[str, Any]:
        if schema.get("type") == "function" and isinstance(schema.get("function"), dict):
            function_schema = schema["function"]
            params = function_schema.get("parameters")
            if isinstance(params, dict):
                return params
        if schema.get("type") == "object" or "properties" in schema or "required" in schema:
            return schema
        params = schema.get("parameters")
        if isinstance(params, dict):
            return params
        return {"type": "object", "properties": {}, "additionalProperties": True}

    def _handler_for(self, tool: MarketplaceRuntimeTool, dispatch_id: str, alias: str | None = None):
        def _handler(args: dict[str, Any], **_kwargs: Any) -> str:
            arguments = args if isinstance(args, dict) else {}
            return self.invoke(tool, dispatch_id, arguments, called_as=alias)

        return _handler

    def invoke(self, tool: MarketplaceRuntimeTool, dispatch_id: str, arguments: dict[str, Any], called_as: str | None = None) -> str:
        url = self._absolute_execution_url(tool.execution_url)
        method = tool.method or "POST"
        safe_route = _safe_log_url(url)
        logger.info(
            "marketplace tool invocation dispatchId=%s callableTool=%s originalTool=%s calledAs=%s route=%s",
            dispatch_id,
            tool.callable_name,
            tool.name,
            called_as or tool.callable_name,
            safe_route,
        )

        token = self.bridge.access_token
        if not token:
            return json.dumps({"error": "ClawChat bridge access token is not available for marketplace tool execution"}, ensure_ascii=False)

        body = json.dumps(arguments, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body if method not in {"GET", "HEAD"} else None,
            method=method,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                text = response.read().decode("utf-8", errors="replace")
                logger.info(
                    "marketplace tool success dispatchId=%s tool=%s route=%s status=%s",
                    dispatch_id,
                    tool.name,
                    safe_route,
                    response.status,
                )
                return self._tool_result_text(text)
        except urllib.error.HTTPError as exc:
            text = exc.read().decode("utf-8", errors="replace")
            logger.warning(
                "marketplace tool failure dispatchId=%s tool=%s route=%s status=%s",
                dispatch_id,
                tool.name,
                safe_route,
                exc.code,
            )
            return json.dumps({
                "error": f"ClawChat marketplace tool returned HTTP {exc.code}",
                "status": exc.code,
                "body": _redact_secret_fields(self._parse_json_or_text(text)),
            }, ensure_ascii=False)
        except Exception as exc:
            logger.warning(
                "marketplace tool failure dispatchId=%s tool=%s route=%s error=%s",
                dispatch_id,
                tool.name,
                safe_route,
                type(exc).__name__,
            )
            return json.dumps({"error": f"ClawChat marketplace tool execution failed: {type(exc).__name__}: {exc}"}, ensure_ascii=False)

    def _absolute_execution_url(self, execution_url: str) -> str:
        parsed = urllib.parse.urlparse(execution_url)
        if parsed.scheme and parsed.netloc:
            return execution_url
        return urllib.parse.urljoin(f"{self.bridge.config.api_url}/", execution_url.lstrip("/"))

    def _parse_json_or_text(self, text: str) -> Any:
        try:
            return json.loads(text)
        except Exception:
            return text[:1000]

    def _tool_result_text(self, text: str) -> str:
        parsed = self._parse_json_or_text(text)
        redacted = _redact_secret_fields(parsed)
        if isinstance(redacted, (dict, list)):
            return json.dumps(redacted, ensure_ascii=False)
        return json.dumps({"result": redacted}, ensure_ascii=False)


class HermesRunManager:
    def __init__(self, bridge: "ClawChatHermesBridge") -> None:
        self.bridge = bridge
        self.snapshot_store = SnapshotStore(get_hermes_home() / "clawchat" / "runtime_sessions")
        self.missing_tool_queue_path = get_hermes_home() / "clawchat" / "missing_tool_requests.jsonl"
        self.default_model = os.getenv("HERMES_DEFAULT_MODEL", "").strip() or _configured_default_model()
        self.default_disabled_toolsets = _csv(
            os.getenv("HERMES_WORKER_DISABLED_TOOLSETS", ",".join(DEFAULT_DISABLED_TOOLSETS))
        )
        self.fake_mode = os.getenv("HERMES_WORKER_FAKE_MODE", "").lower() in {"1", "true", "yes"}
        self._runs: dict[str, ActiveRun] = {}
        self._runs_lock = threading.Lock()
        self._lock_timeout_s = max(1.0, float(os.getenv("HERMES_AGENT_LOCK_TIMEOUT_S", "600")))
        self._lock_status_interval_s = max(1.0, float(os.getenv("HERMES_AGENT_LOCK_STATUS_INTERVAL_S", "30")))
        self._scoped_locks: dict[str, ScopedRunLock] = {}
        self._scoped_locks_guard = threading.Lock()
        self.local_app_runtime = LocalAppRuntimeManager()
        self.marketplace_proxy = MarketplaceRuntimeToolProxy(bridge)
        self.session_db = self._build_session_db()
        self.dispatch_state = DispatchStateStore(_config_dir() / "dispatch_state.json")

    def _build_session_db(self) -> Any | None:
        try:
            from hermes_state import SessionDB

            return SessionDB()
        except Exception:
            logger.warning("Hermes bridge session DB unavailable; session_search will not be attached", exc_info=True)
            return None

    def start(self, payload: dict[str, Any], *, source: str = "websocket") -> ActiveRun:
        dispatch_id = str(payload.get("dispatchId") or "")
        runtime_session_id = str(payload.get("runtimeSessionId") or "")
        if not dispatch_id or not runtime_session_id:
            raise ValueError("dispatchId and runtimeSessionId are required")
        external_agent_id = self._external_agent_id(payload)
        runtime_run_id = str(payload.get("runtimeRunId") or dispatch_id)
        dedupe_reason = self.dispatch_state.dedupe_reason(dispatch_id, runtime_run_id)
        if dedupe_reason:
            logger.info(
                "Hermes dispatch dedupe hit dispatchId=%s runtimeRunId=%s externalAgentId=%s source=%s reason=%s timestamp=%s",
                dispatch_id,
                runtime_run_id,
                external_agent_id,
                source,
                dedupe_reason,
                _now_iso(),
            )
            raise HermesDispatchDedupe(dedupe_reason)
        now = time.monotonic()
        received_monotonic = float(payload.get("_bridgeReceivedMonotonic") or now)
        run = ActiveRun(
            dispatch_id=dispatch_id,
            runtime_session_id=runtime_session_id,
            external_agent_id=external_agent_id,
            received_monotonic=received_monotonic,
            accepted_monotonic=now,
        )
        with self._runs_lock:
            if dispatch_id in self._runs:
                logger.info(
                    "Hermes dispatch dedupe hit dispatchId=%s runtimeRunId=%s externalAgentId=%s source=%s reason=active_run_map timestamp=%s",
                    dispatch_id,
                    runtime_run_id,
                    external_agent_id,
                    source,
                    _now_iso(),
                )
                raise HermesDispatchDedupe("active_run_map")
            self._runs[dispatch_id] = run
        self.dispatch_state.record_start(dispatch_id, runtime_run_id, external_agent_id, source=source)
        registered = external_agent_id in self.bridge.config.external_agent_ids
        logger.info(
            "Hermes dispatch timing dispatchId=%s runtimeRunId=%s externalAgentId=%s phase=accepted source=%s elapsedMs=%s routeRegistered=%s runtimeSessionIdPresent=%s threadIdPresent=%s marketplaceToolsCount=%s availableRuntimeToolsCount=%s",
            dispatch_id,
            runtime_run_id,
            external_agent_id,
            source,
            run.elapsed_ms(now),
            registered,
            bool(runtime_session_id),
            bool(payload.get("threadId")),
            len(payload.get("marketplaceTools") or []) if isinstance(payload.get("marketplaceTools"), list) else 0,
            len(payload.get("availableRuntimeTools") or []) if isinstance(payload.get("availableRuntimeTools"), list) else 0,
        )
        run.emit({
            "type": "run.started",
            "dispatchId": run.dispatch_id,
            "runtimeRunId": runtime_run_id,
            "metadata": {
                "source": source,
                "acceptedByHermesBridge": True,
                "acceptedElapsedMs": run.elapsed_ms(now),
            },
        })
        run.emit({
            "type": "run.received",
            "dispatchId": run.dispatch_id,
            "runtimeRunId": runtime_run_id,
            "metadata": {"source": source, "receivedByHermesBridge": True},
        })
        run.emit({
            "type": "run.accepted",
            "dispatchId": run.dispatch_id,
            "runtimeRunId": runtime_run_id,
            "metadata": {
                "source": source,
                "acceptedByHermesBridge": True,
                "acceptedElapsedMs": run.elapsed_ms(now),
            },
        })
        run.emit({
            "type": "run.queued",
            "dispatchId": run.dispatch_id,
            "runtimeRunId": runtime_run_id,
            "metadata": {
                "source": source,
                "queuedLocally": True,
                "queueElapsedMs": run.elapsed_ms(now),
            },
        })
        run.worker_thread = threading.Thread(
            target=self._run_agent,
            args=(run, payload),
            name=f"clawchat-hermes-{dispatch_id[:8]}",
            daemon=True,
        )
        run.worker_thread.start()
        asyncio.create_task(self._forward_events(run))
        return run

    def _external_agent_id(self, payload: dict[str, Any]) -> str:
        raw = str(payload.get("externalAgentId") or "agent").strip()
        if profile_name_from_external_id(raw):
            return raw
        return _safe_segment(raw) or "agent"

    def _lock_key_for_payload(self, payload: dict[str, Any]) -> str:
        # Keep one agent's workspace/session mutations in order without blocking
        # unrelated Hermes agents behind a long-running run.
        policy = self._autonomy_policy_from_payload(payload)
        if policy and (policy.get("sequentialDispatch") or policy.get("managerFirst")):
            team_key = (
                str(policy.get("teamId") or "").strip()
                or str(payload.get("threadId") or "").strip()
                or str(payload.get("runtimeSessionId") or "").strip()
                or "team"
            )
            app_key = str(policy.get("appSlug") or payload.get("appSlug") or "app").strip() or "app"
            return f"teamDispatch:{_safe_segment(app_key)}:{_safe_segment(team_key)}"
        return f"externalAgentId:{self._external_agent_id(payload)}"

    def _lock_key_for_run(self, run: ActiveRun) -> str:
        if run.execution_lock_key:
            return run.execution_lock_key
        return f"externalAgentId:{_safe_segment(run.external_agent_id) or 'agent'}"

    def _payload_timeout_s(self, payload: dict[str, Any]) -> float | None:
        try:
            timeout_ms = int(payload.get("timeoutMs") or 0)
        except Exception:
            timeout_ms = 0
        if timeout_ms <= 0:
            return None
        return max(1.0, timeout_ms / 1000.0)

    def _effective_lock_timeout_s(self, payload: dict[str, Any]) -> float:
        payload_timeout = self._payload_timeout_s(payload)
        if payload_timeout is None:
            return self._lock_timeout_s
        return max(1.0, min(self._lock_timeout_s, payload_timeout))

    def _force_release_run_lock(self, run: ActiveRun, *, reason: str) -> bool:
        lock_key = self._lock_key_for_run(run)
        with self._scoped_locks_guard:
            scoped = self._scoped_locks.get(lock_key)
            if scoped is None or scoped.owner_dispatch_id != run.dispatch_id:
                logger.info(
                    "Hermes execution lock force release skipped dispatchId=%s externalAgentId=%s lockKey=%s reason=%s ownerDispatchId=%s timestamp=%s",
                    run.dispatch_id,
                    run.external_agent_id,
                    lock_key,
                    reason,
                    scoped.owner_dispatch_id if scoped else None,
                    _now_iso(),
                )
                return False
            try:
                scoped.lock.release()
                released = True
            except RuntimeError:
                released = False
            scoped.owner_dispatch_id = None
            scoped.owner_external_agent_id = None
            scoped.owner_reason = None
            scoped.owner_acquired_at = None
            logger.warning(
                "Hermes execution lock force released dispatchId=%s externalAgentId=%s lockKey=%s reason=%s released=%s timestamp=%s",
                run.dispatch_id,
                run.external_agent_id,
                lock_key,
                reason,
                released,
                _now_iso(),
            )
            return released

    @contextmanager
    def _scoped_execution(self, run: ActiveRun, payload: dict[str, Any], *, reason: str):
        lock_key = self._lock_key_for_payload(payload)
        run.execution_lock_key = lock_key
        external_agent_id = self._external_agent_id(payload)
        lock_timeout_s = self._effective_lock_timeout_s(payload)
        with self._scoped_locks_guard:
            scoped = self._scoped_locks.get(lock_key)
            if scoped is None:
                scoped = ScopedRunLock()
                self._scoped_locks[lock_key] = scoped
            scoped.ref_count += 1

        wait_start = time.monotonic()
        wait_started_at = _now_iso()
        logger.info(
            "Hermes execution lock wait start dispatchId=%s externalAgentId=%s lockKey=%s reason=%s timestamp=%s timeoutS=%.1f",
            run.dispatch_id,
            external_agent_id,
            lock_key,
            reason,
            wait_started_at,
            lock_timeout_s,
        )
        acquired = False
        next_status_at = wait_start + self._lock_status_interval_s
        try:
            while True:
                if run.done.is_set() or run.cancel_requested:
                    logger.info(
                        "Hermes execution lock wait cancelled dispatchId=%s externalAgentId=%s lockKey=%s reason=%s waitedS=%.3f timestamp=%s",
                        run.dispatch_id,
                        external_agent_id,
                        lock_key,
                        reason,
                        time.monotonic() - wait_start,
                        _now_iso(),
                    )
                    if not run.terminal_event_type:
                        run.emit({"type": "run.cancelled", "dispatchId": run.dispatch_id})
                    raise HermesRunCancelled(f"cancelled while waiting for Hermes execution lock {lock_key}")
                remaining = lock_timeout_s - (time.monotonic() - wait_start)
                if remaining <= 0:
                    waited_s = time.monotonic() - wait_start
                    logger.warning(
                        "Hermes execution lock wait timed out dispatchId=%s externalAgentId=%s lockKey=%s reason=%s waitedS=%.3f timestamp=%s",
                        run.dispatch_id,
                        external_agent_id,
                        lock_key,
                        reason,
                        waited_s,
                        _now_iso(),
                    )
                    run.emit({
                        "type": "run.failed",
                        "dispatchId": run.dispatch_id,
                        "code": "execution_lock_timeout",
                        "message": "Hermes run waited too long for this agent's execution slot.",
                        "retryable": True,
                    })
                    raise HermesExecutionLockTimeout(f"timed out waiting for Hermes execution lock {lock_key}")
                acquired = scoped.lock.acquire(timeout=min(1.0, remaining))
                if acquired:
                    waited_s = time.monotonic() - wait_start
                    with self._scoped_locks_guard:
                        scoped.owner_dispatch_id = run.dispatch_id
                        scoped.owner_external_agent_id = external_agent_id
                        scoped.owner_reason = reason
                        scoped.owner_acquired_at = _now_iso()
                    logger.info(
                        "Hermes execution lock acquired dispatchId=%s externalAgentId=%s lockKey=%s reason=%s waitedS=%.3f timestamp=%s",
                        run.dispatch_id,
                        external_agent_id,
                        lock_key,
                        reason,
                        waited_s,
                        _now_iso(),
                    )
                    if run.done.is_set() or run.cancel_requested:
                        logger.info(
                            "Hermes execution lock acquired after cancellation dispatchId=%s externalAgentId=%s lockKey=%s reason=%s waitedS=%.3f timestamp=%s",
                            run.dispatch_id,
                            external_agent_id,
                            lock_key,
                            reason,
                            waited_s,
                            _now_iso(),
                        )
                        if not run.terminal_event_type:
                            run.emit({"type": "run.cancelled", "dispatchId": run.dispatch_id})
                        raise HermesRunCancelled(f"cancelled after acquiring Hermes execution lock {lock_key}")
                    break
                now = time.monotonic()
                if now >= next_status_at:
                    waited_s = now - wait_start
                    logger.info(
                        "Hermes execution lock still waiting dispatchId=%s externalAgentId=%s lockKey=%s reason=%s waitedS=%.3f timestamp=%s",
                        run.dispatch_id,
                        external_agent_id,
                        lock_key,
                        reason,
                        waited_s,
                        _now_iso(),
                    )
                    run.emit({
                        "type": "run.status",
                        "dispatchId": run.dispatch_id,
                        "code": "execution_lock_waiting",
                        "message": f"Waiting for prior {external_agent_id} run to finish.",
                    })
                    next_status_at = now + self._lock_status_interval_s
            yield
        finally:
            if acquired:
                released = False
                with self._scoped_locks_guard:
                    if scoped.owner_dispatch_id == run.dispatch_id:
                        try:
                            scoped.lock.release()
                            released = True
                        except RuntimeError:
                            released = False
                        scoped.owner_dispatch_id = None
                        scoped.owner_external_agent_id = None
                        scoped.owner_reason = None
                        scoped.owner_acquired_at = None
                logger.info(
                    "Hermes execution lock release dispatchId=%s externalAgentId=%s lockKey=%s reason=%s released=%s timestamp=%s",
                    run.dispatch_id,
                    external_agent_id,
                    lock_key,
                    reason,
                    released,
                    _now_iso(),
                )
            with self._scoped_locks_guard:
                scoped.ref_count -= 1
                if scoped.ref_count <= 0:
                    self._scoped_locks.pop(lock_key, None)

    def cancel(self, dispatch_id: str, message: str | None = None) -> bool:
        with self._runs_lock:
            run = self._runs.get(dispatch_id)
        if not run:
            logger.info(
                "Hermes cancel requested for unknown dispatchId=%s timestamp=%s",
                dispatch_id,
                _now_iso(),
            )
            return False
        run.cancel_requested = True
        logger.info(
            "Hermes cancel handling dispatchId=%s externalAgentId=%s hasAgent=%s done=%s timestamp=%s",
            dispatch_id,
            run.external_agent_id,
            bool(run.agent),
            run.done.is_set(),
            _now_iso(),
        )
        if run.agent and hasattr(run.agent, "interrupt"):
            try:
                run.agent.interrupt(message or "Cancelled by ClawChat")
            except Exception:
                logger.warning("failed to interrupt Hermes run %s", dispatch_id, exc_info=True)
        run.emit({"type": "run.status", "dispatchId": dispatch_id, "code": "cancel_requested", "message": "Cancel requested"})
        run.emit({"type": "run.cancelled", "dispatchId": dispatch_id})
        self.dispatch_state.record_terminal(
            dispatch_id,
            dispatch_id,
            run.external_agent_id,
            "run.cancelled",
        )
        self._force_release_run_lock(run, reason="cancel")
        run.done.set()
        self._finish(dispatch_id)
        return True

    def cancel_for_external_agent(self, external_agent_id: str) -> int:
        target = _safe_segment(external_agent_id)
        if not target:
            return 0
        with self._runs_lock:
            dispatch_ids = [
                dispatch_id
                for dispatch_id, run in self._runs.items()
                if _safe_segment(run.external_agent_id) == target and not run.done.is_set()
            ]
        cancelled = 0
        for dispatch_id in dispatch_ids:
            if self.cancel(dispatch_id):
                cancelled += 1
        return cancelled

    def reset_runtime_session(self, runtime_session_id: str) -> bool:
        return self.snapshot_store.delete(runtime_session_id)

    def reload_skills_for_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._skills_context(payload):
            from agent.skill_commands import reload_skills

            return reload_skills()

    def _finish(self, dispatch_id: str) -> None:
        with self._runs_lock:
            self._runs.pop(dispatch_id, None)

    def _start_run_timeout_watchdog(self, run: ActiveRun, payload: dict[str, Any]) -> threading.Timer | None:
        timeout_s = self._payload_timeout_s(payload)
        if timeout_s is None:
            return None

        def on_timeout() -> None:
            if run.done.is_set():
                return
            logger.warning(
                "Hermes run timeout dispatchId=%s externalAgentId=%s timeoutS=%.3f timestamp=%s",
                run.dispatch_id,
                run.external_agent_id,
                timeout_s,
                _now_iso(),
            )
            if run.agent and hasattr(run.agent, "interrupt"):
                try:
                    run.agent.interrupt("Timed out by ClawChat Hermes bridge")
                except Exception:
                    logger.warning("failed to interrupt timed out Hermes run %s", run.dispatch_id, exc_info=True)
            terminal_type = "run.cancelled" if run.cancel_requested else "run.failed"
            event: dict[str, Any] = {
                "type": terminal_type,
                "dispatchId": run.dispatch_id,
            }
            if terminal_type == "run.failed":
                event.update({
                    "code": "timeout",
                    "message": "Hermes run timed out on the bridge.",
                    "retryable": True,
                })
            run.emit(event)
            self.dispatch_state.record_terminal(
                run.dispatch_id,
                str(payload.get("runtimeRunId") or run.dispatch_id),
                run.external_agent_id,
                terminal_type,
            )
            self._force_release_run_lock(run, reason="timeout")
            run.done.set()
            self._finish(run.dispatch_id)

        timer = threading.Timer(timeout_s, on_timeout)
        timer.daemon = True
        timer.start()
        logger.info(
            "Hermes run timeout watchdog started dispatchId=%s externalAgentId=%s timeoutS=%.3f timestamp=%s",
            run.dispatch_id,
            run.external_agent_id,
            timeout_s,
            _now_iso(),
        )
        return timer

    async def _forward_events(self, run: ActiveRun) -> None:
        while True:
            try:
                event = await asyncio.to_thread(run.event_queue.get, True, 0.25)
            except queue.Empty:
                if run.done.is_set():
                    break
                continue
            try:
                if event.get("type") in TERMINAL_EVENT_TYPES:
                    logger.info(
                        "forwarding terminal event dispatchId=%s externalAgentId=%s type=%s elapsedMs=%s",
                        run.dispatch_id,
                        run.external_agent_id,
                        event.get("type"),
                        run.elapsed_ms(),
                    )
                elif event.get("type") == "run.started":
                    run.started_sent_monotonic = time.monotonic()
                    logger.info(
                        "Hermes dispatch timing dispatchId=%s externalAgentId=%s phase=run_started_forward elapsedMs=%s",
                        run.dispatch_id,
                        run.external_agent_id,
                        run.elapsed_ms(run.started_sent_monotonic),
                    )
                await self.bridge.send_event(event)
            except Exception as exc:
                logger.warning(
                    "failed to send Hermes runtime event type=%s dispatchId=%s: %s",
                    event.get("type"),
                    event.get("dispatchId"),
                    exc,
                )
        while True:
            try:
                event = run.event_queue.get_nowait()
            except queue.Empty:
                break
            try:
                if event.get("type") in TERMINAL_EVENT_TYPES:
                    logger.info(
                        "forwarding queued terminal event dispatchId=%s externalAgentId=%s type=%s elapsedMs=%s",
                        run.dispatch_id,
                        run.external_agent_id,
                        event.get("type"),
                        run.elapsed_ms(),
                    )
                elif event.get("type") == "run.started":
                    run.started_sent_monotonic = time.monotonic()
                    logger.info(
                        "Hermes dispatch timing dispatchId=%s externalAgentId=%s phase=run_started_forward elapsedMs=%s",
                        run.dispatch_id,
                        run.external_agent_id,
                        run.elapsed_ms(run.started_sent_monotonic),
                    )
                await self.bridge.send_event(event)
            except Exception as exc:
                logger.warning(
                    "failed to send queued Hermes runtime event type=%s dispatchId=%s: %s",
                    event.get("type"),
                    event.get("dispatchId"),
                    exc,
                )

    def _fallback_workspace_root(self, payload: dict[str, Any]) -> Path:
        workspace_id = _safe_segment(str(payload.get("workspaceId") or payload.get("workspace_id") or "default"))
        external_agent_id = self._external_agent_id(payload)
        root = (
            get_hermes_home()
            / "clawchat"
            / "workspaces"
            / workspace_id
            / "agents"
            / external_agent_id
            / "workspace"
        ).resolve()
        root.mkdir(parents=True, exist_ok=True)
        return root

    def _resolved_workspace_root(self, payload: dict[str, Any]) -> tuple[str, bool]:
        workspace_root = str(payload.get("workspaceRoot") or "").strip()
        if workspace_root:
            return workspace_root, False
        return str(self._fallback_workspace_root(payload)), True

    @contextmanager
    def _workspace_context(self, workspace_root: str | None):
        old_terminal_cwd = os.environ.get("TERMINAL_CWD")
        old_session_source = os.environ.get("HERMES_SESSION_SOURCE")
        old_cwd = os.getcwd()
        try:
            os.environ["HERMES_SESSION_SOURCE"] = "clawchat_bridge"
            if workspace_root:
                os.environ["TERMINAL_CWD"] = workspace_root
                os.chdir(workspace_root)
            yield
        finally:
            if old_terminal_cwd is None:
                os.environ.pop("TERMINAL_CWD", None)
            else:
                os.environ["TERMINAL_CWD"] = old_terminal_cwd
            if old_session_source is None:
                os.environ.pop("HERMES_SESSION_SOURCE", None)
            else:
                os.environ["HERMES_SESSION_SOURCE"] = old_session_source
            os.chdir(old_cwd)

    @contextmanager
    def _skills_context(self, payload: dict[str, Any]):
        old_prepend_dirs = os.environ.get("HERMES_PREPEND_SKILLS_DIRS")
        roots = self._clawchat_skill_roots(payload)
        try:
            if roots:
                os.environ["HERMES_PREPEND_SKILLS_DIRS"] = os.pathsep.join(str(root) for root in roots)
            else:
                os.environ.pop("HERMES_PREPEND_SKILLS_DIRS", None)
            yield roots
        finally:
            if old_prepend_dirs is None:
                os.environ.pop("HERMES_PREPEND_SKILLS_DIRS", None)
            else:
                os.environ["HERMES_PREPEND_SKILLS_DIRS"] = old_prepend_dirs

    def _clawchat_skill_roots(self, payload: dict[str, Any]) -> list[Path]:
        external_agent_id = _safe_segment(str(payload.get("externalAgentId") or "agent"))
        agent_id = _safe_segment(str(payload.get("agentId") or payload.get("agent_id") or ""))
        workspace_id = _safe_segment(str(payload.get("workspaceId") or payload.get("workspace_id") or "default"))
        candidate_roots = [
            get_hermes_home() / "clawchat" / "workspaces" / workspace_id / "agents" / external_agent_id / "workspace" / "skills",
            get_hermes_home() / "clawchat" / "agents" / external_agent_id / "workspace" / "skills",
            get_hermes_home() / "clawchat" / "shared" / "skills",
        ]
        if agent_id and agent_id != external_agent_id:
            candidate_roots.insert(1, get_hermes_home() / "clawchat" / "agents" / agent_id / "workspace" / "skills")
        roots: list[Path] = []
        seen: set[Path] = set()
        for root in candidate_roots:
            try:
                resolved = root.resolve()
            except OSError:
                continue
            if resolved in seen or not resolved.is_dir():
                continue
            seen.add(resolved)
            roots.append(resolved)
        return roots

    @contextmanager
    def _reference_tracking_context(self, run: ActiveRun, payload: dict[str, Any], skill_roots: list[Path]):
        tracker = SkillReferenceTracker(run, payload, skill_roots)
        try:
            import tools.skills_tool as skills_tool
        except Exception:
            yield tracker
            return

        original_skill_view = skills_tool.skill_view

        def tracked_skill_view(name: str, file_path: str = None, task_id: str = None, preprocess: bool = True) -> str:
            result = original_skill_view(
                name,
                file_path=file_path,
                task_id=task_id,
                preprocess=preprocess,
            )
            tracker.record_skill_view(name, file_path, result)
            return result

        skills_tool.skill_view = tracked_skill_view
        try:
            yield tracker
        finally:
            skills_tool.skill_view = original_skill_view

    def _build_agent(self, run: ActiveRun, payload: dict[str, Any]):
        if self.fake_mode:
            return FakeHermesAgent(
                stream_delta_callback=lambda text: self._emit_delta(run, text),
                thinking_callback=lambda text: self._emit_thinking(run, text, "thinking"),
                reasoning_callback=lambda text: self._emit_thinking(run, text, "reasoning"),
                tool_progress_callback=lambda *args: self._emit_tool_callback(run, *args),
                status_callback=lambda topic, message: self._emit_status(run, topic, message),
            )

        from run_agent import AIAgent

        toolset_started = time.monotonic()
        replace_base_harness = self._replace_base_harness_from_payload(payload)
        if replace_base_harness and not self._base_harness_replacement_allowed(payload):
            logger.error(
                "Hermes native harness replacement rejected dispatchId=%s externalAgentId=%s workspaceId=%s runtimeSessionId=%s requestedEnabledToolsets=%s runtimeToolsets=%s",
                run.dispatch_id,
                run.external_agent_id,
                payload.get("workspaceId") or payload.get("workspace_id") or "<none>",
                run.runtime_session_id,
                payload.get("enabledToolsets") or payload.get("enabled_toolsets") or "<none>",
                payload.get("runtimeToolsets") or payload.get("runtime_toolsets") or "<none>",
            )
            raise WorkspaceError(
                "native_harness_replacement_rejected",
                "ClawChat dispatch attempted to replace the Hermes native harness. "
                "Native tools are preserved by default; use runtimeToolsets.additive/disabled instead. "
                "Exceptional replacement requires an audited bridge policy.",
            )
        runtime_requested_toolsets = self._runtime_requested_toolsets_from_payload(payload)
        requested_enabled_toolsets = self._enabled_toolsets_from_payload(payload)
        if replace_base_harness:
            enabled_toolsets = self._enabled_toolsets_for_policy(requested_enabled_toolsets or [], payload.get("_autonomyPolicy"))
            if runtime_requested_toolsets:
                enabled_toolsets = list(dict.fromkeys([*(enabled_toolsets or []), *runtime_requested_toolsets]))
            if payload.get("_marketplaceToolNames"):
                enabled_toolsets = list(dict.fromkeys([*(enabled_toolsets or []), MarketplaceRuntimeToolProxy.TOOLSET]))
        else:
            # Preserve the native Hermes harness.  In AIAgent/get_tool_definitions,
            # enabled_toolsets is a replacement filter, so ClawChat additive
            # requests must not be forwarded here unless replacement is explicit.
            enabled_toolsets = None
        disabled_toolsets = self._disabled_toolsets_from_payload(payload)
        if disabled_toolsets is None:
            disabled_toolsets = self.default_disabled_toolsets
        disabled_toolsets = self._disabled_toolsets_for_policy(disabled_toolsets, payload.get("_autonomyPolicy"))
        if runtime_requested_toolsets and disabled_toolsets:
            requested = set(runtime_requested_toolsets)
            disabled_toolsets = [item for item in disabled_toolsets if item not in requested]
        if payload.get("_marketplaceToolNames") and disabled_toolsets:
            disabled_toolsets = [
                item for item in disabled_toolsets
                if item != MarketplaceRuntimeToolProxy.TOOLSET
            ]
        skip_memory = self._skip_memory_from_payload(payload)
        logger.info(
            "Hermes dispatch timing dispatchId=%s externalAgentId=%s workspaceId=%s runtimeSessionId=%s phase=toolsets_resolved elapsedMs=%s phaseMs=%s nativeBaseHarnessPreserved=%s replaceBaseHarness=%s requestedEnabledToolsets=%s finalEnabledToolsets=%s finalDisabledToolsets=%s additiveToolsets=%s marketplaceToolsCount=%s skipMemory=%s sessionDbPresent=%s workspaceRoot=%s fallbackWorkspace=%s nativeNarrowedByPayload=%s",
            run.dispatch_id,
            run.external_agent_id,
            payload.get("workspaceId") or payload.get("workspace_id") or "<none>",
            run.runtime_session_id,
            run.elapsed_ms(),
            int((time.monotonic() - toolset_started) * 1000),
            not replace_base_harness,
            replace_base_harness,
            ",".join(requested_enabled_toolsets or []) if requested_enabled_toolsets else "<none>",
            ",".join(enabled_toolsets or []) if enabled_toolsets else "<default-native>",
            ",".join(disabled_toolsets or []) if disabled_toolsets else "<none>",
            ",".join(runtime_requested_toolsets or []) if runtime_requested_toolsets else "<none>",
            len(payload.get("_marketplaceToolNames") or []),
            skip_memory,
            bool(self.session_db),
            payload.get("_resolvedWorkspaceRoot") or payload.get("workspaceRoot") or "<none>",
            payload.get("_usingFallbackWorkspace") is True,
            replace_base_harness and bool(requested_enabled_toolsets or runtime_requested_toolsets),
        )
        agent_init_started = time.monotonic()
        agent = AIAgent(
            model=payload.get("model") or self.default_model,
            quiet_mode=True,
            verbose_logging=False,
            session_id=f"{run.runtime_session_id}-{uuid4().hex[:8]}",
            stream_delta_callback=lambda text: self._emit_delta(run, text),
            thinking_callback=lambda text: self._emit_thinking(run, text, "thinking"),
            reasoning_callback=lambda text: self._emit_thinking(run, text, "reasoning"),
            tool_progress_callback=lambda *args: self._emit_tool_callback(run, *args),
            status_callback=lambda topic, message: self._emit_status(run, topic, message),
            enabled_toolsets=enabled_toolsets,
            disabled_toolsets=disabled_toolsets,
            platform="clawchat_bridge",
            skip_memory=skip_memory,
            session_db=self.session_db,
            thread_id=str(payload.get("threadId") or "") or None,
            gateway_session_key=str(payload.get("runtimeSessionId") or "") or None,
        )
        final_model_visible_tool_names = self._model_visible_tool_names(agent)
        try:
            from tools.registry import registry

            registry.update_active_scope_metadata(modelVisibleToolNames=final_model_visible_tool_names)
        except Exception:
            logger.debug("failed to update active marketplace scope metadata", exc_info=True)
        valid_tool_names = set(final_model_visible_tool_names) or set(getattr(agent, "valid_tool_names", set()) or set())
        self._validate_native_harness_visible(
            run,
            payload,
            valid_tool_names,
            disabled_toolsets=disabled_toolsets,
            replace_base_harness=replace_base_harness,
            skip_memory=skip_memory,
        )
        logger.info(
            "Hermes dispatch timing dispatchId=%s externalAgentId=%s workspaceId=%s runtimeSessionId=%s phase=agent_tool_init_complete elapsedMs=%s phaseMs=%s toolCount=%s memoryEnabled=%s sessionSearchEnabled=%s fileToolsPresent=%s terminalPresent=%s skillsPresent=%s browserToolPresent=%s marketplaceToolPresent=%s sessionDbPresent=%s skipMemory=%s contextCwd=%s finalModelVisibleTools=%s",
            run.dispatch_id,
            run.external_agent_id,
            payload.get("workspaceId") or payload.get("workspace_id") or "<none>",
            run.runtime_session_id,
            run.elapsed_ms(),
            int((time.monotonic() - agent_init_started) * 1000),
            len(valid_tool_names),
            "memory" in valid_tool_names and not skip_memory,
            "session_search" in valid_tool_names and bool(self.session_db),
            all(name in valid_tool_names for name in ("read_file", "write_file", "patch", "search_files")),
            "terminal" in valid_tool_names,
            all(name in valid_tool_names for name in ("skills_list", "skill_view", "skill_manage")),
            any(str(name).startswith("browser_") for name in valid_tool_names),
            any(name in valid_tool_names for name in ("linkcrest_agent_api", "exa_search", "x_get_me")),
            bool(self.session_db),
            skip_memory,
            os.getenv("TERMINAL_CWD") or "<none>",
            ",".join(final_model_visible_tool_names) if final_model_visible_tool_names else "<none>",
        )
        marketplace_tool_names = [str(name) for name in payload.get("_marketplaceToolNames") or []]
        if marketplace_tool_names:
            try:
                from tools.registry import registry

                scope_metadata = registry.active_scope_metadata()
            except Exception:
                scope_metadata = {}
            marketplace_callable_names = [
                str(name)
                for name in (
                    payload.get("_marketplaceCallableNames")
                    or scope_metadata.get("callableNames")
                    or [
                        self.marketplace_proxy.callable_name_for_identifier(name)
                        for name in marketplace_tool_names
                    ]
                )
            ]
            model_callable_names = [
                name
                for name in marketplace_callable_names
                if MarketplaceRuntimeToolProxy.TOOL_NAME_PATTERN.fullmatch(name)
            ]
            model_tool_names = {
                tool.get("function", {}).get("name")
                for tool in (getattr(agent, "tools", None) or [])
                if isinstance(tool, dict)
            }
            attached_names = [name for name in model_callable_names if name in model_tool_names]
            missing_names = [name for name in model_callable_names if name not in model_tool_names]
            if missing_names:
                logger.error(
                    "marketplace toolset visibility mismatch dispatchId=%s runtimeSessionId=%s externalAgentId=%s toolsetRevision=%s missing=%s modelVisibleTools=%s",
                    run.dispatch_id,
                    run.runtime_session_id,
                    run.external_agent_id,
                    payload.get("_marketplaceToolsetRevision") or "<none>",
                    ", ".join(missing_names),
                    ",".join(final_model_visible_tool_names) if final_model_visible_tool_names else "<none>",
                )
                raise WorkspaceError(
                    "marketplace_toolset_stale_or_unavailable",
                    "ClawChat marketplace tools were attached to this dispatch but Hermes could not expose "
                    f"the callable tool(s): {', '.join(missing_names)}. Start a fresh dispatch/session so "
                    "the runtime toolset is rebuilt.",
                )
            logger.info(
                "marketplace toolset visibility consistent dispatchId=%s runtimeSessionId=%s externalAgentId=%s toolsetRevision=%s visibleCallableNames=%s",
                run.dispatch_id,
                run.runtime_session_id,
                run.external_agent_id,
                payload.get("_marketplaceToolsetRevision") or "<none>",
                ",".join(marketplace_callable_names) if marketplace_callable_names else "<none>",
            )
            logger.info(
                "marketplace tools attached to Hermes model config dispatchId=%s count=%s names=%s callableNames=%s missing=%s enabledToolsets=%s disabledToolsets=%s toolsetRevision=%s",
                run.dispatch_id,
                len(attached_names),
                ", ".join(attached_names) if attached_names else "<none>",
                ", ".join(marketplace_callable_names) if marketplace_callable_names else "<none>",
                ", ".join(missing_names) if missing_names else "<none>",
                ",".join(enabled_toolsets or []) if enabled_toolsets else "<default>",
                ",".join(disabled_toolsets or []) if disabled_toolsets else "<none>",
                payload.get("_marketplaceToolsetRevision") or "<none>",
            )
        return agent

    def _enabled_toolsets_from_payload(self, payload: dict[str, Any]) -> list[str] | None:
        for container in self._runtime_metadata_containers(payload):
            toolsets = self._enabled_toolsets_from_container(container)
            if toolsets:
                return toolsets
        return None

    def _replace_base_harness_from_payload(self, payload: dict[str, Any]) -> bool:
        for container in self._runtime_metadata_containers(payload):
            for key in (
                "replaceBaseHarness",
                "replace_base_harness",
                "replaceNativeHarness",
                "replace_native_harness",
                "restrictToEnabledToolsets",
                "restrict_to_enabled_toolsets",
            ):
                if key in container:
                    return _bool_from_policy(container.get(key))
            runtime_toolsets = container.get("runtimeToolsets") or container.get("runtime_toolsets")
            if isinstance(runtime_toolsets, dict):
                for key in (
                    "replaceBaseHarness",
                    "replace_base_harness",
                    "replaceNativeHarness",
                    "replace_native_harness",
                    "restrictToEnabledToolsets",
                    "restrict_to_enabled_toolsets",
                ):
                    if key in runtime_toolsets:
                        return _bool_from_policy(runtime_toolsets.get(key))
        policy = payload.get("_autonomyPolicy") or payload.get("autonomyPolicy")
        if isinstance(policy, dict):
            for key in ("replaceBaseHarness", "replace_base_harness", "replaceNativeHarness", "replace_native_harness"):
                if key in policy:
                    return _bool_from_policy(policy.get(key))
        return False

    def _base_harness_replacement_allowed(self, payload: dict[str, Any]) -> bool:
        if os.getenv("HERMES_CLAWCHAT_ALLOW_REPLACE_BASE_HARNESS", "").strip().lower() not in {"1", "true", "yes"}:
            return False
        policy = payload.get("_autonomyPolicy") or payload.get("autonomyPolicy") or payload.get("policy")
        if not isinstance(policy, dict):
            return False
        for key in (
            "allowBaseHarnessReplacement",
            "allow_base_harness_replacement",
            "allowNativeHarnessRestriction",
            "allow_native_harness_restriction",
        ):
            if key in policy:
                return _bool_from_policy(policy.get(key))
        return False

    def _model_visible_tool_names(self, agent: Any) -> list[str]:
        names: list[str] = []
        for tool in getattr(agent, "tools", None) or []:
            if not isinstance(tool, dict):
                continue
            function = tool.get("function")
            if isinstance(function, dict) and isinstance(function.get("name"), str):
                names.append(function["name"])
        if not names:
            names = [str(name) for name in (getattr(agent, "valid_tool_names", set()) or set())]
        return sorted(set(names))

    def _native_harness_missing_allowed_by_disabled_toolsets(self, disabled_toolsets: list[str] | None) -> set[str]:
        allowed: set[str] = set()
        for item in disabled_toolsets or []:
            key = str(item or "").strip()
            if not key:
                continue
            allowed.update(NATIVE_HARNESS_TOOLSET_TO_TOOLS.get(key, set()))
        return allowed

    def _validate_native_harness_visible(
        self,
        run: ActiveRun,
        payload: dict[str, Any],
        final_tool_names: set[str],
        *,
        disabled_toolsets: list[str] | None,
        replace_base_harness: bool,
        skip_memory: bool,
    ) -> None:
        if replace_base_harness and self._base_harness_replacement_allowed(payload):
            logger.warning(
                "Hermes native harness replacement allowed by audited policy dispatchId=%s externalAgentId=%s workspaceId=%s runtimeSessionId=%s",
                run.dispatch_id,
                run.external_agent_id,
                payload.get("workspaceId") or payload.get("workspace_id") or "<none>",
                run.runtime_session_id,
            )
            return

        required = set(NATIVE_HARNESS_REQUIRED_TOOLS)
        required.difference_update(self._native_harness_missing_allowed_by_disabled_toolsets(disabled_toolsets))
        if skip_memory:
            required.discard("memory")

        missing = sorted(required.difference(final_tool_names))
        if not missing:
            return

        marketplace_like = sorted(
            name
            for name in final_tool_names
            if name.startswith("browser_")
            or name.startswith("x_")
            or name.startswith("exa_")
            or name.startswith("marketplace_")
        )
        logger.error(
            "Hermes native harness invariant failed dispatchId=%s externalAgentId=%s workspaceId=%s runtimeSessionId=%s missingNativeTools=%s finalModelVisibleTools=%s disabledToolsets=%s marketplaceLikeTools=%s",
            run.dispatch_id,
            run.external_agent_id,
            payload.get("workspaceId") or payload.get("workspace_id") or "<none>",
            run.runtime_session_id,
            ",".join(missing),
            ",".join(sorted(final_tool_names)) if final_tool_names else "<none>",
            ",".join(disabled_toolsets or []) if disabled_toolsets else "<none>",
            ",".join(marketplace_like) if marketplace_like else "<none>",
        )
        raise WorkspaceError(
            "native_harness_shackled",
            "Hermes native harness invariant failed: final model-visible tools are missing "
            f"{', '.join(missing)}. The bridge refuses to run a marketplace/browser-only Hermes agent.",
        )

    def _skip_memory_from_payload(self, payload: dict[str, Any]) -> bool:
        for container in self._runtime_metadata_containers(payload):
            for key in ("skipMemory", "skip_memory", "stateless", "ephemeral"):
                if key in container:
                    return _bool_from_policy(container.get(key))
        return False

    def _runtime_requested_toolsets_from_payload(self, payload: dict[str, Any]) -> list[str]:
        requested: list[str] = []
        for container in self._runtime_metadata_containers(payload):
            for toolset in self._enabled_toolsets_from_container(container):
                requested.append(toolset)
            tool_names = self._runtime_tool_names_from_container(container)
            if any(name.startswith("browser_") for name in tool_names):
                requested.append("browser")
            if any(name in {"web_search", "web_extract"} for name in tool_names):
                requested.append("web")
        return list(dict.fromkeys(requested))

    def _runtime_metadata_containers(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        containers = [payload]
        for key in ("runtimeContext", "marketplaceRuntimeContext", "dispatchMetadata", "configMetadata", "runtimeToolsets"):
            value = payload.get(key)
            if isinstance(value, dict):
                containers.append(value)
        return containers

    def _enabled_toolsets_from_container(self, container: dict[str, Any]) -> list[str]:
        toolsets: list[str] = []
        for key in ("enabledToolsets", "enabled_toolsets", "additive", "additiveToolsets", "additive_toolsets"):
            toolsets.extend(self._coerce_toolset_list(container.get(key)))
        runtime_toolsets = container.get("runtimeToolsets") or container.get("runtime_toolsets")
        if isinstance(runtime_toolsets, dict):
            for key in ("enabledToolsets", "enabled_toolsets", "additive", "additiveToolsets", "additive_toolsets"):
                toolsets.extend(self._coerce_toolset_list(runtime_toolsets.get(key)))
        return list(dict.fromkeys(toolsets))

    def _disabled_toolsets_from_payload(self, payload: dict[str, Any]) -> list[str] | None:
        disabled: list[str] = []
        found = False
        for container in self._runtime_metadata_containers(payload):
            for key in ("disabledToolsets", "disabled_toolsets", "disabled"):
                if key in container:
                    found = True
                    disabled.extend(self._coerce_toolset_list(container.get(key)))
            runtime_toolsets = container.get("runtimeToolsets") or container.get("runtime_toolsets")
            if isinstance(runtime_toolsets, dict):
                for key in ("disabledToolsets", "disabled_toolsets", "disabled"):
                    if key in runtime_toolsets:
                        found = True
                        disabled.extend(self._coerce_toolset_list(runtime_toolsets.get(key)))
        return list(dict.fromkeys(disabled)) if found else None

    def _coerce_toolset_list(self, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        if isinstance(value, (list, tuple, set)):
            return [str(item).strip() for item in value if str(item).strip()]
        return [str(value).strip()] if str(value).strip() else []

    def _runtime_tool_names_from_container(self, container: dict[str, Any]) -> list[str]:
        names: list[str] = []
        for key in (
            "availableRuntimeTools",
            "available_runtime_tools",
            "requiredToolNames",
            "required_tool_names",
            "requiredRuntimeTools",
            "required_runtime_tools",
            "runtimeToolNames",
            "runtime_tool_names",
            "browserToolNames",
            "browser_tool_names",
            "tools",
        ):
            value = container.get(key)
            if value is None:
                continue
            names.extend(self._tool_names_from_value(value))
        return list(dict.fromkeys(names))

    def _tool_names_from_value(self, value: Any) -> list[str]:
        if isinstance(value, str):
            return [value.strip()] if value.strip() else []
        if not isinstance(value, (list, tuple, set)):
            return []
        names: list[str] = []
        for item in value:
            if isinstance(item, str):
                name = item.strip()
            elif isinstance(item, dict):
                name = str(
                    item.get("functionName")
                    or item.get("function_name")
                    or item.get("name")
                    or item.get("toolName")
                    or ""
                ).strip()
            else:
                name = ""
            if name:
                names.append(name)
        return names

    def _enabled_toolsets_for_policy(self, enabled_toolsets: Any, policy: dict[str, Any] | None) -> list[str] | None:
        if enabled_toolsets is None:
            return None
        if isinstance(enabled_toolsets, str):
            toolsets = [part.strip() for part in enabled_toolsets.split(",") if part.strip()]
        elif isinstance(enabled_toolsets, (list, tuple, set)):
            toolsets = [str(item).strip() for item in enabled_toolsets if str(item).strip()]
        else:
            toolsets = [str(enabled_toolsets).strip()]
        if not policy:
            return toolsets
        allowed = {
            category
            for category in AUTONOMY_CATEGORIES
            if self._policy_allows_category(policy, category) == "allowed"
        }
        additions: list[str] = []
        if allowed & {"browser_navigation", "form_fill", "form_submit", "account_create", "external_publish", "backlink_verify"}:
            additions.append("browser")
        if allowed & {"external_search", "backlink_verify", "index_check"}:
            additions.append("web")
        return list(dict.fromkeys([*toolsets, *additions]))

    def _disabled_toolsets_for_policy(self, disabled_toolsets: Any, policy: dict[str, Any] | None) -> list[str]:
        if isinstance(disabled_toolsets, str):
            toolsets = [part.strip() for part in disabled_toolsets.split(",") if part.strip()]
        elif isinstance(disabled_toolsets, (list, tuple, set)):
            toolsets = [str(item).strip() for item in disabled_toolsets if str(item).strip()]
        elif disabled_toolsets is None:
            toolsets = []
        else:
            toolsets = [str(disabled_toolsets).strip()]
        if not policy:
            return toolsets
        disabled = set(policy.get("disabledToolCategories") or [])
        allowed = {
            category
            for category in AUTONOMY_CATEGORIES
            if self._policy_allows_category(policy, category) == "allowed"
        }
        additions: list[str] = []
        browser_categories = {"browser_navigation", "form_fill", "form_submit", "account_create", "external_publish", "backlink_verify"}
        web_categories = {"external_search", "backlink_verify", "index_check"}
        if disabled & browser_categories or not (allowed & browser_categories):
            additions.append("browser")
        if disabled & web_categories or not (allowed & web_categories):
            additions.append("web")
        return list(dict.fromkeys([*toolsets, *additions]))

    def _emit_delta(self, run: ActiveRun, text: str | None) -> None:
        if text is None:
            return
        seq = getattr(run, "_delta_seq", 0) + 1
        setattr(run, "_delta_seq", seq)
        if not run.first_delta_monotonic:
            run.first_delta_monotonic = time.monotonic()
            since_model_ms = (
                int((run.first_delta_monotonic - run.first_model_call_monotonic) * 1000)
                if run.first_model_call_monotonic else None
            )
            logger.info(
                "Hermes dispatch timing dispatchId=%s externalAgentId=%s phase=first_delta elapsedMs=%s sinceModelStartMs=%s",
                run.dispatch_id,
                run.external_agent_id,
                run.elapsed_ms(run.first_delta_monotonic),
                since_model_ms,
            )
        run.emit({"type": "run.delta", "dispatchId": run.dispatch_id, "seq": seq, "text": text})

    def _emit_thinking(self, run: ActiveRun, text: str | None, kind: str) -> None:
        if text is None:
            return
        seq = getattr(run, "_thinking_seq", 0) + 1
        setattr(run, "_thinking_seq", seq)
        if not run.first_thinking_monotonic:
            run.first_thinking_monotonic = time.monotonic()
            since_model_ms = (
                int((run.first_thinking_monotonic - run.first_model_call_monotonic) * 1000)
                if run.first_model_call_monotonic else None
            )
            logger.info(
                "Hermes dispatch timing dispatchId=%s externalAgentId=%s phase=first_thinking elapsedMs=%s sinceModelStartMs=%s kind=%s",
                run.dispatch_id,
                run.external_agent_id,
                run.elapsed_ms(run.first_thinking_monotonic),
                since_model_ms,
                kind,
            )
        run.emit({"type": "run.thinking", "dispatchId": run.dispatch_id, "seq": seq, "thinking": text, "kind": kind})

    def _emit_tool_callback(self, run: ActiveRun, *args: Any) -> None:
        event_type = None
        name = "tool"
        preview = ""
        tool_args: Any = None
        if len(args) >= 4:
            event_type, name, preview, tool_args = args[0], args[1], args[2], args[3]
        elif len(args) >= 3:
            name, preview = args[0], args[1]
        elif len(args) >= 2:
            name, preview = args[0], args[1]
        elif len(args) == 1:
            name = args[0]

        event_text = str(event_type or "")
        if event_text.endswith(".started"):
            phase = "started"
        elif event_text.endswith(".completed"):
            phase = "completed"
        elif event_text.endswith(".failed"):
            phase = "failed"
        else:
            phase = "updated"
        event: dict[str, Any] = {
            "type": "run.tool",
            "dispatchId": run.dispatch_id,
            "toolName": str(name),
            "phase": phase,
            "summary": str(preview or ""),
        }
        tasks = self._relay_todo_snapshot(run, name, tool_args)
        if tasks is not None:
            event["tasks"] = tasks
        run.emit(event)

    @staticmethod
    def _relay_todo_snapshot(
        run: ActiveRun,
        tool_name: Any,
        tool_args: Any,
    ) -> list[dict[str, str]] | None:
        if str(tool_name).strip().lower() != "todo":
            return None

        current = list(getattr(run, "_relay_todo_tasks", []))
        if not isinstance(tool_args, dict) or "todos" not in tool_args:
            return current

        raw_tasks = tool_args.get("todos")
        if not isinstance(raw_tasks, list):
            return current

        valid_statuses = {"pending", "in_progress", "completed", "cancelled"}
        normalized: list[dict[str, str]] = []
        for index, raw_task in enumerate(raw_tasks[:100]):
            if not isinstance(raw_task, dict):
                continue
            content = str(raw_task.get("content") or "").strip()[:2000]
            if not content:
                continue
            task_id = str(raw_task.get("id") or f"todo-{index + 1}").strip()[:200] or f"todo-{index + 1}"
            status = str(raw_task.get("status") or "pending").strip().lower()
            if status not in valid_statuses:
                status = "pending"
            normalized.append({"id": task_id, "content": content, "status": status})

        if bool(tool_args.get("merge")):
            by_id = {task["id"]: task for task in current}
            order = [task["id"] for task in current]
            for task in normalized:
                if task["id"] not in by_id:
                    order.append(task["id"])
                by_id[task["id"]] = task
            snapshot = [by_id[task_id] for task_id in order]
        else:
            snapshot = normalized

        setattr(run, "_relay_todo_tasks", snapshot)
        return list(snapshot)

    def _emit_status(self, run: ActiveRun, topic: str, message: str) -> None:
        run.emit({"type": "run.status", "dispatchId": run.dispatch_id, "code": str(topic), "message": str(message)})

    def _autonomy_policy_from_payload(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        candidates: list[Any] = [payload.get("autonomyPolicy")]
        runtime_instruction = payload.get("runtimeInstruction")
        if isinstance(runtime_instruction, dict):
            candidates.append(runtime_instruction.get("autonomyPolicy") or runtime_instruction.get("policy"))
        for key in ("dispatchMetadata", "configMetadata", "marketplaceRuntimeContext", "localAppRuntimeContext"):
            value = payload.get(key)
            if isinstance(value, dict):
                candidates.append(value.get("autonomyPolicy") or value.get("autonomy_policy"))
        for candidate in candidates:
            if isinstance(candidate, dict):
                return self._normalize_autonomy_policy(candidate, payload)
        inferred = self._infer_linkcrest_backlink_policy(payload)
        if inferred:
            logger.info(
                "autonomy policy inferred for LinkCrest backlink missing-tool emission dispatchId=%s appSlug=%s campaignId=%s campaignName=%s",
                payload.get("dispatchId"),
                inferred.get("appSlug") or "<none>",
                inferred.get("campaignId") or "<none>",
                inferred.get("campaignName") or "<none>",
            )
            return inferred
        return None

    def _infer_linkcrest_backlink_policy(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        app_slug = self._runtime_context_string(payload, "appSlug", "app_slug", "slug")
        repo_path = self._runtime_context_string(payload, "repoPath", "repo_path")
        app_url = self._runtime_context_string(payload, "appUrl", "app_url", "localAppUrl", "local_app_url")
        linkcrest_context = str(app_slug or "").lower() in {"linkcrest", "local-linkcrest"}
        linkcrest_context = linkcrest_context or "linkcrest" in str(repo_path or "").lower()
        linkcrest_context = linkcrest_context or str(app_url or "").rstrip("/") in {"http://localhost:3052", "http://127.0.0.1:3052"}
        if not linkcrest_context:
            return None
        probe_policy = {
            "campaignName": self._runtime_context_string(payload, "campaignName", "campaign_name", "selectedCampaignName") or "",
            "enabledToolCategories": [],
            "selectedCapabilities": [],
        }
        if not self._linkcrest_backlink_execution_required(payload, probe_policy):
            return None
        return self._normalize_autonomy_policy(
            {
                "mode": "custom_policy",
                "appSlug": app_slug or "linkcrest",
                "linkedAppId": self._runtime_context_string(payload, "linkedAppId", "linked_app_id") or "",
                "teamId": self._runtime_context_string(payload, "teamId", "team_id") or "",
                "campaignId": self._runtime_context_string(payload, "campaignId", "campaign_id", "selectedCampaignId") or "",
                "campaignName": probe_policy["campaignName"],
                "enabledToolCategories": sorted(LINKCREST_BACKLINK_REQUIRED_CATEGORIES),
                "selectedCapabilities": sorted(LINKCREST_BACKLINK_REQUIRED_CATEGORIES),
                "evidenceRequired": True,
                "hardStops": DEFAULT_HARD_STOPS,
            },
            payload,
        )

    def _normalize_autonomy_policy(self, raw_policy: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
        raw_mode = str(
            raw_policy.get("mode")
            or raw_policy.get("autonomyMode")
            or raw_policy.get("autonomy_mode")
            or raw_policy.get("name")
            or ""
        ).strip()
        mode = re.sub(r"[^A-Za-z0-9]+", "_", raw_mode).strip("_").lower() or "custom_policy"
        if mode not in AUTONOMY_MODES:
            mode = "custom_policy"

        selected = self._policy_categories(raw_policy, "selectedCapabilities", "selected_capabilities", "capabilities")
        enabled = self._policy_categories(raw_policy, "enabledToolCategories", "enabled_tool_categories", "enabledCategories", "allowedToolCategories")
        disabled = self._policy_categories(raw_policy, "disabledToolCategories", "disabled_tool_categories", "disabledCategories")
        approval_required = self._policy_categories(raw_policy, "approvalRequiredToolCategories", "approval_required_tool_categories", "approvalRequiredCategories")

        if not selected:
            selected = self._policy_categories(payload, "selectedCapabilities", "capabilities")
        if not enabled and selected:
            enabled = list(selected)

        app_slug = self._runtime_context_string(payload, "appSlug", "app_slug", "slug")
        linked_app_id = self._runtime_context_string(payload, "linkedAppId", "linked_app_id")
        team_id = self._runtime_context_string(payload, "teamId", "team_id")
        campaign_id = self._runtime_context_string(payload, "campaignId", "campaign_id", "selectedCampaignId")
        campaign_name = self._runtime_context_string(payload, "campaignName", "campaign_name", "selectedCampaignName")
        policy = {
            "mode": mode,
            "rawMode": raw_mode or mode,
            "selectedCapabilities": selected,
            "enabledToolCategories": enabled,
            "disabledToolCategories": disabled,
            "approvalRequiredToolCategories": approval_required,
            "evidenceRequired": raw_policy.get("evidenceRequired", raw_policy.get("evidence_required", True)),
            "hardStops": _string_list(raw_policy.get("hardStops") or raw_policy.get("hard_stops")) or DEFAULT_HARD_STOPS,
            "staleContextPolicy": str(raw_policy.get("staleContextPolicy") or raw_policy.get("stale_context_policy") or "").strip(),
            "appSlug": str(raw_policy.get("appSlug") or raw_policy.get("app_slug") or app_slug or "").strip(),
            "linkedAppId": str(raw_policy.get("linkedAppId") or raw_policy.get("linked_app_id") or linked_app_id or "").strip(),
            "teamId": str(raw_policy.get("teamId") or raw_policy.get("team_id") or team_id or "").strip(),
            "campaignId": str(raw_policy.get("campaignId") or raw_policy.get("campaign_id") or campaign_id or "").strip(),
            "campaignName": str(raw_policy.get("campaignName") or raw_policy.get("campaign_name") or campaign_name or "").strip(),
            "toolStatusByCategory": raw_policy.get("toolStatusByCategory") or raw_policy.get("tool_status_by_category") or raw_policy.get("toolAvailability") or raw_policy.get("tool_availability") or raw_policy.get("toolGrants") or raw_policy.get("tool_grants"),
            "managerFirst": _bool_from_policy(raw_policy.get("managerFirst") or raw_policy.get("manager_first")),
            "sequentialDispatch": _bool_from_policy(raw_policy.get("sequentialDispatch") or raw_policy.get("sequential_dispatch")),
            "refreshSnapshotBeforeRun": _bool_from_policy(raw_policy.get("refreshSnapshotBeforeRun", raw_policy.get("refresh_snapshot_before_run", True))),
            "raw": _redact_secret_fields(raw_policy),
        }
        logger.info(
            "autonomy policy accepted dispatchId=%s mode=%s selected=%s enabled=%s disabled=%s staleContextPolicy=%s managerFirst=%s sequentialDispatch=%s",
            payload.get("dispatchId"),
            policy["mode"],
            ",".join(policy["selectedCapabilities"]) or "<none>",
            ",".join(policy["enabledToolCategories"]) or "<none>",
            ",".join(policy["disabledToolCategories"]) or "<none>",
            policy["staleContextPolicy"] or "<none>",
            policy["managerFirst"],
            policy["sequentialDispatch"],
        )
        return policy

    def _runtime_context_string(self, payload: dict[str, Any], *keys: str) -> str | None:
        candidates: list[Any] = [
            payload,
            payload.get("autonomyPolicy"),
            payload.get("marketplaceRuntimeContext"),
            payload.get("runtimeContext"),
            payload.get("localAppRuntimeContext"),
            payload.get("dispatchMetadata"),
            payload.get("configMetadata"),
        ]
        for container_name in ("dispatchMetadata", "configMetadata"):
            container = payload.get(container_name)
            if isinstance(container, dict):
                candidates.extend([
                    container.get("autonomyPolicy"),
                    container.get("marketplaceRuntimeContext"),
                    container.get("runtimeContext"),
                    container.get("localAppRuntimeContext"),
                ])
        for candidate in candidates:
            found = self._find_string_for_keys(candidate, set(keys))
            if found:
                return found
        return None

    def _find_string_for_keys(self, value: Any, keys: set[str]) -> str | None:
        if isinstance(value, dict):
            for key in keys:
                item = value.get(key)
                if isinstance(item, str) and item.strip():
                    return item.strip()
            for child in value.values():
                if isinstance(child, (dict, list)):
                    found = self._find_string_for_keys(child, keys)
                    if found:
                        return found
        elif isinstance(value, list):
            for item in value:
                found = self._find_string_for_keys(item, keys)
                if found:
                    return found
        return None

    def _policy_categories(self, source: dict[str, Any], *keys: str) -> list[str]:
        categories: list[str] = []
        seen: set[str] = set()
        for key in keys:
            for value in _string_list(source.get(key)):
                normalized = _normalize_policy_category(value)
                if normalized and normalized not in seen:
                    seen.add(normalized)
                    categories.append(normalized)
        return categories

    def _policy_allows_category(self, policy: dict[str, Any] | None, category: str) -> str:
        if not policy:
            return "approval_required" if category in EXTERNAL_AUTONOMY_CATEGORIES else "allowed"
        disabled = set(policy.get("disabledToolCategories") or [])
        approval_required = set(policy.get("approvalRequiredToolCategories") or [])
        enabled = set(policy.get("enabledToolCategories") or [])
        selected = set(policy.get("selectedCapabilities") or [])
        mode = str(policy.get("mode") or "safe_default")
        if category in disabled:
            return "disabled"
        if category in approval_required:
            return "approval_required"
        if mode == "safe_default":
            if category in {"read", "draft"} or category in enabled:
                return "allowed"
            return "approval_required" if category in EXTERNAL_AUTONOMY_CATEGORIES else "disabled"
        if mode == "internal_write":
            if category in EXTERNAL_AUTONOMY_CATEGORIES and category not in enabled:
                return "approval_required"
            return "allowed"
        if mode == "supervised_external":
            if category in EXTERNAL_AUTONOMY_CATEGORIES and category not in enabled:
                return "approval_required"
            return "approval_required" if category in {"form_submit", "email_send", "account_create", "external_publish"} else "allowed"
        if mode == "dangerously_skip_permissions":
            return "allowed" if not enabled or category in enabled or category in selected else "disabled"
        return "allowed" if category in enabled or category in selected else "disabled"

    def _marketplace_tool_names_from_payload(self, payload: dict[str, Any]) -> list[str]:
        names: list[str] = []
        for source_name, raw_tools in self.marketplace_proxy._tool_sources(payload):
            for name in self.marketplace_proxy._names_for_log(raw_tools):
                if name not in names:
                    names.append(name)
        return names

    def _is_linkcrest_agent_api_identifier(self, name: Any) -> bool:
        text = str(name or "").strip()
        normalized = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
        return normalized in {"linkcrest_agent_api", "linkcrest_agentapi", "agentapi", "agent_api"}

    def _build_tool_policy_matrix(
        self,
        payload: dict[str, Any],
        policy: dict[str, Any] | None,
        agent: Any,
        marketplace_tool_names: list[str],
    ) -> dict[str, Any]:
        model_tool_names = {
            str(tool.get("function", {}).get("name") or "")
            for tool in (getattr(agent, "tools", None) or [])
            if isinstance(tool, dict)
        }
        descriptor_names = self._marketplace_tool_names_from_payload(payload)
        marketplace_lower = " ".join([*descriptor_names, *marketplace_tool_names]).lower()
        linkcrest_agent_api_attached = any(
            self._is_linkcrest_agent_api_identifier(name)
            for name in [*descriptor_names, *marketplace_tool_names]
        )
        matrix: dict[str, dict[str, Any]] = {}
        for category in AUTONOMY_CATEGORIES:
            policy_state = self._policy_allows_category(policy, category)
            candidates = list(AUTONOMY_TOOL_CANDIDATES.get(category) or ())
            attached_tools = [name for name in candidates if name in model_tool_names]
            attached = bool(attached_tools)
            available = attached
            reason = ""
            tool_status_override = self._tool_status_override(payload, policy, category)

            hints = AUTONOMY_MARKETPLACE_HINTS.get(category) or ()
            if category in {"email_send", "email_draft"}:
                marketplace_match = any(hint in marketplace_lower for hint in hints)
            else:
                marketplace_match = bool(hints and all(hint in marketplace_lower for hint in hints[:1]))
            if category == "linkcrest_agent_api":
                marketplace_match = linkcrest_agent_api_attached
            if marketplace_match and marketplace_tool_names:
                attached = True
                available = True
                attached_tools.extend(marketplace_tool_names)

            if category in {"read", "draft"} and not candidates:
                available = True
            if category in {"email_send", "email_draft"} and not available:
                reason = f"{category} unavailable: no configured mailbox/sender tool."
            elif category == "linkcrest_agent_api" and not available:
                reason = "LinkCrest Agent API tool descriptor missing or not granted by ClawChat."
            elif category in {"task_update", "status_update_internal", "lifecycle_contacted_submitted", "lifecycle_live_indexed", "linkcrest_openclaw_tools", "local_app_record_write"} and not marketplace_tool_names:
                reason = "LinkCrest/OpenClaw tools unavailable because ClawChat did not provide descriptors."
            elif category in {"credential_use"} and not available:
                reason = "credential_use unavailable: no configured credential broker/tool."
            elif candidates and not attached_tools:
                reason = f"{category} unavailable: required tool(s) not attached: {', '.join(candidates)}."
            elif not candidates and not marketplace_match and category not in {"read", "draft"}:
                reason = f"{category} unavailable: no configured runtime tool category."

            tool_status = "attached" if available else "unavailable"
            tool_connected = bool(available)
            tool_granted = bool(available)
            missing_credentials = False
            if tool_status_override:
                tool_connected = _bool_from_policy(tool_status_override.get("connected", tool_connected))
                tool_granted = _bool_from_policy(tool_status_override.get("granted", tool_granted))
                missing_credentials = _bool_from_policy(
                    tool_status_override.get("missingCredentials")
                    or tool_status_override.get("credentialsMissing")
                    or tool_status_override.get("missing_credentials")
                )
                raw_status = str(tool_status_override.get("status") or "").strip().lower()
                if missing_credentials:
                    tool_status = "missing_credentials"
                    reason = tool_status_override.get("reason") or f"{category} unavailable: required identity/credentials are missing."
                elif tool_connected and not tool_granted:
                    tool_status = "connected_but_not_granted"
                    reason = tool_status_override.get("reason") or f"{category} connected but not granted by ClawChat policy/tool grant."
                elif raw_status in {"unknown", "unavailable", "attached", "connected_but_not_granted", "missing_credentials"}:
                    tool_status = raw_status
                    reason = tool_status_override.get("reason") or reason
                available = tool_status == "attached"
                attached = attached or tool_connected

            matrix[category] = {
                "policy": policy_state,
                "tool": "available" if available else "unavailable",
                "toolStatus": tool_status,
                "attached": "attached" if attached else "not_attached",
                "toolConnected": tool_connected,
                "toolGranted": tool_granted,
                "tools": sorted(set(attached_tools)),
                "reason": reason,
                "agentInstruction": self._matrix_instruction(category, policy_state, tool_status, reason),
            }

        diagnostics = {
            "marketplaceToolsCount": len(descriptor_names),
            "marketplaceCallableToolsCount": len(marketplace_tool_names),
            "marketplaceToolNames": descriptor_names,
            "marketplaceCallableToolNames": marketplace_tool_names,
        }
        if not descriptor_names:
            diagnostics["marketplaceToolsDiagnostic"] = (
                "marketplaceToolsCount=0; reason: no descriptors from ClawChat; "
                "required: ClawChat must send local app tool descriptors."
            )
        result = {"categories": matrix, "diagnostics": diagnostics}
        result["missingToolRequests"] = self._missing_tool_requests_from_matrix(payload, policy, result)
        return result

    def _tool_status_override(self, payload: dict[str, Any], policy: dict[str, Any] | None, category: str) -> dict[str, Any] | None:
        candidates: list[Any] = []
        if policy:
            candidates.append(policy.get("toolStatusByCategory"))
            raw_policy = policy.get("raw")
            if isinstance(raw_policy, dict):
                candidates.extend([
                    raw_policy.get("toolStatusByCategory"),
                    raw_policy.get("tool_status_by_category"),
                    raw_policy.get("toolAvailability"),
                    raw_policy.get("tool_availability"),
                    raw_policy.get("toolGrants"),
                    raw_policy.get("tool_grants"),
                ])
        candidates.extend([
            payload.get("toolStatusByCategory"),
            payload.get("toolAvailability"),
            payload.get("toolGrants"),
        ])
        label = MISSING_TOOL_CAPABILITY_LABELS.get(category, category)
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            value = candidate.get(category) or candidate.get(label)
            if isinstance(value, dict):
                return value
            if isinstance(value, str):
                return {"status": value}
        return None

    def _matrix_instruction(self, category: str, policy_state: str, tool_status: str, reason: str) -> str:
        if policy_state == "disabled":
            return f"{category}: tool may exist, but current policy disables this action."
        if tool_status != "attached":
            return f"{category}: policy may allow this, but tool unavailable. Report: {reason}"
        if policy_state == "approval_required":
            return f"{category}: tool available, but approval is required before execution."
        return f"{category}: policy and tool availability allow this action; record evidence where applicable."

    def _missing_tool_requests_from_matrix(
        self,
        payload: dict[str, Any],
        policy: dict[str, Any] | None,
        matrix: dict[str, Any],
    ) -> list[dict[str, Any]]:
        if not policy:
            return []
        explicit = set(policy.get("enabledToolCategories") or []) | set(policy.get("selectedCapabilities") or [])
        app_slug = str(policy.get("appSlug") or payload.get("appSlug") or "").lower()
        if app_slug in {"local-linkcrest", "linkcrest"}:
            explicit |= {"linkcrest_openclaw_tools"}
            if self._linkcrest_backlink_execution_required(payload, policy):
                explicit |= LINKCREST_BACKLINK_REQUIRED_CATEGORIES
        requests: list[dict[str, Any]] = []
        for category, item in (matrix.get("categories") or {}).items():
            if explicit and category not in explicit:
                continue
            policy_state = str(item.get("policy") or "")
            tool_status = str(item.get("toolStatus") or "")
            if policy_state not in {"allowed", "approval_required"} or tool_status == "attached":
                continue
            requests.append(self._missing_tool_request_payload(payload, policy, category, item))
        return requests

    def _linkcrest_backlink_execution_required(self, payload: dict[str, Any], policy: dict[str, Any]) -> bool:
        if any(category in set(policy.get("enabledToolCategories") or []) | set(policy.get("selectedCapabilities") or []) for category in LINKCREST_BACKLINK_REQUIRED_CATEGORIES):
            return True
        text_parts = [
            payload.get("inputText"),
            payload.get("content"),
            payload.get("campaignName"),
            policy.get("campaignName"),
            payload.get("taskName"),
            payload.get("taskTitle"),
        ]
        text = " ".join(str(part or "") for part in text_parts).lower()
        return any(term in text for term in ("backlink", "outreach", "directory", "resource page", "prospect"))

    def _missing_tool_request_payload(
        self,
        payload: dict[str, Any],
        policy: dict[str, Any],
        category: str,
        matrix_item: dict[str, Any],
    ) -> dict[str, Any]:
        requested_capability = MISSING_TOOL_CAPABILITY_LABELS.get(category, category)
        suggested_apps, suggested_categories = MISSING_TOOL_SUGGESTIONS.get(category, ([], [requested_capability]))
        team_or_thread = str(policy.get("teamId") or payload.get("teamId") or payload.get("threadId") or "").strip()
        campaign_name = str(policy.get("campaignName") or payload.get("campaignName") or "").strip()
        app_slug = policy.get("appSlug") or self._runtime_context_string(payload, "appSlug", "app_slug", "slug")
        linked_app_id = policy.get("linkedAppId") or self._runtime_context_string(payload, "linkedAppId", "linked_app_id")
        campaign_id = policy.get("campaignId") or self._runtime_context_string(payload, "campaignId", "campaign_id", "selectedCampaignId")
        if not campaign_name:
            campaign_name = self._runtime_context_string(payload, "campaignName", "campaign_name", "selectedCampaignName") or ""
        related_task_id = payload.get("taskId") or payload.get("relatedTaskId")
        related_record_id = payload.get("recordId") or payload.get("relatedRecordId")
        dedupe_parts = [
            str(app_slug or ""),
            team_or_thread,
            campaign_name,
            requested_capability,
            self._required_action_for_category(category),
            str(related_task_id or related_record_id or ""),
        ]
        return {
            "runtimeDispatchId": payload.get("runtimeDispatchId") or payload.get("runtime_dispatch_id") or payload.get("dispatchId"),
            "agentId": payload.get("agentId") or payload.get("agent_id"),
            "externalAgentId": payload.get("externalAgentId"),
            "teamId": policy.get("teamId") or payload.get("teamId") or self._runtime_context_string(payload, "teamId", "team_id"),
            "threadId": payload.get("threadId") or self._runtime_context_string(payload, "threadId", "thread_id"),
            "linkedAppId": linked_app_id,
            "appSlug": app_slug,
            "campaignId": campaign_id,
            "campaignName": campaign_name or None,
            "requestedCapability": requested_capability,
            "requiredForAction": self._required_action_for_category(category),
            "reason": matrix_item.get("reason") or f"{requested_capability} is not attached or usable.",
            "relatedTaskId": related_task_id,
            "relatedRecordType": payload.get("recordType") or payload.get("relatedRecordType"),
            "relatedRecordId": related_record_id,
            "autonomyModeAtRequest": policy.get("mode"),
            "policyAllowed": matrix_item.get("policy") in {"allowed", "approval_required"},
            "toolAvailable": matrix_item.get("toolStatus") == "attached",
            "toolConnected": bool(matrix_item.get("toolConnected")),
            "toolGranted": bool(matrix_item.get("toolGranted")),
            "suggestedMarketplaceApps": suggested_apps,
            "suggestedToolCategories": suggested_categories,
            "requiredEvidenceType": self._required_evidence_for_category(category),
            "metadata": {
                "dispatchId": payload.get("dispatchId"),
                "runtimeSessionId": payload.get("runtimeSessionId"),
                "policyStatus": matrix_item.get("policy"),
                "toolStatus": matrix_item.get("toolStatus"),
                "attachedTools": matrix_item.get("tools") or [],
            },
            "timestamp": _now_iso(),
            "dedupeKey": "|".join(dedupe_parts),
        }

    def _required_action_for_category(self, category: str) -> str:
        return {
            "browser_navigation": "navigate to external site",
            "form_fill": "fill public form",
            "form_submit": "submit public form",
            "external_search": "search for backlink targets or verification",
            "email_draft": "draft outreach email",
            "email_send": "send outreach email",
            "account_create": "create external account",
            "credential_use": "use configured credentials",
            "external_publish": "publish external content",
            "backlink_verify": "verify backlink",
            "index_check": "check index status",
            "lifecycle_contacted_submitted": "update contacted/submitted lifecycle",
            "lifecycle_live_indexed": "update live/indexed lifecycle",
            "linkcrest_openclaw_tools": "call LinkCrest/OpenClaw operation",
            "local_app_record_write": "write local app record",
        }.get(category, category)

    def _required_evidence_for_category(self, category: str) -> str | None:
        return {
            "form_submit": "submission_receipt_or_confirmation",
            "email_send": "sent_message_id_or_mailbox_confirmation",
            "account_create": "account_confirmation",
            "external_publish": "published_url",
            "backlink_verify": "source_url_and_anchor_evidence",
            "index_check": "search_result_or_index_provider_evidence",
            "lifecycle_contacted_submitted": "contact_or_submission_evidence",
            "lifecycle_live_indexed": "verification_evidence",
        }.get(category)

    def _emit_missing_tool_requests(
        self,
        run: ActiveRun,
        payload: dict[str, Any],
        requests: list[dict[str, Any]],
    ) -> None:
        for request_payload in requests:
            request_payload.setdefault("externalAgentId", run.external_agent_id)
            request_payload.setdefault("metadata", {})
            if isinstance(request_payload["metadata"], dict):
                request_payload["metadata"].setdefault("runtimeDispatchId", payload.get("runtimeDispatchId") or payload.get("dispatchId"))
            logger.warning(
                "missing tool request dispatchId=%s externalAgentId=%s capability=%s reason=%s dedupeKey=%s",
                run.dispatch_id,
                run.external_agent_id,
                request_payload.get("requestedCapability"),
                request_payload.get("reason"),
                request_payload.get("dedupeKey"),
            )
            delivery = self._post_missing_tool_request(run, payload, request_payload)
            posted = bool(delivery.get("posted"))
            post_failed_reason = str(delivery.get("status") or delivery.get("fallbackReason") or "")
            if posted:
                request_payload["requestEmissionStatus"] = post_failed_reason or "created_or_deduped"
                logger.info(
                    "posted_to_clawchat_tool_requests dispatchId=%s externalAgentId=%s capability=%s dedupeKey=%s endpoint=%s statusCode=%s",
                    run.dispatch_id,
                    run.external_agent_id,
                    request_payload.get("requestedCapability"),
                    request_payload.get("dedupeKey"),
                    delivery.get("endpoint"),
                    delivery.get("statusCode"),
                )
            else:
                request_payload["requestEmissionStatus"] = "fallback_queued"
                request_payload["fallbackReason"] = post_failed_reason
                request_payload["fallbackDiagnostics"] = {
                    key: delivery.get(key)
                    for key in ("endpoint", "statusCode", "bodySummary", "runtimeDispatchIdPresent", "bridgeAuthPresent")
                    if delivery.get(key) is not None
                }
                logger.warning(
                    "post_failed_reason dispatchId=%s externalAgentId=%s capability=%s reason=%s dedupeKey=%s endpoint=%s statusCode=%s",
                    run.dispatch_id,
                    run.external_agent_id,
                    request_payload.get("requestedCapability"),
                    post_failed_reason,
                    request_payload.get("dedupeKey"),
                    delivery.get("endpoint"),
                    delivery.get("statusCode"),
                )
                self._queue_missing_tool_request(request_payload, reason=post_failed_reason, delivery=delivery)
            if not posted:
                run.emit({
                    "type": "run.status",
                    "dispatchId": run.dispatch_id,
                    "code": "missing_tool_request.fallback_queued",
                    "message": (
                        "Needed tool request could not be posted through the authenticated "
                        f"bridge endpoint and was queued locally: {request_payload.get('requestedCapability')}"
                    ),
                    "metadata": {
                        "requestedCapability": request_payload.get("requestedCapability"),
                        "fallbackReason": request_payload.get("fallbackReason"),
                    },
                })

    def _post_missing_tool_request(
        self,
        run: ActiveRun,
        payload: dict[str, Any],
        request_payload: dict[str, Any],
    ) -> dict[str, Any]:
        runtime_dispatch_id = str(
            request_payload.get("runtimeDispatchId")
            or payload.get("runtimeDispatchId")
            or payload.get("runtime_dispatch_id")
            or payload.get("dispatchId")
            or run.dispatch_id
            or ""
        ).strip()
        if runtime_dispatch_id:
            request_payload["runtimeDispatchId"] = runtime_dispatch_id
        endpoint = self._tool_request_endpoint_url(runtime_dispatch_id) if runtime_dispatch_id else None
        diagnostics: dict[str, Any] = {
            "runtimeDispatchIdPresent": bool(runtime_dispatch_id),
            "endpoint": endpoint,
            "bridgeAuthPresent": bool(getattr(self.bridge, "access_token", None)),
            "appSlug": request_payload.get("appSlug"),
            "linkedAppId": request_payload.get("linkedAppId"),
            "campaignId": request_payload.get("campaignId"),
            "campaignName": request_payload.get("campaignName"),
            "requestedCapability": request_payload.get("requestedCapability"),
            "posted": False,
            "fallbackJsonlWritten": False,
            "fallbackReason": None,
            "statusCode": None,
        }
        if not runtime_dispatch_id:
            diagnostics["fallbackReason"] = "missing_runtimeDispatchId"
            self._log_missing_tool_delivery_diagnostics(diagnostics)
            return diagnostics
        token = getattr(self.bridge, "access_token", None)
        if not token:
            diagnostics["fallbackReason"] = "missing_bridge_access_token"
            self._log_missing_tool_delivery_diagnostics(diagnostics)
            return diagnostics

        url = str(endpoint)
        body = json.dumps(request_payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                diagnostics["statusCode"] = int(response.status)
                if 200 <= int(response.status) < 300:
                    status = "created_or_deduped"
                    try:
                        body_text = response.read().decode("utf-8", errors="replace")
                        body_json = json.loads(body_text) if body_text else {}
                        if isinstance(body_json, dict):
                            raw_status = str(body_json.get("status") or body_json.get("state") or "").strip()
                            if raw_status:
                                status = raw_status
                            elif body_json.get("deduped") is True or body_json.get("alreadyOpen") is True:
                                status = "already_open_deduped"
                            elif body_json.get("created") is True:
                                status = "created"
                    except Exception:
                        status = "created_or_deduped"
                    diagnostics["posted"] = True
                    diagnostics["status"] = status
                    self._log_missing_tool_delivery_diagnostics(diagnostics)
                    return diagnostics
                diagnostics["fallbackReason"] = f"http_{response.status}"
                self._log_missing_tool_delivery_diagnostics(diagnostics)
                return diagnostics
        except urllib.error.HTTPError as exc:
            try:
                error_body = exc.read().decode("utf-8", errors="replace")[:500]
            except Exception:
                error_body = ""
            diagnostics["statusCode"] = int(exc.code)
            diagnostics["bodySummary"] = self._safe_body_summary(error_body)
            diagnostics["fallbackReason"] = f"http_{exc.code}:{diagnostics['bodySummary']}" if error_body else f"http_{exc.code}"
            self._log_missing_tool_delivery_diagnostics(diagnostics)
            return diagnostics
        except Exception as exc:
            diagnostics["fallbackReason"] = f"{type(exc).__name__}: {exc}"
            self._log_missing_tool_delivery_diagnostics(diagnostics)
            return diagnostics

    def _log_missing_tool_delivery_diagnostics(self, diagnostics: dict[str, Any]) -> None:
        logger.info(
            "needed tool request delivery diagnostics runtimeDispatchIdPresent=%s endpoint=%s bridgeAuthPresent=%s appSlug=%s linkedAppId=%s campaignId=%s campaignName=%s requestedCapability=%s postStatusCode=%s posted_to_clawchat_tool_requests=%s fallback_jsonl_written=%s fallbackReason=%s",
            diagnostics.get("runtimeDispatchIdPresent"),
            diagnostics.get("endpoint") or "<none>",
            diagnostics.get("bridgeAuthPresent"),
            diagnostics.get("appSlug") or "<none>",
            diagnostics.get("linkedAppId") or "<none>",
            diagnostics.get("campaignId") or "<none>",
            diagnostics.get("campaignName") or "<none>",
            diagnostics.get("requestedCapability") or "<none>",
            diagnostics.get("statusCode"),
            diagnostics.get("posted"),
            diagnostics.get("fallbackJsonlWritten"),
            diagnostics.get("fallbackReason") or "<none>",
        )

    def _safe_body_summary(self, text: str) -> str:
        return _redact_secret_fields(text[:500]) if text else ""

    def _tool_request_endpoint_url(self, runtime_dispatch_id: str) -> str:
        safe_dispatch_id = urllib.parse.quote(runtime_dispatch_id, safe="")
        path = f"/api/v1/bridge/runtime-dispatches/{safe_dispatch_id}/tool-requests"
        return urllib.parse.urljoin(f"{self.bridge.config.api_url}/", path.lstrip("/"))

    def _queue_missing_tool_request(
        self,
        request_payload: dict[str, Any],
        *,
        reason: str | None = None,
        delivery: dict[str, Any] | None = None,
    ) -> None:
        try:
            if delivery:
                request_payload.setdefault("fallbackReason", reason)
                request_payload.setdefault("fallbackDiagnostics", {
                    key: delivery.get(key)
                    for key in ("endpoint", "statusCode", "bodySummary", "runtimeDispatchIdPresent", "bridgeAuthPresent")
                    if delivery.get(key) is not None
                })
            self.missing_tool_queue_path.parent.mkdir(parents=True, exist_ok=True)
            with self.missing_tool_queue_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(request_payload, ensure_ascii=True, sort_keys=True) + "\n")
            if delivery is not None:
                delivery["fallbackJsonlWritten"] = True
                self._log_missing_tool_delivery_diagnostics(delivery)
            logger.warning(
                "fallback_jsonl_written path=%s capability=%s reason=%s dedupeKey=%s endpoint=%s statusCode=%s",
                self.missing_tool_queue_path,
                request_payload.get("requestedCapability"),
                reason or "not_posted",
                request_payload.get("dedupeKey"),
                (delivery or {}).get("endpoint"),
                (delivery or {}).get("statusCode"),
            )
        except Exception:
            logger.warning(
                "failed to queue missing tool request capability=%s dedupeKey=%s",
                request_payload.get("requestedCapability"),
                request_payload.get("dedupeKey"),
                exc_info=True,
            )

    def _autonomy_policy_prompt(
        self,
        policy: dict[str, Any] | None,
        matrix: dict[str, Any] | None,
    ) -> tuple[str | None, dict[str, Any]]:
        if not policy:
            return None, {
                "autonomyPolicyPresent": False,
                "autonomyMode": None,
                "toolPolicyMatrix": None,
            }
        mode = str(policy.get("mode") or "custom_policy")
        hard_stops = [str(item) for item in (policy.get("hardStops") or DEFAULT_HARD_STOPS)]
        stale_policy = str(policy.get("staleContextPolicy") or "").strip()
        lines = [
            f"CURRENT AUTONOMY POLICY FOR THIS APP: {mode}",
            "",
            "This current policy is the active app policy for this dispatch.",
        ]
        if stale_policy == "current_policy_supersedes_old_chat":
            lines.append("Older no-external-action instructions in chat history, scheduled messages, runtime summaries, or older generated docs are historical and superseded where they conflict with this current autonomy policy.")
        else:
            lines.append("Use this current policy when interpreting older chat history and installed docs.")
        lines.extend([
            "External execution is allowed only for categories marked policy=allowed and tool=available in the runtime matrix.",
            "If policy allows an action but the tool is unavailable, report \"tool unavailable\" with the reason; do not describe it as not allowed.",
            "If a tool exists but policy disables the action, report that current policy disables it.",
            "If policy and tool availability allow an action but identity or credentials are missing, report the missing identity/credential.",
            "Record evidence for external actions. Update contacted/submitted only after contact/submission actually occurred. Update live/indexed only after verification evidence.",
            "",
            "Hard stops:",
        ])
        lines.extend(f"- {item}" for item in hard_stops)
        if matrix:
            compact_rows = []
            for category, item in (matrix.get("categories") or {}).items():
                compact_rows.append(
                    f"- {category}: policy={item.get('policy')}; toolStatus={item.get('toolStatus')}; attached={item.get('attached')}; reason={item.get('reason') or 'none'}"
                )
            diagnostics = matrix.get("diagnostics") or {}
            missing_requests = matrix.get("missingToolRequests") or []
            lines.extend([
                "",
                "Runtime tool/capability matrix:",
                *compact_rows,
                "",
                "Needed Tools:",
                *(
                    [
                        f"- {request.get('requestedCapability')}: policy allows this, but the required tool is unavailable: {request.get('reason')} Structured Needed Tool request status: {request.get('requestEmissionStatus') or 'pending_emit'}."
                        for request in missing_requests
                    ]
                    or ["- none"]
                ),
                "",
                "When a needed tool blocks the current action, say whether the structured Needed Tool request was created, already open/deduped, or fallback-queued. Do not merely say the tool is unavailable in prose. Do not say \"not allowed\" unless policy actually blocks it. Do not repeatedly request the same missing tool every turn unless context materially changes.",
                "",
                "Marketplace/OpenClaw diagnostics:",
                f"- marketplaceToolsCount={diagnostics.get('marketplaceToolsCount', 0)}",
                f"- marketplaceCallableToolsCount={diagnostics.get('marketplaceCallableToolsCount', 0)}",
                f"- marketplaceToolNames={', '.join(diagnostics.get('marketplaceToolNames') or []) or '<none>'}",
                f"- marketplaceCallableToolNames={', '.join(diagnostics.get('marketplaceCallableToolNames') or []) or '<none>'}",
            ])
            if diagnostics.get("marketplaceToolsDiagnostic"):
                lines.append(f"- {diagnostics['marketplaceToolsDiagnostic']}")
        prompt = "\n".join(lines)
        return prompt, {
            "autonomyPolicyPresent": True,
            "autonomyMode": mode,
            "toolPolicyMatrix": matrix,
            "staleContextPolicy": stale_policy or None,
        }

    def _default_skills(self, payload: dict[str, Any]) -> list[str]:
        raw = payload.get("defaultSkills")
        if raw is None:
            config_metadata = payload.get("configMetadata")
            if isinstance(config_metadata, dict):
                raw = config_metadata.get("defaultSkills")
        if raw is None:
            return []
        if isinstance(raw, str):
            values = raw.split(",")
        elif isinstance(raw, (list, tuple)):
            values = [str(item) for item in raw if item is not None]
        else:
            values = [str(raw)]

        skills: list[str] = []
        seen: set[str] = set()
        for value in values:
            for part in str(value).split(","):
                skill = part.strip()
                if not skill or skill in seen:
                    continue
                if Path(skill).expanduser().is_absolute() or skill.startswith("~"):
                    raise WorkspaceError(
                        "invalid_default_skill",
                        "Hermes defaultSkills must be skill names, not filesystem paths",
                    )
                seen.add(skill)
                skills.append(skill)
        return skills

    def _build_default_skills_prompt(self, run: ActiveRun, payload: dict[str, Any]) -> tuple[str | None, list[str]]:
        skills = self._default_skills(payload)
        if not skills:
            return None, []
        from agent.skill_commands import build_preloaded_skills_prompt

        prompt, loaded_skills, missing_skills = build_preloaded_skills_prompt(
            skills,
            task_id=run.dispatch_id,
        )
        if missing_skills:
            raise WorkspaceError(
                "default_skill_not_found",
                f"Unknown Hermes default skill(s): {', '.join(missing_skills)}",
            )
        return prompt or None, loaded_skills

    def _recent_messages_from_payload(self, payload: dict[str, Any], current_input: str) -> list[dict[str, Any]]:
        raw = payload.get("recentMessages")
        if raw is None:
            metadata = payload.get("dispatchMetadata")
            if isinstance(metadata, dict):
                raw = metadata.get("recentMessages")
        if not isinstance(raw, list):
            return []
        messages: list[dict[str, Any]] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or item.get("speaker") or "").strip().lower()
            if role not in {"system", "user", "assistant", "tool"}:
                continue
            content = item.get("content")
            if content is None:
                content = item.get("text")
            if isinstance(content, list):
                normalized_content = content
            else:
                normalized_content = str(content or "")
            if not normalized_content:
                continue
            messages.append({"role": role, "content": normalized_content})
        if messages and messages[-1].get("role") == "user" and str(messages[-1].get("content") or "") == current_input:
            messages.pop()
        return messages

    def _conversation_history_for_run(
        self,
        snapshot: list[dict[str, Any]],
        payload: dict[str, Any],
        current_input: str,
    ) -> tuple[list[dict[str, Any]], str]:
        if snapshot:
            return snapshot, "bridge_snapshot"
        recent = self._recent_messages_from_payload(payload, current_input)
        if recent:
            return recent, "clawchat_recent_messages"
        return [], "empty"

    def _response_presentation(self, payload: dict[str, Any]) -> str:
        raw = payload.get("responsePresentation")
        if raw is None:
            response_contract = payload.get("responseContract")
            if isinstance(response_contract, dict):
                raw = response_contract.get("responsePresentation") or response_contract.get("presentation")
        return str(raw or "").strip()

    def _response_contract_prompt(self, payload: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
        presentation = self._response_presentation(payload)
        expected_format = str(payload.get("expectedContentFormat") or "").strip()
        fields_present = {
            "responsePresentation": bool(presentation),
            "expectedContentFormat": bool(expected_format),
            "responseFormatContract": payload.get("responseFormatContract") is not None,
            "runtimeInstruction": payload.get("runtimeInstruction") is not None,
            "systemInstruction": payload.get("systemInstruction") is not None,
            "responseContract": payload.get("responseContract") is not None,
        }
        logger.info(
            "Hermes dispatch responsePresentation=%s expectedContentFormat=%s contractFields=%s",
            presentation or "<none>",
            expected_format or "<none>",
            ",".join(name for name, present in fields_present.items() if present) or "<none>",
        )

        sections: list[str] = []
        system_instruction = _as_prompt_text(payload.get("systemInstruction"))
        runtime_instruction = _as_prompt_text(payload.get("runtimeInstruction"))
        response_format_contract = _as_prompt_text(payload.get("responseFormatContract"))
        response_contract = _as_prompt_text(payload.get("responseContract"))

        if system_instruction:
            sections.append("## ClawChat System Instruction\n" + system_instruction)
        if runtime_instruction:
            sections.append("## ClawChat Runtime Instruction\n" + runtime_instruction)
        if response_format_contract:
            sections.append("## ClawChat Response Format Contract\n" + response_format_contract)
        elif response_contract:
            sections.append("## ClawChat Response Contract\n" + response_contract)
        if expected_format:
            sections.append("## Expected Content Format\n" + expected_format)

        if presentation == "html_native":
            sections.append(
                "## HTML/CSS Native Response Mode\n"
                "Return the final answer directly as an HTML/CSS fragment suitable for ClawChat to sanitize and store. "
                "Use native HTML elements and inline or scoped CSS when styling is needed. "
                "Do not wrap the answer in Markdown fences, do not explain the HTML, and do not return Markdown for ClawChat to convert. "
                "This is the single normal model call; produce the final HTML/CSS output directly."
            )

        prompt = "\n\n".join(sections).strip() or None
        injected = bool(prompt)
        logger.info(
            "Hermes response contract prompt injected=%s responsePresentation=%s responseFormatContract=%s runtimeInstruction=%s",
            injected,
            presentation or "<none>",
            bool(response_format_contract),
            bool(runtime_instruction),
        )
        return prompt, {
            "responsePresentation": presentation or None,
            "expectedContentFormat": expected_format or None,
            "responseContractInjected": injected,
            "responseFormatContractPresent": bool(response_format_contract),
            "runtimeInstructionPresent": bool(runtime_instruction),
            "systemInstructionPresent": bool(system_instruction),
            "responseContractPresent": bool(response_contract),
        }

    def _compose_system_message(self, *parts: str | None) -> str | None:
        text = "\n\n".join(part.strip() for part in parts if isinstance(part, str) and part.strip())
        return text or None

    def _run_agent(self, run: ActiveRun, payload: dict[str, Any]) -> None:
        logger.info(
            "Hermes dispatch timing dispatchId=%s externalAgentId=%s phase=worker_started elapsedMs=%s",
            run.dispatch_id,
            run.external_agent_id,
            run.elapsed_ms(),
        )
        run.emit({
            "type": "run.worker_started",
            "dispatchId": run.dispatch_id,
            "runtimeRunId": str(payload.get("runtimeRunId") or run.dispatch_id),
            "metadata": {
                "workerStartedByHermesBridge": True,
                "source": payload.get("_dispatchSource") or "websocket",
                "workerStartedElapsedMs": run.elapsed_ms(),
            },
        })
        workspace_root, using_fallback_workspace = self._resolved_workspace_root(payload)
        payload["_resolvedWorkspaceRoot"] = workspace_root
        payload["_usingFallbackWorkspace"] = using_fallback_workspace
        if workspace_root and not Path(str(workspace_root)).exists():
            run.emit({
                "type": "run.failed",
                "dispatchId": run.dispatch_id,
                "code": "workspace_root_missing",
                "message": f"Workspace root does not exist on Hermes machine: {workspace_root}",
                "retryable": False,
            })
            run.done.set()
            self._finish(run.dispatch_id)
            return
        logger.info(
            "Hermes workspace resolved dispatchId=%s externalAgentId=%s workspaceId=%s runtimeSessionId=%s workspaceRoot=%s fallbackWorkspace=%s",
            run.dispatch_id,
            run.external_agent_id,
            payload.get("workspaceId") or payload.get("workspace_id") or "<none>",
            run.runtime_session_id,
            workspace_root or "<none>",
            using_fallback_workspace,
        )

        timeout_watchdog: threading.Timer | None = None
        policy = self._autonomy_policy_from_payload(payload)
        input_text = str(payload.get("inputText") or payload.get("content") or "")
        local_app_started = time.monotonic()
        local_app_context = self.local_app_runtime.prepare_for_run(run, payload)
        logger.info(
            "Hermes dispatch timing dispatchId=%s externalAgentId=%s phase=local_app_context_ready elapsedMs=%s phaseMs=%s",
            run.dispatch_id,
            run.external_agent_id,
            run.elapsed_ms(),
            int((time.monotonic() - local_app_started) * 1000),
        )
        if local_app_context:
            input_text = f"{local_app_context}\n\n{input_text}"
        try:
            lock_started = time.monotonic()
            with self._scoped_execution(run, payload, reason="agent_run"):
                logger.info(
                    "Hermes dispatch timing dispatchId=%s externalAgentId=%s phase=execution_lock_acquired elapsedMs=%s phaseMs=%s lockKey=%s",
                    run.dispatch_id,
                    run.external_agent_id,
                    run.elapsed_ms(),
                    int((time.monotonic() - lock_started) * 1000),
                    run.execution_lock_key or self._lock_key_for_run(run),
                )
                snapshot_loaded_at = _now_iso()
                snapshot_started = time.monotonic()
                snapshot = self.snapshot_store.load(run.runtime_session_id)
                snapshot_phase_ms = int((time.monotonic() - snapshot_started) * 1000)
                latest_message = snapshot[-1] if snapshot else {}
                latest_message_preview = str(latest_message.get("content") or latest_message.get("text") or "")[:240] if isinstance(latest_message, dict) else ""
                logger.info(
                    "Hermes runtime snapshot loaded dispatchId=%s externalAgentId=%s runtimeSessionId=%s loadedAt=%s messageCount=%s latestRole=%s latestPreview=%s lockKey=%s refreshedAfterLock=true phaseMs=%s elapsedMs=%s",
                    run.dispatch_id,
                    run.external_agent_id,
                    run.runtime_session_id,
                    snapshot_loaded_at,
                    len(snapshot),
                    latest_message.get("role") if isinstance(latest_message, dict) else None,
                    latest_message_preview.replace("\n", " "),
                    run.execution_lock_key or self._lock_key_for_run(run),
                    snapshot_phase_ms,
                    run.elapsed_ms(),
                )
                if policy and policy.get("refreshSnapshotBeforeRun"):
                    run.emit({
                        "type": "run.status",
                        "dispatchId": run.dispatch_id,
                        "code": "context.snapshot_refreshed",
                        "message": f"Runtime snapshot refreshed after execution lock; messages={len(snapshot)}.",
                    })
                with self._workspace_context(str(workspace_root) if workspace_root else None):
                    skills_context_started = time.monotonic()
                    with self._skills_context(payload) as skill_roots:
                        logger.info(
                            "Hermes dispatch timing dispatchId=%s externalAgentId=%s phase=skills_context_ready elapsedMs=%s phaseMs=%s skillRootsCount=%s defaultSkillsCount=%s",
                            run.dispatch_id,
                            run.external_agent_id,
                            run.elapsed_ms(),
                            int((time.monotonic() - skills_context_started) * 1000),
                            len(skill_roots),
                            len(self._default_skills(payload)),
                        )
                        logger.info(
                            "entering marketplace tool registration dispatchId=%s externalAgentId=%s timestamp=%s",
                            run.dispatch_id,
                            run.external_agent_id,
                            _now_iso(),
                        )
                        marketplace_started = time.monotonic()
                        with self.marketplace_proxy.registered_for_payload(payload, run) as marketplace_tool_names:
                            logger.info(
                                "Hermes dispatch timing dispatchId=%s externalAgentId=%s phase=marketplace_tools_registered elapsedMs=%s phaseMs=%s marketplaceToolsCount=%s",
                                run.dispatch_id,
                                run.external_agent_id,
                                run.elapsed_ms(),
                                int((time.monotonic() - marketplace_started) * 1000),
                                len(marketplace_tool_names),
                            )
                            agent_payload = {**payload, "_marketplaceToolNames": marketplace_tool_names, "_autonomyPolicy": policy}
                            with self._reference_tracking_context(run, agent_payload, skill_roots):
                                agent_build_started = time.monotonic()
                                agent = self._build_agent(run, agent_payload)
                                logger.info(
                                    "Hermes dispatch timing dispatchId=%s externalAgentId=%s phase=agent_built elapsedMs=%s phaseMs=%s",
                                    run.dispatch_id,
                                    run.external_agent_id,
                                    run.elapsed_ms(),
                                    int((time.monotonic() - agent_build_started) * 1000),
                                )
                                run.agent = agent
                                default_skills_started = time.monotonic()
                                skills_prompt, loaded_skills = self._build_default_skills_prompt(run, payload)
                                logger.info(
                                    "Hermes dispatch timing dispatchId=%s externalAgentId=%s phase=default_skills_loaded elapsedMs=%s phaseMs=%s loadedSkillsCount=%s",
                                    run.dispatch_id,
                                    run.external_agent_id,
                                    run.elapsed_ms(),
                                    int((time.monotonic() - default_skills_started) * 1000),
                                    len(loaded_skills),
                                )
                                response_contract_prompt, response_contract_metadata = self._response_contract_prompt(payload)
                                tool_policy_matrix = self._build_tool_policy_matrix(payload, policy, agent, marketplace_tool_names)
                                self._emit_missing_tool_requests(
                                    run,
                                    payload,
                                    tool_policy_matrix.get("missingToolRequests") or [],
                                )
                                autonomy_policy_prompt, autonomy_policy_metadata = self._autonomy_policy_prompt(policy, tool_policy_matrix)
                                if policy:
                                    run.emit({
                                        "type": "run.status",
                                        "dispatchId": run.dispatch_id,
                                        "code": "autonomy.policy",
                                        "message": f"Autonomy policy active: {policy.get('mode')}; marketplaceToolsCount={tool_policy_matrix.get('diagnostics', {}).get('marketplaceToolsCount', 0)}.",
                                    })
                                system_message = self._compose_system_message(autonomy_policy_prompt, skills_prompt, response_contract_prompt)
                                logger.info(
                                    "Hermes dispatch timing dispatchId=%s externalAgentId=%s phase=pre_model_ready elapsedMs=%s loadedSkillsCount=%s marketplaceToolsCount=%s",
                                    run.dispatch_id,
                                    run.external_agent_id,
                                    run.elapsed_ms(),
                                    len(loaded_skills),
                                    len(marketplace_tool_names),
                                )
                                timeout_watchdog = self._start_run_timeout_watchdog(run, payload)
                                if loaded_skills:
                                    run.emit({
                                        "type": "run.status",
                                        "dispatchId": run.dispatch_id,
                                        "code": "skills.loaded",
                                        "message": f"Loaded Hermes skill(s): {', '.join(loaded_skills)}",
                                    })
                                run.first_model_call_monotonic = time.monotonic()
                                conversation_history, conversation_history_source = self._conversation_history_for_run(
                                    snapshot,
                                    payload,
                                    input_text,
                                )
                                logger.info(
                                    "Hermes conversation history resolved dispatchId=%s externalAgentId=%s runtimeSessionId=%s source=%s bridgeSnapshotCount=%s recentMessagesCount=%s finalHistoryCount=%s",
                                    run.dispatch_id,
                                    run.external_agent_id,
                                    run.runtime_session_id,
                                    conversation_history_source,
                                    len(snapshot),
                                    len(self._recent_messages_from_payload(payload, input_text)),
                                    len(conversation_history),
                                )
                                logger.info(
                                    "Hermes dispatch timing dispatchId=%s externalAgentId=%s phase=first_model_call_start elapsedMs=%s",
                                    run.dispatch_id,
                                    run.external_agent_id,
                                    run.elapsed_ms(run.first_model_call_monotonic),
                                )
                                run.emit({
                                    "type": "run.model_started",
                                    "dispatchId": run.dispatch_id,
                                    "runtimeRunId": str(payload.get("runtimeRunId") or run.dispatch_id),
                                    "metadata": {
                                        "source": payload.get("_dispatchSource") or "websocket",
                                        "modelStartedElapsedMs": run.elapsed_ms(run.first_model_call_monotonic),
                                    },
                                })
                                result = agent.run_conversation(
                                    user_message=input_text,
                                    system_message=system_message,
                                    conversation_history=conversation_history,
                                    task_id=run.dispatch_id,
                                )
                                if run.done.is_set():
                                    logger.warning(
                                        "Hermes run returned after terminal state dispatchId=%s externalAgentId=%s terminalType=%s",
                                        run.dispatch_id,
                                        run.external_agent_id,
                                        run.terminal_event_type,
                                    )
                                    return

            if result.get("interrupted"):
                run.emit({"type": "run.cancelled", "dispatchId": run.dispatch_id})
            elif result.get("completed"):
                messages = result.get("messages")
                if isinstance(messages, list):
                    self.snapshot_store.save(run.runtime_session_id, messages)
                run.emit({
                    "type": "run.completed",
                    "dispatchId": run.dispatch_id,
                    "finalText": result.get("final_response") or "",
                    "metadata": {
                        "snapshotMessageCount": len(messages) if isinstance(messages, list) else None,
                        "hermesBridge": True,
                        "defaultSkills": loaded_skills,
                        "skillRoots": [str(root) for root in skill_roots],
                        "marketplaceTools": marketplace_tool_names,
                        "marketplaceToolsetRevision": agent_payload.get("_marketplaceToolsetRevision"),
                        "snapshotLoadedAt": snapshot_loaded_at,
                        "snapshotRefreshedAfterLock": True,
                        **autonomy_policy_metadata,
                        **response_contract_metadata,
                    },
                })
            else:
                run.emit({
                    "type": "run.failed",
                    "dispatchId": run.dispatch_id,
                    "code": "hermes_run_failed",
                    "message": result.get("error") or result.get("final_response") or "Hermes run failed",
                    "retryable": True,
                })
        except HermesExecutionLockTimeout:
            pass
        except HermesRunCancelled:
            pass
        except WorkspaceError as exc:
            logger.warning("Hermes bridge run rejected: %s", exc.message)
            run.emit({
                "type": "run.failed",
                "dispatchId": run.dispatch_id,
                "code": exc.code,
                "message": exc.message,
                "retryable": False,
            })
        except Exception as exc:
            logger.exception("Hermes bridge run failed")
            run.emit({
                "type": "run.failed",
                "dispatchId": run.dispatch_id,
                "code": "hermes_run_exception",
                "message": str(exc),
                "retryable": False,
            })
        finally:
            if timeout_watchdog:
                timeout_watchdog.cancel()
            if not run.terminal_event_type:
                logger.warning(
                    "Hermes run finished without terminal event; emitting failure dispatchId=%s externalAgentId=%s timestamp=%s",
                    run.dispatch_id,
                    run.external_agent_id,
                    _now_iso(),
                )
                run.emit({
                    "type": "run.failed",
                    "dispatchId": run.dispatch_id,
                    "code": "missing_terminal_event",
                    "message": "Hermes bridge run ended without a terminal event.",
                    "retryable": True,
                })
            if run.terminal_event_type:
                self.dispatch_state.record_terminal(
                    run.dispatch_id,
                    str(payload.get("runtimeRunId") or run.dispatch_id),
                    run.external_agent_id,
                    run.terminal_event_type,
                )
            run.done.set()
            self._finish(run.dispatch_id)


class HermesStructuredJobRunner:
    def __init__(self, bridge: "ClawChatHermesBridge") -> None:
        self.bridge = bridge
        self.run_manager = bridge.run_manager

    async def handle(self, payload: dict[str, Any]) -> None:
        job_id = str(payload.get("jobId") or "")
        if not job_id:
            logger.warning("received Hermes structured job without jobId")
            return

        try:
            output, model, metadata = await asyncio.wait_for(
                asyncio.to_thread(self._run_and_validate, payload),
                timeout=max(1, int(payload.get("timeoutMs") or 60_000)) / 1000,
            )
            await self.bridge.post_structured_job_result(job_id, {
                "output": output,
                "model": model,
                "usage": None,
                "metadata": metadata,
            })
        except asyncio.TimeoutError:
            await self.bridge.post_structured_job_error(job_id, {
                "code": "timeout",
                "message": "Hermes structured job timed out",
                "retryable": True,
                "metadata": {"runtimeType": "hermes"},
            })
        except StructuredJobError as exc:
            await self.bridge.post_structured_job_error(job_id, {
                "code": exc.code,
                "message": exc.message,
                "retryable": exc.retryable,
                "metadata": exc.metadata,
            })
        except Exception as exc:
            logger.exception("Hermes structured job failed jobId=%s", job_id)
            await self.bridge.post_structured_job_error(job_id, {
                "code": "runtime_error",
                "message": str(exc),
                "retryable": False,
                "metadata": {"runtimeType": "hermes"},
            })

    def _run_and_validate(self, payload: dict[str, Any]) -> tuple[dict[str, Any], str | None, dict[str, Any]]:
        job_id = str(payload.get("jobId") or "")
        raw_external_agent_id = str(payload.get("externalAgentId") or "").strip()
        external_agent_id = (
            raw_external_agent_id
            if profile_name_from_external_id(raw_external_agent_id)
            else _safe_segment(raw_external_agent_id)
        )
        if not external_agent_id:
            raise StructuredJobError("agent_not_live", "Hermes structured job missing externalAgentId")
        if external_agent_id not in self.bridge.config.external_agent_ids:
            raise StructuredJobError(
                "agent_not_live",
                f"Hermes agent is not registered on this bridge: {external_agent_id}",
                metadata={"externalAgentId": external_agent_id, "registeredAgents": self.bridge.config.external_agent_ids},
            )

        schema = payload.get("schema")
        if not isinstance(schema, dict):
            raise StructuredJobError("runtime_error", "Hermes structured job payload missing JSON schema")
        prompt = str(payload.get("prompt") or "")
        if not prompt:
            raise StructuredJobError("runtime_error", "Hermes structured job payload missing prompt")

        workspace_root = (
            get_hermes_home()
            if profile_name_from_external_id(external_agent_id)
            else (
                get_hermes_home()
                / "clawchat"
                / "agents"
                / external_agent_id
                / "workspace"
            )
        )
        if not workspace_root.exists():
            raise StructuredJobError(
                "agent_not_live",
                f"Hermes workspace root does not exist for agent: {external_agent_id}",
                metadata={"externalAgentId": external_agent_id, "workspaceRoot": str(workspace_root)},
            )

        run = ActiveRun(
            dispatch_id=job_id,
            runtime_session_id=f"structured-{job_id}",
            external_agent_id=external_agent_id,
        )
        structured_prompt = "\n\n".join([
            "This is a hidden ClawChat structured job.",
            "Return only a JSON object that conforms to the supplied JSON Schema.",
            "Do not include markdown, code fences, commentary, or a visible chat reply.",
            f"JSON Schema:\n{json.dumps(schema, ensure_ascii=True)}",
            prompt,
        ])
        run_payload = {
            **payload,
            "externalAgentId": external_agent_id,
            "workspaceRoot": str(workspace_root),
            "model": payload.get("model") or self.run_manager.default_model,
            "disabledToolsets": payload.get("disabledToolsets") or self.run_manager.default_disabled_toolsets,
        }

        timeout_watchdog: threading.Timer | None = None
        try:
            with self.run_manager._scoped_execution(run, run_payload, reason="structured_job"):
                with self.run_manager._workspace_context(str(workspace_root)):
                    with self.run_manager._skills_context(run_payload) as skill_roots:
                        agent = self.run_manager._build_agent(run, run_payload)
                        run.agent = agent
                        timeout_watchdog = self.run_manager._start_run_timeout_watchdog(run, run_payload)
                        result = agent.run_conversation(
                            user_message=structured_prompt,
                            system_message=None,
                            conversation_history=[],
                            task_id=job_id,
                        )
                        if run.done.is_set() and run.terminal_event_type:
                            raise StructuredJobError(
                                "timeout" if run.terminal_event_type == "run.failed" else "cancelled",
                                "Hermes structured job timed out or was cancelled",
                                retryable=True,
                                metadata={"runtimeType": "hermes", "externalAgentId": external_agent_id},
                            )
        finally:
            if timeout_watchdog:
                timeout_watchdog.cancel()

        if result.get("interrupted"):
            raise StructuredJobError("cancelled", "Hermes structured job was cancelled")
        if not result.get("completed"):
            raise StructuredJobError(
                "runtime_error",
                str(result.get("error") or result.get("final_response") or "Hermes structured job failed"),
                retryable=True,
            )

        output = _extract_json_object(str(result.get("final_response") or ""))
        validation_errors = _validate_json_schema(output, schema)
        if validation_errors:
            raise StructuredJobError(
                "schema_validation_failed",
                "; ".join(validation_errors[:6]),
                metadata={"runtimeType": "hermes", "externalAgentId": external_agent_id},
            )

        return output, str(run_payload.get("model") or "") or None, {
            "runtimeType": "hermes",
            "externalAgentId": external_agent_id,
            "skillRoots": [str(root) for root in skill_roots],
        }


class FakeHermesAgent:
    def __init__(self, *, stream_delta_callback=None, thinking_callback=None, reasoning_callback=None, tool_progress_callback=None, status_callback=None):
        self._interrupted = False
        self.stream_delta_callback = stream_delta_callback
        self.thinking_callback = thinking_callback
        self.reasoning_callback = reasoning_callback
        self.tool_progress_callback = tool_progress_callback
        self.status_callback = status_callback

    def interrupt(self, _message: str | None = None) -> None:
        self._interrupted = True

    def run_conversation(self, user_message: str, system_message: str | None = None, conversation_history: list[dict[str, Any]] | None = None, task_id: str | None = None) -> dict[str, Any]:
        history = list(conversation_history or [])
        if self.status_callback:
            self.status_callback("lifecycle", "fake Hermes bridge run started")
        if self.thinking_callback:
            self.thinking_callback(f"Planning response for: {user_message}")
        if self.tool_progress_callback:
            self.tool_progress_callback("terminal", "fake bridge run", {})
        reply_text = f"Hermes bridge reply: {user_message}"
        for chunk in ["Hermes ", "bridge ", "reply: ", user_message]:
            if self._interrupted:
                return {"interrupted": True, "completed": False, "final_response": "", "messages": history}
            if self.stream_delta_callback:
                self.stream_delta_callback(chunk)
            time.sleep(0.1)
        messages = history + [{"role": "user", "content": user_message}, {"role": "assistant", "content": reply_text}]
        return {"completed": True, "final_response": reply_text, "messages": messages}


class SkillReferenceTracker:
    def __init__(self, run: ActiveRun, payload: dict[str, Any], skill_roots: list[Path]):
        self.run = run
        self.payload = payload
        self.skill_roots = [root.resolve() for root in skill_roots]
        self.hermes_skills_root = (get_hermes_home() / "skills").resolve()
        self.references: list[dict[str, str]] = []
        self._seen_uris: set[str] = set()

    def record_skill_view(self, name: str, file_path: str | None, result: str) -> None:
        try:
            parsed = json.loads(result)
        except Exception:
            return
        if not isinstance(parsed, dict) or not parsed.get("success"):
            return

        skill_dir = self._skill_dir_from_result(name, parsed)
        if not skill_dir:
            return
        target_file = skill_dir / (file_path.strip() if isinstance(file_path, str) and file_path.strip() else "SKILL.md")
        try:
            target_file = target_file.resolve()
            target_file.relative_to(skill_dir.resolve())
        except Exception:
            return

        kind = "skill" if target_file.name == "SKILL.md" and target_file.parent == skill_dir.resolve() else "skill_reference"
        ref = self._reference_for(skill_dir.resolve(), target_file, kind)
        if ref:
            self.add(ref)

    def add(self, ref: dict[str, str]) -> None:
        uri = ref.get("uri")
        if not uri or uri in self._seen_uris:
            return
        self._seen_uris.add(uri)
        self.references.append(ref)
        self.run.emit({
            "type": "run.context",
            "dispatchId": self.run.dispatch_id,
            "totalTokens": None,
            "contextTokens": None,
            "percentUsed": None,
            "level": "unknown",
            "fresh": True,
            "references": list(self.references),
        })

    def _skill_dir_from_result(self, name: str, parsed: dict[str, Any]) -> Path | None:
        raw_skill_dir = parsed.get("skill_dir")
        if isinstance(raw_skill_dir, str) and raw_skill_dir.strip():
            path = Path(raw_skill_dir).expanduser()
            if path.is_dir():
                return path.resolve()
        return self._find_skill_dir(name)

    def _find_skill_dir(self, name: str) -> Path | None:
        raw_name = str(name or "").strip().lstrip("/")
        if not raw_name or Path(raw_name).expanduser().is_absolute() or raw_name.startswith("~"):
            return None

        local_category_name = None
        if ":" in raw_name:
            namespace, bare = raw_name.split(":", 1)
            if bare:
                local_category_name = f"{namespace}/{bare}"

        try:
            from agent.skill_utils import get_all_skills_dirs, iter_skill_index_files
        except Exception:
            return None

        all_dirs = [root for root in get_all_skills_dirs() if root.exists()]
        for search_dir in all_dirs:
            for candidate_name in [raw_name, local_category_name]:
                if not candidate_name:
                    continue
                direct = search_dir / candidate_name
                if direct.is_dir() and (direct / "SKILL.md").exists():
                    return direct.resolve()

        for search_dir in all_dirs:
            for found_skill_md in iter_skill_index_files(search_dir, "SKILL.md"):
                if found_skill_md.parent.name == raw_name:
                    return found_skill_md.parent.resolve()
        return None

    def _reference_for(self, skill_dir: Path, target_file: Path, kind: str) -> dict[str, str] | None:
        try:
            rel_to_skill = target_file.relative_to(skill_dir).as_posix()
        except ValueError:
            return None

        for root in self.skill_roots:
            try:
                rel_to_root = target_file.relative_to(root).as_posix()
            except ValueError:
                continue
            prefix = "agent" if self._is_agent_skill_root(root) else "shared"
            title = (
                f"{skill_dir.name}/SKILL.md"
                if kind == "skill"
                else rel_to_skill
            )
            return {
                "uri": f"{prefix}:/skills/{rel_to_root}",
                "title": title,
                "kind": kind,
                "source": "hermes",
            }

        try:
            rel_to_hermes = target_file.relative_to(self.hermes_skills_root).as_posix()
        except ValueError:
            rel_to_hermes = f"{skill_dir.name}/{rel_to_skill}"
        title = f"{skill_dir.name}/SKILL.md" if kind == "skill" else rel_to_skill
        return {
            "uri": f"hermes-skill:/{rel_to_hermes}",
            "title": title,
            "kind": kind,
            "source": "hermes",
        }

    def _is_agent_skill_root(self, root: Path) -> bool:
        external_agent_id = _safe_segment(str(self.payload.get("externalAgentId") or "agent"))
        expected = (
            get_hermes_home()
            / "clawchat"
            / "agents"
            / external_agent_id
            / "workspace"
            / "skills"
        ).resolve()
        return root == expected


class ClawChatHermesBridge:
    def __init__(self, config: BridgeConfig, config_path: Path | None = None) -> None:
        config.validate_for_run()
        self.config = config
        self.config_path = config_path or _config_path()
        self.session: aiohttp.ClientSession | None = None
        self.ws: aiohttp.ClientWebSocketResponse | None = None
        self.access_token: str | None = None
        self.run_manager = HermesRunManager(self)
        self.workspace_manager = HermesWorkspaceManager()
        self.marketplace_installer = MarketplaceSkillInstaller()
        self.marketplace_local_repo_docs_reader = MarketplaceLocalRepoDocsReader()
        self.marketplace_local_repo_docs_writer = MarketplaceLocalRepoDocsWriter()
        self.marketplace_local_app_agent_api_setup = MarketplaceLocalAppAgentApiSetup()
        self.marketplace_local_app_agent_api_request_proxy = MarketplaceLocalAppAgentApiRequestProxy(
            self.run_manager.local_app_runtime
        )
        self.structured_job_runner = HermesStructuredJobRunner(self)
        self._send_lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self._terminal_outbox_path = _config_dir() / "terminal_event_outbox.json"
        self._terminal_outbox: dict[str, PendingTerminalEvent] = self._load_terminal_outbox()
        self._terminal_outbox_lock = asyncio.Lock()
        self._terminal_retry_task: asyncio.Task[None] | None = None
        self._provision_callback_outbox_path = (
            _config_dir() / "provision_callback_outbox.json"
        )
        self._provision_callback_outbox = self._load_provision_callback_outbox()
        self._provision_callback_outbox_lock = asyncio.Lock()
        self._runtime_event_sent_monotonic: dict[tuple[str, str], float] = {}
        self._backfill_lock = asyncio.Lock()
        self._agent_sync_task: asyncio.Task[None] | None = None
        self._agent_sync_wakeup = asyncio.Event()
        self._agent_sync_protocol = RELAY_CONNECTOR_V3
        self._native_profiles: dict[str, NativeHermesProfile] = {}
        self._native_dispatch_profiles: dict[str, str] = {}
        self._registered_agent_ids: list[str] = list(
            dict.fromkeys(config.external_agent_ids)
        )
        self.profile_supervisor = HermesProfileSupervisor(
            worker_script=Path(__file__).with_name("profile_worker.py"),
            api_url=config.api_url,
            workspace_id=config.workspace_id or "",
            event_handler=self.send_event,
            message_handler=self._handle_profile_worker_message,
        )

    def _refresh_native_profiles(self) -> dict[str, NativeHermesProfile]:
        try:
            self._native_profiles = {
                profile.external_id: profile
                for profile in enumerate_native_profiles()
            }
            self.workspace_manager.native_profile_roots = {
                external_id: profile.home
                for external_id, profile in self._native_profiles.items()
            }
        except Exception:
            logger.warning("failed to enumerate native Hermes profiles", exc_info=True)
        return self._native_profiles

    def _agent_workspace_root(self, external_agent_id: str) -> Path:
        native = self._native_profiles.get(external_agent_id)
        if native:
            return native.home
        root = (
            get_hermes_home()
            / "clawchat"
            / "workspaces"
            / _safe_segment(self.config.workspace_id or "default")
            / "agents"
            / _safe_segment(external_agent_id)
            / "workspace"
        ).resolve()
        root.mkdir(parents=True, exist_ok=True)
        return root

    def _agent_workspace_roots(self, external_agent_id: str) -> list[Path]:
        native = self._native_profiles.get(external_agent_id)
        if native:
            return [native.home]
        # Inventory is read-only. Do not recreate a missing workspace here:
        # absence may be a transient mount or permission failure and must be
        # reported as an incomplete scan, never as an authoritative empty
        # manifest that tombstones every prior document.
        primary = (
            get_hermes_home()
            / "clawchat"
            / "workspaces"
            / _safe_segment(self.config.workspace_id or "default")
            / "agents"
            / _safe_segment(external_agent_id)
            / "workspace"
        ).resolve()
        legacy_default_workspace = (
            get_hermes_home()
            / "clawchat"
            / "workspaces"
            / "default"
            / "agents"
            / _safe_segment(external_agent_id)
            / "workspace"
        ).resolve()
        legacy_unscoped = (
            get_hermes_home()
            / "clawchat"
            / "agents"
            / _safe_segment(external_agent_id)
            / "workspace"
        ).resolve()
        roots = [primary]
        for candidate in [legacy_default_workspace, legacy_unscoped]:
            if candidate.is_dir() and candidate not in roots:
                roots.append(candidate)
        return roots

    async def run_forever(self) -> None:
        backoff_s = 1.0
        while not self._stop.is_set():
            try:
                await self._connect_once()
                backoff_s = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("ClawChat Hermes bridge disconnected: %s", exc)
            if self._stop.is_set():
                break
            await asyncio.sleep(backoff_s)
            backoff_s = min(backoff_s * 2, 30.0)

    def stop(self) -> None:
        self._stop.set()

    async def _connect_once(self) -> None:
        self._agent_sync_protocol = RELAY_CONNECTOR_V3
        async with aiohttp.ClientSession() as session:
            self.session = session
            auth = await self._authenticate_device(session)
            tokens = auth.get("tokens") or {}
            access_token = tokens.get("accessToken") or auth.get("accessToken")
            ws_token = access_token or tokens.get("wsToken") or auth.get("wsToken") or auth.get("token")
            self.access_token = access_token or tokens.get("wsToken") or auth.get("wsToken") or auth.get("token")
            if not ws_token:
                raise RuntimeError("ClawChat bridge auth response did not include wsToken")
            await self._flush_provision_callback_outbox()
            await self._publish_runtime_model_catalog(session)

            ws_url = _ws_url_for(self.config.api_url)
            logger.info("connecting ClawChat Hermes bridge websocket to %s", ws_url)
            async with session.ws_connect(ws_url, heartbeat=30) as ws:
                self.ws = ws
                await self._send_raw({"type": "authenticate", "token": ws_token, "capabilities": BRIDGE_CAPABILITIES})
                async for message in ws:
                    if message.type == aiohttp.WSMsgType.TEXT:
                        await self._handle_ws_text(message.data)
                    elif message.type in {aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR}:
                        break
                self.ws = None
                self._stop_terminal_retry_task()
                self._stop_agent_sync_task()
                await self.profile_supervisor.shutdown()

    async def _authenticate_device(self, session: aiohttp.ClientSession) -> dict[str, Any]:
        payload = {
            "devicePublicId": self.config.device_public_id,
            "deviceToken": self.config.device_token,
            **_bridge_device_metadata(),
        }
        url = f"{self.config.api_url}/api/v1/bridge/device/auth"
        async with session.post(url, json=payload) as response:
            text = await response.text()
            if response.status >= 400:
                raise RuntimeError(f"bridge device auth failed: HTTP {response.status} {text[:300]}")
            body = json.loads(text or "{}")
        credentials = body.get("credentials") or {}
        replacement_public_id = str(credentials.get("devicePublicId") or "").strip()
        replacement_token = str(credentials.get("deviceToken") or "").strip()
        if replacement_public_id != self.config.device_public_id or not replacement_token:
            raise RuntimeError(
                "bridge API v2 authentication response did not include matching replacement credentials"
            )
        # The backend consumes the previous credential during authentication.
        # Keep the replacement in memory even if the durable write fails, but
        # never use the returned access tokens until owner-only persistence succeeds.
        self.config.device_token = replacement_token
        compatibility = (body.get("device") or {}).get("compatibility") or {}
        self.config.compatibility_level = str(compatibility.get("level") or "").strip() or None
        self.config.operating_mode = str(compatibility.get("operatingMode") or "").strip() or None
        self.config.enabled_capabilities = [
            str(item).strip()
            for item in compatibility.get("enabledCapabilities") or []
            if str(item).strip()
        ]
        try:
            self.config.save(self.config_path)
        except Exception as exc:
            raise RuntimeError(
                "bridge API v2 authentication rotated the device credential but durable persistence failed; retry without restarting or re-enroll the device"
            ) from exc
        return body

    async def _publish_runtime_model_catalog(
        self,
        session: aiohttp.ClientSession,
    ) -> None:
        token = self.access_token
        if not token:
            logger.warning("Hermes model catalogue publication skipped: no bridge access token")
            return
        try:
            catalog = await asyncio.to_thread(_runtime_model_catalog)
            url = f"{self.config.api_url}/api/v1/bridge/runtime-model-catalog"
            async with session.post(
                url,
                json=catalog,
                headers={"Authorization": f"Bearer {token}"},
            ) as response:
                text = await response.text()
                if response.status >= 400:
                    raise RuntimeError(
                        "runtime model catalogue publication failed: "
                        f"HTTP {response.status} {text[:300]}"
                    )
            logger.info(
                "published Hermes model catalogue source=%s count=%s default=%s observedAt=%s",
                catalog["source"],
                len(catalog["models"]),
                catalog["defaultModel"],
                catalog["observedAt"],
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            # Catalogue publication must not prevent dispatch recovery or reconnect.
            logger.warning(
                "failed to publish Hermes model catalogue; bridge will continue",
                exc_info=True,
            )

    async def _run_reconnect_backfill(self, *, reason: str) -> None:
        if not self.session:
            logger.info("backfill skipped reason=no_http_session")
            return
        async with self._backfill_lock:
            agent_ids = list(self._registered_agent_ids)
            logger.info(
                "ClawChat Hermes bridge reconnect/auth success; backfill request starting reason=%s devicePublicId=%s workspaceId=%s registeredAgentIds=%s endpoint=%s timestamp=%s",
                reason,
                self.config.device_public_id,
                self.config.workspace_id or "<none>",
                ",".join(agent_ids),
                BACKFILL_ENDPOINT_PATH,
                _now_iso(),
            )
            if not agent_ids:
                logger.warning("backfill skipped reason=no_registered_agents")
                return

            last_error: str | None = None
            for attempt in range(1, BACKFILL_MAX_ATTEMPTS + 1):
                try:
                    dispatches = await self._fetch_backfill_dispatches(agent_ids)
                    logger.info(
                        "backfill response count=%s registeredAgentIds=%s attempt=%s",
                        len(dispatches),
                        ",".join(agent_ids),
                        attempt,
                    )
                    for payload in dispatches:
                        await self._accept_backfilled_dispatch(payload)
                    return
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    last_error = str(exc)
                    logger.warning(
                        "backfill API failure attempt=%s maxAttempts=%s error=%s endpoint=%s timestamp=%s",
                        attempt,
                        BACKFILL_MAX_ATTEMPTS,
                        exc,
                        BACKFILL_ENDPOINT_PATH,
                        _now_iso(),
                    )
                    if attempt >= BACKFILL_MAX_ATTEMPTS:
                        break
                    delay_s = min(BACKFILL_RETRY_MAX_S, BACKFILL_RETRY_BASE_S * (2 ** (attempt - 1)))
                    logger.info(
                        "backfill retry/backoff decision attempt=%s nextDelayS=%.1f reason=api_failure",
                        attempt,
                        delay_s,
                    )
                    await asyncio.sleep(delay_s)
            logger.error(
                "backfill exhausted without recovery endpoint=%s attempts=%s lastError=%s nextRetry=next_reconnect",
                BACKFILL_ENDPOINT_PATH,
                BACKFILL_MAX_ATTEMPTS,
                last_error,
            )

    async def _fetch_backfill_dispatches(self, agent_ids: list[str]) -> list[dict[str, Any]]:
        token = self.access_token
        if not token:
            raise RuntimeError("missing ClawChat access token for backfill")
        payload = {
            "devicePublicId": self.config.device_public_id,
            "workspaceId": self.config.workspace_id,
            "externalAgentIds": agent_ids,
            "states": ["pending", "started_unaccepted", "unaccepted"],
            "capabilities": BRIDGE_CAPABILITIES,
        }
        url = f"{self.config.api_url}{BACKFILL_ENDPOINT_PATH}"
        timeout = aiohttp.ClientTimeout(total=BACKFILL_REQUEST_TIMEOUT_S)
        assert self.session is not None
        async with self.session.post(
            url,
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
        ) as response:
            text = await response.text()
            if response.status == 404:
                raise RuntimeError(
                    "blocked by missing ClawChat endpoint: POST "
                    f"{BACKFILL_ENDPOINT_PATH} must return pending/unaccepted Hermes dispatches"
                )
            if response.status >= 400:
                raise RuntimeError(f"backfill HTTP {response.status}: {text[:300]}")
            body = json.loads(text or "{}")
        raw_dispatches = body if isinstance(body, list) else body.get("dispatches")
        if not isinstance(raw_dispatches, list):
            raise RuntimeError("backfill response must be an array or {dispatches: array}")
        return [item for item in raw_dispatches if isinstance(item, dict)]

    async def _accept_backfilled_dispatch(self, raw_payload: dict[str, Any]) -> None:
        payload = raw_payload.get("data") if isinstance(raw_payload.get("data"), dict) else raw_payload
        payload, normalization = DispatchPayloadNormalizer().normalize(payload)
        dispatch_id = str(payload.get("dispatchId") or "")
        runtime_run_id = str(payload.get("runtimeRunId") or dispatch_id)
        external_agent_id = str(payload.get("externalAgentId") or "").strip()
        status = str(
            payload.get("status")
            or payload.get("dispatchStatus")
            or payload.get("state")
            or ""
        ).strip().lower()
        logger.info(
            "backfill dispatch returned dispatchId=%s runtimeRunId=%s externalAgentId=%s status=%s createdAt=%s normalizedBytes=%s",
            dispatch_id,
            runtime_run_id,
            external_agent_id or "<none>",
            status or "<none>",
            payload.get("createdAt") or payload.get("created_at") or "<none>",
            normalization.normalized_size_bytes,
        )
        if not dispatch_id or not payload.get("runtimeSessionId"):
            logger.warning(
                "backfill dispatch skipped dispatchId=%s externalAgentId=%s reason=missing_required_ids",
                dispatch_id or "<none>",
                external_agent_id or "<none>",
            )
            return
        if external_agent_id not in self._registered_agent_ids:
            logger.warning(
                "backfill dispatch skipped dispatchId=%s externalAgentId=%s reason=unregistered_agent registeredAgentIds=%s",
                dispatch_id,
                external_agent_id,
                ",".join(self._registered_agent_ids),
            )
            return
        if status in {"cancelled", "canceled", "timed_out", "timeout", "expired", "completed", "failed"}:
            logger.info(
                "backfill dispatch skipped dispatchId=%s runtimeRunId=%s externalAgentId=%s reason=remote_terminal_status status=%s",
                dispatch_id,
                runtime_run_id,
                external_agent_id,
                status,
            )
            self.run_manager.dispatch_state.record_skipped_terminal(
                dispatch_id,
                runtime_run_id,
                external_agent_id,
                reason=f"remote_{status}",
            )
            return
        dedupe_reason = self.run_manager.dispatch_state.dedupe_reason(dispatch_id, runtime_run_id)
        if dedupe_reason:
            logger.info(
                "backfill dispatch skipped dispatchId=%s runtimeRunId=%s externalAgentId=%s reason=dedupe_hit dedupeReason=%s",
                dispatch_id,
                runtime_run_id,
                external_agent_id,
                dedupe_reason,
            )
            return
        payload["_bridgeReceivedMonotonic"] = time.monotonic()
        payload["_dispatchSource"] = "backfill"
        native_profile = self._refresh_native_profiles().get(external_agent_id)
        if native_profile:
            await self.profile_supervisor.dispatch(
                external_id=external_agent_id,
                profile_home=native_profile.home,
                binding_epoch=str(
                    payload.get("bindingEpoch")
                    or payload.get("assignmentEpoch")
                    or "0"
                ),
                payload=payload,
            )
            self._native_dispatch_profiles[dispatch_id] = external_agent_id
            logger.info(
                "backfill dispatch accepted by isolated profile worker dispatchId=%s externalAgentId=%s",
                dispatch_id,
                external_agent_id,
            )
            return
        try:
            self.run_manager.start(payload, source="backfill")
            logger.info(
                "backfill dispatch accepted dispatchId=%s runtimeRunId=%s externalAgentId=%s queueElapsedMs=0",
                dispatch_id,
                runtime_run_id,
                external_agent_id,
            )
        except HermesDispatchDedupe as exc:
            logger.info(
                "backfill dispatch skipped dispatchId=%s runtimeRunId=%s externalAgentId=%s reason=dedupe_hit dedupeReason=%s",
                dispatch_id,
                runtime_run_id,
                external_agent_id,
                exc,
            )
        except Exception as exc:
            logger.exception(
                "backfill dispatch failed to start dispatchId=%s runtimeRunId=%s externalAgentId=%s error=%s",
                dispatch_id,
                runtime_run_id,
                external_agent_id,
                exc,
            )

    def _parse_control_commands(self, payload: dict[str, Any]) -> list[tuple[str, str]] | None:
        raw = str(payload.get("inputText") or payload.get("content") or "").strip()
        if not raw:
            return None
        lines = [line.strip() for line in raw.splitlines() if line.strip()]
        if not lines or any(not line.startswith("/") for line in lines):
            return None

        commands: list[tuple[str, str]] = []
        for line in lines:
            body = line[1:].strip()
            if not body:
                return None
            name, _, rest = body.partition(" ")
            canonical = name.strip().lower().replace("_", "-")
            if canonical in {"new", "reset", "reload-skills", "stop"}:
                commands.append((canonical, rest.strip()))
            else:
                return None
        return commands or None

    async def _handle_control_dispatch(self, payload: dict[str, Any], commands: list[tuple[str, str]]) -> None:
        dispatch_id = str(payload.get("dispatchId") or "")
        runtime_session_id = str(payload.get("runtimeSessionId") or "")
        external_agent_id = _safe_segment(str(payload.get("externalAgentId") or "agent")) or "agent"
        completed: list[str] = []
        metadata: dict[str, Any] = {
            "hermesBridge": True,
            "runtimeControlCommand": True,
            "authoredBy": "hermes_bridge",
            # Team fan-out is owned by ClawChat: the bridge only receives one
            # Hermes-bound runtime dispatch at a time, scoped to this agent/session.
            "teamControlScope": "single_runtime_session",
        }

        try:
            for command, _args in commands:
                if command in {"new", "reset"}:
                    if not runtime_session_id:
                        raise ValueError("runtimeSessionId is required for /new and /reset")
                    deleted = self.run_manager.reset_runtime_session(runtime_session_id)
                    metadata["snapshotDeleted"] = deleted
                    metadata["runtimeSessionId"] = runtime_session_id
                    completed.append(f"Hermes session reset for {external_agent_id}.")
                elif command == "reload-skills":
                    result = self.run_manager.reload_skills_for_payload(payload)
                    metadata["reloadSkills"] = result
                    total = result.get("total") if isinstance(result, dict) else None
                    suffix = f" total={total}." if total is not None else "."
                    completed.append(f"Hermes skills reloaded for {external_agent_id}{suffix}")
                elif command == "stop":
                    cancelled = self.run_manager.cancel_for_external_agent(external_agent_id)
                    metadata["cancelledRuns"] = cancelled
                    if cancelled:
                        completed.append(f"Cancel requested for {cancelled} active Hermes run(s) for {external_agent_id}.")
                    else:
                        completed.append(f"No active Hermes run was running for {external_agent_id}.")

            logger.info(
                "Hermes control command executed dispatchId=%s externalAgentId=%s commands=%s runtimeSessionId=%s",
                dispatch_id,
                external_agent_id,
                ",".join(command for command, _args in commands),
                runtime_session_id or "<none>",
            )
            await self.send_event({
                "type": "run.completed",
                "dispatchId": dispatch_id,
                "externalAgentId": external_agent_id,
                "finalText": "\n".join(completed),
                "metadata": metadata,
            })
        except Exception as exc:
            logger.exception(
                "Hermes control command failed dispatchId=%s externalAgentId=%s",
                dispatch_id,
                external_agent_id,
            )
            await self.send_event({
                "type": "run.failed",
                "dispatchId": dispatch_id,
                "externalAgentId": external_agent_id,
                "code": "hermes_control_command_failed",
                "message": str(exc),
                "retryable": False,
                "metadata": metadata,
            })

    async def _handle_ws_text(self, text: str) -> None:
        try:
            message = json.loads(text)
        except json.JSONDecodeError:
            logger.debug("ignoring non-json websocket message")
            return
        msg_type = message.get("type")
        if msg_type == "authenticated":
            logger.info("ClawChat Hermes bridge authenticated")
            logger.info(
                "ClawChat Hermes bridge capabilities advertised capabilities=%s marketplaceLocalAppAgentApiRequestReady=%s",
                ",".join(BRIDGE_CAPABILITIES),
                MARKETPLACE_LOCAL_APP_AGENT_API_REQUEST_CAPABILITY in BRIDGE_CAPABILITIES,
            )
            synchronized_agent_ids = await self._exchange_agent_replicas()
            self._registered_agent_ids = list(
                dict.fromkeys(synchronized_agent_ids)
            )
            for external_id in synchronized_agent_ids:
                await self.register_hermes_agent(external_id)
            logger.info("registered Hermes agent(s): %s", ", ".join(synchronized_agent_ids))
            self._start_terminal_retry_task()
            self._start_agent_sync_task()
            await self._flush_terminal_outbox(reason="reconnect")
            await self._run_reconnect_backfill(reason="authenticated")
            return
        if msg_type in {"hermes_runtime_event_ack", "hermes.runtime_event.ack", "runtime_event_ack"}:
            await self._handle_runtime_event_ack(message)
            return
        if msg_type == "auth_error":
            raise RuntimeError(f"ClawChat websocket auth_error: {message.get('data') or message.get('error')}")
        if msg_type == "hermes.run.dispatch":
            received_monotonic = time.monotonic()
            data = message.get("data")
            if not isinstance(data, dict):
                logger.warning("received hermes.run.dispatch without data")
                return
            data, normalization = DispatchPayloadNormalizer().normalize(data)
            data["_bridgeReceivedMonotonic"] = received_monotonic
            logger.info(
                "received Hermes dispatch dispatchId=%s externalAgentId=%s thread=%s timestamp=%s",
                data.get("dispatchId"),
                data.get("externalAgentId"),
                data.get("threadId"),
                _now_iso(),
            )
            logger.info(
                "Hermes dispatch normalization dispatchId=%s incomingBytes=%s normalizedBytes=%s droppedDuplicateFields=%s toolCountBefore=%s toolCountAfter=%s instructionCharsBefore=%s instructionCharsAfter=%s",
                data.get("dispatchId"),
                normalization.incoming_size_bytes,
                normalization.normalized_size_bytes,
                ",".join(normalization.dropped_duplicate_fields) if normalization.dropped_duplicate_fields else "<none>",
                normalization.tool_count_before,
                normalization.tool_count_after,
                normalization.instruction_chars_before,
                normalization.instruction_chars_after,
            )
            external_agent_id = str(data.get("externalAgentId") or "").strip()
            native_profile = self._refresh_native_profiles().get(external_agent_id)
            if native_profile:
                dispatch_id = str(data.get("dispatchId") or "").strip()
                try:
                    await self.profile_supervisor.dispatch(
                        external_id=external_agent_id,
                        profile_home=native_profile.home,
                        binding_epoch=str(
                            data.get("bindingEpoch")
                            or data.get("assignmentEpoch")
                            or "0"
                        ),
                        payload=data,
                    )
                    if dispatch_id:
                        self._native_dispatch_profiles[dispatch_id] = external_agent_id
                except Exception as exc:
                    logger.exception(
                        "failed to dispatch to isolated Hermes profile worker profile=%s",
                        external_agent_id,
                    )
                    if dispatch_id:
                        await self.send_event({
                            "type": "run.failed",
                            "dispatchId": dispatch_id,
                            "externalAgentId": external_agent_id,
                            "code": str(exc) if str(exc).startswith("profile_") else "worker_failed",
                            "message": str(exc),
                            "retryable": True,
                        })
                return
            control_commands = self._parse_control_commands(data)
            if control_commands:
                await self._handle_control_dispatch(data, control_commands)
                return
            try:
                data["_dispatchSource"] = "websocket"
                self.run_manager.start(data)
            except HermesDispatchDedupe as exc:
                logger.info(
                    "skipped duplicate live Hermes dispatch dispatchId=%s externalAgentId=%s reason=%s",
                    data.get("dispatchId"),
                    data.get("externalAgentId"),
                    exc,
                )
            except Exception as exc:
                dispatch_id = str(data.get("dispatchId") or "")
                logger.exception("failed to start Hermes dispatch %s", dispatch_id)
                if dispatch_id:
                    await self.send_event({
                        "type": "run.failed",
                        "dispatchId": dispatch_id,
                        "code": "bridge_dispatch_start_failed",
                        "message": str(exc),
                        "retryable": False,
                    })
            return
        if msg_type == "hermes.run.cancel":
            data = message.get("data") if isinstance(message.get("data"), dict) else {}
            dispatch_id = str(data.get("dispatchId") or "")
            if dispatch_id:
                native_external_id = self._native_dispatch_profiles.get(dispatch_id)
                if native_external_id and await self.profile_supervisor.cancel(
                    native_external_id,
                    dispatch_id,
                ):
                    return
                cancelled = self.run_manager.cancel(dispatch_id)
                if not cancelled:
                    logger.info(
                        "Hermes cancel requested for unknown/missed dispatchId=%s; suppressing synthetic terminal retry timestamp=%s",
                        dispatch_id,
                        _now_iso(),
                    )
            return
        if msg_type == "agent.inventory.request":
            self._agent_sync_wakeup.set()
            logger.info("requested an immediate Hermes native profile inventory scan")
            return
        if msg_type in {
            "hermes.workspace.list",
            "hermes.workspace.read",
            "hermes.workspace.write",
            "hermes.workspace.delete",
            "hermes.workspace.mkdir",
        }:
            data = message.get("data")
            if not isinstance(data, dict):
                data = {}
            logger.info(
                "received Hermes workspace request type=%s requestId=%s folder=%s externalAgentId=%s",
                msg_type,
                data.get("requestId"),
                data.get("folder"),
                data.get("externalAgentId"),
            )
            result = await asyncio.to_thread(self.workspace_manager.handle, msg_type, data)
            await self.send_workspace_result(result)
            return
        if msg_type in {
            "clawchat.host.cron.list",
            "clawchat.host.scheduler.maintain",
        }:
            data = message.get("data")
            if not isinstance(data, dict):
                data = {}
            external_agent_id = str(data.get("externalAgentId") or "").strip()
            native_profile = self._refresh_native_profiles().get(external_agent_id)
            if not native_profile:
                await self._send_raw({
                    "type": f"{msg_type}.error",
                    "data": {
                        "requestId": data.get("requestId"),
                        "error": "profile_unavailable",
                    },
                })
                return
            try:
                await self.profile_supervisor.host_command(
                    external_id=external_agent_id,
                    profile_home=native_profile.home,
                    binding_epoch=str(
                        data.get("bindingEpoch")
                        or data.get("assignmentEpoch")
                        or "0"
                    ),
                    command_type=msg_type,
                    payload=data,
                )
            except Exception as exc:
                await self._send_raw({
                    "type": f"{msg_type}.error",
                    "data": {
                        "requestId": data.get("requestId"),
                        "error": str(exc),
                    },
                })
            return
        if msg_type == "hermes.agent.provision":
            data = message.get("data")
            if not isinstance(data, dict):
                data = {}
            logger.info(
                "received Hermes agent provision requestId=%s agentId=%s externalAgentId=%s name=%s",
                data.get("requestId"),
                data.get("agentId"),
                data.get("externalAgentId"),
                data.get("name"),
            )
            await self.handle_agent_provision(data)
            return
        if msg_type == "marketplace.installHermesSkill":
            data = message.get("data")
            if not isinstance(data, dict):
                data = {}
            data = {"type": msg_type, **data}
            logger.info(
                "received Hermes marketplace skill install requestId=%s agentId=%s appSlug=%s skillName=%s",
                data.get("requestId"),
                data.get("agentId"),
                data.get("appSlug"),
                data.get("skillName"),
            )
            external_agent_id = str(data.get("externalAgentId") or "").strip()
            native_profile = self._refresh_native_profiles().get(external_agent_id)
            if native_profile:
                try:
                    await self.profile_supervisor.install_skill(
                        external_id=external_agent_id,
                        profile_home=native_profile.home,
                        binding_epoch=str(
                            data.get("bindingEpoch")
                            or data.get("assignmentEpoch")
                            or "0"
                        ),
                        payload=data,
                    )
                except Exception as exc:
                    await self.send_marketplace_install_result({
                        "requestId": data.get("requestId"),
                        "status": "failed",
                        "agentId": data.get("agentId"),
                        "externalAgentId": external_agent_id,
                        "appSlug": data.get("appSlug"),
                        "error": {
                            "code": "profile_worker_failed",
                            "message": str(exc),
                        },
                    })
                return
            result = await asyncio.to_thread(self.marketplace_installer.install, data)
            await self.send_marketplace_install_result(result)
            return
        if msg_type == "marketplace.readLocalRepoDocs":
            data = message.get("data")
            if not isinstance(data, dict):
                data = {}
            logger.info(
                "received marketplace local repo docs read requestId=%s repoPath=%s docsSourcePath=%s",
                data.get("requestId"),
                data.get("repoPath"),
                data.get("docsSourcePath") or MarketplaceLocalRepoDocsReader.DEFAULT_DOCS_SOURCE_PATH,
            )
            result = await asyncio.to_thread(self.marketplace_local_repo_docs_reader.read, data)
            await self.send_marketplace_local_repo_docs_result(result)
            return
        if msg_type == "marketplace.applyLocalRepoDocs":
            data = message.get("data")
            if not isinstance(data, dict):
                data = {}
            logger.info(
                "received marketplace local repo docs apply requestId=%s repoPath=%s docsSourcePath=%s",
                data.get("requestId"),
                data.get("repoPath"),
                data.get("docsSourcePath") or MarketplaceLocalRepoDocsReader.DEFAULT_DOCS_SOURCE_PATH,
            )
            result = await asyncio.to_thread(self.marketplace_local_repo_docs_writer.apply, data)
            await self.send_marketplace_local_repo_docs_apply_result(result)
            return
        if msg_type == "marketplace.localAppAgentApiSetup":
            data = message.get("data")
            if not isinstance(data, dict):
                data = {}
            logger.info(
                "received marketplace local app Agent API setup requestId=%s appSlug=%s repoPath=%s localAppUrl=%s",
                data.get("requestId"),
                data.get("appSlug"),
                data.get("repoPath"),
                data.get("localAppUrl") or data.get("appUrl"),
            )
            result = await asyncio.to_thread(self.marketplace_local_app_agent_api_setup.setup, data)
            await self.send_marketplace_local_app_agent_api_setup_result(result)
            return
        if msg_type == "marketplace.localAppAgentApiRequest":
            data = message.get("data")
            if not isinstance(data, dict):
                data = {}
            logger.info(
                "received marketplace local app Agent API proxy requestId=%s appSlug=%s method=%s path=%s",
                data.get("requestId"),
                data.get("appSlug"),
                data.get("method"),
                data.get("path") or data.get("endpoint"),
            )
            result = await asyncio.to_thread(self.marketplace_local_app_agent_api_request_proxy.handle, data)
            await self.send_marketplace_local_app_agent_api_request_result(result)
            return
        if msg_type in {"localApp.getRuntimeStatus", "localApp.ensureRunning", "localApp.start", "localApp.restart"}:
            data = message.get("data")
            if not isinstance(data, dict):
                data = {}
            profile = data.get("runtimeProfile") if isinstance(data.get("runtimeProfile"), dict) else {}
            logger.info(
                "received local app runtime action name=%s requestId=%s appSlug=%s repoPath=%s appUrl=%s healthCheckUrl=%s backendHealthCheckUrl=%s",
                msg_type,
                data.get("requestId"),
                data.get("appSlug"),
                profile.get("repoPath"),
                profile.get("appUrl"),
                profile.get("healthCheckUrl"),
                profile.get("backendHealthCheckUrl"),
            )
            result = await asyncio.to_thread(self.run_manager.local_app_runtime.handle_action, msg_type, data)
            await self.send_local_app_runtime_result(msg_type, result)
            return
        if msg_type == "hermes.structured_job.dispatch":
            data = message.get("data")
            if not isinstance(data, dict):
                data = {}
            logger.info(
                "received Hermes structured job jobId=%s externalAgentId=%s jobType=%s",
                data.get("jobId"),
                data.get("externalAgentId"),
                data.get("jobType"),
            )
            external_agent_id = str(data.get("externalAgentId") or "").strip()
            native_profile = self._refresh_native_profiles().get(external_agent_id)
            if native_profile:
                asyncio.create_task(
                    self._dispatch_native_structured_job(
                        external_agent_id,
                        native_profile,
                        data,
                    )
                )
                return
            asyncio.create_task(self.structured_job_runner.handle(data))
            return
        logger.debug("ignored ClawChat websocket message type=%s", msg_type)

    async def _dispatch_native_structured_job(
        self,
        external_agent_id: str,
        native_profile: NativeHermesProfile,
        data: dict[str, Any],
    ) -> None:
        job_id = str(data.get("jobId") or "").strip()
        try:
            await self.profile_supervisor.dispatch_structured(
                external_id=external_agent_id,
                profile_home=native_profile.home,
                binding_epoch=str(
                    data.get("bindingEpoch")
                    or data.get("assignmentEpoch")
                    or "0"
                ),
                payload=data,
            )
        except Exception as exc:
            logger.exception(
                "failed to dispatch structured job to isolated profile jobId=%s profile=%s",
                job_id,
                external_agent_id,
            )
            if job_id:
                await self.post_structured_job_error(
                    job_id,
                    {
                        "code": str(exc)
                        if str(exc).startswith("profile_")
                        else "profile_worker_failed",
                        "message": str(exc),
                        "retryable": True,
                        "metadata": {
                            "runtimeType": "hermes",
                            "externalAgentId": external_agent_id,
                        },
                    },
                )

    async def send_event(self, event: dict[str, Any]) -> None:
        if event.get("type") in TERMINAL_EVENT_TYPES:
            dispatch_id = str(event.get("dispatchId") or "").strip()
            if dispatch_id:
                getattr(self, "_native_dispatch_profiles", {}).pop(
                    dispatch_id,
                    None,
                )
            await self._send_terminal_event(event)
            return
        logger.info(
            "sending Hermes runtime event type=%s dispatchId=%s socketOpen=%s",
            event.get("type"),
            event.get("dispatchId"),
            self._socket_open(),
        )
        dispatch_id = str(event.get("dispatchId") or "").strip()
        event_type = str(event.get("type") or "").strip()
        if dispatch_id and event_type:
            self._runtime_event_sent_monotonic[(dispatch_id, event_type)] = time.monotonic()
        await self._send_raw({"type": "hermes_runtime_event", "event": event})

    async def _handle_profile_worker_message(
        self,
        message: dict[str, Any],
    ) -> None:
        data = message.get("data")
        if (
            message.get("type") == "marketplace.installHermesSkill.result"
            and isinstance(data, dict)
        ):
            await self.send_marketplace_install_result(data)
            return
        if message.get("type") == "host_command.result":
            result_type = str(message.get("resultType") or "").strip()
            if result_type and isinstance(data, dict):
                await self._send_raw({"type": result_type, "data": data})
            return
        job_id = str(message.get("jobId") or "").strip()
        if not job_id or not isinstance(data, dict):
            logger.warning("ignored malformed Hermes profile worker response")
            return
        if message.get("type") == "structured_job.result":
            await self.post_structured_job_result(job_id, data)
        elif message.get("type") == "structured_job.error":
            await self.post_structured_job_error(job_id, data)

    def _socket_open(self) -> bool:
        return bool(self.ws and not self.ws.closed)

    def _terminal_event_id(self, event: dict[str, Any]) -> str:
        existing = event.get("eventId") or event.get("runtimeEventId")
        if existing:
            return str(existing)
        dispatch_id = str(event.get("dispatchId") or "unknown")
        event_type = str(event.get("type") or "terminal")
        return f"{dispatch_id}:{event_type}:{uuid4().hex}"

    async def _send_terminal_event(self, event: dict[str, Any]) -> None:
        event_id = self._terminal_event_id(event)
        event = dict(event)
        event["eventId"] = event_id
        async with self._terminal_outbox_lock:
            pending = self._terminal_outbox.get(event_id)
            if pending is None:
                pending = PendingTerminalEvent(event_id=event_id, event=event)
                self._terminal_outbox[event_id] = pending
                self._persist_terminal_outbox()
        await self._attempt_terminal_event_delivery(pending, reason="initial")

    async def _attempt_terminal_event_delivery(self, pending: PendingTerminalEvent, *, reason: str) -> None:
        if pending.acknowledged:
            return
        if pending.attempts >= TERMINAL_EVENT_MAX_ATTEMPTS:
            logger.error(
                "terminal event delivery exhausted waiting for terminal ack eventId=%s type=%s dispatchId=%s attempts=%s lastError=%s timestamp=%s",
                pending.event_id,
                pending.event.get("type"),
                pending.event.get("dispatchId"),
                pending.attempts,
                pending.last_error,
                _now_iso(),
            )
            return
        pending.attempts += 1
        if not pending.first_attempt_monotonic:
            pending.first_attempt_monotonic = time.monotonic()
        pending.last_attempt_monotonic = time.monotonic()
        envelope = {
            "type": "hermes_runtime_event",
            "eventId": pending.event_id,
            "requiresAck": True,
            "event": pending.event,
        }
        logger.info(
            "terminal event delivery attempt=%s reason=%s eventId=%s type=%s dispatchId=%s socketOpen=%s timestamp=%s",
            pending.attempts,
            reason,
            pending.event_id,
            pending.event.get("type"),
            pending.event.get("dispatchId"),
            self._socket_open(),
            _now_iso(),
        )
        try:
            await self._send_raw(envelope)
        except Exception as exc:
            pending.last_error = str(exc)
            self._persist_terminal_outbox()
            logger.warning(
                "terminal event delivery failed eventId=%s type=%s dispatchId=%s socketOpen=%s error=%s timestamp=%s",
                pending.event_id,
                pending.event.get("type"),
                pending.event.get("dispatchId"),
                self._socket_open(),
                exc,
                _now_iso(),
            )
            return
        pending.last_error = None
        self._persist_terminal_outbox()
        logger.info(
            "terminal event sent awaiting ack eventId=%s type=%s dispatchId=%s timestamp=%s firstAttemptMsAgo=%s",
            pending.event_id,
            pending.event.get("type"),
            pending.event.get("dispatchId"),
            _now_iso(),
            int((time.monotonic() - pending.first_attempt_monotonic) * 1000) if pending.first_attempt_monotonic else 0,
        )

    def _start_terminal_retry_task(self) -> None:
        if self._terminal_retry_task and not self._terminal_retry_task.done():
            return
        self._terminal_retry_task = asyncio.create_task(self._terminal_retry_loop())

    def _stop_terminal_retry_task(self) -> None:
        task = self._terminal_retry_task
        self._terminal_retry_task = None
        if task and not task.done():
            task.cancel()

    async def _terminal_retry_loop(self) -> None:
        try:
            while self._socket_open() and not self._stop.is_set():
                await asyncio.sleep(TERMINAL_EVENT_RETRY_INTERVAL_S)
                await self._flush_terminal_outbox(reason="retry")
        except asyncio.CancelledError:
            return

    async def _flush_terminal_outbox(self, *, reason: str) -> None:
        async with self._terminal_outbox_lock:
            pending_events = [pending for pending in self._terminal_outbox.values() if not pending.acknowledged]
        for pending in pending_events:
            if pending.attempts >= TERMINAL_EVENT_MAX_ATTEMPTS:
                if not pending.exhausted_logged:
                    pending.exhausted_logged = True
                    logger.error(
                        "terminal event delivery retry limit reached waiting for terminal ack eventId=%s type=%s dispatchId=%s attempts=%s lastError=%s timestamp=%s",
                        pending.event_id,
                        pending.event.get("type"),
                        pending.event.get("dispatchId"),
                        pending.attempts,
                        pending.last_error,
                        _now_iso(),
                    )
                continue
            if reason == "retry" and pending.last_attempt_monotonic:
                elapsed = time.monotonic() - pending.last_attempt_monotonic
                if elapsed < TERMINAL_EVENT_RETRY_INTERVAL_S:
                    continue
            await self._attempt_terminal_event_delivery(pending, reason=reason)

    async def _handle_runtime_event_ack(self, message: dict[str, Any]) -> None:
        data = message.get("data") if isinstance(message.get("data"), dict) else message
        event_id = str(data.get("eventId") or data.get("runtimeEventId") or "").strip()
        dispatch_id = str(data.get("dispatchId") or "").strip()
        event_type = str(data.get("eventType") or data.get("type") or "").strip()
        sent_at = self._runtime_event_sent_monotonic.pop((dispatch_id, event_type), None) if dispatch_id and event_type else None
        if sent_at is not None:
            logger.info(
                "runtime event acknowledged dispatchId=%s eventType=%s ackLatencyMs=%s",
                dispatch_id,
                event_type,
                int((time.monotonic() - sent_at) * 1000),
            )
        matched: list[str] = []
        async with self._terminal_outbox_lock:
            if event_id and event_id in self._terminal_outbox:
                matched.append(event_id)
            elif dispatch_id:
                for candidate_id, pending in self._terminal_outbox.items():
                    if pending.event.get("dispatchId") == dispatch_id and (
                        not event_type or pending.event.get("type") == event_type
                    ):
                        matched.append(candidate_id)
            for candidate_id in matched:
                pending = self._terminal_outbox.pop(candidate_id, None)
                if pending:
                    pending.acknowledged = True
                    ack_now = time.monotonic()
                    logger.info(
                        "terminal event acknowledged eventId=%s type=%s dispatchId=%s attempts=%s ackLatencyMs=%s totalAckMs=%s",
                        pending.event_id,
                        pending.event.get("type"),
                        pending.event.get("dispatchId"),
                        pending.attempts,
                        int((ack_now - pending.last_attempt_monotonic) * 1000) if pending.last_attempt_monotonic else None,
                        int((ack_now - pending.first_attempt_monotonic) * 1000) if pending.first_attempt_monotonic else None,
                    )
            if matched:
                self._persist_terminal_outbox()
        if not matched:
            logger.info(
                "received terminal event ack with no pending match eventId=%s dispatchId=%s eventType=%s",
                event_id or "<none>",
                dispatch_id or "<none>",
                event_type or "<none>",
            )

    def _load_terminal_outbox(self) -> dict[str, PendingTerminalEvent]:
        try:
            raw = json.loads(self._terminal_outbox_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except Exception:
            logger.warning("failed to load terminal event outbox", exc_info=True)
            return {}
        if not isinstance(raw, list):
            return {}
        outbox: dict[str, PendingTerminalEvent] = {}
        for item in raw:
            if not isinstance(item, dict):
                continue
            event_id = str(item.get("eventId") or "").strip()
            event = item.get("event")
            if not event_id or not isinstance(event, dict):
                continue
            pending = PendingTerminalEvent(
                event_id=event_id,
                event=event,
                attempts=max(0, int(item.get("attempts") or 0)),
                last_error=str(item.get("lastError") or "") or None,
            )
            outbox[event_id] = pending
        if outbox:
            logger.info("loaded pending terminal event outbox count=%s", len(outbox))
        return outbox

    def _persist_terminal_outbox(self) -> None:
        try:
            self._terminal_outbox_path.parent.mkdir(parents=True, exist_ok=True)
            payload = [
                {
                    "eventId": pending.event_id,
                    "event": pending.event,
                    "attempts": pending.attempts,
                    "lastError": pending.last_error,
                }
                for pending in self._terminal_outbox.values()
                if not pending.acknowledged
            ]
            tmp = self._terminal_outbox_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")
            tmp.replace(self._terminal_outbox_path)
        except Exception:
            logger.warning("failed to persist terminal event outbox", exc_info=True)

    def _start_agent_sync_task(self) -> None:
        if self._agent_sync_task and not self._agent_sync_task.done():
            return
        self._agent_sync_task = asyncio.create_task(self._agent_sync_loop())

    def _stop_agent_sync_task(self) -> None:
        if self._agent_sync_task and not self._agent_sync_task.done():
            self._agent_sync_task.cancel()
        self._agent_sync_task = None

    async def _agent_sync_loop(self) -> None:
        while self.ws and not self.ws.closed and not self._stop.is_set():
            try:
                try:
                    await asyncio.wait_for(self._agent_sync_wakeup.wait(), timeout=10)
                except asyncio.TimeoutError:
                    pass
                self._agent_sync_wakeup.clear()
                synchronized_agent_ids = await self._exchange_agent_replicas()
                self._registered_agent_ids = list(
                    dict.fromkeys(synchronized_agent_ids)
                )
                for external_id in synchronized_agent_ids:
                    await self.register_hermes_agent(external_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "Hermes agent replica sync failed code=%s",
                    _safe_agent_sync_error_code(exc),
                )

    def _agent_sync_state_path(self) -> Path:
        return _config_dir() / "agent_sync_state.json"

    def _load_agent_sync_state(self) -> dict[str, Any]:
        try:
            value = json.loads(self._agent_sync_state_path().read_text(encoding="utf-8"))
            if isinstance(value, dict):
                return {
                    "version": 2,
                    "profiles": value.get("profiles") if isinstance(value.get("profiles"), dict) else {},
                    "documents": value.get("documents") if isinstance(value.get("documents"), dict) else {},
                }
        except (OSError, ValueError, TypeError):
            pass
        return {"version": 2, "profiles": {}, "documents": {}}

    def _save_agent_sync_state(self, state: dict[str, Any]) -> None:
        path = self._agent_sync_state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(state, ensure_ascii=True, indent=2), encoding="utf-8")
        temporary.replace(path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    @staticmethod
    def _agent_document_hash(content: str) -> str:
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    @staticmethod
    def _agent_document_state_key(external_agent_id: str, folder: str, filename: str) -> str:
        relative = f"{folder}/{filename}" if folder else filename
        return f"{external_agent_id}:{relative}"

    @staticmethod
    def _split_agent_document_path(relative: str) -> tuple[str, str]:
        parts = [part for part in relative.replace("\\", "/").strip("/").split("/") if part]
        if not parts or len(parts) > 7 or any(part in {".", ".."} for part in parts):
            raise ValueError("invalid agent document path")
        return "/".join(parts[:-1]), parts[-1]

    def _scan_agent_documents(self, external_agent_id: str, limit: int) -> tuple[list[dict[str, Any]], bool]:
        native = self._native_profiles.get(external_agent_id)
        if native:
            try:
                return scan_profile_documents(native.home, limit=limit)
            except (OSError, ValueError):
                return [], False
        roots = self._agent_workspace_roots(external_agent_id)
        documents: list[dict[str, Any]] = []
        seen_paths: set[str] = set()
        complete = True
        for root in roots:
            try:
                scanned, root_complete = scan_profile_documents(
                    root,
                    limit=limit + 1,
                )
            except (OSError, ValueError):
                complete = False
                continue
            complete = complete and root_complete
            for document in scanned:
                relative_key = (
                    f"{document['folder']}/{document['filename']}"
                    if document["folder"]
                    else document["filename"]
                )
                if relative_key in seen_paths:
                    continue
                if len(documents) >= limit:
                    complete = False
                    break
                seen_paths.add(relative_key)
                documents.append(document)
        return documents, complete

    async def _exchange_agent_replicas(self) -> list[str]:
        if not self.session or not self.access_token:
            return []
        state = self._load_agent_sync_state()
        profiles = state["profiles"]
        document_state = state["documents"]
        agents: list[dict[str, Any]] = []
        live_keys: set[str] = set()
        native_profiles = self._refresh_native_profiles()
        inventory_ids = sorted(
            set(native_profiles).union(self.config.external_agent_ids)
        )
        complete_manifest = len(inventory_ids) <= 250
        complete_inventory = len(inventory_ids) <= 250
        incomplete_scan_ids: set[str] = set()
        for external_id in inventory_ids[:250]:
            native = native_profiles.get(external_id)
            local_profile = {
                "externalId": external_id,
                "name": native.display_name if native else external_id,
                "role": native.description if native and native.description else "assistant",
                "status": "active",
                "modelPrimary": native.model if native else _configured_default_model(),
                "description": native.description if native else None,
                "providerLabel": native.provider if native else None,
                "skillCount": native.skill_count if native else None,
                "nativeKind": "hermes_profile" if native else "relay_legacy_workspace",
            }
            profile_hash = self._agent_document_hash(json.dumps(local_profile, sort_keys=True))
            previous_profile = profiles.get(external_id) if isinstance(profiles.get(external_id), dict) else None
            document_sync_allowed = (
                self._agent_sync_protocol != RELAY_CONNECTOR_V3
                or bool(previous_profile and previous_profile.get("canonicalAgentId"))
            )
            documents, scan_complete = (
                self._scan_agent_documents(external_id, 2_000)
                if document_sync_allowed
                else ([], True)
            )
            if not scan_complete:
                complete_manifest = False
                incomplete_scan_ids.add(external_id)
            for document in documents:
                key = self._agent_document_state_key(external_id, document["folder"], document["filename"])
                live_keys.add(key)
                prior = document_state.get(key) if isinstance(document_state.get(key), dict) else None
                if prior:
                    document["objectId"] = prior.get("objectId")
                    document["baseServerVersion"] = prior.get("serverVersion")
            for key, prior in list(document_state.items()):
                if not document_sync_allowed or not scan_complete:
                    continue
                if not key.startswith(f"{external_id}:") or not isinstance(prior, dict) or prior.get("deleted") or key in live_keys:
                    continue
                if len(documents) >= 2_000:
                    complete_manifest = False
                    break
                folder, filename = self._split_agent_document_path(key[len(external_id) + 1 :])
                documents.append({
                    "folder": folder,
                    "filename": filename,
                    "content": "",
                    "contentHash": prior.get("contentHash"),
                    "objectId": prior.get("objectId"),
                    "baseServerVersion": prior.get("serverVersion"),
                    "deleted": True,
                })
            agent = {
                **local_profile,
                "bindingEpoch": previous_profile.get("bindingEpoch") if previous_profile else None,
                "profileBaseServerVersion": previous_profile.get("serverVersion") if previous_profile and previous_profile.get("localHash") != profile_hash else None,
                "documents": documents,
            }
            if (
                self._agent_sync_protocol == RELAY_CONNECTOR_V2
                and previous_profile
                and previous_profile.get("canonicalAgentId")
            ):
                agent["canonicalAgentId"] = previous_profile["canonicalAgentId"]
            agents.append(agent)

        url = f"{self.config.api_url}/api/v1/bridge/agent-sync/exchange"
        acknowledgements = [
                {
                    "objectId": entry.get("objectId"),
                    "serverVersion": entry.get("serverVersion"),
                    "contentHash": entry.get("contentHash"),
                    "status": "applied",
                }
                for entry in document_state.values()
                if isinstance(entry, dict) and entry.get("objectId") and entry.get("serverVersion")
            ]
        manifest_agents = []
        for agent in agents:
            profile = {key: agent.get(key) for key in ["externalId", "name", "role", "status", "modelPrimary"]}
            manifest_agents.append({
                "externalId": agent["externalId"],
                "canonicalAgentId": agent.get("canonicalAgentId"),
                "profileHash": self._agent_document_hash(json.dumps(profile, sort_keys=True, separators=(",", ":"))),
                "documents": sorted(
                    [
                        {
                            "folder": document.get("folder") or "",
                            "filename": document.get("filename") or "",
                            "contentHash": document.get("contentHash") or "",
                        }
                        for document in agent["documents"]
                        if not document.get("deleted")
                    ],
                    key=lambda document: f"{document['folder']}/{document['filename']}",
                ),
            })
        manifest_hash = self._agent_document_hash(json.dumps(manifest_agents, sort_keys=True, separators=(",", ":")))
        inventory_generation = self._agent_document_hash(
            json.dumps(
                [
                    {
                        "externalId": agent["externalId"],
                        "name": agent["name"],
                        "modelPrimary": agent.get("modelPrimary"),
                    }
                    for agent in agents
                ],
                sort_keys=True,
                separators=(",", ":"),
            )
        )

        def build_payload(protocol: str) -> dict[str, Any]:
            payload: dict[str, Any] = {
                "protocolVersion": protocol,
                "runtimeType": "hermes",
                "agents": agents,
                "acknowledgements": acknowledgements,
            }
            if protocol in {RELAY_CONNECTOR_V3, RELAY_CONNECTOR_V2}:
                payload.update({
                    "manifestHash": manifest_hash,
                    "completeManifest": complete_manifest,
                    "completeInventory": complete_inventory,
                    "inventoryGeneration": inventory_generation,
                    "host": {
                        "softwareVersion": PLUGIN_VERSION,
                        "protocolVersion": "3" if protocol == RELAY_CONNECTOR_V3 else "2",
                        "capabilities": {
                            "connectorProtocol": protocol,
                            "pluginVersion": PLUGIN_VERSION,
                            "runtimeVersion": OPEN_CORE_VERSION,
                            "completeManifest": complete_manifest,
                            "completeInventory": complete_inventory,
                            "metadataOnlyDiscovery": protocol == RELAY_CONNECTOR_V3,
                            "profileIsolation": "fixed_process",
                        },
                    },
                })
            else:
                for agent in payload["agents"]:
                    agent.pop("canonicalAgentId", None)
            return payload

        async def post_exchange(protocol: str) -> dict[str, Any]:
            payload = build_payload(protocol)
            async with self.session.post(
                url,
                json=payload,
                headers={"Authorization": f"Bearer {self.access_token}"},
            ) as response:
                text = await response.text()
                if response.status >= 400:
                    if (
                        protocol in {RELAY_CONNECTOR_V3, RELAY_CONNECTOR_V2}
                        and response.status in {400, 422}
                        and "UNSUPPORTED_AGENT_REPLICA_PROTOCOL" in text
                    ):
                        raise LookupError(f"{protocol} unsupported")
                    raise RuntimeError(
                        f"HERMES_AGENT_SYNC_HTTP_{response.status}"
                    )
                body = json.loads(text or "{}")
            if body.get("protocolVersion") != protocol:
                raise RuntimeError("HERMES_AGENT_SYNC_PROTOCOL_MISMATCH")
            return body

        while True:
            try:
                body = await post_exchange(self._agent_sync_protocol)
                break
            except LookupError:
                if self._agent_sync_protocol == RELAY_CONNECTOR_V3:
                    self._agent_sync_protocol = RELAY_CONNECTOR_V2
                    logger.warning(
                        "Relay backend does not support relay-connector.v3; "
                        "using relay-connector.v2 until bridge reconnect"
                    )
                    continue
                if self._agent_sync_protocol == RELAY_CONNECTOR_V2:
                    self._agent_sync_protocol = AGENT_REPLICA_V1
                    logger.warning(
                        "Relay backend does not support relay-connector.v2; "
                        "using agent-replica.v1 until bridge reconnect"
                    )
                    continue
                raise
        self._agent_sync_protocol = str(body.get("protocolVersion") or self._agent_sync_protocol)

        synchronized_agent_ids: list[str] = []
        for discovery in body.get("discoveries") or []:
            if not isinstance(discovery, dict):
                continue
            external_id = str(discovery.get("externalId") or "").strip()
            if (
                discovery.get("connectionState") != "connected"
                or not discovery.get("canonicalAgentId")
            ):
                profile_state = profiles.get(external_id)
                if isinstance(profile_state, dict):
                    profile_state.pop("canonicalAgentId", None)
        for remote_agent in body.get("agents") or []:
            if not isinstance(remote_agent, dict):
                continue
            external_id = str(remote_agent.get("externalId") or "").strip()
            if not external_id:
                continue
            synchronized_agent_ids.append(external_id)
            local_profile = next((agent for agent in agents if agent["externalId"] == external_id), None)
            if local_profile:
                profiles[external_id] = {
                    "serverVersion": str(remote_agent.get("profileServerVersion") or "1"),
                    "localHash": self._agent_document_hash(json.dumps({key: local_profile.get(key) for key in ["externalId", "name", "role", "status", "modelPrimary"]}, sort_keys=True)),
                    "canonicalAgentId": remote_agent.get("canonicalAgentId") or (profiles.get(external_id) or {}).get("canonicalAgentId"),
                    "bindingEpoch": str(
                        remote_agent.get("bindingEpoch")
                        or (profiles.get(external_id) or {}).get("bindingEpoch")
                        or ""
                    ) or None,
                }
            if external_id in incomplete_scan_ids:
                continue
            remote_documents = [
                remote
                for remote in (remote_agent.get("documents") or [])
                if isinstance(remote, dict)
            ]
            if not remote_documents:
                continue
            root = self._agent_workspace_root(external_id)
            for remote in remote_documents:
                folder = str(remote.get("folder") or "")
                filename = str(remote.get("filename") or "")
                safe_folder, safe_filename = self._split_agent_document_path(f"{folder}/{filename}" if folder else filename)
                try:
                    target = safe_profile_document_path(
                        root,
                        safe_folder,
                        safe_filename,
                    )
                except ValueError:
                    logger.warning(
                        "rejected non-allowlisted Hermes document externalAgentId=%s",
                        external_id,
                    )
                    continue
                key = self._agent_document_state_key(external_id, safe_folder, safe_filename)
                prior = document_state.get(key) if isinstance(document_state.get(key), dict) else None
                try:
                    local_content = target.read_text(encoding="utf-8") if target.exists() and target.is_file() and not target.is_symlink() else None
                except (OSError, UnicodeDecodeError):
                    local_content = None
                local_hash = self._agent_document_hash(local_content) if local_content is not None else None
                if prior and not prior.get("deleted") and local_hash is not None and local_hash != prior.get("contentHash") and local_hash != remote.get("contentHash") and str(remote.get("serverVersion")) != str(prior.get("serverVersion")):
                    logger.warning(
                        "preserving conflicting Hermes local edit externalAgentId=%s",
                        external_id,
                    )
                    continue
                if remote.get("deleted"):
                    if target.exists() and target.is_file() and not target.is_symlink():
                        target = safe_profile_document_path(
                            root,
                            safe_folder,
                            safe_filename,
                        )
                        target.unlink()
                    document_state[key] = {
                        "objectId": remote.get("objectId"),
                        "serverVersion": str(remote.get("serverVersion") or "0"),
                        "contentHash": remote.get("contentHash") or "",
                        "deleted": True,
                    }
                    continue
                content = remote.get("content")
                if not isinstance(content, str) or len(content.encode("utf-8")) > 500_000:
                    continue
                if local_hash != remote.get("contentHash"):
                    target = safe_profile_document_path(
                        root,
                        safe_folder,
                        safe_filename,
                    )
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target = safe_profile_document_path(
                        root,
                        safe_folder,
                        safe_filename,
                    )
                    temporary = target.with_suffix(target.suffix + ".clawchat.tmp")
                    try:
                        temporary.write_text(content, encoding="utf-8")
                        temporary.chmod(0o600)
                        target = safe_profile_document_path(
                            root,
                            safe_folder,
                            safe_filename,
                        )
                        temporary.replace(target)
                    finally:
                        temporary.unlink(missing_ok=True)
                document_state[key] = {
                    "objectId": remote.get("objectId"),
                    "serverVersion": str(remote.get("serverVersion") or "0"),
                    "contentHash": remote.get("contentHash") or self._agent_document_hash(content),
                    "deleted": False,
                }
        self._save_agent_sync_state(state)
        conflicts = body.get("conflicts") or []
        if conflicts:
            logger.warning("Hermes agent replica exchange retained %s conflict(s)", len(conflicts))
        return synchronized_agent_ids

    async def register_hermes_agent(self, external_agent_id: str) -> None:
        await self._send_raw({
            "type": "register_hermes_agent",
            "externalAgentId": external_agent_id,
            "capabilities": BRIDGE_CAPABILITIES,
        })

    async def handle_agent_provision(self, data: dict[str, Any]) -> None:
        agent_id = str(data.get("agentId") or "").strip()
        runtime_host_id = str(data.get("runtimeHostId") or "").strip()
        if (
            not agent_id
            or not runtime_host_id
            or data.get("runtimeType") not in {None, "hermes"}
        ):
            logger.warning("Hermes agent provision missing or invalid target identity")
            return
        try:
            result = await asyncio.to_thread(
                provision_native_profile,
                data,
                state_dir=_config_dir(),
            )
            self._refresh_native_profiles()
            callback_payload = {
                "runtimeHostId": runtime_host_id,
                "externalAgentId": result.profile.external_id,
                "nativeProfileName": result.profile.native_name,
                "idempotencyKey": str(data.get("idempotencyKey") or ""),
                "idempotentReplay": result.idempotent_replay,
                "profile": result.profile.metadata(),
            }
            await self._enqueue_provision_callback(
                agent_id,
                callback_payload,
                failed=False,
            )
            try:
                await self._post_hermes_provision_result(
                    agent_id,
                    callback_payload,
                    failed=False,
                )
            except Exception:
                logger.warning(
                    "native Hermes provision completion callback queued agentId=%s",
                    agent_id,
                    exc_info=True,
                )
            self._agent_sync_wakeup.set()
            logger.info(
                "provisioned native Hermes profile externalAgentId=%s replay=%s",
                result.profile.external_id,
                result.idempotent_replay,
            )
        except Exception as exc:
            error_code = _safe_native_provision_error_code(exc)
            logger.error(
                "native Hermes profile provisioning failed agentId=%s code=%s",
                agent_id,
                error_code,
            )
            callback_payload = {
                "runtimeHostId": runtime_host_id,
                "idempotencyKey": str(data.get("idempotencyKey") or ""),
                "error": error_code,
            }
            await self._enqueue_provision_callback(
                agent_id,
                callback_payload,
                failed=True,
            )
            try:
                await self._post_hermes_provision_result(
                    agent_id,
                    callback_payload,
                    failed=True,
                )
            except Exception:
                logger.warning(
                    "native Hermes provision failure callback queued agentId=%s",
                    agent_id,
                    exc_info=True,
                )

    async def _enqueue_provision_callback(
        self,
        agent_id: str,
        payload: dict[str, Any],
        *,
        failed: bool,
    ) -> None:
        key = (
            f"{agent_id}:{payload.get('idempotencyKey') or ''}:"
            f"{'fail' if failed else 'complete'}"
        )
        async with self._provision_callback_outbox_lock:
            self._provision_callback_outbox[key] = {
                "agentId": agent_id,
                "payload": payload,
                "failed": failed,
            }
            self._persist_provision_callback_outbox()

    async def _post_hermes_provision_result(
        self,
        agent_id: str,
        payload: dict[str, Any],
        *,
        failed: bool,
    ) -> None:
        if not self.session or not self.access_token:
            raise RuntimeError("Hermes provision callback channel is unavailable")
        suffix = "fail" if failed else "complete"
        url = (
            f"{self.config.api_url}/api/v1/bridge/hermes-provisions/"
            f"{urllib.parse.quote(agent_id, safe='')}/{suffix}"
        )
        async with self.session.post(
            url,
            json=payload,
            headers={"Authorization": f"Bearer {self.access_token}"},
        ) as response:
            text = await response.text()
            if response.status >= 400:
                raise RuntimeError(
                    f"Hermes provision callback failed: HTTP {response.status} {text[:300]}"
                )
        key = (
            f"{agent_id}:{payload.get('idempotencyKey') or ''}:"
            f"{'fail' if failed else 'complete'}"
        )
        async with self._provision_callback_outbox_lock:
            self._provision_callback_outbox.pop(key, None)
            self._persist_provision_callback_outbox()

    def _load_provision_callback_outbox(self) -> dict[str, dict[str, Any]]:
        try:
            raw = json.loads(
                self._provision_callback_outbox_path.read_text(encoding="utf-8")
            )
        except FileNotFoundError:
            return {}
        except Exception:
            logger.warning("failed to load Hermes provision callback outbox", exc_info=True)
            return {}
        return {
            str(key): value
            for key, value in raw.items()
            if isinstance(key, str) and isinstance(value, dict)
        } if isinstance(raw, dict) else {}

    def _persist_provision_callback_outbox(self) -> None:
        try:
            self._provision_callback_outbox_path.parent.mkdir(
                parents=True,
                exist_ok=True,
            )
            temporary = self._provision_callback_outbox_path.with_suffix(
                ".json.tmp"
            )
            temporary.write_text(
                json.dumps(
                    self._provision_callback_outbox,
                    ensure_ascii=True,
                    indent=2,
                ),
                encoding="utf-8",
            )
            os.chmod(temporary, 0o600)
            temporary.replace(self._provision_callback_outbox_path)
        except Exception:
            logger.warning("failed to persist Hermes provision callback outbox", exc_info=True)

    async def _flush_provision_callback_outbox(self) -> None:
        for item in list(self._provision_callback_outbox.values()):
            agent_id = str(item.get("agentId") or "").strip()
            payload = item.get("payload")
            if not agent_id or not isinstance(payload, dict):
                continue
            try:
                await self._post_hermes_provision_result(
                    agent_id,
                    payload,
                    failed=bool(item.get("failed")),
                )
            except Exception:
                logger.warning(
                    "Hermes provision callback remains queued agentId=%s",
                    agent_id,
                    exc_info=True,
                )

    async def send_workspace_result(self, data: dict[str, Any]) -> None:
        logger.info(
            "sending Hermes workspace result requestId=%s ok=%s",
            data.get("requestId"),
            data.get("ok"),
        )
        await self._send_raw({"type": "hermes.workspace.result", "data": data})

    async def send_marketplace_install_result(self, data: dict[str, Any]) -> None:
        logger.info(
            "sending Hermes marketplace skill install result requestId=%s status=%s agentId=%s appSlug=%s",
            data.get("requestId"),
            data.get("status"),
            data.get("agentId"),
            data.get("appSlug"),
        )
        await self._send_raw({"type": "marketplace.installHermesSkill.result", "data": data})

    async def send_marketplace_local_repo_docs_result(self, data: dict[str, Any]) -> None:
        logger.info(
            "sending marketplace local repo docs result requestId=%s status=%s repoPath=%s files=%s",
            data.get("requestId"),
            data.get("status"),
            data.get("repoPath"),
            len(data.get("files") or []),
        )
        await self._send_raw({"type": "marketplace.readLocalRepoDocs.result", "data": data})

    async def send_marketplace_local_repo_docs_apply_result(self, data: dict[str, Any]) -> None:
        logger.info(
            "sending marketplace local repo docs apply result requestId=%s status=%s repoPath=%s filesWritten=%s filesSkipped=%s",
            data.get("requestId"),
            data.get("status"),
            data.get("repoPath"),
            len(data.get("filesWritten") or []),
            len(data.get("filesSkipped") or []),
        )
        await self._send_raw({"type": "marketplace.applyLocalRepoDocs.result", "data": data})

    async def send_marketplace_local_app_agent_api_setup_result(self, data: dict[str, Any]) -> None:
        logger.info(
            "sending marketplace local app Agent API setup result requestId=%s status=%s repoPath=%s appReachable=%s agentApiRouteReachable=%s bearerConfigured=%s campaigns=%s selectedCampaign=%s",
            data.get("requestId"),
            data.get("status"),
            data.get("repoPath"),
            data.get("appReachable"),
            data.get("agentApiRouteReachable"),
            data.get("bearerConfigured"),
            len(data.get("campaigns") or []),
            bool(data.get("selectedCampaign")),
        )
        await self._send_raw({"type": "marketplace.localAppAgentApiSetup.result", "data": data})

    async def send_marketplace_local_app_agent_api_request_result(self, data: dict[str, Any]) -> None:
        logger.info(
            "sending marketplace local app Agent API proxy result requestId=%s status=%s httpStatus=%s errorCode=%s",
            data.get("requestId"),
            data.get("status"),
            data.get("httpStatus"),
            (data.get("error") or {}).get("code") if isinstance(data.get("error"), dict) else None,
        )
        await self._send_raw({
            "type": "marketplace.localAppAgentApiRequest.result",
            "requestId": data.get("requestId"),
            "data": data,
        })

    async def send_local_app_runtime_result(self, action: str, data: dict[str, Any]) -> None:
        runtime_status = data.get("runtimeStatus") if isinstance(data.get("runtimeStatus"), dict) else {}
        logger.info(
            "sending local app runtime result action=%s requestId=%s status=%s runtimeState=%s errorCode=%s",
            action,
            data.get("requestId"),
            data.get("status"),
            data.get("runtimeState") or runtime_status.get("runtimeState"),
            (data.get("error") or {}).get("code") if isinstance(data.get("error"), dict) else None,
        )
        await self._send_raw({"type": f"{action}.result", "requestId": data.get("requestId"), "data": data})

    async def post_structured_job_result(self, job_id: str, data: dict[str, Any]) -> None:
        await self._post_structured_job(job_id, "result", data)
        logger.info("posted Hermes structured job result jobId=%s", job_id)

    async def post_structured_job_error(self, job_id: str, data: dict[str, Any]) -> None:
        await self._post_structured_job(job_id, "error", data)
        logger.info("posted Hermes structured job error jobId=%s code=%s", job_id, data.get("code"))

    async def _post_structured_job(self, job_id: str, kind: str, data: dict[str, Any]) -> None:
        if not self.session:
            raise RuntimeError("ClawChat HTTP session is not connected")
        token = self.access_token
        if not token:
            auth = await self._authenticate_device(self.session)
            token = ((auth.get("tokens") or {}).get("accessToken") or auth.get("accessToken") or (auth.get("tokens") or {}).get("wsToken") or auth.get("wsToken") or auth.get("token"))
            self.access_token = token
        if not token:
            raise RuntimeError("ClawChat bridge auth response did not include accessToken")
        url = f"{self.config.api_url}/api/v1/bridge/structured-jobs/{job_id}/{kind}"
        async with self.session.post(
            url,
            json=data,
            headers={"Authorization": f"Bearer {token}"},
        ) as response:
            text = await response.text()
            if response.status >= 400:
                raise RuntimeError(f"structured job {kind} postback failed: HTTP {response.status} {text[:300]}")

    async def _send_raw(self, message: dict[str, Any]) -> None:
        if not self.ws or self.ws.closed:
            raise RuntimeError("ClawChat websocket is not connected")
        async with self._send_lock:
            await self.ws.send_str(json.dumps(message, ensure_ascii=True))


async def enroll(api_url: str, code: str, agents: list[str], device_label: str, config_path: Path | None = None) -> BridgeConfig:
    api_url = _normalize_api_url(api_url)
    payload = {
        "code": code,
        "deviceLabel": device_label,
        **_bridge_device_metadata(),
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(f"{api_url}/api/v1/bridge/enroll", json=payload) as response:
            text = await response.text()
            if response.status >= 400:
                raise RuntimeError(f"bridge enroll failed: HTTP {response.status} {text[:300]}")
            body = json.loads(text or "{}")

    credentials = body.get("credentials") or {}
    workspace = body.get("workspace") or {}
    compatibility = (body.get("device") or {}).get("compatibility") or {}
    config = BridgeConfig(
        api_url=api_url,
        workspace_id=workspace.get("id"),
        workspace_name=workspace.get("name"),
        device_public_id=credentials.get("devicePublicId") or body.get("devicePublicId") or "",
        device_token=credentials.get("deviceToken") or body.get("deviceToken") or "",
        external_agent_ids=agents,
        device_label=device_label,
        compatibility_level=str(compatibility.get("level") or "").strip() or None,
        operating_mode=str(compatibility.get("operatingMode") or "").strip() or None,
        enabled_capabilities=[
            str(item).strip()
            for item in compatibility.get("enabledCapabilities") or []
            if str(item).strip()
        ],
    )
    config.validate_for_run()
    config.save(config_path)
    return config


async def rotate_device_credential(
    config: BridgeConfig,
    config_path: Path | None = None,
) -> None:
    payload = {
        "devicePublicId": config.device_public_id,
        "deviceToken": config.device_token,
        **_bridge_device_metadata(),
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{config.api_url}/api/v1/bridge/device/rotate",
            json=payload,
        ) as response:
            text = await response.text()
            if response.status >= 400:
                raise RuntimeError(
                    f"bridge credential rotation failed: HTTP {response.status} {text[:300]}"
                )
            body = json.loads(text or "{}")
    credentials = body.get("credentials") or {}
    replacement = str(credentials.get("deviceToken") or "").strip()
    if not replacement:
        raise RuntimeError("bridge credential rotation response did not include a replacement")
    config.device_token = replacement
    config.save(config_path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the Hermes Agent bridge for Relay Console")
    parser.add_argument("--config", type=Path, default=None, help=f"Config file path (default: {_config_path()})")
    parser.add_argument("--log-level", default=os.getenv("CLAWCHAT_HERMES_BRIDGE_LOG_LEVEL", "INFO"))
    sub = parser.add_subparsers(dest="command", required=True)

    enroll_parser = sub.add_parser("enroll", help="Enroll this computer as a Relay Console Hermes bridge device")
    enroll_parser.add_argument("--api-url", required=True, help="Your Relay Console Railway backend URL")
    enrollment_code = enroll_parser.add_mutually_exclusive_group(required=True)
    enrollment_code.add_argument("--code", help="One-time bridge enrollment code from Relay Console")
    enrollment_code.add_argument(
        "--code-stdin",
        action="store_true",
        help="Read the one-time bridge enrollment code from standard input",
    )
    enroll_parser.add_argument("--agent", action="append", dest="agents", default=[], help="Legacy Hermes external agent ID to register. Repeat for multiple agents.")
    enroll_parser.add_argument("--device-label", default=DEFAULT_DEVICE_LABEL)

    run_parser = sub.add_parser("run", help="Connect to Relay Console and process Hermes runtime dispatches")
    run_parser.add_argument("--agent", action="append", dest="agents", help="Override/add registered Hermes externalId. Repeat for multiple agents.")

    sub.add_parser("status", help="Show saved Relay Console Hermes bridge config with secrets redacted")
    sub.add_parser("rotate-credential", help="Rotate and securely replace this bridge device credential")
    return parser


async def async_main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.command == "enroll":
        code = args.code
        if args.code_stdin:
            if sys.stdin.isatty():
                parser.error("--code-stdin requires redirected standard input")
            code_input = sys.stdin.read(MAX_ENROLLMENT_CODE_BYTES + 1)
            if len(code_input.encode("utf-8")) > MAX_ENROLLMENT_CODE_BYTES:
                parser.error("the bridge enrollment code from standard input is too large")
            code = code_input.strip()
            if not code:
                parser.error("standard input did not contain a bridge enrollment code")
        config = await enroll(args.api_url, code, args.agents, args.device_label, args.config)
        print(f"Enrolled Relay Console Hermes bridge for workspace {config.workspace_name or config.workspace_id or '<unknown>'}")
        print(f"Config saved to {args.config or _config_path()}")
        print(f"Registered agent externalId(s): {', '.join(config.external_agent_ids)}")
        return 0

    if args.command == "status":
        try:
            config = BridgeConfig.load(args.config)
        except Exception as exc:
            print(f"Relay Console Hermes bridge is not configured: {exc}", file=sys.stderr)
            return 1
        redacted = config.to_json()
        if redacted.get("deviceToken"):
            redacted["deviceToken"] = "<redacted>"
        print(json.dumps(redacted, ensure_ascii=True, indent=2))
        return 0

    if args.command == "rotate-credential":
        try:
            config = BridgeConfig.load(args.config)
            await rotate_device_credential(config, args.config)
        except Exception as exc:
            print(f"Relay Console bridge credential rotation failed: {exc}", file=sys.stderr)
            return 1
        print("Relay Console bridge credential rotated and saved.")
        print("Restart the bridge using your existing runtime lifecycle.")
        return 0

    if args.command == "run":
        try:
            config = BridgeConfig.load(args.config)
        except Exception as exc:
            print(f"Relay Console Hermes bridge is not configured: {exc}", file=sys.stderr)
            return 1
        if args.agents:
            config.external_agent_ids = list(dict.fromkeys([*config.external_agent_ids, *args.agents]))
        bridge = ClawChatHermesBridge(config, args.config or _config_path())
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, bridge.stop)
            except NotImplementedError:
                pass
        logger.info(
            "starting ClawChat Hermes bridge at %s pid=%s codePath=%s cwd=%s pluginVersion=%s openCoreVersion=%s",
            _now_iso(),
            os.getpid(),
            Path(__file__).resolve(),
            Path.cwd(),
            PLUGIN_VERSION,
            OPEN_CORE_VERSION,
        )
        await bridge.run_forever()
        return 0

    parser.error("unknown command")
    return 2


def main() -> None:
    try:
        raise SystemExit(asyncio.run(async_main()))
    except KeyboardInterrupt:
        raise SystemExit(130)


if __name__ == "__main__":
    main()
