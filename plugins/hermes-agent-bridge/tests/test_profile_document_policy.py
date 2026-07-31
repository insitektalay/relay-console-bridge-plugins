from pathlib import Path

import pytest

from clawchat_bridge.document_policy import (
    safe_profile_document_path,
    scan_profile_documents,
)


def test_profile_document_policy_reads_only_allowlisted_agent_text(tmp_path):
    (tmp_path / "SOUL.md").write_text("Native instructions.\n", encoding="utf8")
    (tmp_path / "config.yaml").write_text("api_key: secret\n", encoding="utf8")
    (tmp_path / ".env").write_text("TOKEN=secret\n", encoding="utf8")
    (tmp_path / "memory").mkdir()
    (tmp_path / "memory" / "customer.md").write_text("Customer memory.\n", encoding="utf8")
    (tmp_path / "memory" / "api-token.md").write_text("secret\n", encoding="utf8")

    documents, complete = scan_profile_documents(tmp_path)

    assert complete is True
    assert {
        f"{document['folder']}/{document['filename']}".strip("/")
        for document in documents
    } == {"SOUL.md", "memory/customer.md"}


def test_profile_document_policy_rejects_symlink_escape(tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (outside / "stolen.md").write_text("outside\n", encoding="utf8")
    (tmp_path / "memory").symlink_to(outside, target_is_directory=True)

    documents, _complete = scan_profile_documents(tmp_path)
    assert documents == []
    with pytest.raises(ValueError, match="symbolic link"):
        safe_profile_document_path(tmp_path, "memory", "new.md")


def test_profile_document_scan_is_incomplete_when_an_allowlisted_file_cannot_be_read(
    tmp_path,
    monkeypatch,
):
    soul = tmp_path / "SOUL.md"
    soul.write_text("Native instructions.\n", encoding="utf8")
    original_read_text = Path.read_text

    def fail_for_soul(path, *args, **kwargs):
        if path == soul.resolve():
            raise OSError("simulated read failure")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fail_for_soul)
    documents, complete = scan_profile_documents(tmp_path)

    assert documents == []
    assert complete is False


def test_profile_document_limit_counts_only_allowlisted_documents(tmp_path):
    (tmp_path / "SOUL.md").write_text("Native instructions.\n", encoding="utf8")
    (tmp_path / "notes.md").write_text("not allowlisted\n", encoding="utf8")

    documents, complete = scan_profile_documents(tmp_path, limit=1)
    assert [document["filename"] for document in documents] == ["SOUL.md"]
    assert complete is True

    (tmp_path / "MEMORY.md").write_text("Second native document.\n", encoding="utf8")
    documents, complete = scan_profile_documents(tmp_path, limit=1)
    assert [document["filename"] for document in documents] == ["MEMORY.md"]
    assert complete is False
