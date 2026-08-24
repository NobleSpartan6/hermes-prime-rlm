"""Candidate evidence: porcelain -z parsing, tracked diff, tree digest.

NUL-safe everywhere paths are parsed (spec §19): ``git status --porcelain=v1
-z --untracked-files=all`` separates entries with NUL bytes; rename records
are NUL-separated pairs. Never split porcelain output by newlines.
"""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

from .models import ChangedPaths
from .schemas import (
    TREE_DIGEST_MAX_ENTRIES,
    TREE_DIGEST_MAX_TOTAL_FILE_BYTES,
    ValidationError,
)
from .validation import run_git
from .workspace import atomic_write_bytes


def collect_status_z(candidate_path: str) -> list[bytes]:
    """Return raw NUL-separated status entry byte strings."""
    out = run_git(
        ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
        cwd=candidate_path,
    )
    raw = out.encode("utf-8", errors="surrogateescape")
    return [entry for entry in raw.split(b"\x00") if entry]


def parse_status_z(entries: list[bytes]) -> ChangedPaths:
    """Classify porcelain-v1 -z entries into changed-path buckets.

    XY codes: M/A/D modified-added-deleted, R/C rename-copy (followed by the
    NUL-separated new path), ?? untracked. Paths with spaces survive because
    splitting is on NUL only; quoted paths are unnecessary with -z.
    """
    changed = ChangedPaths()
    i = 0
    while i < len(entries):
        entry = entries[i]
        if len(entry) < 3:
            i += 1
            continue
        xy = entry[:2].decode("ascii", errors="replace")
        path_raw = entry[3:]
        if xy == "??":
            changed.untracked.append(path_raw.decode("utf-8", errors="replace"))
            i += 1
            continue
        if xy and xy[0] in "RC" and i + 1 < len(entries):
            # Porcelain -z rename layout: `XY NEW\0ORIG\0`. Record the NEW path.
            changed.renamed.append(path_raw.decode("utf-8", errors="replace"))
            i += 2
            continue
        decoded = path_raw.decode("utf-8", errors="replace")
        if xy and xy[0] == "D" or xy and xy[1] == "D":
            changed.deleted.append(decoded)
        else:
            changed.modified.append(decoded)
        i += 1
    # Deterministic ordering for receipts.
    changed.modified.sort()
    changed.deleted.sort()
    changed.renamed.sort()
    changed.untracked.sort()
    return changed


def write_tracked_diff(
    candidate_path: str,
    base_commit: str,
    output_path: str,
) -> str:
    """Write the binary-capable tracked diff vs base; returns its sha256."""
    diff = run_git(
        [
            "diff",
            "--binary",
            "--full-index",
            "--no-ext-diff",
            base_commit,
            "--",
        ],
        cwd=candidate_path,
    )
    data = diff.encode("utf-8", errors="surrogateescape")
    atomic_write_bytes(output_path, data)
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str | os.PathLike) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# Candidate-tree digest (deterministic, cross-platform)
# ---------------------------------------------------------------------------


