import sys
import types
from pathlib import Path
from types import SimpleNamespace

from clawchat_bridge.native_profiles import (
    enumerate_native_profiles,
    external_id_for_profile,
    profile_name_from_external_id,
)


def test_native_profile_inventory_uses_hermes_api_and_normalizes_default(tmp_path, monkeypatch):
    default = tmp_path / "default"
    sales = tmp_path / "profiles" / "sales"
    default.mkdir()
    sales.mkdir(parents=True)
    api = types.ModuleType("hermes_cli.profiles")
    api.list_profiles = lambda: [
        SimpleNamespace(
            name="default",
            model="gpt-default",
            provider="openai",
            skill_count=2,
            gateway_running=True,
            description="General assistant",
        ),
        SimpleNamespace(
            name="sales",
            model="gpt-sales",
            provider="openrouter",
            skill_count=4,
            gateway_running=False,
            description="Sales specialist",
        ),
    ]
    api.get_profile_dir = lambda name: default if name == "default" else sales
    monkeypatch.setitem(sys.modules, "hermes_cli.profiles", api)

    profiles = enumerate_native_profiles()

    assert [profile.external_id for profile in profiles] == ["default", "profile:sales"]
    assert profiles[1].home == sales.resolve()
    assert profiles[1].metadata()["skillCount"] == 4


def test_native_profile_ids_round_trip_without_path_inference():
    assert external_id_for_profile("default") == "default"
    assert external_id_for_profile("Sales") == "profile:sales"
    assert profile_name_from_external_id("profile:sales") == "sales"
    assert profile_name_from_external_id("legacy-agent") is None
