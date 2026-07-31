from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class NativeHermesProfile:
    external_id: str
    native_name: str
    home: Path
    display_name: str
    description: str
    model: str | None
    provider: str | None
    skill_count: int
    gateway_running: bool

    def metadata(self) -> dict[str, Any]:
        return {
            "name": self.display_name,
            "role": self.description or "assistant",
            "description": self.description or None,
            "modelPrimary": self.model,
            "providerLabel": self.provider,
            "skillCount": self.skill_count,
            "gatewayRunning": self.gateway_running,
            "nativeKind": "hermes_profile",
            "nativeProfileName": self.native_name,
        }


def external_id_for_profile(name: str) -> str:
    canonical = str(name).strip().lower()
    return "default" if canonical == "default" else f"profile:{canonical}"


def profile_name_from_external_id(external_id: str) -> str | None:
    value = str(external_id).strip()
    if value == "default":
        return "default"
    if value.startswith("profile:") and value.count(":") == 1:
        name = value.split(":", 1)[1]
        return name or None
    return None


def enumerate_native_profiles() -> list[NativeHermesProfile]:
    """Use Hermes' supported profile API; never infer profiles from directories."""
    from hermes_cli.profiles import get_profile_dir, list_profiles

    profiles: list[NativeHermesProfile] = []
    seen: set[str] = set()
    for info in list_profiles():
        native_name = str(info.name).strip().lower()
        external_id = external_id_for_profile(native_name)
        if external_id in seen:
            continue
        home = Path(get_profile_dir(native_name)).expanduser().resolve()
        if not home.is_dir():
            continue
        seen.add(external_id)
        profiles.append(
            NativeHermesProfile(
                external_id=external_id,
                native_name=native_name,
                home=home,
                display_name="Default Hermes profile" if native_name == "default" else native_name,
                description=str(getattr(info, "description", "") or "").strip(),
                model=str(info.model).strip() if getattr(info, "model", None) else None,
                provider=str(info.provider).strip() if getattr(info, "provider", None) else None,
                skill_count=max(0, int(getattr(info, "skill_count", 0) or 0)),
                gateway_running=bool(getattr(info, "gateway_running", False)),
            )
        )
    return sorted(profiles, key=lambda profile: (profile.native_name != "default", profile.native_name))


def create_native_profile(
    native_name: str,
    *,
    description: str | None = None,
    clone_from: str | None = None,
) -> NativeHermesProfile:
    from hermes_cli.profiles import create_profile, get_profile_dir, list_profiles

    requested = str(native_name).strip().lower()
    create_profile(
        requested,
        clone_from=clone_from,
        clone_config=True,
        no_alias=True,
        description=description,
    )
    expected = Path(get_profile_dir(requested)).expanduser().resolve()
    for profile in enumerate_native_profiles():
        if profile.native_name == requested and profile.home == expected:
            return profile
    names = [str(info.name) for info in list_profiles()]
    raise RuntimeError(
        f"Hermes created profile {requested!r} but native enumeration did not return it; observed={names}"
    )
