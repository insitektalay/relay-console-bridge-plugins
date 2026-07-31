import subprocess

from clawchat_bridge.main import (
    BRIDGE_CAPABILITIES,
    MarketplaceLocalRepoDocsWriter,
)


def _write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _apply(repo, files, docs_source_path=".clawchat/"):
    return MarketplaceLocalRepoDocsWriter().apply({
        "requestId": "req",
        "repoPath": str(repo),
        "docsSourcePath": docs_source_path,
        "files": files,
    })


def test_capability_list_includes_local_repo_docs_write():
    assert "marketplaceLocalRepoDocsWrite" in BRIDGE_CAPABILITIES


def test_writer_writes_approved_file_under_clawchat(tmp_path):
    repo = tmp_path / "Repo"
    repo.mkdir()

    result = _apply(repo, [{
        "relativePath": "manager-docs-source/IDENTITY.md",
        "content": "# Manager\n",
        "approved": True,
    }])

    assert result["status"] == "ok"
    assert result["filesWritten"][0]["relativePath"] == "manager-docs-source/IDENTITY.md"
    assert result["filesSkipped"] == []
    assert result["errors"] == []
    assert (repo / ".clawchat" / "manager-docs-source" / "IDENTITY.md").read_text(encoding="utf-8") == "# Manager\n"


def test_writer_creates_missing_parent_directories_under_clawchat(tmp_path):
    repo = tmp_path / "Repo"
    repo.mkdir()

    result = _apply(repo, [{
        "relativePath": ".clawchat/manager-docs-source/nested/WORKFLOW.md",
        "content": "# Workflow\n",
    }])

    assert result["status"] == "ok"
    assert (repo / ".clawchat" / "manager-docs-source" / "nested" / "WORKFLOW.md").exists()


def test_writer_rejects_path_traversal(tmp_path):
    repo = tmp_path / "Repo"
    repo.mkdir()

    result = _apply(repo, [{"relativePath": "../README.md", "content": "bad"}])

    assert result["status"] == "failed"
    assert result["filesWritten"] == []
    assert result["filesSkipped"][0]["relativePath"] == "../README.md"
    assert any("Blocked unsafe relative path" in error for error in result["errors"])
    assert not (repo / "README.md").exists()


def test_writer_rejects_absolute_paths(tmp_path):
    repo = tmp_path / "Repo"
    repo.mkdir()

    result = _apply(repo, [{"relativePath": "/tmp/evil.md", "content": "bad"}])

    assert result["status"] == "failed"
    assert result["filesWritten"] == []
    assert any("Blocked unsafe relative path" in error for error in result["errors"])


def test_writer_rejects_writes_outside_clawchat_docs_root(tmp_path):
    repo = tmp_path / "Repo"
    repo.mkdir()

    result = _apply(
        repo,
        [{"relativePath": "manager-docs-source/IDENTITY.md", "content": "bad"}],
        docs_source_path="docs/",
    )

    assert result["status"] == "failed"
    assert result["filesWritten"] == []
    assert any("only writes under .clawchat" in error for error in result["errors"])
    assert not (repo / "docs" / "manager-docs-source" / "IDENTITY.md").exists()


def test_writer_rejects_symlink_escape_attempts(tmp_path):
    repo = tmp_path / "Repo"
    outside = tmp_path / "outside"
    (repo / ".clawchat").mkdir(parents=True)
    outside.mkdir()
    (repo / ".clawchat" / "manager-docs-source").symlink_to(outside, target_is_directory=True)

    result = _apply(repo, [{
        "relativePath": "manager-docs-source/IDENTITY.md",
        "content": "bad",
    }])

    assert result["status"] == "failed"
    assert result["filesWritten"] == []
    assert any("symlink escape" in error or "path escape" in error for error in result["errors"])
    assert not (outside / "IDENTITY.md").exists()


def test_writer_does_not_touch_unrelated_repo_files(tmp_path):
    repo = tmp_path / "Repo"
    repo.mkdir()
    unrelated = repo / "README.md"
    unrelated.write_text("keep\n", encoding="utf-8")

    result = _apply(repo, [
        {"relativePath": "manager-docs-source/IDENTITY.md", "content": "# Manager\n"},
        {"relativePath": "README.md", "content": "replace\n"},
    ])

    assert result["status"] == "partial"
    assert len(result["filesWritten"]) == 1
    assert result["filesSkipped"][0]["relativePath"] == "README.md"
    assert unrelated.read_text(encoding="utf-8") == "keep\n"


def test_writer_returns_git_status_commit_and_dirty_state(tmp_path):
    repo = tmp_path / "Repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
    _write(repo / ".clawchat" / "app_manifest.json", "{}\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, check=True, capture_output=True)
    before = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, text=True, check=True, capture_output=True).stdout.strip()

    result = _apply(repo, [{
        "relativePath": "manager-docs-source/IDENTITY.md",
        "content": "# Manager\n",
    }])

    assert result["status"] == "ok"
    assert result["gitCommitBefore"] == before
    assert result["gitCommitAfter"] == before
    assert result["dirtyStateAfter"] == "dirty"
    assert ".clawchat/manager-docs-source/" in result["gitStatusAfter"]
