from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    from .document_policy import safe_profile_document_path
    from .native_profiles import (
        NativeHermesProfile,
        create_native_profile,
        enumerate_native_profiles,
        profile_name_from_external_id,
    )
except ImportError:  # pragma: no cover - direct source execution
    from document_policy import safe_profile_document_path
    from native_profiles import (
        NativeHermesProfile,
        create_native_profile,
        enumerate_native_profiles,
        profile_name_from_external_id,
    )


PROFILE_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


@dataclass(frozen=True)
class HermesProvisionResult:
    profile: NativeHermesProfile
    idempotent_replay: bool


def _marker_path(state_dir: Path, idempotency_key: str) -> Path:
    digest = hashlib.sha256(idempotency_key.encode("utf8")).hexdigest()
    return state_dir / "provisioning" / f"{digest}.json"


def _read_marker(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf8"))
        return value if isinstance(value, dict) and value.get("version") == 1 else None
    except (OSError, ValueError, TypeError):
        return None


def _write_marker(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=True, indent=2), encoding="utf8")
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def _profile_name(request: dict[str, Any]) -> str:
    external_id = str(request.get("externalAgentId") or "").strip()
    native = profile_name_from_external_id(external_id)
    candidate = native if native and native != "default" else external_id
    candidate = candidate.removeprefix("profile:").strip().lower()
    candidate = re.sub(r"[^a-z0-9_-]+", "-", candidate).strip("-_")
    if not PROFILE_NAME.fullmatch(candidate):
        raise ValueError("Hermes native profile name is invalid")
    return candidate


def provision_native_profile(
    request: dict[str, Any],
    *,
    state_dir: Path,
) -> HermesProvisionResult:
    idempotency_key = str(request.get("idempotencyKey") or "").strip()
    if not idempotency_key or len(idempotency_key) > 500:
        raise ValueError("Hermes provisioning idempotency key is required")
    profile_name = _profile_name(request)
    marker_path = _marker_path(state_dir, idempotency_key)
    marker = _read_marker(marker_path)
    if marker and marker.get("profileName") != profile_name:
        raise ValueError("Hermes provisioning idempotency key was reused")

    existing = {
        profile.native_name: profile
        for profile in enumerate_native_profiles()
    }
    if marker and marker.get("status") == "completed":
        profile = existing.get(profile_name)
        if not profile:
            raise RuntimeError("Hermes idempotency marker exists but the native profile is unavailable")
        return HermesProvisionResult(profile=profile, idempotent_replay=True)
    if profile_name in existing and not marker:
        raise FileExistsError(
            f"Hermes profile {profile_name!r} already exists and is not owned by this provisioning request"
        )

    _write_marker(
        marker_path,
        {
            "version": 1,
            "idempotencyKey": idempotency_key,
            "profileName": profile_name,
            "status": "started",
        },
    )
    profile = existing.get(profile_name)
    if not profile:
        profile = create_native_profile(
            profile_name,
            description=str(request.get("role") or request.get("name") or "").strip() or None,
            clone_from="default",
        )

    for file in request.get("files") or []:
        if not isinstance(file, dict):
            continue
        filename = str(file.get("filename") or "")
        folder = str(file.get("folder") or "")
        content = file.get("content")
        if not isinstance(content, str) or len(content.encode("utf8")) > 500_000:
            raise ValueError("Hermes provisioning document is invalid or too large")
        target = safe_profile_document_path(profile.home, folder, filename)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(target.suffix + ".relay.tmp")
        temporary.write_text(content, encoding="utf8")
        os.chmod(temporary, 0o600)
        temporary.replace(target)

    _write_marker(
        marker_path,
        {
            "version": 1,
            "idempotencyKey": idempotency_key,
            "profileName": profile_name,
            "externalAgentId": profile.external_id,
            "status": "completed",
        },
    )
    return HermesProvisionResult(profile=profile, idempotent_replay=False)
