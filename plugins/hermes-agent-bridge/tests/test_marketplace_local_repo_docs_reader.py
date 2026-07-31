import json

from clawchat_bridge.main import MarketplaceLocalRepoDocsReader


def _write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_local_repo_docs_reader_returns_supported_clawchat_docs(tmp_path):
    repo = tmp_path / "GapMiner"
    docs = repo / ".clawchat"
    _write(docs / "app_manifest.json", json.dumps({"appSlug": "gapminer"}))
    _write(docs / "roles_manifest.json", json.dumps({"roles": []}))
    _write(docs / "clawchat.config.json", json.dumps({"docsSourcePath": ".clawchat/"}))
    _write(docs / "api" / "openapi.json", json.dumps({"openapi": "3.1.0"}))
    _write(docs / "api" / "endpoints.md", "# Endpoints\n")
    _write(docs / "agent-docs-source" / "workflow.md", "# Worker workflow\n")
    _write(docs / "agent-docs-source" / "workflows" / "research_topic.md", "# Research topic\n")
    _write(docs / "auditor-docs-source" / "SOUL.md", "# Auditor soul\n")
    _write(docs / "auditor-docs-source" / "WORKFLOW.md", "# Auditor workflow\n")
    _write(docs / "manager-docs-source" / "IDENTITY.md", "# Manager identity\n")
    _write(docs / "manager-docs-source" / "nested" / "WORKFLOW.md", "# Nested manager workflow\n")

    result = MarketplaceLocalRepoDocsReader().read({
        "requestId": "req",
        "repoPath": str(repo),
        "docsSourcePath": ".clawchat/",
    })

    assert result["status"] == "ok"
    paths = [item["relativePath"] for item in result["files"]]
    assert paths == [
        "agent-docs-source/workflow.md",
        "agent-docs-source/workflows/research_topic.md",
        "api/endpoints.md",
        "api/openapi.json",
        "app_manifest.json",
        "auditor-docs-source/SOUL.md",
        "auditor-docs-source/WORKFLOW.md",
        "clawchat.config.json",
        "manager-docs-source/IDENTITY.md",
        "manager-docs-source/nested/WORKFLOW.md",
        "roles_manifest.json",
    ]
    for item in result["files"]:
        assert isinstance(item["sha256"], str) and len(item["sha256"]) == 64
        assert isinstance(item["sizeBytes"], int) and item["sizeBytes"] > 0


def test_local_repo_docs_reader_accepts_auditor_include_globs(tmp_path):
    repo = tmp_path / "GapMiner"
    docs = repo / ".clawchat"
    _write(docs / "agent-docs-source" / "workflow.md", "# Worker workflow\n")
    _write(docs / "auditor-docs-source" / "SOUL.md", "# Auditor soul\n")
    _write(docs / "auditor-docs-source" / "nested" / "CHECK.md", "# Nested auditor check\n")

    result = MarketplaceLocalRepoDocsReader().read({
        "requestId": "req",
        "repoPath": str(repo),
        "docsSourcePath": ".clawchat/",
        "includeGlobs": [
            ".clawchat/auditor-docs-source/*.md",
            ".clawchat/auditor-docs-source/**/*.md",
        ],
    })

    assert result["status"] == "ok"
    assert [item["relativePath"] for item in result["files"]] == [
        "auditor-docs-source/SOUL.md",
        "auditor-docs-source/nested/CHECK.md",
    ]


def test_local_repo_docs_reader_accepts_manager_include_globs(tmp_path):
    repo = tmp_path / "GapMiner"
    docs = repo / ".clawchat"
    _write(docs / "agent-docs-source" / "workflow.md", "# Worker workflow\n")
    _write(docs / "manager-docs-source" / "IDENTITY.md", "# Manager identity\n")
    _write(docs / "manager-docs-source" / "nested" / "CHECK.md", "# Nested manager check\n")

    result = MarketplaceLocalRepoDocsReader().read({
        "requestId": "req",
        "repoPath": str(repo),
        "docsSourcePath": ".clawchat/",
        "includeGlobs": [
            ".clawchat/manager-docs-source/*.md",
            ".clawchat/manager-docs-source/**/*.md",
        ],
    })

    assert result["status"] == "ok"
    assert [item["relativePath"] for item in result["files"]] == [
        "manager-docs-source/IDENTITY.md",
        "manager-docs-source/nested/CHECK.md",
    ]


def test_local_repo_docs_reader_accepts_config_file_as_docs_source_path(tmp_path):
    repo = tmp_path / "LinkCrest"
    docs = repo / ".clawchat"
    _write(docs / "app_manifest.json", json.dumps({"name": "LinkCrest"}))
    _write(docs / "clawchat.config.json", json.dumps({"docs_source_path": ".clawchat/"}))
    _write(docs / "api" / "openapi.json", json.dumps({"openapi": "3.1.0"}))
    _write(docs / "agent-docs-source" / "workflow.md", "# Agent workflow\n")

    result = MarketplaceLocalRepoDocsReader().read({
        "requestId": "req",
        "repoPath": str(repo),
        "docsSourcePath": ".clawchat/clawchat.config.json",
    })

    assert result["status"] == "ok"
    assert result["docsSourcePath"] == ".clawchat/"
    paths = [item["relativePath"] for item in result["files"]]
    assert "app_manifest.json" in paths
    assert "clawchat.config.json" in paths
    assert "api/openapi.json" in paths
    assert "agent-docs-source/workflow.md" in paths


def test_local_repo_docs_reader_rejects_docs_source_path_traversal(tmp_path):
    repo = tmp_path / "GapMiner"
    repo.mkdir()

    result = MarketplaceLocalRepoDocsReader().read({
        "requestId": "req",
        "repoPath": str(repo),
        "docsSourcePath": "../.clawchat/",
    })

    assert result["status"] == "failed"
    assert any("Invalid docsSourcePath" in error for error in result["errors"])
