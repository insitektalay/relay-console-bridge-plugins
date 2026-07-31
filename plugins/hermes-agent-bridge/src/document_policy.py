from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any


MAX_DOCUMENT_BYTES = 500_000
MAX_DOCUMENTS = 2_000
ROOT_FILES = {
    "AGENTS.md",
    "HEARTBEAT.md",
    "IDENTITY.md",
    "MEMORY.md",
    "SOUL.md",
    "TOOLS.md",
    "USER.md",
}
ALLOWED_TREES = {"memory", "skills"}
SENSITIVE_NAME = re.compile(
    r"(^|[._-])(auth|credential|password|secret|token|keychain)([._-]|$)",
    re.IGNORECASE,
)


def _allowed(relative: Path) -> bool:
    parts = relative.parts
    if not parts or len(parts) > 7:
        return False
    if any(part in {"", ".", ".."} or part.startswith(".") for part in parts):
        return False
    if any(SENSITIVE_NAME.search(part) for part in parts):
        return False
    if len(parts) == 1:
        return parts[0] in ROOT_FILES
    return parts[0].lower() in ALLOWED_TREES and relative.suffix.lower() in {".md", ".markdown"}


def scan_profile_documents(
    profile_home: Path,
    *,
    limit: int = MAX_DOCUMENTS,
) -> tuple[list[dict[str, Any]], bool]:
    """Read only agent-owned Markdown after Relay has granted document consent."""
    root = profile_home.expanduser().resolve()
    if not root.is_dir() or root.is_symlink():
        raise ValueError("Hermes profile home is unavailable or is a symbolic link")
    documents: list[dict[str, Any]] = []
    complete = True
    candidates = [root / filename for filename in sorted(ROOT_FILES)]
    for tree in sorted(ALLOWED_TREES):
        tree_root = root / tree
        if tree_root.is_dir() and not tree_root.is_symlink():
            try:
                candidates.extend(sorted(tree_root.rglob("*")))
            except OSError:
                complete = False
    for path in candidates:
        try:
            relative = path.relative_to(root)
        except ValueError:
            continue
        if not _allowed(relative) or path.is_symlink() or not path.is_file():
            continue
        if len(documents) >= limit:
            complete = False
            break
        try:
            resolved = path.resolve(strict=True)
            if root != resolved and root not in resolved.parents:
                continue
            if resolved.stat().st_size > MAX_DOCUMENT_BYTES:
                continue
            content = resolved.read_text(encoding="utf8")
        except (OSError, UnicodeDecodeError):
            complete = False
            continue
        if "\x00" in content:
            continue
        documents.append(
            {
                "folder": relative.parent.as_posix() if relative.parent != Path(".") else "",
                "filename": relative.name,
                "content": content,
                "contentHash": hashlib.sha256(content.encode("utf8")).hexdigest(),
            }
        )
    return documents, complete


def safe_profile_document_path(profile_home: Path, folder: str, filename: str) -> Path:
    relative = Path(folder) / filename if folder else Path(filename)
    if not _allowed(relative):
        raise ValueError("Hermes profile document path is not allowlisted")
    root = profile_home.expanduser().resolve()
    current = root
    for part in relative.parts[:-1]:
        current = current / part
        if current.exists() and current.is_symlink():
            raise ValueError("Hermes profile document traversed a symbolic link")
    target = (root / relative).resolve()
    if root != target and root not in target.parents:
        raise ValueError("Hermes profile document escaped its profile home")
    if target.exists() and target.is_symlink():
        raise ValueError("Hermes profile document is a symbolic link")
    return target
