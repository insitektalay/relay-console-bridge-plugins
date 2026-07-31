import asyncio
import hashlib
import json
from types import SimpleNamespace

import pytest

from clawchat_bridge.main import (
    ActiveRun,
    BridgeConfig,
    ClawChatHermesBridge,
    HermesRunManager,
    HermesWorkspaceManager,
    MarketplaceSkillInstaller,
    SnapshotStore,
)
from model_tools import get_tool_definitions


def _run_manager(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    bridge = SimpleNamespace(config=SimpleNamespace(api_url="https://example.test"), access_token="token")
    manager = HermesRunManager(bridge)
    manager.session_db = object()
    return manager


class _CapturingAIAgent:
    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.tools = get_tool_definitions(
            enabled_toolsets=kwargs.get("enabled_toolsets"),
            disabled_toolsets=kwargs.get("disabled_toolsets"),
            quiet_mode=True,
        )
        self.valid_tool_names = {tool["function"]["name"] for tool in self.tools}
        _CapturingAIAgent.instances.append(self)


class _MarketplaceOnlyAIAgent:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.tools = [
            {"type": "function", "function": {"name": name, "description": name, "parameters": {"type": "object", "properties": {}}}}
            for name in ("browser_navigate", "exa_search", "x_get_me")
        ]
        self.valid_tool_names = {tool["function"]["name"] for tool in self.tools}


def _build_agent(monkeypatch, tmp_path, payload):
    _CapturingAIAgent.instances = []
    monkeypatch.setattr("run_agent.AIAgent", _CapturingAIAgent)
    manager = _run_manager(monkeypatch, tmp_path)
    run = ActiveRun(dispatch_id="dispatch-123", runtime_session_id="runtime-123", external_agent_id="social_hermes")
    agent = manager._build_agent(run, {"model": "test-model", **payload})
    return manager, agent, agent.kwargs


def test_default_clawchat_dispatch_preserves_native_harness(monkeypatch, tmp_path):
    _manager, agent, kwargs = _build_agent(monkeypatch, tmp_path, {})

    names = agent.valid_tool_names
    assert kwargs["enabled_toolsets"] is None
    assert kwargs["skip_memory"] is False
    assert kwargs["session_db"] is not None
    assert {"memory", "session_search", "read_file", "write_file", "patch", "search_files", "terminal", "process", "skills_list", "skill_view", "skill_manage"}.issubset(names)


def test_runtime_toolsets_additive_browser_preserves_native_defaults(monkeypatch, tmp_path):
    _manager, agent, kwargs = _build_agent(
        monkeypatch,
        tmp_path,
        {"runtimeToolsets": {"additive": ["browser"]}},
    )

    assert kwargs["enabled_toolsets"] is None
    assert {"browser_navigate", "memory", "read_file", "terminal", "skill_manage"}.issubset(agent.valid_tool_names)


def test_marketplace_additive_preserves_native_defaults(monkeypatch, tmp_path):
    manager, _agent, _kwargs = _build_agent(monkeypatch, tmp_path, {})
    run = ActiveRun(dispatch_id="dispatch-123", runtime_session_id="runtime-123", external_agent_id="social_hermes")
    descriptor = {
        "name": "x.getMe",
        "description": "Get profile",
        "inputSchema": {"type": "object", "properties": {}},
        "executionUrl": "/api/v1/bridge/runtime-dispatches/dispatch-123/marketplace-tools/x/getMe",
    }

    with manager.marketplace_proxy.registered_for_payload(
        {"dispatchId": "dispatch-123", "marketplaceTools": [descriptor]},
        run,
    ) as marketplace_names:
        _CapturingAIAgent.instances = []
        agent = manager._build_agent(
            run,
            {"model": "test-model", "_marketplaceToolNames": marketplace_names},
        )

    assert "x_get_me" in agent.valid_tool_names
    assert {"memory", "read_file", "terminal", "skill_manage"}.issubset(agent.valid_tool_names)
    assert agent.kwargs["enabled_toolsets"] is None


def test_social_hermes_style_dispatch_final_tools_include_native_x_exa_and_browser(monkeypatch, tmp_path):
    manager, _agent, _kwargs = _build_agent(monkeypatch, tmp_path, {})
    run = ActiveRun(dispatch_id="dispatch-123", runtime_session_id="runtime-123", external_agent_id="social_hermes")
    descriptors = [
        {
            "name": "x.getMe",
            "description": "Get profile",
            "inputSchema": {"type": "object", "properties": {}},
            "executionUrl": "/api/v1/bridge/runtime-dispatches/dispatch-123/marketplace-tools/x/getMe",
        },
        {
            "name": "exa.search",
            "description": "Search Exa",
            "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}},
            "executionUrl": "/api/v1/bridge/runtime-dispatches/dispatch-123/marketplace-tools/exa/search",
        },
    ]

    with manager.marketplace_proxy.registered_for_payload(
        {"dispatchId": "dispatch-123", "marketplaceTools": descriptors},
        run,
    ) as marketplace_names:
        _CapturingAIAgent.instances = []
        agent = manager._build_agent(
            run,
            {
                "model": "test-model",
                "workspaceId": "workspace-1",
                "externalAgentId": "social_hermes",
                "runtimeToolsets": {"additive": ["browser", "marketplace"]},
                "_marketplaceToolNames": marketplace_names,
            },
        )

    assert {"memory", "session_search", "read_file", "write_file", "patch", "terminal", "skills_list", "skill_view", "skill_manage"}.issubset(agent.valid_tool_names)
    assert {"browser_navigate", "x_get_me", "exa_search"}.issubset(agent.valid_tool_names)
    assert agent.kwargs["enabled_toolsets"] is None


def test_disabled_toolsets_are_subtractive_without_narrowing_native_defaults(monkeypatch, tmp_path):
    _manager, agent, kwargs = _build_agent(
        monkeypatch,
        tmp_path,
        {"runtimeToolsets": {"disabled": ["terminal"]}},
    )

    assert kwargs["enabled_toolsets"] is None
    assert "terminal" not in agent.valid_tool_names
    assert "process" not in agent.valid_tool_names
    assert {"memory", "read_file", "write_file", "patch", "skill_manage"}.issubset(agent.valid_tool_names)


def test_legacy_enabled_toolsets_do_not_replace_base_harness_without_flag(monkeypatch, tmp_path):
    _manager, agent, kwargs = _build_agent(monkeypatch, tmp_path, {"enabledToolsets": ["skills"]})

    assert kwargs["enabled_toolsets"] is None
    assert {"skills_list", "read_file", "terminal", "memory"}.issubset(agent.valid_tool_names)


def test_replace_base_harness_is_rejected_without_audited_policy(monkeypatch, tmp_path):
    with pytest.raises(Exception) as exc:
        _build_agent(
            monkeypatch,
            tmp_path,
            {"enabledToolsets": ["skills"], "replaceBaseHarness": True},
        )

    assert getattr(exc.value, "code", None) == "native_harness_replacement_rejected"


def test_replace_base_harness_requires_env_and_policy(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_CLAWCHAT_ALLOW_REPLACE_BASE_HARNESS", "1")
    _manager, agent, kwargs = _build_agent(
        monkeypatch,
        tmp_path,
        {
            "enabledToolsets": ["skills"],
            "replaceBaseHarness": True,
            "autonomyPolicy": {"allowBaseHarnessReplacement": True},
        },
    )

    assert kwargs["enabled_toolsets"] == ["skills"]
    assert "skill_manage" in agent.valid_tool_names
    assert "read_file" not in agent.valid_tool_names
    assert "terminal" not in agent.valid_tool_names


def test_marketplace_only_final_tools_fail_native_harness_invariant(monkeypatch, tmp_path):
    monkeypatch.setattr("run_agent.AIAgent", _MarketplaceOnlyAIAgent)
    manager = _run_manager(monkeypatch, tmp_path)
    run = ActiveRun(dispatch_id="dispatch-123", runtime_session_id="runtime-123", external_agent_id="social_hermes")

    with pytest.raises(Exception) as exc:
        manager._build_agent(run, {"model": "test-model", "marketplaceTools": []})

    assert getattr(exc.value, "code", None) == "native_harness_shackled"


def test_skip_memory_requires_explicit_stateless_payload(monkeypatch, tmp_path):
    _manager, _agent, kwargs = _build_agent(monkeypatch, tmp_path, {"stateless": True})

    assert kwargs["skip_memory"] is True


def test_missing_workspace_root_resolves_stable_per_agent_workspace(monkeypatch, tmp_path):
    manager = _run_manager(monkeypatch, tmp_path)
    payload = {
        "workspaceId": "workspace-1",
        "externalAgentId": "social_hermes",
    }

    first, first_fallback = manager._resolved_workspace_root(payload)
    second, second_fallback = manager._resolved_workspace_root(payload)

    assert first == second
    assert first_fallback is True
    assert second_fallback is True
    assert first.endswith("/clawchat/workspaces/workspace-1/agents/social_hermes/workspace")


def test_new_clears_snapshot_but_not_social_agent_workspace(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    manager = HermesRunManager(SimpleNamespace(config=SimpleNamespace(api_url="https://example.test"), access_token="token"))
    manager.snapshot_store = SnapshotStore(tmp_path / "runtime_sessions")
    workspace, _fallback = manager._resolved_workspace_root({
        "workspaceId": "workspace-1",
        "externalAgentId": "social_hermes",
    })
    method_path = tmp_path / ".hermes" / "clawchat" / "workspaces" / "workspace-1" / "agents" / "social_hermes" / "workspace" / "actionable_intelligence_tweet_method.md"
    method_path.write_text("Actionable Intelligence Tweet Method", encoding="utf-8")
    manager.snapshot_store.save("runtime-1", [{"role": "user", "content": "temporary thread context"}])

    assert workspace == str(method_path.parent)
    assert manager.reset_runtime_session("runtime-1") is True

    assert manager.snapshot_store.load("runtime-1") == []
    assert method_path.read_text(encoding="utf-8") == "Actionable Intelligence Tweet Method"


def test_new_clears_snapshot_but_not_plain_agent_workspace(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    manager = HermesRunManager(SimpleNamespace(config=SimpleNamespace(api_url="https://example.test"), access_token="token"))
    manager.snapshot_store = SnapshotStore(tmp_path / "runtime_sessions")
    workspace, _fallback = manager._resolved_workspace_root({
        "workspaceId": "workspace-1",
        "externalAgentId": "plain_hermes",
    })
    note_path = tmp_path / ".hermes" / "clawchat" / "workspaces" / "workspace-1" / "agents" / "plain_hermes" / "workspace" / "durable-note.md"
    note_path.write_text("Use the durable method.", encoding="utf-8")
    manager.snapshot_store.save("runtime-plain", [{"role": "assistant", "content": "temporary thread context"}])

    assert workspace == str(note_path.parent)
    assert manager.reset_runtime_session("runtime-plain") is True

    assert manager.snapshot_store.load("runtime-plain") == []
    assert note_path.read_text(encoding="utf-8") == "Use the durable method."


def test_marketplace_skill_pack_installs_and_runtime_roots_include_external_agent_id(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    content = "# Test skill\n"
    result = MarketplaceSkillInstaller().install({
        "type": "marketplace.installHermesSkill",
        "runtimeFormat": "hermes",
        "requestId": "req-1",
        "workspaceId": "workspace-1",
        "agentId": "agent-social",
        "externalAgentId": "social_hermes",
        "appSlug": "x",
        "skillName": "x-router",
        "targetRoot": "skills/x-router",
        "policy": {"overwrite": "managed_files_only", "removeStaleManagedFiles": False},
        "metadata": {"generatedBy": "clawchat-marketplace"},
        "files": [{"relativePath": "SKILL.md", "content": content, "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest()}],
    })
    manager = HermesRunManager(SimpleNamespace(config=SimpleNamespace(api_url="https://example.test"), access_token="token"))

    roots = manager._clawchat_skill_roots({"externalAgentId": "social_hermes", "agentId": "agent-social", "workspaceId": "workspace-1"})

    assert result["status"] == "installed"
    assert result["externalAgentId"] == "social_hermes"
    assert any(str(root).endswith("/clawchat/agents/social_hermes/workspace/skills") for root in roots)


def test_marketplace_skill_pack_is_visible_in_agent_workspace_browser(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    skill_content = "# Outlook router\n"
    reference_content = "# Outlook reference\n"
    result = MarketplaceSkillInstaller().install({
        "type": "marketplace.installHermesSkill",
        "runtimeFormat": "hermes",
        "requestId": "req-1",
        "workspaceId": "workspace-1",
        "agentId": "42bb6542-3382-4000-b18b-5af5299026b8",
        "externalAgentId": "linc_snr_hermes",
        "appSlug": "outlook",
        "skillName": "outlook-router",
        "targetRoot": "skills/outlook-router",
        "policy": {"overwrite": "managed_files_only", "removeStaleManagedFiles": False},
        "metadata": {"generatedBy": "clawchat-marketplace"},
        "files": [
            {
                "relativePath": "SKILL.md",
                "content": skill_content,
                "sha256": hashlib.sha256(skill_content.encode("utf-8")).hexdigest(),
            },
            {
                "relativePath": "references/INDEX.md",
                "content": reference_content,
                "sha256": hashlib.sha256(reference_content.encode("utf-8")).hexdigest(),
            },
        ],
    })
    manager = HermesWorkspaceManager()
    request = {
        "folder": "agent",
        "workspaceId": "workspace-1",
        "externalAgentId": "linc_snr_hermes",
    }

    skills = manager.list({**request, "path": "/skills"})
    skill_root = manager.list({**request, "path": "/skills/outlook-router"})
    references = manager.list({**request, "path": "/skills/outlook-router/references"})
    skill_file = manager.read({**request, "path": "/skills/outlook-router", "filename": "SKILL.md"})

    assert result["status"] == "installed"
    assert {entry["name"] for entry in skills["entries"]} == {"outlook-router"}
    assert {entry["name"] for entry in skill_root["entries"]} == {"SKILL.md", "references"}
    assert {entry["name"] for entry in references["entries"]} == {"INDEX.md"}
    assert skill_file["content"] == skill_content
    assert (
        tmp_path
        / ".hermes"
        / "clawchat"
        / "agents"
        / "linc_snr_hermes"
        / "workspace"
        / "skills"
        / "outlook-router"
        / "SKILL.md"
    ).read_text(encoding="utf-8") == skill_content


def test_control_new_does_not_delete_installed_marketplace_skills(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    bridge = ClawChatHermesBridge(
        BridgeConfig(
            api_url="http://clawchat.local",
            device_public_id="device",
            device_token="token",
            external_agent_ids=["social_hermes"],
        )
    )
    bridge.run_manager.snapshot_store = SnapshotStore(tmp_path / "runtime_sessions")
    skill_file = tmp_path / ".hermes" / "clawchat" / "agents" / "agent-social" / "workspace" / "skills" / "x-router" / "SKILL.md"
    skill_file.parent.mkdir(parents=True, exist_ok=True)
    skill_file.write_text("# X router\n", encoding="utf-8")
    sent = []

    async def fake_send_event(event):
        sent.append(event)

    monkeypatch.setattr(bridge, "send_event", fake_send_event)
    asyncio.run(bridge._handle_ws_text(json.dumps({
        "type": "hermes.run.dispatch",
        "data": {
            "dispatchId": "dispatch-1",
            "runtimeSessionId": "runtime-1",
            "externalAgentId": "social_hermes",
            "agentId": "agent-social",
            "inputText": "/new",
        },
    })))

    assert sent[0]["type"] == "run.completed"
    assert skill_file.read_text(encoding="utf-8") == "# X router\n"


def test_workspace_manager_agent_folder_uses_external_agent_workspace_when_workspace_id_present(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    result = HermesWorkspaceManager().write({
        "folder": "agent",
        "workspaceId": "workspace-1",
        "externalAgentId": "plain_hermes",
        "path": "/",
        "filename": "method.md",
        "content": "durable",
    })

    assert result["filename"] == "method.md"
    assert (
        tmp_path
        / ".hermes"
        / "clawchat"
        / "agents"
        / "plain_hermes"
        / "workspace"
        / "method.md"
    ).read_text(encoding="utf-8") == "durable"


def test_recent_messages_are_fallback_when_bridge_snapshot_is_empty(monkeypatch, tmp_path):
    manager = _run_manager(monkeypatch, tmp_path)

    history, source = manager._conversation_history_for_run(
        [],
        {
            "recentMessages": [
                {"role": "user", "content": "older question"},
                {"role": "assistant", "content": "older answer"},
                {"role": "user", "content": "current input"},
            ]
        },
        "current input",
    )

    assert source == "clawchat_recent_messages"
    assert history == [
        {"role": "user", "content": "older question"},
        {"role": "assistant", "content": "older answer"},
    ]


def test_bridge_snapshot_is_authoritative_over_recent_messages(monkeypatch, tmp_path):
    manager = _run_manager(monkeypatch, tmp_path)
    snapshot = [{"role": "user", "content": "snapshot context"}]

    history, source = manager._conversation_history_for_run(
        snapshot,
        {"recentMessages": [{"role": "user", "content": "clawchat context"}]},
        "current input",
    )

    assert source == "bridge_snapshot"
    assert history == snapshot