class TreeDigestLimitExceeded(ValidationError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(code, message)


def _is_special(entry: os.stat_result) -> bool:
    mode = entry.st_mode
    return (
        stat.S_ISFIFO(mode)
        or stat.S_ISSOCK(mode)
        or stat.S_ISCHR(mode)
        or stat.S_ISBLK(mode)
    )


def candidate_tree_digest(
    candidate_path: str,
    *,
    max_entries: int = TREE_DIGEST_MAX_ENTRIES,
    max_total_file_bytes: int = TREE_DIGEST_MAX_TOTAL_FILE_BYTES,
) -> str:
    """Deterministic SHA-256 over a framed representation of the candidate.

    Framing per entry (all lengths little-endian u64, UTF-8 bytes):
        b"HPRLM1" | relpath_len | relpath | kind_len | kind |
        size | content_len | content
    where content is raw file bytes ("f"), symlink targets as bytes ("l"), or
    empty for directories ("d"). Entries sort by UTF-8 bytes of the normalized
    POSIX relative path. Permission bits are excluded (tracked-mode changes
    live in tracked.patch). The worktree's own ``.git`` administrative entry
    is excluded whether it is a file or directory.

    This is a digest of the locally observed candidate — NOT a reproducible-
    build guarantee. Fails closed on special files, traversal outside the
    candidate, symlinked directories, and bound overruns.
    """
    root = Path(candidate_path)
    try:
        root_real = root.resolve(strict=True)
    except OSError as exc:
        raise TreeDigestLimitExceeded(
            "TREE_ROOT_UNRESOLVABLE", f"candidate root cannot be resolved: {exc}"
        ) from exc

    framed_parts: list[tuple[bytes, bytes]] = []
    total_file_bytes = 0
    count = 0

    def fail_closed_outside(p: Path) -> None:
        resolved_parent = p.resolve(strict=False)
        try:
            resolved_parent.relative_to(root_real)
        except ValueError as exc:
            raise TreeDigestLimitExceeded(
                "TRAVERSAL_OUTSIDE_CANDIDATE",
                f"path escapes candidate: {p}",
            ) from exc

    stack = [root_real]
    while stack:
        current = stack.pop()
        fail_closed_outside(current)
        try:
            children = sorted(
                current.iterdir(),
                key=lambda c: c.name.encode("utf-8", "surrogateescape"),
            )
        except OSError as exc:
            raise TreeDigestLimitExceeded(
                "TREE_UNREADABLE", f"cannot list {current}: {exc}"
            ) from exc
        for child in children:
            rel_os = child.relative_to(root_real)
            rel_posix = rel_os.as_posix()
            if rel_posix == ".git" or rel_posix.startswith(".git/"):
                continue  # worktree administrative entry (file or dir)
            rel_bytes = rel_posix.encode("utf-8", "surrogateescape")
            try:
                st = child.lstat()
            except OSError as exc:
                raise TreeDigestLimitExceeded(
                    "TREE_UNREADABLE", f"cannot lstat {child}: {exc}"
                ) from exc
            if _is_special(st):
                raise TreeDigestLimitExceeded(
                    "SPECIAL_FILE_UNSUPPORTED",
                    f"sockets/FIFOs/devices are unsupported: {rel_posix}",
                )
            if stat.S_ISLNK(st.st_mode):
                try:
                    target_rooted = Path(os.path.realpath(child, strict=True))
                    target_rooted.relative_to(root_real)
                except (OSError, ValueError) as exc:
                    raise TreeDigestLimitExceeded(
                        "SYMLINK_ESCAPES_CANDIDATE",
                        f"symlink escapes candidate or dangles: {rel_posix}",
                    ) from exc
                target = os.readlink(child)
                content = os.fsencode(target)
                framed_parts.append((rel_bytes, _frame(rel_bytes, b"l", len(content), content)))
                count += 1
            elif stat.S_ISDIR(st.st_mode):
                # Do not follow directory symlinks (lstat above already routed
                # them to the symlink branch); plain dirs get an empty frame
                # and are recursed via realpath containment check.
                real_dir = Path(os.path.realpath(child, strict=True))
                try:
                    real_dir.relative_to(root_real)
                except ValueError as exc:
                    raise TreeDigestLimitExceeded(
                        "TRAVERSAL_OUTSIDE_CANDIDATE",
                        f"directory escapes candidate: {rel_posix}",
                    ) from exc
                framed_parts.append((rel_bytes, _frame(rel_bytes, b"d", 0, b"")))
                count += 1
                stack.append(real_dir)
            elif stat.S_ISREG(st.st_mode):
                size = st.st_size
                total_file_bytes += size
                if total_file_bytes > max_total_file_bytes:
                    raise TreeDigestLimitExceeded(
                        "TREE_TOO_LARGE",
                        f"candidate exceeds {max_total_file_bytes} regular-file bytes",
                    )
                content = child.read_bytes()
                framed_parts.append((rel_bytes, _frame(rel_bytes, b"f", len(content), content)))
                count += 1
            else:
                raise TreeDigestLimitExceeded(
                    "UNKNOWN_ENTRY_KIND", f"unhandled filesystem entry: {rel_posix}"
                )
            if count > max_entries:
                raise TreeDigestLimitExceeded(
                    "TOO_MANY_ENTRIES",
                    f"candidate exceeds {max_entries} entries",
                )

    framed_parts.sort(key=lambda pair: pair[0])
    digest = hashlib.sha256()
    digest.update(b"HPRLM-TREE-V1")
    for _, frame in framed_parts:
        digest.update(frame)
    return digest.hexdigest()


def _frame(rel: bytes, kind: bytes, size: int, content: bytes) -> bytes:
    import struct

    return b"".join(
        [
            b"HPRLM1",
            struct.pack("<Q", len(rel)),
            rel,
            struct.pack("<Q", len(kind)),
            kind,
            struct.pack("<Q", size),
            struct.pack("<Q", len(content)),
            content,
        ]
    )


__all__ = [
    "collect_status_z",
    "parse_status_z",
    "write_tracked_diff",
    "sha256_file",
    "candidate_tree_digest",
    "TreeDigestLimitExceeded",
]
